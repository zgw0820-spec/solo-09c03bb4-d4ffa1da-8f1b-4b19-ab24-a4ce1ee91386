#!/usr/bin/env python3
"""日志卷回收值班台（Flask）。

流程：盘点（POST /api/inventory）→ 勾选 ≤12 个进程组并填目标空间
→ 生成方案（POST /api/plan，用同一次盘点快照计算）→ 复核（POST /api/review，
重新盘点并比对，发现变化或更优方案即作废旧方案）。
"""

import itertools
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from planner import make_plan, reclaimable
from scanner import take_snapshot

LOG_VOLUME = os.environ.get("LOG_VOLUME", "/logs")
MAX_GROUPS = 12

app = Flask(__name__)
_lock = threading.Lock()
_snap_seq = itertools.count(1)
_plan_seq = itertools.count(1)
_snapshots = {}  # snap_id -> snapshot
_plans = {}      # plan_id -> plan
_current_snap_id = None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _mib(n):
    return f"{n / 1048576:.1f} MiB"


def _plans_list():
    return sorted(_plans.values(), key=lambda p: p["id"], reverse=True)


# ---------------- 页面 ----------------

@app.get("/")
def index():
    return render_template("index.html", max_groups=MAX_GROUPS)


# ---------------- 盘点 ----------------

@app.post("/api/inventory")
def api_inventory():
    global _current_snap_id
    snap = take_snapshot(LOG_VOLUME, f"s{next(_snap_seq)}")
    with _lock:
        _snapshots[snap["id"]] = snap
        _current_snap_id = snap["id"]
        plans = _plans_list()
    return jsonify({"snapshot": snap, "plans": plans})


@app.get("/api/state")
def api_state():
    with _lock:
        snap = _snapshots.get(_current_snap_id) if _current_snap_id else None
        plans = _plans_list()
    return jsonify(
        {"snapshot": snap, "plans": plans, "log_volume": LOG_VOLUME, "max_groups": MAX_GROUPS}
    )


# ---------------- 生成方案 ----------------

@app.post("/api/plan")
def api_plan():
    body = request.get_json(force=True, silent=True) or {}
    snap_id = body.get("snapshot_id")
    try:
        groups = [int(g) for g in (body.get("groups") or [])]
        target = int(body.get("target_bytes") or 0)
    except (TypeError, ValueError):
        return _err("参数格式错误")
    if target <= 0:
        return _err("目标腾出空间必须大于 0")
    if not groups:
        return _err("请至少勾选 1 个进程组")
    if len(groups) > MAX_GROUPS:
        return _err(f"最多选择 {MAX_GROUPS} 个进程组")
    with _lock:
        snap = _snapshots.get(snap_id)
    if snap is None:
        return _err("盘点快照不存在，请重新盘点", 404)
    known = {int(k) for k in snap["groups"]}
    cands = [g for g in groups if g in known]
    if not cands:
        return _err("所选进程组都不在该次盘点中，请重新盘点")

    # 关键：方案完全由同一次盘点快照算出，不重新扫描
    result = make_plan(snap["files"], cands, target)

    plan = None
    if result["feasible"]:
        plan_id = f"p{next(_plan_seq)}"
        ginfo = snap["groups"]
        plan = {
            "id": plan_id,
            "snapshot_id": snap_id,
            "created_at": _now(),
            "candidates": cands,
            "target_bytes": target,
            "groups": result["groups"],
            "group_details": [ginfo[str(g)] for g in result["groups"]],
            "files": result["files"],
            "bytes": result["bytes"],
            "max_freeable": result["max_freeable"],
            "review": None,
        }
        with _lock:
            _plans[plan_id] = plan
    with _lock:
        plans = _plans_list()
    return jsonify(
        {
            "plan": plan,
            "feasible": result["feasible"],
            "max_freeable": result["max_freeable"],
            "max_files": result.get("max_files", []),
            "plans": plans,
        }
    )


