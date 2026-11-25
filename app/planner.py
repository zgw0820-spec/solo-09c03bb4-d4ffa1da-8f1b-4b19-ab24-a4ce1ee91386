"""回收方案求解：在候选进程组中找"达标且重启组数最少"的方案。

候选组最多 12 个，直接枚举全部子集（2^12 = 4096），
结果确定、可复现。文件只有在它的全部持有组都被重启时才可回收；
同一文件（同一 inode）只计一次。
"""

from itertools import combinations


def reclaimable(files, group_set):
    """返回 (可回收字节数, 可回收文件列表)。holders 为空或未被完全覆盖的文件不计。"""
    total = 0
    picked = []
    for f in files:
        holders = set(f["holders"])
        if holders and holders <= group_set:
            total += f["bytes"]
            picked.append(f)
    return total, picked


def make_plan(files, candidates, target_bytes):
    """枚举候选子集，找达标（>= target_bytes）且组数最少的方案。

    并列时取可回收量更大者，再按 PGID 字典序取前者（保证确定性）。
    返回 dict：
      feasible  是否达标
      groups    需重启的 PGID 列表（达标时）
      bytes     预计释放字节数
      files     可回收文件列表
      max_freeable / max_files  全部候选组都重启时的上限（不达标时用于上报）
    """
    cands = sorted(set(candidates))
    max_bytes, max_files = reclaimable(files, set(cands))
    for size in range(1, len(cands) + 1):
        tier = []
        for combo in combinations(cands, size):
            total, picked = reclaimable(files, set(combo))
            if total >= target_bytes:
                tier.append((combo, total, picked))
        if tier:
            tier.sort(key=lambda t: (-t[1], t[0]))
            combo, total, picked = tier[0]
            return {
                "feasible": True,
                "groups": list(combo),
                "bytes": total,
                "files": picked,
                "max_freeable": max_bytes,
            }
    return {
        "feasible": False,
        "groups": [],
        "bytes": 0,
        "files": [],
        "max_freeable": max_bytes,
        "max_files": max_files,
    }
