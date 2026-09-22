#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_trip.py · alun-travel-plan 的确定性校验脚本（只用标准库）

用法：
  python3 scripts/validate_trip.py travel-plan/<trip_id>/trip.json
  python3 scripts/validate_trip.py travel-plan/<trip_id>/trip.json --out travel-plan/<trip_id>/audit.v1.json
  python3 scripts/validate_trip.py travel-plan/<trip_id>/trip.json --check-outputs [--manifest path]

做什么：结构（按 assets/schemas/trip.schema.json 的子集实现）、ID 引用、日期与星期、
时间重叠、路段留时、开放窗口、预约状态一致性、预算算术、锁定项、必去项、状态冲突、
凭据与危险链接扫描；--check-outputs 时再检查 MD/HTML 文件存在、指纹、署名、覆盖率、无远程依赖。

不做什么：不判断来源是否真的支持结论、是否适用目标日期——那是模型按 references/audit.md 逐项做的。
程序看到 verified 字段不等于事实已验证；本脚本输出的每条检查都标 method=script。

退出码：0 = 无阻断问题；1 = 有阻断问题；2 = 文件无法解析或结构错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import math
from trip_support import canonical_hash, inside, atomic_json, secret_hits, configure_console, budget_limit
from datetime import datetime, timedelta, date

SCRIPT_VERSION = "1.2.1"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
SCHEMA_PATH = os.path.join(SKILL_ROOT, "assets", "schemas", "trip.schema.json")
ATTRIBUTION_PATH = os.path.join(SKILL_ROOT, "assets", "templates", "attribution.json")

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ----------------------------------------------------------------------------
# 极简 JSON Schema 子集校验（type/required/properties/items/enum/const/anyOf/
# pattern/minimum/maximum/minItems/additionalProperties/$ref 同文档）
# ----------------------------------------------------------------------------
class MiniSchema:
    def __init__(self, schema: dict):
        self.root = schema
        self.errors: list[str] = []

    def _resolve(self, ref: str) -> dict:
        assert ref.startswith("#/"), ref
        node = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    @staticmethod
    def _type_ok(value, t: str) -> bool:
        if t == "object":
            return isinstance(value, dict)
        if t == "array":
            return isinstance(value, list)
        if t == "string":
            return isinstance(value, str)
        if t == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if t == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if t == "boolean":
            return isinstance(value, bool)
        if t == "null":
            return value is None
        return True

    def check(self, value, schema: dict, path: str) -> bool:
        if "$ref" in schema:
            return self.check(value, self._resolve(schema["$ref"]), path)
        ok = True
        if "const" in schema and value != schema["const"]:
            self.errors.append(f"{path}: 应为常量 {schema['const']!r}，实际 {value!r}")
            ok = False
        if "enum" in schema and value not in schema["enum"]:
            self.errors.append(f"{path}: 值 {value!r} 不在枚举 {schema['enum']} 内")
            ok = False
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self._type_ok(value, t) for t in types):
                self.errors.append(f"{path}: 类型应为 {types}，实际 {type(value).__name__}")
                return False
        if "anyOf" in schema:
            saved = self.errors
            matched = False
            for sub in schema["anyOf"]:
                self.errors = []
                if self.check(value, sub, path):
                    matched = True
                    break
            self.errors = saved
            if not matched:
                self.errors.append(f"{path}: 值 {value!r} 不满足任一候选类型")
                ok = False
        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):
                self.errors.append(f"{path}: 字符串 {value!r} 不匹配 {schema['pattern']}")
                ok = False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                self.errors.append(f"{path}: {value} 小于最小值 {schema['minimum']}")
                ok = False
            if "maximum" in schema and value > schema["maximum"]:
                self.errors.append(f"{path}: {value} 大于最大值 {schema['maximum']}")
                ok = False
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                self.errors.append(f"{path}: 至少需要 {schema['minItems']} 项")
                ok = False
            if "items" in schema:
                for i, v in enumerate(value):
                    if not self.check(v, schema["items"], f"{path}[{i}]"):
                        ok = False
        if isinstance(value, dict):
            for req in schema.get("required", []):
                if req not in value:
                    self.errors.append(f"{path}: 缺少必填字段 {req!r}")
                    ok = False
            props = schema.get("properties", {})
            for k, v in value.items():
                if k in props:
                    if not self.check(v, props[k], f"{path}.{k}"):
                        ok = False
                elif "additionalProperties" in schema:
                    ap = schema["additionalProperties"]
                    if ap is False:
                        self.errors.append(f"{path}: 不允许的字段 {k!r}")
                        ok = False
                    elif isinstance(ap, dict):
                        if not self.check(v, ap, f"{path}.{k}"):
                            ok = False
        return ok


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo is not None else None
    except ValueError:
        return None


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def hhmm_on(d: date, hhmm: str, tzinfo) -> datetime:
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=tzinfo)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_strings(node, path="$"):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from walk_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_strings(v, f"{path}[{i}]")


SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9\-_\.]{12,}"), "疑似键值形式的凭据"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=]{16,}"), "疑似 Bearer 令牌"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "疑似 sk- 开头的密钥"),
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), "疑似长十六进制令牌"),
    (re.compile(r"TIKHUB_API_KEY\s*=\s*(?!<|\$|\{|your|YOUR|xxx|XXX|\.\.\.)\S{8,}"), "TIKHUB_API_KEY 出现了实值"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "疑似 18 位证件号码"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "疑似银行卡号"),
]
BAD_SCHEME = re.compile(r"(?i)^\s*(javascript|data|file|vbscript):")