# ---------------- 复核 ----------------

def _review(plan, old_snap, new_snap):
    """比对方案所依据的旧盘点与新盘点，返回复核结论。"""
    invalidations, notices = [], []
    new_files = {f["key"]: f for f in new_snap["files"]}
    old_files = {f["key"]: f for f in old_snap["files"]}

    # 1) 方案涉及的文件：消失 / 占用变化 / 持有组变化
    for pf in plan["files"]:
        nf = new_files.get(pf["key"])
        if nf is None:
            invalidations.append(f"方案文件已消失：{pf['path']}（占用可能已被释放）")
            continue
        if nf["bytes"] != pf["bytes"]:
            invalidations.append(
                f"方案文件占用变化：{pf['path']} {_mib(pf['bytes'])} → {_mib(nf['bytes'])}"
            )
        if sorted(nf["holders"]) != sorted(pf["holders"]):
            invalidations.append(
                f"方案文件持有组变化：{pf['path']} {sorted(pf['holders'])} → {sorted(nf['holders'])}"
            )

    # 2) 卷上其他变化（提示，不直接作废）
    plan_keys = {f["key"] for f in plan["files"]}
    added = set(new_files) - set(old_files)
    gone = (set(old_files) - set(new_files)) - plan_keys
    changed = [
        k
        for k in (set(old_files) & set(new_files)) - plan_keys
        if old_files[k]["bytes"] != new_files[k]["bytes"]
        or sorted(old_files[k]["holders"]) != sorted(new_files[k]["holders"])
    ]
    if added:
        notices.append(f"卷上新增 {len(added)} 个已删除打开文件")
    if gone:
        notices.append(f"卷上 {len(gone)} 个非方案文件已消失")
    for k in changed:
        of, nf = old_files[k], new_files[k]
        notices.append(
            f"非方案文件变化：{nf['path']} 占用 {_mib(of['bytes'])} → {_mib(nf['bytes'])}，"
            f"持有组 {sorted(of['holders'])} → {sorted(nf['holders'])}"
        )

    # 3) 用新盘点重算：原方案是否仍达标；是否出现组数更少的达标方案
    still, _ = reclaimable(new_snap["files"], set(plan["groups"]))
    if still < plan["target_bytes"]:
        invalidations.append(
            f"原方案按新盘点仅可回收 {_mib(still)}，低于目标 {_mib(plan['target_bytes'])}"
        )
    opt = make_plan(new_snap["files"], plan["candidates"], plan["target_bytes"])
    if opt["feasible"] and len(opt["groups"]) < len(plan["groups"]):
        invalidations.append(
            f"出现组数更少的达标方案：{len(opt['groups'])} 组（原方案 {len(plan['groups'])} 组）"
        )

    return {
        "valid": not invalidations,
        "invalidations": invalidations,
        "notices": notices,
        "still_bytes": still,
        "new_optimal": {
            "feasible": opt["feasible"],
            "groups": opt["groups"],
            "bytes": opt["bytes"],
            "max_freeable": opt["max_freeable"],
        },
        "checked_at": _now(),
        "snapshot_id": new_snap["id"],
    }


@app.post("/api/review")
def api_review():
    global _current_snap_id
    body = request.get_json(force=True, silent=True) or {}
    plan_id = body.get("plan_id")
    with _lock:
        plan = _plans.get(plan_id)
        old_snap = _snapshots.get(plan["snapshot_id"]) if plan else None
    if plan is None or old_snap is None:
        return _err("方案或其盘点快照不存在", 404)

    new_snap = take_snapshot(LOG_VOLUME, f"s{next(_snap_seq)}")  # 复核时重新盘点
    review = _review(plan, old_snap, new_snap)
    with _lock:
        _snapshots[new_snap["id"]] = new_snap
        _current_snap_id = new_snap["id"]
        plan["review"] = review
        plans = _plans_list()
    return jsonify({"review": review, "snapshot": new_snap, "plans": plans})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
