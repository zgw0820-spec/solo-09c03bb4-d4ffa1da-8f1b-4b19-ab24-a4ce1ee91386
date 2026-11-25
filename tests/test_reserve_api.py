#!/usr/bin/env python3
"""应急储备 HTTP 接口端到端测试（Flask test client，模拟网页调用）。

运行：
  PYTHONPATH=/tmp/pylibs python3 -I tests/test_reserve_api.py
需要 Flask（容器内已安装；本地用隔离的 /tmp/pylibs）。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_APP = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
for _p in ("/tmp/pylibs", _APP):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_tmp = tempfile.mkdtemp(prefix="reserve-api-")
os.environ["LOG_VOLUME"] = _tmp
os.environ["RESERVE_ROOT"] = os.path.join(_tmp, ".duty-reserve")
os.environ["RESERVE_ALLOC_DELAY"] = "0.15"

import app as flask_app  # noqa: E402


class ReserveApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = flask_app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_tmp, ignore_errors=True)

    def _drain(self):
        """每例开始前置空储备，保证用例相互独立。"""
        _, j = self.get("/api/reserve")
        if j["reserve"]["ready_portions"]:
            self.post("/api/reserve/release", {"count": 16})
        flask_app._reserve.fail_after_blocks = None

    def setUp(self):
        self._drain()

    def get(self, path):
        rv = self.client.get(path)
        return rv.status_code, rv.get_json()

    def post(self, path, body):
        rv = self.client.post(path, json=body)
        return rv.status_code, rv.get_json()

    def wait_job(self, job_id):
        deadline = time.time() + 30
        while time.time() < deadline:
            _, j = self.get(f"/api/reserve/jobs/{job_id}")
            if j["job"]["state"] in ("done", "failed"):
                return j["job"]
            time.sleep(0.03)
        raise AssertionError("job timeout")

    def test_01_full_flow_release_during_allocation_failure_restart(self):
        # 初始为空
        code, j = self.get("/api/reserve")
        self.assertEqual(code, 200)
        self.assertEqual(j["reserve"]["ready_portions"], 0)
        self.assertEqual(j["reserve"]["max_portions"], 16)

        # 空储备释放 → 409
        code, j = self.post("/api/reserve/release", {"count": 1})
        self.assertEqual(code, 409)
        self.assertIn("为空", j["error"])

        # 补回 3 份并等待整批生效
        code, j = self.post("/api/reserve/replenish", {"count": 3})
        self.assertEqual(code, 200)
        job = self.wait_job(j["job"]["id"])
        self.assertEqual(job["state"], "done")
        code, j = self.get("/api/reserve")
        self.assertEqual(j["reserve"]["ready_portions"], 3)
        self.assertEqual(j["reserve"]["ready_bytes"], 3 * 8 * 1024 * 1024)
        # 每份实占（含文件系统元数据块，至少 8 MiB 数据块）
        ready_dir = os.path.join(os.environ["RESERVE_ROOT"], "ready")
        blocks = sum(
            os.stat(os.path.join(r, f)).st_blocks
            for r, _, fs in os.walk(ready_dir)
            for f in fs
        )
        self.assertGreaterEqual(blocks, 3 * 8 * 1024 * 1024 // 512)

        # 非法参数：格式错误 400；零/负数能转 int 但不满足业务约束，409
        code, _ = self.post("/api/reserve/replenish", {"count": "abc"})
        self.assertEqual(code, 400)
        code, j400 = self.post("/api/reserve/replenish", {"count": 0})
        self.assertEqual(code, 409)
        self.assertIn("正整数", j400["error"])

        # 超上限 → 409
        code, j = self.post("/api/reserve/replenish", {"count": 14})
        self.assertEqual(code, 409)
        self.assertIn("上限", j["error"])

        # 慢补回 2 份，分配期间释放 1 份已有储备
        code, j = self.post("/api/reserve/replenish", {"count": 2})
        self.assertEqual(code, 200)
        job_id = j["job"]["id"]
        deadline = time.time() + 5
        while True:
            _, jj = self.get(f"/api/reserve/jobs/{job_id}")
            if jj["job"]["allocated"] >= 1 or time.time() > deadline:
                break
            time.sleep(0.02)
        code, j = self.post("/api/reserve/release", {"count": 1})
        self.assertEqual(code, 200)
        self.assertEqual(j["released"]["released"], 1)
        self.assertEqual(j["released"]["bytes_freed"], 8 * 1024 * 1024)
        self.assertEqual(self.wait_job(job_id)["state"], "done")
        code, j = self.get("/api/reserve")
        # 3 - 1 + 2 = 4
        self.assertEqual(j["reserve"]["ready_portions"], 4)
        self.assertEqual(j["reserve"]["staging_portions"], 0)

        # 审计事件已记录
        kinds = {e["kind"] for e in j["events"]}
        self.assertIn("replenish_done", kinds)
        self.assertIn("release", kinds)

        # 释放全部
        code, j = self.post("/api/reserve/release", {"count": 16})
        self.assertEqual(code, 200)
        self.assertEqual(j["released"]["released"], 4)
        self.assertEqual(j["reserve"]["ready_portions"], 0)

    def test_02_failure_rollback_via_http(self):
        flask_app._reserve._injected_blocks = 0
        flask_app._reserve.fail_after_blocks = (8 * 1024 * 1024 // 512) * 2 + 1
        try:
            code, j = self.post("/api/reserve/replenish", {"count": 4})
            self.assertEqual(code, 200)
            job = self.wait_job(j["job"]["id"])
            self.assertEqual(job["state"], "failed")
            self.assertIn("28", job["error"])
        finally:
            flask_app._reserve.fail_after_blocks = None
        code, j = self.get("/api/reserve")
        self.assertEqual(j["reserve"]["staging_portions"], 0)
        staging = os.path.join(os.environ["RESERVE_ROOT"], "staging")
        self.assertEqual(os.listdir(staging), [])
        # 失败前没有已生效储备（setUp 已置空），失败回滚后仍为空
        self.assertEqual(j["reserve"]["ready_portions"], 0)

    def test_03_restart_recovery(self):
        # setUp 已置空；先备 2 份
        code, j = self.post("/api/reserve/replenish", {"count": 2})
        self.wait_job(j["job"]["id"])
        _, before = self.get("/api/reserve")
        self.assertEqual(before["reserve"]["ready_portions"], 2)
        # 再造一个未生效的暂存批次（手工建目录+份文件，模拟崩溃在分配途中）
        root = os.environ["RESERVE_ROOT"]
        fake = os.path.join(root, "staging", "b999999.staging")
        os.makedirs(fake)
        fd = os.open(os.path.join(fake, "p01"), os.O_WRONLY | os.O_CREAT, 0o600)
        os.posix_fallocate(fd, 0, 8 * 1024 * 1024)
        os.close(fd)
        # 模拟服务重启：重新执行启动恢复
        cleaned = flask_app._reserve.recover_on_start()
        self.assertIn("b999999", cleaned)
        code, j = self.get("/api/reserve")
        self.assertEqual(j["reserve"]["ready_portions"], 2)   # 已生效保留
        self.assertEqual(j["reserve"]["staging_portions"], 0)  # 未生效清理
        self.assertEqual(
            os.listdir(os.path.join(root, "staging")), []
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