# ----------------------------------------------------------------------------
# 报告构造
# ----------------------------------------------------------------------------
class Audit:
    def __init__(self, trip: dict):
        self.trip = trip
        self.checks: list[dict] = []
        self.issues: list[dict] = []
        self._n = 0

    def check(self, check_id: str, dimension: str, result: str, detail: str, method: str = "script"):
        self.checks.append({"check_id": check_id, "dimension": dimension, "method": method, "result": result, "detail": detail})

    def issue(self, severity: str, affected: list[str], evidence: str, fix: str):
        self._n += 1
        self.issues.append({
            "issue_id": f"issue-{self._n:03d}",
            "severity": severity,
            "affected_ids": affected,
            "evidence": evidence,
            "fix_action": fix,
            "resolved": False,
        })

    def summary(self) -> dict:
        s = {"blocking": 0, "conditional": 0, "info": 0}
        for i in self.issues:
            s[i["severity"]] += 1
        return s

    def recommended_status(self) -> str:
        s = self.summary()
        req = self.trip.get("request", {})
        missing_core = not (req.get("origin", {}) or {}).get("name") or not (req.get("dates", {}) or {}).get("start")
        if s["blocking"] > 0 or missing_core:
            return "draft"
        if s["conditional"] > 0:
            return "conditional"
        return "executable_as_of_check"

    def to_json(self) -> dict:
        return {
            "trip_id": self.trip.get("trip_id"),
            "plan_version": self.trip.get("plan_version"),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "script_version": SCRIPT_VERSION,
            "checks": self.checks,
            "issues": self.issues,
            "summary": self.summary(),
            "recommended_status": self.recommended_status(),
        }


