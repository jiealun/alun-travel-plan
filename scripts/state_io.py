#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_io.py · alun-travel-plan 的存档、版本、恢复与变更追踪（只用标准库）

子命令：
  init     <trip_id> [--dir travel-plan] [--title ...] [--destination ...]   建目录 + 骨架 trip.json（全部未知，等待填写）
  checkpoint <trip.json>                                                   编辑前建立不可覆盖快照
  save     <trip.json> --from <edited.json> --summary "..."                 保留原版、版本 +1、写 changelog；同目录候选文件
  versions <trip_dir>                                                         列出历史版本
  diff     <trip_dir> <vA> <vB>                                               两个版本的字段级变更摘要
  restore  <trip_dir> <vN>                                                    把 vN 恢复为当前 trip.json（当前先快照，版本号继续递增）
  scan     <trip_dir>                                                         扫描目录内所有文本文件是否含疑似凭据

约束：原子写入；不静默覆盖；trip_id 只允许 [a-z0-9-]；所有路径必须落在行程目录内；令牌不落盘。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from validate_trip import SECRET_PATTERNS
from trip_support import (atomic_json, inside, preflight, checkpoint,
                          secret_hits, configure_console, canonical_hash)

TRIP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_json(path: str, data: dict) -> None:
    atomic_json(path, data)


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_inside(base: str, path: str) -> str:
    return inside(base, path)


def skeleton(trip_id: str, title: str | None, destination: str | None) -> dict:
    ts = now_iso()
    return {
        "schema_version": "1.0",
        "trip_id": trip_id,
        "plan_version": 1,
        "status": "draft",
        "user_confirmed": False,
        "research_mode": "standard",
        "generated_at": ts,
        "checked_at": None,
        "request": {
            "origin": {"name": None, "region": None},
            "dates": {"start": None, "end": None, "flexible": True, "nights": None, "return_by": None},
            "timezone": "Asia/Shanghai",
            "party": {"adults": 1, "children_ages": [], "seniors": None, "mobility_notes": None},
            "budget": {"mode": "unknown", "currency": "CNY", "amount_min": None, "amount_max": None, "includes": [], "paid_amount": None, "flexibility": None},
            "pace": {"early_start": None, "daily_hours": None, "walking": None, "rest_needs": None},
            "interests": {"like": [], "must": [], "optional": [], "avoid": []},
            "fixed_commitments": [],
            "transport": {"mode": "unknown", "luggage": None, "notes": None},
            "special_needs": [],
            "user_materials": [],
            "field_status": {"origin": "unknown", "dates": "unknown", "party": "unknown", "budget": "unknown", "pace": "unknown", "interests": "unknown", "fixed_commitments": "unknown", "transport": "unknown"},
        },
        "capabilities": [],
        "sources": [],
        "facts": [],
        "places": [],
        "legs": [],
        "itinerary": {
            "summary": {"title": title or f"{destination or '待定目的地'} 行程（草案）", "destination": destination or "待定", "tagline": None},
            "assumptions": [],
            "tradeoffs": [],
            "unmet": [],
            "days": [],
            "lodging": {"strategy": "待定", "regions": [], "change_lodging": False, "booked": None},
            "transport_major": [],
            "budget": {"currency": "CNY", "hard_limit": None, "categories": [], "note": None},
            "alternatives": [],
            "checklist": [],
            "weather": {"kind": "unavailable", "entries": [], "note": None, "prep": []},
            "risks": [],
            "sources_note": None,
        },
        "decisions": [],
        "changelog": [{"version": 1, "at": ts, "summary": "创建骨架", "affected_ids": []}],
    }


# ----------------------------------------------------------------------------
def cmd_init(a) -> int:
    if not TRIP_ID_RE.match(a.trip_id):
        print("✖ trip_id 只允许小写字母、数字、连字符，长度 3–64，例如 2026-10-10-suzhou-3d", file=sys.stderr)
        return 2
    data = skeleton(a.trip_id, a.title, a.destination)
    preflight(data)
    base = os.path.abspath(a.dir)
    os.makedirs(base, exist_ok=True)
    trip_dir = ensure_inside(base, os.path.join(base, a.trip_id))
    if os.path.exists(os.path.join(trip_dir, "trip.json")):
        print(f"✖ 已存在 {trip_dir}/trip.json，不覆盖。要另起一份请换 trip_id。", file=sys.stderr)
        return 1
    for sub in ("versions", "outputs"):
        os.makedirs(ensure_inside(base, os.path.join(trip_dir, sub)), exist_ok=True)
    atomic_write_json(ensure_inside(base, os.path.join(trip_dir, "trip.json")), data)
    print(f"✔ 已创建 {trip_dir}/trip.json（骨架，status=draft）。令牌与长期偏好请放在此目录之外。")
    return 0


def cmd_checkpoint(a) -> int:
    path = os.path.abspath(a.trip)
    trip_dir = os.path.dirname(path)
    ensure_inside(trip_dir, path)
    target = checkpoint(trip_dir, load_json(path))
    print(f"✔ 已保留不可覆盖的快照：{target}")
    return 0


