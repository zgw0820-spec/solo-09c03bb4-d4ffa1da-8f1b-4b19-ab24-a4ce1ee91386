"""日志卷应急储备管理。

需求（与值班约定一致）：
- 每份应急储备在日志卷上**实占** 8 MiB 磁盘数据块（用 fallocate 保证分配，
  按 st_blocks 复核），全卷最多保留 MAX_PORTIONS 份；
- 值班员可**释放**已有储备（每份立即腾出 8 MiB），也可按份数**补回**；
- 补回只先分配新增份额的数据块（放在暂存目录），**凑齐后整批 rename 生效**，
  分配期间已有储备仍可释放；
- 补回任一份失败即中止，并**退回本次暂占的全部块**（删除暂存文件）；
- 服务重启时清理尚未生效的补回（暂存目录），已生效的补回与释放继续有效；
- 由单进程服务管理：进程内 threading.RLock 串行化 + 锁文件 lockf 排他锁
  拦截第二个服务进程。

储备状态完全由文件系统目录结构推导（不依赖易失内存），保证重启后状态一致：
  <reserve_root>/
    lock                 单进程/跨进程排他锁
    ready/bNNNNNN/pNN    已生效储备（一个目录=一次成功的补回批次）
    staging/bNNNNNN/pNN  尚未生效的补回（重启即清理）
    events.jsonl         审计事件（追加写）
"""

import fcntl
import glob
import json
import os
import threading
import time

PORTION_BYTES = 8 * 1024 * 1024  # 每份 8 MiB
MAX_PORTIONS = 16                 # 全卷最多 16 份
EXPECTED_BLOCKS = PORTION_BYTES // 512  # st_blocks 以 512 字节为单位

_READY = "ready"
_STAGING = "staging"
_LOCK = "lock"
_EVENTS = "events.jsonl"


class ReserveError(Exception):
    """储备操作的业务错误（映射为 4xx）。"""