# ----------------------------------------------------------------------------
# 语义检查
# ----------------------------------------------------------------------------
def semantic_checks(trip: dict, audit: Audit) -> None:
    req = trip["request"]
    it = trip["itinerary"]
    if not it["days"]:
        audit.issue("conditional" if trip["status"] == "draft" else "blocking", [], "尚未规划每日行程", "补充需求后规划；未规划只能是 draft")
    if not trip.get("checked_at"):
        audit.issue("conditional", [], "尚未完成事实核查", "执行模型核查后记录 checked_at")
    for values, id_key in [(trip["places"], "place_id"), (trip["facts"], "fact_id"),
                           (trip["sources"], "source_id"), (trip["legs"], "leg_id"),
                           (it["days"], "day_id"),
                           ([i for d in it["days"] for i in d["items"]], "item_id")]:
        ids = [v[id_key] for v in values]
        if len(ids) != len(set(ids)):
            audit.issue("blocking", [], f"存在重复 {id_key}", "为每个对象使用独立 ID")
    places = {p["place_id"]: p for p in trip["places"]}
    legs = {l["leg_id"]: l for l in trip["legs"]}
    facts = {f["fact_id"]: f for f in trip["facts"]}
    sources = {s["source_id"]: s for s in trip["sources"]}
    alts = {a["alt_id"]: a for a in it["alternatives"]}
    items: dict[str, dict] = {}
    for d in it["days"]:
        for i in d["items"]:
            items[i["item_id"]] = i
    all_ids = set(places) | set(legs) | set(facts) | set(sources) | set(alts) | set(items) | {d["day_id"] for d in it["days"]}

    # ---- 1. ID 引用 ------------------------------------------------------
    ref_errors = []
    for p in trip["places"]:
        for fid in p.get("fact_ids", []):
            if fid not in facts:
                ref_errors.append(f"{p['place_id']} → fact {fid}")
    for f in trip["facts"]:
        for sid in f["source_ids"]:
            if sid not in sources:
                ref_errors.append(f"{f['fact_id']} → source {sid}")
        if f["subject_id"] not in all_ids:
            audit.issue("info", [f["fact_id"]], f"事实 {f['fact_id']} 的 subject_id={f['subject_id']} 不是已知对象", "改为正确的 place/item/leg ID")
        if f["status"] == "verified" and not f["source_ids"]:
            audit.issue("blocking", [f["fact_id"]], f"事实 {f['fact_id']} 标为 verified 但没有来源", "补来源或降为 unknown")
    for l in trip["legs"]:
        for pid in (l["from_id"], l["to_id"]):
            if pid not in places:
                ref_errors.append(f"{l['leg_id']} → place {pid}")
        for sid in l["source_ids"]:
            if sid not in sources:
                ref_errors.append(f"{l['leg_id']} → source {sid}")
        if l["evidence_level"] == "unknown":
            if l["time_min"] is not None or l["time_max"] is not None:
                audit.issue("blocking", [l["leg_id"]], f"路段 {l['leg_id']} 证据等级 unknown 却填了时间", "未知不能转成数字；删掉时间或补证据等级")
        else:
            if l["time_min"] is None or l["time_max"] is None:
                audit.issue("blocking", [l["leg_id"]], f"路段 {l['leg_id']} 有证据等级但缺时间范围", "补 time_min/time_max 或改为 unknown")
            elif l["time_min"] > l["time_max"]:
                audit.issue("blocking", [l["leg_id"]], f"路段 {l['leg_id']} time_min > time_max", "修正范围")
            elif l["time_max"] == 0 and l["from_id"] != l["to_id"]:
                audit.issue("conditional", [l["leg_id"]], f"路段 {l['leg_id']} 上界为 0 分钟", "0 通常是未知被写成了零，请核对")
    for i in items.values():
        if i["place_id"] and i["place_id"] not in places:
            ref_errors.append(f"{i['item_id']} → place {i['place_id']}")
        for fid in i["fact_ids"]:
            if fid not in facts:
                ref_errors.append(f"{i['item_id']} → fact {fid}")
        if i["incoming_leg_id"] and i["incoming_leg_id"] not in legs:
            ref_errors.append(f"{i['item_id']} → leg {i['incoming_leg_id']}")
        if i["alternative_id"] and i["alternative_id"] not in alts:
            ref_errors.append(f"{i['item_id']} → alternative {i['alternative_id']}")
    for a in alts.values():
        for iid in a["replaces_item_ids"]:
            if iid not in items:
                ref_errors.append(f"{a['alt_id']} → item {iid}")
        if a["rejoin_at_item_id"] and a["rejoin_at_item_id"] not in items:
            ref_errors.append(f"{a['alt_id']} → rejoin item {a['rejoin_at_item_id']}")
        if a.get("place_id") and a["place_id"] not in places:
            ref_errors.append(f"{a['alt_id']} → place {a['place_id']}")
        if a["instant"] and a["requires_booking"]:
            audit.issue("blocking", [a["alt_id"]], f"替代方案 {a['alt_id']} 标为即时备用却需要预约", "instant 只能给无需预约、随时可切换的活动")
    for c in it["checklist"]:
        for iid in c.get("related_item_ids", []):
            if iid not in items:
                ref_errors.append(f"{c['check_id']} → item {iid}")
    for d in it["days"]:
        for aid in d["alternative_ids"]:
            if aid not in alts:
                ref_errors.append(f"{d['day_id']} → alternative {aid}")
    for dec in trip["decisions"]:
        for x in dec["affected_ids"]:
            if x not in all_ids:
                audit.issue("info", [dec["decision_id"]], f"决策 {dec['decision_id']} 引用了未知 ID {x}", "核对 affected_ids")
    if ref_errors:
        audit.check("ref-integrity", "ID 引用", "fail", "; ".join(ref_errors))
        audit.issue("blocking", [], f"存在 {len(ref_errors)} 处悬空引用：" + "; ".join(ref_errors[:10]), "修正 ID 或补对象")
    else:
        audit.check("ref-integrity", "ID 引用", "pass", "所有 place/fact/source/leg/alternative/item 引用均可解析")

    # ---- 2. 日期、星期、时间线 ----------------------------------------------
    start = parse_date(req["dates"].get("start"))
    end = parse_date(req["dates"].get("end"))
    return_by = parse_dt(req["dates"].get("return_by"))
    prev_date = None
    weekday_notes = []
    for d in it["days"]:
        dd = parse_date(d["date"])
        if dd is None:
            audit.issue("blocking", [d["day_id"]], f"{d['day_id']} 日期无法解析", "改为 YYYY-MM-DD")
            continue
        weekday_notes.append(f"{d['date']}={WEEKDAY_ZH[dd.weekday()]}")
        if start and dd < start or end and dd > end:
            audit.issue("conditional", [d["day_id"]], f"{d['day_id']} 的日期 {d['date']} 不在需求日期范围 {req['dates'].get('start')}~{req['dates'].get('end')} 内", "核对日期或更新需求")
        if prev_date and (dd - prev_date).days != 1:
            audit.issue("info", [d["day_id"]], f"{d['day_id']} 与上一天不连续", "确认是否故意留空一天")
        prev_date = dd
        if not d.get("weekday_check", False):
            audit.issue("conditional", [d["day_id"]], f"{d['day_id']}（{d['date']} {WEEKDAY_ZH[dd.weekday()]}）尚未确认星期", "模型确认星期与开放规则后置 weekday_check=true")

        # 时间线
        seq = d["items"]
        parsed = []
        for i in seq:
            s, e = parse_dt(i["planned_start"]), parse_dt(i["planned_end"])
            if s is None or e is None:
                audit.issue("blocking", [i["item_id"]], f"{i['item_id']} 时间无法解析", "使用带时区的 ISO 8601")
                parsed.append(None)
                continue
            if e <= s:
                audit.issue("blocking", [i["item_id"]], f"{i['item_id']} 结束时间不晚于开始时间", "修正时间")
            if s.date() != dd:
                audit.issue("conditional", [i["item_id"]], f"{i['item_id']} 的开始日期 {s.date()} 与 {d['day_id']} 的 {dd} 不同", "跨午夜/跨时区项目请确认归属日")
            parsed.append((s, e))
        for k in range(1, len(seq)):
            if parsed[k] is None or parsed[k - 1] is None:
                continue
            ps, pe = parsed[k - 1]
            cs, ce = parsed[k]
            cur, prev = seq[k], seq[k - 1]
            if cs < pe:
                audit.issue("blocking", [prev["item_id"], cur["item_id"]], f"{prev['item_id']} 与 {cur['item_id']} 时间重叠（{pe.time()} > {cs.time()}）", "调整顺序或时长")
            # 路段留时
            leg_id = cur.get("incoming_leg_id")
            if leg_id and leg_id in legs:
                leg = legs[leg_id]
                if leg["evidence_level"] == "unknown":
                    if cur["verification_status"] == "verified":
                        audit.issue("blocking", [cur["item_id"], leg_id], f"{cur['item_id']} 依赖 unknown 路段却标 verified", "降为 conditional 或补路段证据")
                    else:
                        audit.issue("conditional", [cur["item_id"], leg_id], f"{cur['item_id']} 的到达路段 {leg_id} 没有时间依据", "写清无法确认，或补公开路线资料/估算")
                else:
                    tmax = timedelta(minutes=leg["time_max"])
                    if prev.get("kind") == "transit" and prev.get("incoming_leg_id") == leg_id:
                        # 上一项就是这段交通：检查交通项时长 >= 上界
                        if (pe - ps) < tmax:
                            audit.issue("blocking", [prev["item_id"], leg_id], f"交通项 {prev['item_id']} 只留了 {int((pe-ps).total_seconds()//60)} 分钟，少于路段上界 {leg['time_max']} 分钟", "按上界留时")
                    elif cur.get("kind") == "transit":
                        if (ce - cs) < tmax:
                            audit.issue("blocking", [cur["item_id"], leg_id], f"交通项 {cur['item_id']} 只留了 {int((ce-cs).total_seconds()//60)} 分钟，少于路段上界 {leg['time_max']} 分钟", "按上界留时")
                    else:
                        if cs < pe + tmax:
                            audit.issue("blocking", [cur["item_id"], leg_id], f"{cur['item_id']} 开始时间未留够路段上界 {leg['time_max']} 分钟（上一项 {pe.time()} 结束）", "推迟开始或插入交通项")
                    if cur.get("place_id") and leg["to_id"] != cur["place_id"] and cur.get("kind") != "transit":
                        audit.issue("info", [cur["item_id"], leg_id], f"路段 {leg_id} 终点 {leg['to_id']} 与 {cur['item_id']} 的地点 {cur['place_id']} 不一致", "核对路段方向（A→B 不等于 B→A）")
            else:
                # 没有路段：不同地点之间是否说明了同片区步行
                if prev.get("place_id") and cur.get("place_id") and prev["place_id"] != cur["place_id"] and cur.get("kind") not in ("transit",):
                    text = (cur.get("notes") or "") + cur.get("description", "")
                    if not any(kw in text for kw in ("步行", "同片区", "同一场所", "相邻", "同一区域")):
                        audit.issue("conditional", [prev["item_id"], cur["item_id"]], f"{prev['item_id']} → {cur['item_id']} 地点不同但没有路段也没有步行说明", "补 TravelLeg 或在 notes 写明同片区步行")

        # 地点规则适用于实际在场活动；不能通过 meal/rest 类型绕开入场要求。
        for idx, i in enumerate(seq):
            admission_id = i.get("admission_item_id")
            if i["kind"] in ("transit", "transport_major"):
                if admission_id:
                    audit.issue("blocking", [i["item_id"]], "交通项不能沿用场所入场资格", "移除 admission_item_id，独立核对交通预订")
                continue
            if parsed[idx] is None:
                continue
            s, e = parsed[idx]
            p = places.get(i["place_id"]) if i["place_id"] else None
            if p is None:
                if i["kind"] in ("visit", "meal", "checkin") or admission_id:
                    audit.issue("blocking", [i["item_id"]], f"{i['kind']} 项 {i['item_id']} 没有 place_id", "补活动实际发生的地点，不借用附近地标")
                continue
            if i["kind"] == "visit" and not i["fact_ids"]:
                audit.issue("conditional", [i["item_id"]], f"visit 项 {i['item_id']} 没有任何事实依据", "至少挂一条开放相关的 FactRecord（状态可以是 unknown）")
            admission = None
            if admission_id:
                origin_idx = next((n for n, x in enumerate(seq[:idx]) if x["item_id"] == admission_id), None)
                origin = seq[origin_idx] if origin_idx is not None else None
                if (origin is None or origin["kind"] != "visit" or origin.get("admission_item_id")
                        or origin["place_id"] != i["place_id"] or parsed[origin_idx] is None
                        or parsed[origin_idx][1] > s
                        or any(x["place_id"] != i["place_id"] or x["kind"] in ("transit", "transport_major")
                               for x in seq[origin_idx + 1:idx])):
                    audit.issue("blocking", [i["item_id"], admission_id], "入场沿用关系无效：必须引用同日、同地点、已结束且中间未离场的直接 visit 项", "院外活动建立独立地点；再次入场重新核查预约，不串联或跨日沿用")
                else:
                    admission = origin
            # 预约一致性
            br = (p.get("booking") or {}).get("required")
            bs = (admission or i)["booking_status"]
            # 退房是离场动作，既有住宿需要预订不等于退房要新增预约。
            if br is True and i["kind"] != "checkout":
                if bs == "not_required":
                    audit.issue("blocking", [i["item_id"], p["place_id"]], f"{p['name']} 需要预约，但 {i['item_id']} 的入场状态标为 not_required", "核对实际地点；馆内活动用 admission_item_id 明确沿用入场项，院外活动另建地点")
                elif bs in ("pending_user", "not_open", "unknown", "user_claimed"):
                    audit.issue("conditional", [i["item_id"]], f"{p['name']} 需要预约，当前状态 {bs}", "写入清单：渠道、截止时间、失败替代；确认凭证后才可改 confirmed")
                    if i["verification_status"] == "verified":
                        audit.issue("blocking", [i["item_id"]], f"{i['item_id']} 预约状态 {bs} 却标 verified", "降为 conditional")
                elif bs == "failed":
                    audit.issue("blocking", [i["item_id"]], f"{i['item_id']} 预约失败仍在主行程", "切换到替代方案或让用户选择")
            elif br is None and bs not in ("unknown", "not_required") and i["verification_status"] == "verified":
                audit.issue("conditional", [i["item_id"], p["place_id"]], f"{p['name']} 是否需要预约未知", "查清 booking.required")
            # 事实状态与核查状态一致
            if i["verification_status"] == "verified":
                bad = [fid for fid in i["fact_ids"] if facts.get(fid, {}).get("status") != "verified"]
                if bad:
                    audit.issue("blocking", [i["item_id"]] + bad, f"{i['item_id']} 标 verified，但依据 {bad} 不是 verified", "降级行程项或补核查")
            if i["alternative_id"] is None and i["verification_status"] in ("conditional", "unknown") and p.get("booking", {}).get("required"):
                audit.issue("conditional", [i["item_id"]], f"{i['item_id']} 有前置条件但没有替代方案", "补 Alternative")

            # 活动另需预约时，入场已确认也不能把活动本身升级成已核实。
            if admission and i["booking_status"] not in ("confirmed", "not_required"):
                audit.issue("conditional", [i["item_id"]], f"活动自身预约尚未确认：{i['booking_status']}", "分别核对场所入场和活动预约")
                if i["verification_status"] == "verified":
                    audit.issue("blocking", [i["item_id"]], "活动预约未确认却标 verified", "降级活动核查状态")
            if admission and admission["verification_status"] != "verified" and i["verification_status"] == "verified":
                audit.issue("blocking", [i["item_id"], admission_id], "沿用的入场项未核实，活动却标 verified", "保留入场前提并降级活动核查状态")

            wins = p.get("opening_windows", [])
            if not wins:
                if i["kind"] == "visit" and i["verification_status"] == "verified":
                    audit.issue("blocking", [i["item_id"], p["place_id"]], f"{i['item_id']} 标 verified 但地点 {p['place_id']} 没有开放窗口记录", "补窗口或降级")
                else:
                    audit.issue("info", [i["item_id"], p["place_id"]], f"地点 {p['place_id']} 开放时间未知（空数组），{i['item_id']} 按未知处理", "查到后补 opening_windows")
                continue
            applicable = [w for w in wins if (w.get("dates") and d["date"] in w["dates"]) or (w.get("days") and dd.isoweekday() in w["days"]) or (not w.get("dates") and not w.get("days"))]
            if not applicable:
                audit.issue("blocking", [i["item_id"], p["place_id"]], f"{d['date']}（{WEEKDAY_ZH[dd.weekday()]}）{p['name']} 没有适用的开放窗口（常规闭馆日？）", "换日或换点；若有特别开放公告，添加 dates 窗口并挂事实")
                continue
            fits = False
            reasons = []
            for w in applicable:
                o = hhmm_on(dd, w["open"], s.tzinfo)
                c = hhmm_on(dd, w["close"], s.tzinfo)
                le = hhmm_on(dd, w["last_entry"], s.tzinfo) if w.get("last_entry") else c
                if s >= o and (admission is not None or s <= le) and e <= c:
                    fits = True
                    break
                reasons.append(f"窗口 {w['open']}–{w['close']}（最后入场 {w.get('last_entry') or w['close']}）")
            if not fits:
                audit.issue("blocking", [i["item_id"], p["place_id"]], f"{i['item_id']} 计划 {s.time()}–{e.time()} 不在 {p['name']} 的开放窗口内：{'; '.join(reasons)}", "调整时段，或确认分时段开放后选择完整可用区间")

    timeline = []
    for day in it["days"]:
        for item in day["items"]:
            begin, finish = parse_dt(item["planned_start"]), parse_dt(item["planned_end"])
            if begin and finish:
                timeline.append((begin, finish, day["day_id"], item))
            if item["booking_status"] == "failed" and not any("预约失败仍在主行程" in x["evidence"] and item["item_id"] in x["affected_ids"] for x in audit.issues):
                audit.issue("blocking", [item["item_id"]], "预约失败仍在主行程", "切换到已核查的替代方案")
            if item["verification_status"] == "blocked":
                audit.issue("blocking", [item["item_id"]], "阻断项目仍在主行程", "解决冲突或移出主行程")
    active = []
    for begin, finish, did, item in sorted(timeline, key=lambda x: x[0]):
        active = [v for v in active if v[1] > begin]
        for previous in active:
            if previous[2] != did:
                audit.issue("blocking", [previous[3]["item_id"], item["item_id"]],
                            "跨日时间重叠（已按时间中的 UTC 偏移比较）", "调整跨日安排与次日首项")
        active.append((begin, finish, did, item))

    audit.check("dates-weekdays", "日期与身份", "pass", "星期换算：" + ", ".join(weekday_notes))

    # ---- 3. 返程边界与锁定项 -------------------------------------------------
    last_items = [i for d in it["days"] for i in d["items"]]
    last_end = max((parse_dt(i["planned_end"]) for i in last_items if parse_dt(i["planned_end"])), default=None)
    if return_by and last_end and last_end > return_by:
        audit.issue("blocking", [it["days"][-1]["day_id"]], f"最后一项 {last_end.isoformat()} 晚于最晚返回 {return_by.isoformat()}", "压缩最后一天或与用户确认返程边界")
    locked_items = [i for i in items.values() if i["locked"]]
    for fc in req["fixed_commitments"]:
        if fc["status"] != "confirmed":
            continue
        fs, fe = parse_dt(fc.get("start")), parse_dt(fc.get("end"))
        if fs is None:
            continue
        hit = [i for i in locked_items if parse_dt(i["planned_start"]) == fs]
        if not hit:
            audit.issue("blocking", [fc["id"]], f"已确认的锁定安排 {fc['id']}（{fc['description']}）没有对应的 locked 行程项", "把锁定项放回行程，⛔ 不擅自改动已订安排")
        else:
            i = hit[0]
            if fe and parse_dt(i["planned_end"]) != fe:
                audit.issue("blocking", [fc["id"], i["item_id"]], f"锁定项 {i['item_id']} 的结束时间与预订 {fc['id']} 不一致", "以预订为准")
            if i["booking_status"] not in ("confirmed", "user_claimed", "not_required"):
                audit.issue("conditional", [i["item_id"]], f"锁定项 {i['item_id']} 的预约状态是 {i['booking_status']}", "核对预订凭证")
    audit.check("locked-items", "用户要求", "pass" if not any(x["severity"] == "blocking" and any(f["id"] in x["affected_ids"] for f in req["fixed_commitments"]) for x in audit.issues) else "fail", f"检查了 {len(req['fixed_commitments'])} 项已定安排")

    # ---- 4. 必去项 -----------------------------------------------------------
    item_places = {i["place_id"] for i in items.values() if i["place_id"]}
    item_titles = " ".join(i["title"] for i in items.values())
    for m in req["interests"]["must"]:
        pid = m.get("place_id")
        present = (pid and pid in item_places) or (not pid and m["label"] in item_titles)
        if not present:
            if any(m["label"] in u for u in it["unmet"]):
                audit.issue("conditional", [pid or m["label"]], f"必去项目「{m['label']}」未排入，已列入 unmet", "确认已与用户沟通并记录决策")
            else:
                audit.issue("blocking", [pid or m["label"]], f"必去项目「{m['label']}」不在行程中，也没有写进 unmet", "排入或报告冲突让用户取舍，⛔ 不悄悄删")
    audit.check("must-go", "用户要求", "pass", f"检查了 {len(req['interests']['must'])} 个必去项目")

    # ---- 5. 预算算术 -----------------------------------------------------------
    b = it["budget"]
    tot_min = tot_max = paid = 0.0
    unknown_cats = []
    for c in b["categories"]:
        paid += c.get("paid") or 0
        if c["status"] == "unknown":
            unknown_cats.append(c["name"])
            if c["min"] is not None or c["max"] is not None:
                audit.issue("info", [], f"预算类别「{c['name']}」标 unknown 却有数字", "未知就留 null")
            continue
        if c["min"] is None or c["max"] is None:
            audit.issue("blocking", [], f"预算类别「{c['name']}」状态 {c['status']} 但缺 min/max", "补数字或改为 unknown")
            continue
        if c["min"] > c["max"]:
            audit.issue("blocking", [], f"预算类别「{c['name']}」min > max", "修正")
        tot_min += c["min"]
        tot_max += c["max"]
    hard = budget_limit(trip)
    if hard is None:
        audit.issue("conditional", [], "预算上限或计价口径尚未确认", "人均预算填写 budget_persons；跨币种先明确折算后的总额")
    if req["budget"].get("currency") == b["currency"] and req["budget"].get("paid_amount") is not None and abs(req["budget"]["paid_amount"] - paid) > .01:
        audit.issue("blocking", [], "需求已付金额与分类已付合计不一致", "按相同币种与全体同行者口径对齐")
    for day in it["days"]:
        for item in day["items"]:
            cost = item["cost"]
            if cost["status"] == "unknown" and (cost["min"] is not None or cost["max"] is not None):
                audit.issue("blocking", [item["item_id"]], "费用未知却填写数字", "未知金额使用 null")
            if cost["currency"] != b["currency"]:
                audit.issue("conditional", [item["item_id"]], "存在异币种费用，尚未验证折算", "保留原币与汇率日期，确认分类汇总是否涵盖")
    detail = f"已付 {paid:g}，预计未付 {tot_min:g}–{tot_max:g}，总成本 {paid+tot_min:g}–{paid+tot_max:g} {b['currency']}；未知类别 {len(unknown_cats)} 项"
    if hard is not None:
        if paid + tot_min > hard:
            audit.issue("blocking", [], f"预算下界 {paid+tot_min:g} 已超硬上限 {hard:g}", "删减可选项目或请用户调整预算")
        elif paid + tot_max > hard:
            audit.issue("conditional", [], f"预算上界 {paid+tot_max:g} 超硬上限 {hard:g}（下界未超）", "说明超支风险与可删项")
    if unknown_cats:
        audit.issue("conditional", [], f"预算有未知类别：{', '.join(unknown_cats)}（未知不是零）", "交付里单独列出")
    audit.check("budget-arith", "预算", "pass", detail)

    # ---- 6. 事实时效 -----------------------------------------------------------
    for f in trip["facts"]:
        vf = f.get("valid_for")
        if f["status"] == "verified" and vf and start and end:
            vfrom, vto = parse_date(vf.get("from")), parse_date(vf.get("to"))
            if (vfrom and vfrom > end) or (vto and vto < start):
                audit.issue("conditional", [f["fact_id"]], f"事实 {f['fact_id']} 的适用期 {vf} 不覆盖旅行日期", "重查目标日期")
        if f["status"] == "conflict":
            audit.issue("conditional", [f["fact_id"], f["subject_id"]], f"事实 {f['fact_id']} 存在未解决冲突", "对应安排降级并保留双方信息")
    audit.check("fact-validity", "开放与入场", "pass", f"检查了 {len(trip['facts'])} 条事实的适用期与冲突标记")

    # ---- 7. 天气 -----------------------------------------------------------------
    w = it["weather"]
    if w["kind"] == "forecast":
        for e in w["entries"]:
            if not e.get("source_id"):
                audit.issue("conditional", [], f"天气条目 {e['date']} 标为预报但没有来源", "补来源或改为 seasonal")
    elif w["kind"] == "seasonal" and w["entries"]:
        for e in w["entries"]:
            if re.search(r"\d+\s*[°℃]", e["summary"]):
                audit.issue("conditional", [], f"季节参考条目 {e['date']} 含具体温度，容易被当成预报", "去掉逐日温度或改为区间描述")
    audit.check("weather-kind", "风险与备选", "pass", f"weather.kind={w['kind']}")

    # ---- 8. 清单与替代 -------------------------------------------------------------
    must_todo = [c for c in it["checklist"] if c["priority"] == "must" and c["status"] == "todo"]
    for c in must_todo:
        if not c.get("deadline") or not c.get("if_unresolved"):
            audit.issue("conditional", [c["check_id"]], f"清单项 {c['check_id']} 缺截止时间或未解决怎么办", "按六段式补全")
    if must_todo:
        audit.issue("conditional", [c["check_id"] for c in must_todo], f"有 {len(must_todo)} 项必须完成的出发前事项未完成", "方案最高为 conditional；在日程位置重复提醒")
    audit.check("checklist", "风险与备选", "pass", f"清单 {len(it['checklist'])} 项，其中 must/todo {len(must_todo)} 项")

    # ---- 9. 状态冲突 ---------------------------------------------------------------
    s = audit.summary()
    if trip["status"] == "executable_as_of_check":
        if s["blocking"] > 0 or s["conditional"] > 0:
            audit.issue("blocking", [], f"方案标为 executable_as_of_check，但存在 {s['blocking']} 个阻断、{s['conditional']} 个条件问题", "改为 conditional/draft，或先解决问题")
        weak = [i["item_id"] for i in items.values() if i["kind"] == "visit" and i["verification_status"] != "verified"]
        if weak:
            audit.issue("blocking", weak, f"方案标为可执行，但 {len(weak)} 个游览项目未 verified", "降级方案状态")
    if trip.get("user_confirmed") and any(i["booking_status"] in ("pending_user", "not_open") for i in items.values()):
        audit.check("confirm-vs-booking", "输出一致性", "pass", "用户已确认，但仍有待预约项——两者独立，未自动改写预约状态")
    audit.check("status-consistency", "输出一致性", "fail" if trip["status"] == "executable_as_of_check" and (s["blocking"] or s["conditional"]) else "pass", f"trip.status={trip['status']}，建议 {audit.recommended_status()}")

    # ---- 10. 安全扫描 ----------------------------------------------------------------
    hits = []
    for path, text in walk_strings(trip):
        for pat, label in SECRET_PATTERNS:
            if pat.search(text):
                hits.append(f"{path}: {label}")
                break
        if BAD_SCHEME.match(text):
            hits.append(f"{path}: 危险协议链接")
        if path.endswith(".url") or path.endswith(".nav_link"):
            if text and not text.lower().startswith(("http://", "https://")):
                hits.append(f"{path}: 非 http(s) 链接")
        if "../" in text and any(k in path for k in (".path", ".file", ".url")):
            hits.append(f"{path}: 疑似路径穿越")
    if hits:
        audit.check("secret-scan", "文件与安全", "fail", "; ".join(hits[:20]))
        audit.issue("blocking", [], f"数据里发现 {len(hits)} 处疑似凭据/危险链接：" + "; ".join(hits[:5]), "移除；令牌只能放在安全配置或环境变量，⛔ 不进 trip.json")
    else:
        audit.check("secret-scan", "文件与安全", "pass", "未发现凭据、证件号或危险协议链接")

    # ---- 11. 脚本不覆盖、需模型逐项做的检查（显式列出，避免"检查完成 = 条件成立"）----
    for cid, dim, what in [
        ("model-evidence-supports-claim", "开放与入场", "每条 verified 事实的来源是否真的支持结论、是否适用目标日期"),
        ("model-route-sanity", "路线与衔接", "路线有无明显无意义折返、首末日接驳是否合理"),
        ("model-description-useful", "输出一致性", "每日活动是否解释了为什么安排、看什么、怎么游、为什么顺路"),
        ("model-social-overreach", "风险与备选", "社媒结论是否过度推断（少量评论被写成统计结论）"),
        ("model-alternative-executable", "风险与备选", "替代方案是否也检查了开放、位置、时间、费用"),
    ]:
        audit.check(cid, dim, "skip", f"需模型按 references/audit.md §2 核查：{what}", method="model")


