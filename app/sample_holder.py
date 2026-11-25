#!/usr/bin/env python3
"""样例日志进程：写入指定大小的日志后删除文件、保持 fd 打开，可选持续增长。

用于在网页上试演"已删除但仍被占用"的场景。每个样例用 --new-session
自成进程组，方便演示按进程组回收；两个进程指向同一文件时可演示
"方案必须涵盖全部持有组"。
"""

import argparse
import os
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="日志文件路径")
    ap.add_argument("--size-mb", type=float, default=10, help="初始写入大小（MiB）")
    ap.add_argument("--grow-kb-per-sec", type=float, default=0, help="删除后每秒追加量（KiB）")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="删除前等待秒数（保证多进程先打开同一 inode 再删除）")
    ap.add_argument("--new-session", action="store_true",
                    help="调用 setsid 自成会话/进程组（PGID=PID）")
    a = ap.parse_args()

    if a.new_session:
        os.setsid()

    os.makedirs(os.path.dirname(a.file) or ".", exist_ok=True)
    f = open(a.file, "ab")
    chunk = b"\xab" * (1024 * 1024)
    left = int(a.size_mb * 1024 * 1024)
    while left > 0:
        n = min(len(chunk), left)
        f.write(chunk[:n])
        left -= n
    f.flush()
    os.fsync(f.fileno())

    time.sleep(a.settle)
    try:
        os.unlink(a.file)
    except FileNotFoundError:
        pass  # 共享同一文件的另一个进程已删除
    print(
        f"[holder] pid={os.getpid()} pgid={os.getpgrp()} "
        f"file={a.file} size={a.size_mb}MiB deleted, holding fd",
        flush=True,
    )

    grow = int(a.grow_kb_per_sec * 1024)
    while True:
        time.sleep(1)
        if grow > 0:
            f.write(b"\xcd" * grow)
            f.flush()


if __name__ == "__main__":
    main()
