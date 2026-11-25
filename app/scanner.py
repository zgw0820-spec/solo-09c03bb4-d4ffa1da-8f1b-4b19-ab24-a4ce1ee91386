"""从 /proc 盘点指定日志卷上"已删除但仍被打开"的常规文件。

前提假设（与需求一致）：
- 只处理单个专用日志卷（按挂载点设备号识别）；
- 所有持有进程的文件描述符均可从 /proc 读取（容器内以 root 运行）；
- 日志不做内存映射，因此只扫描 fd，不扫 /proc/<pid>/maps。

空间按已分配磁盘块估算：st_blocks * 512（稀疏文件只计实际占用）。
同一文件（同一 dev+inode）在一次盘点中只计一次。
"""

import os
import re
import stat
import time

DELETED_SUFFIX = " (deleted)"

_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape_mount(s):
    return _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), s)


def list_mounts():
    """解析 /proc/self/mountinfo → [(major, minor, mountpoint), ...]"""
    mounts = []
    with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                maj, min_ = parts[2].split(":", 1)
                mounts.append((int(maj), int(min_), _unescape_mount(parts[4])))
            except ValueError:
                continue
    return mounts


def resolve_volume(path):
    """找到 path 所属的挂载点，返回 (major, minor, mountpoint)。"""
    real = os.path.realpath(path)
    best = None
    for maj, min_, mp in list_mounts():
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[2]):
                best = (maj, min_, mp)
    if best is None:  # 理论上不会走到（至少有 /）
        st = os.stat(real)
        best = (os.major(st.st_dev), os.minor(st.st_dev), "/")
    return best


def _read_pgid(pid):
    """从 /proc/<pid>/stat 读进程组 ID（comm 字段可能含空格/括号，需特殊解析）。"""
    with open(f"/proc/{pid}/stat", "rb") as fh:
        data = fh.read().decode("utf-8", "replace")
    rparen = data.rfind(")")
    if rparen < 0:
        raise ValueError("bad stat")
    fields = data[rparen + 2:].split()
    # state, ppid, pgrp → 下标 2
    return int(fields[2])


def _read_comm(pid):
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


def _read_cmdline(pid, limit=160):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read(4096)
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:limit]
    except OSError:
        return ""


def _scan(volume_maj, volume_min):
    """扫描全部进程 fd，返回 (files, groups)。

    files:  {(dev, ino): {"key", "path", "blocks", "bytes", "size", "holders": set}}
    groups: {pgid: {"pgid", "pids": set, "names": set, "cmdline": str}}
    """
    files = {}
    groups = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            pgid = _read_pgid(pid)
        except (OSError, ValueError, IndexError):
            continue  # 进程已退出
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # 无权限或已退出
        proc_hits = []
        for fd in fds:
            link = os.path.join(fd_dir, fd)
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if not target.endswith(DELETED_SUFFIX):
                continue
            try:
                st = os.stat(link)  # 穿过 magic link，等价于对该 fd 做 fstat
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # 只看常规文件（跳过 socket/管道等）
            if os.major(st.st_dev) != volume_maj or os.minor(st.st_dev) != volume_min:
                continue  # 不在本日志卷上
            proc_hits.append((st, target[: -len(DELETED_SUFFIX)]))
        if not proc_hits:
            continue
        pid = int(pid)
        grp = groups.setdefault(
            pgid, {"pgid": pgid, "pids": set(), "names": set(), "cmdline": ""}
        )
        grp["pids"].add(pid)
        grp["names"].add(_read_comm(str(pid)))
        if not grp["cmdline"]:
            grp["cmdline"] = _read_cmdline(str(pid))
        for st, path in proc_hits:
            key = (st.st_dev, st.st_ino)
            entry = files.get(key)
            if entry is None:
                entry = {
                    "key": f"{st.st_dev}:{st.st_ino}",
                    "path": path,
                    "blocks": st.st_blocks,
                    "bytes": st.st_blocks * 512,
                    "size": st.st_size,
                    "holders": set(),
                }
                files[key] = entry
            elif st.st_blocks > entry["blocks"]:
                # 同一文件只计一次；盘点期间文件可能仍在增长，取观测到的最大值
                entry["blocks"] = st.st_blocks
                entry["bytes"] = st.st_blocks * 512
                entry["size"] = st.st_size
            entry["holders"].add(pgid)
    return files, groups


def take_snapshot(volume_path, snap_id):
    """对日志卷做一次盘点，返回快照 dict（可直接 JSON 序列化）。"""
    maj, min_, mountpoint = resolve_volume(volume_path)
    vfs = os.statvfs(volume_path)
    files, groups = _scan(maj, min_)
    fr = vfs.f_frsize
    file_list = sorted(files.values(), key=lambda f: -f["bytes"])
    for f in file_list:
        f["holders"] = sorted(f["holders"])
    group_map = {}
    for g in sorted(groups.values(), key=lambda g: g["pgid"]):
        g["pids"] = sorted(g["pids"])
        g["names"] = sorted(g["names"])
        group_map[str(g["pgid"])] = g
    return {
        "id": snap_id,
        "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "volume": {
            "path": volume_path,
            "mountpoint": mountpoint,
            "dev": f"{maj}:{min_}",
            "total": vfs.f_blocks * fr,
            "used": (vfs.f_blocks - vfs.f_bfree) * fr,
            "free": vfs.f_bavail * fr,
        },
        "files": file_list,
        "groups": group_map,
    }