class ReserveManager:
    _roots = {}            # root -> 实例：单进程内同一储备目录只允许一个管理器
    _roots_guard = threading.Lock()

    def __init__(self, root, alloc_delay=0.0, fail_after_blocks=None):
        """
        root               储备根目录（位于日志卷上）
        alloc_delay        每分配一份后等待的秒数，便于演示"分配期间仍可释放"
        fail_after_blocks  测试钩子：累计分配这么多块后注入 ENOSPC；
                           真实 ENOSPC 等 OSError 也走同一条失败回滚路径
        """
        self.root = os.path.realpath(root)
        self.alloc_delay = max(0.0, float(alloc_delay))
        self.fail_after_blocks = fail_after_blocks
        self._tlock = threading.RLock()
        with self._roots_guard:
            if self.root in self._roots:
                raise ReserveError(f"储备目录已在本进程内被另一个管理器占用：{self.root}")
            os.makedirs(os.path.join(self.root, _READY), exist_ok=True)
            os.makedirs(os.path.join(self.root, _STAGING), exist_ok=True)
            self._lock_fd = os.open(
                os.path.join(self.root, _LOCK), os.O_RDWR | os.O_CREAT, 0o600
            )
            # 单进程服务约定：拿不到排他锁说明已有另一个服务进程在管理本卷。
            # 用 POSIX 记录锁（lockf）：flock 在部分文件系统（如 overlay）上不互斥。
            try:
                fcntl.lockf(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(self._lock_fd)
                raise ReserveError(
                    f"储备目录已被另一个服务进程锁定：{self.root}"
                ) from exc
            self._jobs = {}          # job_id -> job dict（运行期内存）
            self._job_seq = 0
            self._injected_blocks = 0
            self._closed = False
            self._roots[self.root] = self
        self.recover_on_start()

    # ---------- 底层工具 ----------

    def _locked(self):
        # 跨进程单实例已由启动时的 lockf 排他锁保证；进程内多线程用 RLock 串行化。
        # POSIX 记录锁按进程关联，不能在同一 fd 上反复 lock/unlock，故这里不加锁。
        return self._tlock

    @staticmethod
    def _fsync_dir(path):
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _event(self, kind, **fields):
        rec = {"ts": _now(), "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with self._locked():
            with open(os.path.join(self.root, _EVENTS), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def _ready_batches(self):
        """返回 [(batch_id, batch_dir, [portion_path, ...])]，按批次号升序。"""
        out = []
        for d in sorted(glob.glob(os.path.join(self.root, _READY, "b*"))):
            if not os.path.isdir(d):
                continue
            parts = sorted(
                p for p in glob.glob(os.path.join(d, "p*")) if os.path.isfile(p)
            )
            out.append((os.path.basename(d), d, parts))
        return out

    def _staging_dirs(self):
        return sorted(
            d
            for d in glob.glob(os.path.join(self.root, _STAGING, "b*"))
            if os.path.isdir(d)
        )

    def _ready_portions(self):
        return [p for _, _, parts in self._ready_batches() for p in parts]

    def _staging_portion_count(self):
        return sum(
            len([p for p in glob.glob(os.path.join(d, "p*")) if os.path.isfile(p)])
            for d in self._staging_dirs()
        )

    def _next_batch_id(self):
        existing = [int(b[1:]) for b, _, _ in self._ready_batches()]
        existing += [
            int(os.path.basename(d.rstrip("/")).split(".")[0][1:])
            for d in self._staging_dirs()
        ]
        nxt = (max(existing) + 1) if existing else 1
        return f"b{nxt:06d}"

    def _alloc_portion(self, path):
        """分配一份实占 8 MiB 的数据块，并按 st_blocks 复核。"""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                os.posix_fallocate(fd, 0, PORTION_BYTES)
            except (AttributeError, OSError):
                # 个别文件系统不支持 fallocate：退化为写零，同样实占数据块
                os.ftruncate(fd, 0)
                written = 0
                chunk = b"\0" * (1024 * 1024)
                while written < PORTION_BYTES:
                    n = min(len(chunk), PORTION_BYTES - written)
                    os.write(fd, chunk[:n])
                    written += n
            os.fsync(fd)
        finally:
            os.close(fd)
        st = os.stat(path)
        # ext4 等文件系统会额外计入间接块/元数据块，故只校验下限：
        # 数据块必须实占至少 8 MiB（不是稀疏空洞），不允许短分配
        if st.st_blocks < EXPECTED_BLOCKS:
            raise OSError(
                f"储备块实占校验失败：{path} st_blocks={st.st_blocks}，"
                f"期望至少 {EXPECTED_BLOCKS}"
            )
        # 测试钩子：模拟卷写满，走与真实 ENOSPC 相同的失败/回滚路径
        self._injected_blocks += st.st_blocks
        if self.fail_after_blocks is not None:
            if self._injected_blocks >= self.fail_after_blocks:
                raise OSError(28, "No space left on device (injected)")

    # ---------- 对外操作 ----------

    def recover_on_start(self):
        """启动恢复：清理尚未生效的补回（暂存目录）；已生效储备原样保留。"""
        cleaned = []
        with self._locked():
            for d in self._staging_dirs():
                bid = os.path.basename(d.rstrip("/")).split(".")[0]
                _rmtree(d)
                cleaned.append(bid)
            if cleaned:
                self._fsync_dir(os.path.join(self.root, _STAGING))
        if cleaned:
            self._event("startup_cleanup", batches=cleaned)
        return cleaned

    def close(self):
        """释放锁并注销实例（测试或进程退出时使用）。"""
        with self._roots_guard:
            if self._closed:
                return
            self._closed = True
            try:
                fcntl.lockf(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
            self._roots.pop(self.root, None)

    # 别名：语义上模拟"服务进程退出"（放锁、注销，状态留在卷上由下一个进程恢复）
    _simulate_exit = close

    def status(self):
        with self._locked():
            batches = self._ready_batches()
            ready = len(self._ready_portions())
            staging = self._staging_portion_count()
            vfs = os.statvfs(self.root)
            jobs = [self._public_job(j) for j in self._jobs.values()]
            jobs.sort(key=lambda j: j["id"], reverse=True)
            return {
                "portion_bytes": PORTION_BYTES,
                "max_portions": MAX_PORTIONS,
                "ready_portions": ready,
                "staging_portions": staging,
                "available_portions": MAX_PORTIONS - ready,
                "ready_bytes": ready * PORTION_BYTES,
                "volume_free_bytes": vfs.f_bavail * vfs.f_frsize,
                "batches": [
                    {
                        "id": bid,
                        "portions": len(parts),
                        "bytes": len(parts) * PORTION_BYTES,
                    }
                    for bid, _, parts in batches
                ],
                "jobs": jobs,
            }

    def release(self, count):
        """释放最旧的 count 份已生效储备（FIFO，按批次/份序号）。"""
        if not isinstance(count, int) or count <= 0:
            raise ReserveError("释放份数必须是正整数")
        with self._locked():
            portions = self._ready_portions()  # 已按批次、序号升序
            if not portions:
                raise ReserveError("应急储备已为空，没有可释放的份")
            n = min(count, len(portions))
            removed = []
            for p in portions[:n]:
                os.unlink(p)
                removed.append(os.path.relpath(p, self.root))
            # 清掉变空的批次目录并 fsync，保证崩溃后目录结构依然一致
            for bid, bdir, parts in self._ready_batches():
                if not parts:
                    os.rmdir(bdir)
            self._fsync_dir(os.path.join(self.root, _READY))
            self._event(
                "release",
                requested=count,
                released=n,
                portions=removed,
                bytes_freed=n * PORTION_BYTES,
            )
            return {
                "requested": count,
                "released": n,
                "portions": removed,
                "bytes_freed": n * PORTION_BYTES,
            }

    def replenish(self, count):
        """申请补回 count 份：校验后后台分配，凑齐整批生效。"""
        if not isinstance(count, int) or count <= 0:
            raise ReserveError("补回份数必须是正整数")
        with self._locked():
            ready = len(self._ready_portions())
            if ready >= MAX_PORTIONS:
                raise ReserveError(f"应急储备已达上限 {MAX_PORTIONS} 份，无需补回")
            in_flight = sum(
                j["requested"]
                for j in self._jobs.values()
                if j["state"] in ("pending", "allocating", "promoting")
            )
            if ready + in_flight + count > MAX_PORTIONS:
                raise ReserveError(
                    f"补回 {count} 份将超过上限 {MAX_PORTIONS} 份"
                    f"（现有 {ready} 份，另有 {in_flight} 份补回在途）"
                )
            vfs = os.statvfs(self.root)
            free_bytes = vfs.f_bavail * vfs.f_frsize
            need_bytes = count * PORTION_BYTES
            if free_bytes < need_bytes:
                raise ReserveError(
                    f"日志卷可用空间不足：需要 {need_bytes} 字节，"
                    f"当前仅 {free_bytes} 字节"
                )
            self._job_seq += 1
            job_id = f"j{self._job_seq:06d}"
            batch_id = self._next_batch_id()
            job = {
                "id": job_id,
                "batch_id": batch_id,
                "requested": count,
                "allocated": 0,
                "state": "pending",
                "started_at": _now(),
                "finished_at": None,
                "error": None,
                "portion_bytes": PORTION_BYTES,
            }
            self._jobs[job_id] = job
            self._trim_jobs_locked()
            thread = threading.Thread(
                target=self._run_replenish,
                args=(job_id, batch_id, count),
                name=f"replenish-{job_id}",
                daemon=True,
            )
            thread.start()
            return self._public_job(job)

    def _run_replenish(self, job_id, batch_id, count):
        staging = os.path.join(self.root, _STAGING, f"{batch_id}.staging")
        ready_dir = os.path.join(self.root, _READY, batch_id)
        made = []
        job = self._jobs[job_id]
        try:
            os.makedirs(staging)
            self._fsync_dir(os.path.join(self.root, _STAGING))
            job["state"] = "allocating"
            for i in range(1, count + 1):
                # 分配在锁外逐份进行：补回期间释放已有储备不会被长时间阻塞
                p = os.path.join(staging, f"p{i:02d}")
                self._alloc_portion(p)
                made.append(p)
                with self._locked():
                    job["allocated"] = i
                if self.alloc_delay:
                    time.sleep(self.alloc_delay)
            # 凑齐 → 整批生效：rename 是原子的，外界要么看到整批、要么看不到
            with self._locked():
                job["state"] = "promoting"
                try:
                    os.rename(staging, ready_dir)
                except OSError:
                    # 生效动作本身失败：同样退回本次暂占的块，按补回失败处理
                    job["state"] = "failed"
                    raise
                self._fsync_dir(os.path.join(self.root, _READY))
                self._fsync_dir(os.path.join(self.root, _STAGING))
                job["state"] = "done"
                job["finished_at"] = _now()
            self._event(
                "replenish_done",
                job=job_id,
                batch=batch_id,
                portions=count,
                bytes_reserved=count * PORTION_BYTES,
            )
        except OSError as exc:
            # 分配失败：退回本次暂占的全部块
            with self._locked():
                job["state"] = "failed"
                job["error"] = _errno_text(exc)
                job["finished_at"] = _now()
            _rmtree(staging)
            self._fsync_dir(os.path.join(self.root, _STAGING))
            self._event(
                "replenish_failed",
                job=job_id,
                batch=batch_id,
                requested=count,
                allocated=len(made),
                rolled_back=len(made),
                error=job["error"],
            )

    def get_job(self, job_id):
        with self._locked():
            job = self._jobs.get(job_id)
            return self._public_job(job) if job else None

    def list_events(self, limit=100):
        path = os.path.join(self.root, _EVENTS)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def _public_job(job):
        return {k: v for k, v in job.items()}

    _MAX_JOBS_KEPT = 50

    def _trim_jobs_locked(self):
        """只保留最近若干个任务，避免长期运行后内存无限增长。"""
        if len(self._jobs) <= self._MAX_JOBS_KEPT:
            return
        for old_id in sorted(self._jobs)[: len(self._jobs) - self._MAX_JOBS_KEPT]:
            if self._jobs[old_id]["state"] in ("done", "failed"):
                self._jobs.pop(old_id, None)


def _rmtree(path):
    """删除暂存目录（只含普通文件，手工递归，避免引入 shutil 的复杂语义）。"""
    if not os.path.isdir(path):
        return
    for p in glob.glob(os.path.join(path, "p*")):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass


def _errno_text(exc):
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"[errno {exc.errno}] {exc.strerror or exc}"
    return str(exc)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")