def invalidate_review(trip, all_facts=False):
    trip["checked_at"] = None
    trip["user_confirmed"] = False
    trip["status"] = "draft"
    if all_facts:
        for f in trip["facts"]:
            if f["status"] == "verified":
                f["status"] = "unknown"
                f.setdefault("conflicts", []).append("恢复或日期变更后待重查")
        for d in trip["itinerary"]["days"]:
            d["weekday_check"] = False
            for i in d["items"]:
                if i["verification_status"] == "verified":
                    i["verification_status"] = "unknown"


def cmd_save(a) -> int:
    path = os.path.abspath(a.trip)
    trip_dir = os.path.dirname(path)
    ensure_inside(trip_dir, path)
    current = load_json(path)
    preflight(current)
    cur_v = current["plan_version"]
    snap_path = ensure_inside(trip_dir, os.path.join(trip_dir, "versions", f"trip.v{cur_v}.json"))
    candidate = getattr(a, "from_file", None)
    if candidate:
        candidate = ensure_inside(trip_dir, os.path.abspath(candidate))
        if candidate == os.path.realpath(path):
            raise ValueError("--from 必须是独立修改稿，不能是当前 trip.json")
        new = load_json(candidate)
        original = current
        if os.path.exists(snap_path) and canonical_hash(load_json(snap_path)) != canonical_hash(current):
            raise ValueError("当前数据已被原地修改；先按已有快照保存当前修改，不能冒充旧版")
    else:
        if not os.path.exists(snap_path):
            raise ValueError("找不到修改前快照。请在修改前 checkpoint，或保留原 trip.json 并使用 save --from 修改稿")
        original = load_json(snap_path)
        new = copy.deepcopy(current)
    preflight(original)
    preflight(new)
    if (new["trip_id"], new["plan_version"]) != (original["trip_id"], cur_v):
        raise ValueError("修改稿的行程 ID 或基础版本与当前版本不一致")
    new["plan_version"] = max([cur_v] + list_versions(trip_dir)) + 1
    new["generated_at"] = now_iso()
    invalidate_review(new, original["request"]["dates"] != new["request"]["dates"])
    affected = [x.strip() for x in (a.affected or "").split(",") if x.strip()]
    new.setdefault("changelog", []).append({"version": new["plan_version"], "at": new["generated_at"], "summary": a.summary, "affected_ids": affected})
    preflight(new)  # Includes user-supplied summary, before any new file is written.
    checkpoint(trip_dir, original)
    atomic_write_json(path, new)
    print(f"✔ 原 v{cur_v} 已保留；当前升级为 v{new['plan_version']}（待重新核查与确认）。")
    print("  下一步：重新核查并请用户确认方案 → 展示四列风格选择图并请用户选择 → render_outputs.py → 长图导出；旧长图已过期。")
    return 0


def list_versions(trip_dir: str) -> list[int]:
    vdir = ensure_inside(trip_dir, os.path.join(trip_dir, "versions"))
    if not os.path.isdir(vdir):
        return []
    vs = []
    for name in os.listdir(vdir):
        m = re.match(r"^trip\.v(\d+)\.json$", name)
        if m:
            vs.append(int(m.group(1)))
    return sorted(vs)


def cmd_versions(a) -> int:
    trip_dir = os.path.abspath(a.trip_dir)
    cur = load_json(ensure_inside(trip_dir, os.path.join(trip_dir, "trip.json")))
    preflight(cur)
    print(f"当前：v{cur['plan_version']} · {cur.get('status')} · {cur.get('generated_at')}")
    for v in list_versions(trip_dir):
        t = load_json(ensure_inside(trip_dir, os.path.join(trip_dir, "versions", f"trip.v{v}.json")))
        preflight(t)
        ch = next((c for c in t.get("changelog", []) if c.get("version") == v), {})
        print(f"  v{v} · {t.get('status')} · {t.get('generated_at')} · {ch.get('summary', '')}")
    return 0


def flatten(obj, prefix="$", out=None):
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}", out)
    elif isinstance(obj, list):
        # 有 ID 的数组按 ID 对齐，否则按下标
        for i, v in enumerate(obj):
            key = None
            if isinstance(v, dict):
                for idk in ("item_id", "day_id", "place_id", "leg_id", "fact_id", "source_id", "alt_id", "check_id", "decision_id", "id"):
                    if idk in v:
                        key = v[idk]
                        break
            flatten(v, f"{prefix}[{key if key is not None else i}]", out)
    else:
        out[prefix] = obj
    return out


