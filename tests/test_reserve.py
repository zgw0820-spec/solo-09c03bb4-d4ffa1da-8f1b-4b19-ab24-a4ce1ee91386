#!/usr/bin/env python3
"""应急储备核心模块测试：块实占、上限、补回/释放并发、失败回滚、重启恢复。

运行：
  python3 -I tests/test_reserve.py
数据目录全部使用 tempfile，不触碰真实日志卷。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from reserve import EXPECTED_BLOCKS, MAX_PORTIONS, PORTION_BYTES, ReserveError, ReserveManager  # noqa: E402


def wait_job(mgr, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = mgr.get_job(job_id)
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def du_blocks(path):
    """统计目录下常规文件实际占用的 512 字节块总数。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            total += os.stat(os.path.join(root, name)).st_blocks
    return total


class ReserveTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="reserve-test-")

    def tearDown(self):
        for mgr in list(ReserveManager._roots.values()):
            if mgr.root == os.path.realpath(self.root):
                mgr.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def mgr(self, **kw):
        return ReserveManager(self.root, **kw)


class TestBasic(ReserveTestBase):
    def test_each_portion_occupies_8mib_blocks(self):
        mgr = self.mgr()
        self.assertEqual(mgr.status()["ready_portions"], 0)
        job = mgr.replenish(3)
        self.assertEqual(wait_job(mgr, job["id"])["state"], "done")
        st = mgr.status()
        self.assertEqual(st["ready_portions"], 3)
        self.assertEqual(st["staging_portions"], 0)
        self.assertEqual(st["ready_bytes"], 3 * PORTION_BYTES)
        # 每份必须实占至少 8 MiB 数据块（st_blocks*512），不是稀疏空洞
        self.assertGreaterEqual(
            du_blocks(os.path.join(self.root, "ready")), 3 * EXPECTED_BLOCKS
        )

    def test_release_fifo_and_empty(self):
        mgr = self.mgr()
        wait_job(mgr, mgr.replenish(2)["id"])
        wait_job(mgr, mgr.replenish(1)["id"])
        r = mgr.release(1)
        self.assertEqual(r["released"], 1)
        self.assertEqual(r["bytes_freed"], PORTION_BYTES)
        # FIFO：先释放最早批次的份
        self.assertTrue(r["portions"][0].startswith(os.path.join("ready", "b000001")))
        self.assertEqual(mgr.status()["ready_portions"], 2)
        r = mgr.release(99)  # 请求超过存量：有多少放多少
        self.assertEqual(r["released"], 2)
        self.assertEqual(mgr.status()["ready_portions"], 0)
        with self.assertRaises(ReserveError):
            mgr.release(1)

    def test_max_16_and_in_flight_limit(self):
        mgr = self.mgr()
        with self.assertRaises(ReserveError):
            mgr.replenish(MAX_PORTIONS + 1)
        wait_job(mgr, mgr.replenish(MAX_PORTIONS)["id"])
        self.assertEqual(mgr.status()["ready_portions"], MAX_PORTIONS)
        with self.assertRaises(ReserveError):
            mgr.replenish(1)
        mgr.release(4)
        mgr._simulate_exit()
        mgr2 = ReserveManager(self.root, alloc_delay=0.2)
        job = mgr2.replenish(2)  # 在途 2 份
        with self.assertRaises(ReserveError):
            mgr2.replenish(3)  # 12 + 2 + 3 > 16
        self.assertEqual(wait_job(mgr2, job["id"])["state"], "done")
        self.assertEqual(mgr2.status()["ready_portions"], 14)

    def test_fallocate_unsupported_falls_back_to_write(self):
        mgr = self.mgr()
        import reserve as reserve_mod

        def boom(fd, offset, length):
            raise OSError(95, "Operation not supported")

        orig = os.posix_fallocate
        os.posix_fallocate = boom
        try:
            job = mgr.replenish(1)
            self.assertEqual(wait_job(mgr, job["id"])["state"], "done")
        finally:
            os.posix_fallocate = orig
        self.assertGreaterEqual(
            du_blocks(os.path.join(self.root, "ready")), EXPECTED_BLOCKS
        )