# ----------------------------------------------------------------------------
# 产物检查
# ----------------------------------------------------------------------------
def check_outputs(trip: dict, trip_path: str, manifest_path: str | None, audit: Audit) -> None:
    from render_outputs import Ctx, coverage, html_to_text, load_attribution
    base = os.path.dirname(os.path.abspath(trip_path))
    try:
        path = inside(base, manifest_path or os.path.join(base, f"manifest.v{trip['plan_version']}.json"))
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
        with open(os.path.join(SKILL_ROOT, "assets", "schemas", "manifest.schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        ms = MiniSchema(schema)
        if not ms.check(man, schema, "$"):
            raise ValueError("manifest 结构错误")
        if man["trip_id"] != trip["trip_id"] or man["plan_version"] != trip["plan_version"]:
            raise ValueError("manifest ID/版本与当前数据不一致")
        if man["content_hash"] != canonical_hash(trip):
            raise ValueError("当前 trip.json 内容指纹与 manifest 不一致，产物已过期")
        # Validate all paths before reading any listed file.
        paths = [inside(base, os.path.join(base, f["path"])) for f in man["files"]]
        kinds = [f["kind"] for f in man["files"]]
        if kinds.count("md") != 1 or kinds.count("html") != 1:
            raise ValueError("manifest 必须恰好包含一个 MD 和一个 HTML")
        ctx = Ctx(trip)
        for desc, full in zip(man["files"], paths):
            if sha256_file(full) != desc["sha256"] or os.path.getsize(full) != desc["bytes"]:
                raise ValueError("产物指纹或体积不一致")
            if desc["kind"] not in ("md", "html"):
                continue
            with open(full, encoding="utf-8") as f:
                content = f.read()
            kind = desc["kind"]
            visible = html_to_text(content) if kind == "html" else content
            cov = coverage(visible, ctx.required_strings())
            if cov["missing"]:
                raise ValueError(f"{kind} 可见业务内容覆盖缺失（{len(cov['missing'])} 项）")
            if man["coverage"].get(kind) != cov:
                raise ValueError(f"{kind} 覆盖报告与实际文件不一致")
            if load_attribution() not in visible:
                raise ValueError("产物缺少署名")
            scan_text = content.replace(man["content_hash"], "")
            if secret_hits(scan_text):
                raise ValueError("产物含疑似凭据")
            if kind == "html":
                if re.search(r"<script[^>]+src\s*=|<link[^>]+href\s*=\s*[\"']https?://|\bfetch\s*\(|XMLHttpRequest|navigator\.sendBeacon", content, re.I):
                    raise ValueError("HTML 含远程依赖或运行时网络请求")
                if re.search(r"(?:href|src)\s*=\s*[\"']\s*(?:javascript|file|vbscript):", content, re.I):
                    raise ValueError("HTML 含危险协议")
        audit.check("outputs-check", "输出一致性", "pass", "当前数据指纹、路径、文件、可见内容覆盖与署名已核对")
    except (OSError, ValueError, KeyError, TypeError) as e:
        # Error text is generated locally, never includes file contents.
        detail = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else "产物文件/manifest 缺失、不可读或结构无效"
        audit.issue("blocking", [], detail, "从当前数据重新核查与渲染")
        audit.check("outputs-check", "输出一致性", "fail", detail)


# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    configure_console()
    ap = argparse.ArgumentParser(description="alun-travel-plan 行程校验")
    ap.add_argument("trip", help="trip.json 路径")
    ap.add_argument("--out", help="AuditReport 输出路径（默认 <trip目录>/audit.v<N>.json）")
    ap.add_argument("--check-outputs", action="store_true", help="同时检查 MD/HTML 产物与 manifest")
    ap.add_argument("--manifest", help="manifest 路径（配合 --check-outputs）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        with open(args.trip, "r", encoding="utf-8") as f:
            trip = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"✖ 无法读取 trip.json：{e}", file=sys.stderr)
        return 2

    if not isinstance(trip, dict) or secret_hits(trip):
        print("✖ 输入不是行程对象或包含疑似凭据，拒绝生成报告。", file=sys.stderr)
        return 2

    audit = Audit(trip if isinstance(trip, dict) else {})

    # 结构
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        ms = MiniSchema(schema)
        ok = ms.check(trip, schema, "$")
        if ok:
            audit.check("schema", "契约类型", "pass", "trip.json 符合 assets/schemas/trip.schema.json")
        else:
            audit.check("schema", "契约类型", "fail", "; ".join(x.split(":", 1)[0] + ": 字段无效" for x in ms.errors[:30]))
            audit.issue("blocking", [], f"结构错误 {len(ms.errors)} 处：" + "; ".join(x.split(":", 1)[0] + ": 字段无效" for x in ms.errors[:8]), "按 assets/schemas/README.md 修正后重跑")
    except FileNotFoundError:
        audit.issue("blocking", [], "缺少数据契约文件", "修复安装")
        ok = False

    if ok:
        try:
            semantic_checks(trip, audit)
        except (KeyError, TypeError, AttributeError) as e:
            audit.check("semantic", "契约类型", "fail", f"语义检查中断：{type(e).__name__}: {e}")
            audit.issue("blocking", [], f"数据缺字段或类型不对，语义检查无法完成：{e}", "先修结构")
    else:
        for cid in ("ref-integrity", "dates-weekdays", "budget-arith", "secret-scan"):
            audit.check(cid, "—", "skip", "结构错误，未执行")

    if args.check_outputs and ok:
        check_outputs(trip, args.trip, args.manifest, audit)

    report = audit.to_json()
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.trip)), f"audit.v{trip.get('plan_version', 0)}.json")
    try:
        from trip_support import artifact_path
        out = artifact_path(os.path.dirname(os.path.abspath(args.trip)), os.path.abspath(out), args.trip, 'audit')
        atomic_json(out, report)
    except (OSError, ValueError):
        print("✖ 无法安全写入核查报告", file=sys.stderr)
        return 2

    s = report["summary"]
    if not args.quiet:
        print(f"alun-travel-plan validate · {trip.get('trip_id')} v{trip.get('plan_version')} · script {SCRIPT_VERSION}")
        print(f"  阻断 {s['blocking']} · 条件 {s['conditional']} · 提醒 {s['info']} · 建议状态 {report['recommended_status']}（当前 {trip.get('status')}）")
        for i in report["issues"]:
            mark = {"blocking": "✖", "conditional": "◐", "info": "·"}[i["severity"]]
            print(f"  {mark} [{i['issue_id']}] {i['evidence']}\n      → {i['fix_action']}")
        skipped = [c for c in report["checks"] if c["method"] == "model"]
        print(f"  另有 {len(skipped)} 项需模型逐项核查（见报告 checks，method=model）。报告：{out}")
    if not ok:
        return 2
    if s["blocking"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