def cmd_diff(a) -> int:
    trip_dir = os.path.abspath(a.trip_dir)

    def load_v(v: str):
        if v in ("cur", "current"):
            return load_json(ensure_inside(trip_dir, os.path.join(trip_dir, "trip.json")))
        p = ensure_inside(trip_dir, os.path.join(trip_dir, "versions", f"trip.v{int(v)}.json"))
        if not os.path.exists(p):
            cur = load_json(ensure_inside(trip_dir, os.path.join(trip_dir, "trip.json")))
            if int(cur.get("plan_version", -1)) == int(v):
                return cur
            raise SystemExit(f"✖ 没有版本 v{v}")
        return load_json(p)

    av, bv = load_v(a.va), load_v(a.vb)
    preflight(av)
    preflight(bv)
    A, B = flatten(av), flatten(bv)
    added = sorted(k for k in B if k not in A)
    removed = sorted(k for k in A if k not in B)
    changed = sorted(k for k in A if k in B and A[k] != B[k] and not k.endswith(".generated_at") and not k.endswith(".plan_version"))
    print(f"v{a.va} → v{a.vb}：新增 {len(added)}，删除 {len(removed)}，修改 {len(changed)}")
    for k in changed[: a.limit]:
        print(f"  ~ {k}: {A[k]!r} → {B[k]!r}")
    for k in added[: a.limit]:
        print(f"  + {k}: {B[k]!r}")
    for k in removed[: a.limit]:
        print(f"  - {k}: {A[k]!r}")
    # 受影响对象（按 ID 聚合），便于决定哪些事实需要重查
    ids = set()
    for k in added + removed + changed:
        for m in re.finditer(r"\[([a-zA-Z0-9_-]+)\]", k):
            if not m.group(1).isdigit():
                ids.add(m.group(1))
    if ids:
        print("  受影响 ID：" + ", ".join(sorted(ids)))
    return 0


def cmd_restore(a) -> int:
    trip_dir = os.path.abspath(a.trip_dir)
    src = ensure_inside(trip_dir, os.path.join(trip_dir, "versions", f"trip.v{int(a.v)}.json"))
    if not os.path.exists(src):
        print(f"✖ 没有版本 v{a.v}", file=sys.stderr)
        return 1
    cur_path = ensure_inside(trip_dir, os.path.join(trip_dir, "trip.json"))
    cur = load_json(cur_path)
    old = load_json(src)
    preflight(cur)
    preflight(old)
    if old["trip_id"] != cur["trip_id"] or old["plan_version"] != int(a.v):
        raise ValueError("快照 ID/版本与恢复目标不一致")
    cur_v = cur["plan_version"]
    # Never overwrite an immutable snapshot or silently discard unsaved edits.
    target = ensure_inside(trip_dir, os.path.join(trip_dir, "versions", f"trip.v{cur_v}.json"))
    if os.path.exists(target) and canonical_hash(load_json(target)) != canonical_hash(cur):
        raise ValueError("当前存在未保存修改，请先 save 后恢复")
    old["plan_version"] = max([cur_v] + list_versions(trip_dir)) + 1
    old["generated_at"] = now_iso()
    invalidate_review(old, all_facts=True)
    old["changelog"] = copy.deepcopy(cur.get("changelog", []))
    old["changelog"].append({"version": old["plan_version"], "at": old["generated_at"], "summary": f"从 v{a.v} 恢复", "affected_ids": []})
    preflight(old)
    checkpoint(trip_dir, cur)
    atomic_write_json(cur_path, old)
    print(f"✔ 已把 v{a.v} 恢复为当前 v{old['plan_version']}；事实与行程核查状态已失效，需重新核查。")
    return 0


def scan_obj_for_secrets(obj) -> list[str]:
    return secret_hits(obj)


def cmd_scan(a) -> int:
    trip_dir = os.path.abspath(a.trip_dir)
    hits = []
    for root, _, files in os.walk(trip_dir):
        for name in files:
            if not name.lower().endswith((".json", ".md", ".html", ".txt", ".log")):
                continue
            p = ensure_inside(trip_dir, os.path.join(root, name))
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            # 去掉合法的 64 位指纹
            text = re.sub(r"\b[0-9a-f]{64}\b", "", text)
            for pat, label in SECRET_PATTERNS:
                if pat.search(text):
                    hits.append(f"{os.path.relpath(p, trip_dir)}: {label}")
                    break
    if hits:
        print("✖ 发现疑似凭据：")
        for h in hits:
            print("  " + h)
        return 1
    print("✔ 未发现疑似凭据。")
    return 0


def main(argv=None) -> int:
    configure_console()
    ap = argparse.ArgumentParser(description="alun-travel-plan 存档与版本")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("trip_id"); p.add_argument("--dir", default="travel-plan"); p.add_argument("--title"); p.add_argument("--destination"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("save"); p.add_argument("trip"); p.add_argument("--summary", required=True); p.add_argument("--affected"); p.add_argument("--from", dest="from_file", help="独立修改稿路径，位于本行程目录内"); p.set_defaults(fn=cmd_save)
    p = sub.add_parser("checkpoint"); p.add_argument("trip"); p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser("versions"); p.add_argument("trip_dir"); p.set_defaults(fn=cmd_versions)
    p = sub.add_parser("diff"); p.add_argument("trip_dir"); p.add_argument("va"); p.add_argument("vb"); p.add_argument("--limit", type=int, default=40); p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("restore"); p.add_argument("trip_dir"); p.add_argument("v"); p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("scan"); p.add_argument("trip_dir"); p.set_defaults(fn=cmd_scan)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, OSError, KeyError, TypeError):
        print("✖ 操作未完成：数据、快照或路径检查未通过。请检查版本与修改稿；未覆盖历史快照。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