class TestFailureAndConcurrency(ReserveTestBase):
    def test_failure_rolls_back_all_staged_blocks(self):
        # 注入故障：累计分到第 3 份时抛 ENOSPC（与真实写满走同一回滚路径）
        mgr = self.mgr(fail_after_blocks=EXPECTED_BLOCKS * 2 + 1)
        job = mgr.replenish(4)
        done = wait_job(mgr, job["id"])
        self.assertEqual(done["state"], "failed")
        self.assertIn("28", done["error"])
        st = mgr.status()
        self.assertEqual(st["ready_portions"], 0)
        self.assertEqual(st["staging_portions"], 0)
        # 本次暂占的块全部退回
        self.assertEqual(os.listdir(os.path.join(self.root, "staging")), [])
        self.assertEqual(du_blocks(os.path.join(self.root, "staging")), 0)
        self.assertEqual(mgr.status()["ready_portions"], 0)

    def test_release_works_during_allocation(self):
        mgr = self.mgr()
        wait_job(mgr, mgr.replenish(2)["id"])  # 已有储备 2 份
        mgr._simulate_exit()
        slow = ReserveManager(self.root, alloc_delay=0.25)
        job = slow.replenish(3)                # 慢补回 3 份
        # 等到确认进入分配期，再释放已有储备
        deadline = time.time() + 5
        while slow.get_job(job["id"])["allocated"] == 0 and time.time() < deadline:
            time.sleep(0.02)
        r = slow.release(1)                   # 分配期间释放，不应被阻塞
        self.assertEqual(r["released"], 1)
        self.assertEqual(wait_job(slow, job["id"])["state"], "done")
        # 2（原有）- 1（释放）+ 3（整批生效）= 4
        self.assertEqual(slow.status()["ready_portions"], 4)
        self.assertEqual(slow.status()["staging_portions"], 0)

    def test_restart_cleans_pending_keeps_effective(self):
        mgr = self.mgr(alloc_delay=0.2)
        wait_job(mgr, mgr.replenish(2)["id"])  # 已生效 2 份
        job = mgr.replenish(3)
        deadline = time.time() + 5
        while mgr.get_job(job["id"])["allocated"] < 2 and time.time() < deadline:
            time.sleep(0.02)
        # 模拟服务进程崩溃：正常关闭（释放锁、注销实例），不做任何业务清理
        mgr.close()
        del mgr
        time.sleep(0.1)
        # 新进程启动：构造时即执行重启恢复
        mgr2 = ReserveManager(self.root)
        st = mgr2.status()
        self.assertEqual(st["ready_portions"], 2)       # 已生效补回继续有效
        self.assertEqual(st["staging_portions"], 0)     # 未生效补回被清理
        self.assertEqual(os.listdir(os.path.join(self.root, "staging")), [])
        # 释放历史依然有效：只剩 2 份可放
        self.assertEqual(mgr2.release(99)["released"], 2)

    def test_second_manager_locked_out(self):
        mgr = self.mgr()  # 保持强引用，避免被 GC 回收而释放锁
        # 同进程内：注册表拦截
        with self.assertRaises(ReserveError):
            ReserveManager(self.root)
        # 跨进程：锁文件 lockf 排他锁拦截（必须在本进程放锁之前检查）
        import subprocess

        app_dir = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
        )
        code = (
            "import sys\n"
            f"sys.path.insert(0, {app_dir!r})\n"
            "from reserve import ReserveManager, ReserveError\n"
            "try:\n"
            f"    ReserveManager({self.root!r})\n"
            "except ReserveError:\n"
            "    print('locked')\n"
            "else:\n"
            "    print('unexpected-ok')\n"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True
        )
        self.assertIn("locked", out.stdout + out.stderr)
        mgr.close()
        # 放锁后新进程可正常接管（重启恢复路径）
        mgr2 = ReserveManager(self.root)
        self.assertEqual(mgr2.status()["ready_portions"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
