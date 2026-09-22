#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_outputs.py · 从同一份 trip.json 渲染完整 MD + 单文件 HTML，并写 ArtifactManifest（只用标准库）

用法：
  python3 scripts/render_outputs.py travel-plan/<trip_id>/trip.json
  python3 scripts/render_outputs.py travel-plan/<trip_id>/trip.json --out-dir travel-plan/<trip_id>/outputs

规则（对应 references/delivery.md）：
  - 不重新规划：只把 trip.json 里已有的内容排出来；缺什么就显示"未知/待确认"，不补数字。
  - MD 与 HTML 覆盖相同业务内容；两者都带 trip_id、plan_version、generated_at、内容指纹与固定署名。
  - HTML 自包含：内联 CSS/JS，无 CDN，无运行时网络请求；所有外部文本经 html.escape 写入。
  - 署名文字来自 assets/templates/attribution.json（只读配置）。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from trip_support import (preflight, inside, atomic_text, atomic_json, checkpoint,
                          configure_console, budget_limit, budget_verdict)
from datetime import datetime, date

RENDERER_VERSION = "2.2.1"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
TEMPLATE_PATH = os.path.join(SKILL_ROOT, "assets", "templates", "itinerary.html")
ATTRIBUTION_PATH = os.path.join(SKILL_ROOT, "assets", "templates", "attribution.json")

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
STATUS_ZH = {"draft": "探索草案", "conditional": "条件式方案", "executable_as_of_check": "截至核查时可执行"}
BOOKING_ZH = {"not_required": "无需预约", "not_open": "尚未放票", "pending_user": "待你预约", "user_claimed": "你说已预约（待凭证）", "confirmed": "已确认预约", "failed": "预约失败", "unknown": "预约情况未知"}
VERIFY_ZH = {"verified": "已核实", "conditional": "有条件", "estimate": "估算", "unknown": "未知", "blocked": "阻断"}
EVIDENCE_ZH = {"tool_route": "地图工具", "public_route": "公开路线资料", "estimate": "估算", "unknown": "无依据"}
FACT_ZH = {"verified": "已核实", "experience": "经验参考", "estimate": "估算", "unknown": "未知", "conflict": "存在冲突"}
KIND_ZH = {"visit": "游览", "meal": "用餐", "transit": "交通", "rest": "休息", "checkin": "入住", "checkout": "退房", "transport_major": "大交通", "free": "自由活动", "buffer": "机动"}
INTENSITY_ZH = {"light": "轻松", "moderate": "适中", "heavy": "偏累"}
TM_STATUS_ZH = {"booked": "已订", "to_book": "待订", "suggested": "建议"}
PRIORITY_ZH = {"must": "必须", "should": "建议", "nice": "可选"}
WEATHER_KIND_ZH = {"forecast": "真实预报", "seasonal": "季节参考（不是预报）", "unavailable": "未获取到天气"}
CAP_ZH = {"web_search": "联网搜索", "web_fetch": "网页读取", "file_read": "文件读取", "file_write": "文件写入", "script_exec": "脚本执行", "map_route": "地图/路线", "weather": "天气", "image_fetch": "图片获取", "image_gen": "生图", "secret_store": "安全凭据", "tikhub": "TikHub 小红书"}
AVAIL_ZH = {"available": "可用", "unavailable": "不可用", "unknown": "未知"}


# ----------------------------------------------------------------------------
def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def booking_label(item):
    return ("活动预约：" if item.get("admission_item_id") else "") + BOOKING_ZH[item["booking_status"]]


def admission_label(item, items):
    origin = items.get(item.get("admission_item_id"))
    if not origin:
        return None
    return f"入场沿用「{origin['title']}」：{BOOKING_ZH[origin['booking_status']]}；仅限同日未离场，活动无需额外预约不代表免入场预约"


def fmt_dt(s: str | None, with_date=False) -> str:
    if not s:
        return "—"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    return d.strftime("%m-%d %H:%M") if with_date else d.strftime("%H:%M")


def fmt_date(s: str | None) -> str:
    if not s:
        return "—"
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return s
    return f"{d.month}月{d.day}日（{WEEKDAY_ZH[d.weekday()]}）"


def money(v, cur="") -> str:
    if v is None:
        return "未知"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:g} {cur}".strip() if isinstance(v, (int, float)) else str(v)


def money_range(lo, hi, cur="") -> str:
    if lo is None and hi is None:
        return "未知"
    if lo is None:
        return f"≤ {money(hi, cur)}"
    if hi is None:
        return f"≥ {money(lo, cur)}"
    if lo == hi:
        return money(lo, cur)
    return f"{money(lo)}–{money(hi, cur)}"


def safe_url(u: str | None) -> str | None:
    if not u or not isinstance(u, str):
        return None
    return u if u.lower().startswith(("http://", "https://")) else None


def canonical_hash(trip: dict) -> str:
    payload = json.dumps(trip, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_attribution() -> str:
    try:
        with open(ATTRIBUTION_PATH, "r", encoding="utf-8") as f:
            return json.load(f)["text"]
    except Exception:  # noqa: BLE001
        return "由 Alun旅行规划生成"


# ----------------------------------------------------------------------------
class Ctx:
    def __init__(self, trip: dict):
        self.trip = trip
        self.req = trip["request"]
        self.it = trip["itinerary"]
        self.places = {p["place_id"]: p for p in trip["places"]}
        self.legs = {l["leg_id"]: l for l in trip["legs"]}
        self.facts = {f["fact_id"]: f for f in trip["facts"]}
        self.sources = {s["source_id"]: s for s in trip["sources"]}
        self.alts = {a["alt_id"]: a for a in self.it["alternatives"]}
        self.items = {i["item_id"]: i for d in self.it["days"] for i in d["items"]}
        self.attribution = load_attribution()
        self.hash = canonical_hash(trip)
        self.cur = self.it["budget"]["currency"]

    # 便捷
    def place_name(self, pid):
        p = self.places.get(pid)
        return p["name"] if p else (pid or "—")

    def party_brief(self):
        p = self.req["party"]
        parts = [f"成人 {p.get('adults', 0)}"]
        if p.get("children_ages"):
            parts.append("儿童 " + "/".join(f"{a}岁" for a in p["children_ages"]))
        if p.get("seniors"):
            parts.append(f"长者 {p['seniors']}")
        return "，".join(parts)

    def budget_brief(self):
        b = self.req["budget"]
        rng = money_range(b.get("amount_min"), b.get("amount_max"), b.get("currency", ""))
        mode = {"total": "总预算", "per_person": "人均", "unknown": "预算"}.get(b.get("mode"), "预算")
        count = b.get('budget_persons')
        suffix = (f"（预算人数 {count} 人）" if count else "（预算人数待确认）") if b.get('mode') == 'per_person' else ''
        return f"{mode} {rng}{suffix}"

    def date_range(self):
        d = self.req["dates"]
        return f"{d.get('start') or '未定'} 至 {d.get('end') or '未定'}"

    def budget_totals(self):
        b = self.it["budget"]
        lo = hi = paid = 0.0
        unknown = []
        for c in b["categories"]:
            paid += c.get("paid") or 0
            if c["status"] == "unknown" or c["min"] is None or c["max"] is None:
                unknown.append(c["name"])
                continue
            lo += c["min"]
            hi += c["max"]
        return lo, hi, paid, unknown

    def required_strings(self) -> list[str]:
        """两个文件都必须包含的业务字符串（覆盖率检查用）。"""
        it = self.it
        req = [it["summary"]["title"], it["summary"]["destination"], STATUS_ZH[self.trip["status"]], self.attribution, f"v{self.trip['plan_version']}", self.trip["trip_id"]]
        for d in it["days"]:
            req.append(d["theme"])
            for i in d["items"]:
                req.append(i["title"])
        for a in it["alternatives"]:
            req.append(a["trigger"])
        for c in it["checklist"]:
            req.append(c["action"])
        for c in it["budget"]["categories"]:
            req.append(c["name"])
        for r in it["lodging"]["regions"]:
            req.append(r["name"])
        for t in it["transport_major"]:
            req.append(f"{t['from']} → {t['to']}")
        req.extend(it["assumptions"] + it["tradeoffs"] + it["unmet"] + it["risks"] + it["weather"]["prep"])
        # Required *visible* business content, not the embedded JSON snapshot.
        req.extend([self.date_range(), self.party_brief(), self.budget_brief()])
        req.extend(self.req["special_needs"] + self.req["interests"]["optional"])
        req.extend(self.req["interests"]["like"] + self.req["interests"]["avoid"])
        req.extend(m["label"] for m in self.req["interests"]["must"])
        req.extend(v for v in self.req.get("pace", {}).values() if v)
        req.extend(self.req["transport"].get(k) for k in ("luggage", "notes"))
        req.append(self.req["party"].get("mobility_notes"))
        for fc in self.req["fixed_commitments"]:
            req.append(fc["description"])
        for d in it["days"]:
            req.extend([fmt_date(d["date"]), d.get("region"), d.get("lodging_base")])
            req.extend(d["notes"])
            for i in d["items"]:
                req.extend([i["description"], fmt_dt(i["planned_start"]), fmt_dt(i["planned_end"]),
                            VERIFY_ZH[i["verification_status"]], i.get("notes")])
                req.extend(i["tips"])
                if i["kind"] in ("visit", "meal", "checkin", "transport_major") or i["booking_status"] != "not_required" or i.get("admission_item_id"):
                    req.append(booking_label(i))
                req.append(admission_label(i, self.items))
                cost = i["cost"]
                if not (cost.get("min") == 0 and cost.get("max") == 0 and cost.get("status") == "known" and not cost.get("unit")):
                    req.extend([money_range(cost.get("min"), cost.get("max"), cost.get("currency", self.cur)), cost.get("unit")])
        for a in it["alternatives"]:
            req.extend([a["description"], a.get("cost_delta"), a.get("time_delta")])
        for ck in it["checklist"]:
            req.extend([ck.get("deadline"), ck.get("channel"), ck.get("if_unresolved"),
                        {"todo":"待办", "done":"已完成", "na":"不适用"}[ck["status"]]])
        for r in it["lodging"]["regions"]:
            req.extend(r.get(k) for k in ("pros", "cons", "budget_ref", "suits_days"))
        req.append(it["lodging"]["strategy"])
        for f in self.trip["facts"]:
            req.append(f["claim"])
        for source in self.trip["sources"]:
            req.append(source["title"])
        for cap in self.trip["capabilities"]:
            req.append(cap.get("test_result"))
        for cat in it["budget"]["categories"]:
            req.extend([cat.get("note"), money_range(cat.get("min"), cat.get("max"))])
        req.extend([it["budget"].get("note"), it.get("sources_note"), it["weather"].get("note")])
        req.extend(e["summary"] for e in it["weather"]["entries"])
        req.extend(ch["summary"] for ch in self.trip["changelog"])
        return list(dict.fromkeys(s for s in req if s))


# ----------------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------------
def render_md(c: Ctx) -> str:
    t, it, req = c.trip, c.it, c.req
    L: list[str] = []
    add = L.append
    status = STATUS_ZH[t["status"]]
    add(f"# {it['summary']['title']}")
    add("")
    if it["summary"].get("tagline"):
        add(f"> {it['summary']['tagline']}")
        add("")
    add(f"**方案状态：{status}** · 版本 v{t['plan_version']} · 生成于 {t['generated_at']} · 核查于 {t.get('checked_at') or '未核查'} · 编号 {t['trip_id']}")
    add("")
    if t["status"] == "conditional":
        add("> ⚠️ 这是条件式方案：下面标注「待你预约 / 待订 / 待确认」的事项完成后才能照着走。出发前清单里列了每一项的截止时间和没做成怎么办。")
        add("")
    elif t["status"] == "draft":
        add("> ⚠️ 这是探索草案：关键信息不全或实时核查未完成，其中的假设见「规划依据」。请把它当作讨论稿，而不是可直接执行的行程。")
        add("")

    add("## 一、开篇总览")
    add("")
    add(f"- 目的地：{it['summary']['destination']}")
    add(f"- 日期：{c.date_range()}（{len(it['days'])} 天）")
    add(f"- 出发地：{req['origin'].get('name') or '未提供'}")
    add(f"- 人数：{c.party_brief()}")
    add(f"- 预算口径：{c.budget_brief()}；包含 {('、'.join(req['budget'].get('includes') or []) or '未说明')}；已付 {money(req['budget'].get('paid_amount'), req['budget'].get('currency', ''))}")
    pace = req.get("pace") or {}
    pace_txt = "；".join(f"{k}：{v}" for k, v in [("早起", pace.get("early_start")), ("每日时长", pace.get("daily_hours")), ("步行", pace.get("walking")), ("休息", pace.get("rest_needs"))] if v)
    add(f"- 节奏：{pace_txt or '未说明'}")
    add(f"- 调研档位：{'深度' if t.get('research_mode') == 'deep' else '标准'}")
    add("")

    add("## 二、规划依据")
    add("")
    add("**你的要求**")
    add("")
    ints = req["interests"]
    add(f"- 必去：{('、'.join(m['label'] for m in ints['must']) or '无')}")
    add(f"- 喜欢：{('、'.join(ints['like']) or '未说明')}；可选：{('、'.join(ints['optional']) or '无')}；不去：{('、'.join(ints['avoid']) or '无')}")
    if req["special_needs"]:
        add(f"- 特殊需求：{'、'.join(req['special_needs'])}")
    add("")
    for label, value in [("行李", req["transport"].get("luggage")), ("交通要求", req["transport"].get("notes")), ("行动便利要求", req["party"].get("mobility_notes"))]:
        if value:
            add(f"- {label}：{value}")
    add("")
    add("**锁定安排（未经你允许不会改动）**")
    add("")
    if req["fixed_commitments"]:
        for fc in req["fixed_commitments"]:
            add(f"- {fc['description']}（{fmt_dt(fc.get('start'), True)} → {fmt_dt(fc.get('end'), True)}，{ {'confirmed': '已确认', 'pending_payment': '待支付', 'intent': '仅意向'}[fc['status']] }）")
    else:
        add("- 无")
    add("")
    add("**采用的假设**")
    add("")
    for a in it["assumptions"] or ["无"]:
        add(f"- {a}")
    add("")
    add("**重要取舍**")
    add("")
    for a in it["tradeoffs"] or ["无"]:
        add(f"- {a}")
    add("")
    if it["unmet"]:
        add("**未能满足的项目**")
        add("")
        for a in it["unmet"]:
            add(f"- {a}")
        add("")

    add("## 三、行程总表")
    add("")
    add("| 天 | 日期 | 主题 | 片区 | 住宿基点 | 强度 | 主要提醒 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for n, d in enumerate(it["days"], 1):
        reminders = "；".join(d["notes"]) if d["notes"] else "—"
        add(f"| D{n} | {fmt_date(d['date'])} | {d['theme']} | {d.get('region') or '—'} | {d.get('lodging_base') or '—'} | {INTENSITY_ZH[d['intensity']]} | {reminders} |")
    add("")

    add("## 四、每日详细行程")
    add("")
    add("时间为当地时间。标「估算」的路段时长来自资料估算而非地图工具，已按上界留时；排程时刻是规划结果，不代表交通或营业时间本身有分钟级精度。")
    add("")
    for n, d in enumerate(it["days"], 1):
        add(f"### D{n} · {fmt_date(d['date'])} · {d['theme']}")
        add("")
        add(f"片区：{d.get('region') or '—'} · 住宿基点：{d.get('lodging_base') or '—'} · 强度：{INTENSITY_ZH[d['intensity']]}")
        if d["notes"]:
            add("")
            for x in d["notes"]:
                add(f"- {x}")
        add("")
        for i in d["items"]:
            badges = []
            if i["locked"]:
                badges.append("🔒 锁定")
            if i["kind"] in ("visit", "meal", "checkin", "transport_major") or i["booking_status"] not in ("not_required",) or i.get("admission_item_id"):
                badges.append(booking_label(i))
            badges.append(VERIFY_ZH[i["verification_status"]])
            add(f"**{fmt_dt(i['planned_start'])}–{fmt_dt(i['planned_end'])} · {i['title']}** · {KIND_ZH[i['kind']]} · {' · '.join(badges)}")
            add("")
            add(i["description"])
            p = c.places.get(i["place_id"]) if i["place_id"] else None
            if p and i["kind"] not in ("transit", "transport_major"):
                loc = p["name"] + (f"（{p['branch']}）" if p.get("branch") else "")
                if p.get("entrance"):
                    loc += f" · 入口/站点：{p['entrance']}"
                if p.get("address"):
                    loc += f" · 地址：{p['address']}"
                add("")
                add(f"- 地点：{loc}")
                if p.get("opening_windows"):
                    ws = "；".join(f"{w['open']}–{w['close']}" + (f"（最后入场 {w['last_entry']}）" if w.get("last_entry") else "") + (f" {w['note']}" if w.get("note") else "") for w in p["opening_windows"])
                    add(f"- 开放：{ws}")
                elif i["kind"] == "visit":
                    add("- 开放：未查到明确开放时间（按未知处理）")
                b = p.get("booking") or {}
                if b.get("required") is True:
                    scope = "场所预约规则：需要（退房不新增预约）" if i["kind"] == "checkout" else "场所入场预约：需要"
                    add(f"- {scope} · 渠道：{b.get('channel') or '未找到可核实入口'}" + (f" · {b['note']}" if b.get("note") else ""))
                elif b.get("required") is None:
                    add("- 场所入场预约：是否需要预约未知，待查")
                if safe_url(p.get("nav_link")):
                    add(f"- 导航：{p['nav_link']}")
            admission_text = admission_label(i, c.items)
            if admission_text:
                add(f"- {admission_text}")
            leg = c.legs.get(i["incoming_leg_id"]) if i.get("incoming_leg_id") else None
            if leg and i["kind"] == "transit":
                tm = "时长无依据" if leg["evidence_level"] == "unknown" else f"预计 {leg['time_min']}–{leg['time_max']} 分钟，按 {leg['time_max']} 分钟留时"
                extra = []
                if leg.get("transfers"):
                    extra.append(f"换乘 {leg['transfers']} 次")
                if leg.get("walking_min"):
                    extra.append(f"步行约 {leg['walking_min']} 分钟")
                add("")
                add(f"- 路段：{c.place_name(leg['from_id'])} → {c.place_name(leg['to_id'])} · {leg['mode']} · {tm}（{EVIDENCE_ZH[leg['evidence_level']]}）" + (f" · {'，'.join(extra)}" if extra else "") + (f" · {leg['note']}" if leg.get("note") else ""))
            cost = i.get("cost") or {}
            if cost and not (cost.get("min") == 0 and cost.get("max") == 0 and cost.get("status") == "known" and not cost.get("unit")):
                add(f"- 费用：{money_range(cost.get('min'), cost.get('max'), cost.get('currency', c.cur))}" + (f"（{cost['unit']}）" if cost.get("unit") else "") + ("" if cost.get("status") == "known" else f" · {({'estimate': '估算', 'unknown': '未知'}).get(cost.get('status'), '')}"))
            if i.get("tips"):
                for tip in i["tips"]:
                    add(f"- 提示：{tip}")
            if i.get("notes"):
                add(f"- 备注：{i['notes']}")
            fs = [c.facts[f] for f in i["fact_ids"] if f in c.facts]
            if fs:
                add("- 依据：" + "；".join(f"{f['claim']}（{FACT_ZH[f['status']]}）" for f in fs))
            add("")
        day_alts = [c.alts[a] for a in d["alternative_ids"] if a in c.alts]
        if day_alts:
            add(f"**D{n} 备用方案**")
            add("")
            for a in day_alts:
                add(f"- 触发：{a['trigger']} → 替换：{'、'.join(c.items[x]['title'] for x in a['replaces_item_ids'] if x in c.items)} → 接回：{c.items[a['rejoin_at_item_id']]['title'] if a.get('rejoin_at_item_id') in c.items else '—'}")
                add(f"  {a['description']}（费用 {a.get('cost_delta') or '无变化'}；时间 {a.get('time_delta') or '无变化'}；{'可随时切换' if a['instant'] else '需先满足前置条件'}{'，需预约' if a['requires_booking'] else ''}；{VERIFY_ZH[a['verification_status']]}）")
            add("")

    add("## 五、住宿与大交通")
    add("")
    lo_ = it["lodging"]
    add(f"**住宿策略**：{lo_['strategy']}" + ("（含换住宿）" if lo_["change_lodging"] else ""))
    add("")
    if lo_.get("booked"):
        bk = lo_["booked"]
        add(f"- 已订：{bk['name']}" + (f"（{bk['region']}）" if bk.get("region") else "") + (f" · {bk['address']}" if bk.get("address") else ""))
    for r in lo_["regions"]:
        add(f"- **{r['name']}**：优点 {r.get('pros') or '—'}；缺点 {r.get('cons') or '—'}；参考 {r.get('budget_ref') or '未知'}；适合 {r.get('suits_days') or '—'}")
    add("")
    add("**大交通**")
    add("")
    add("| 段 | 方式 | 出发 | 到达 | 状态 | 说明 | 价格参考 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for tm in it["transport_major"]:
        add(f"| {tm['from']} → {tm['to']} | {tm['kind']} | {fmt_dt(tm.get('depart'), True)} | {fmt_dt(tm.get('arrive'), True)} | {TM_STATUS_ZH[tm['status']]} | {tm.get('note') or '—'} | {tm.get('price_ref') or '未知'} |")
    add("")
    add("待订的大交通只给出路线建议或价格参考，不代表有余票或可按该价购买。")
    add("")

    add("## 六、预算")
    add("")
    b = it["budget"]
    lo, hi, paid, unknown = c.budget_totals()
    add(f"| 类别 | 已付 | 预计未付（低—高） | 状态 | 说明 |")
    add("| --- | --- | --- | --- | --- |")
    for cat in b["categories"]:
        add(f"| {cat['name']} | {money(cat.get('paid') or 0)} | {money_range(cat.get('min'), cat.get('max'))} | {({'known': '已知', 'estimate': '估算', 'unknown': '未知'})[cat['status']]} | {cat.get('note') or '—'} |")
    add("")
    add(f"- 已付合计：{money(paid, c.cur)}")
    add(f"- 接下来还要准备：{money_range(lo, hi, c.cur)}" + (f"（另有 {len(unknown)} 项未知：{'、'.join(unknown)}，未知不是零）" if unknown else ""))
    add(f"- 总旅行成本（含已付）：{money_range(paid + lo, paid + hi, c.cur)}")
    limit = budget_limit(t)
    verdict = budget_verdict(lo, hi, paid, unknown, limit)
    add(f"- 预算上限 {money(limit, c.cur)}：{verdict}")
    if b.get("note"):
        add(f"- {b['note']}")
    add("")

    add("## 七、备用方案")
    add("")
    if it["alternatives"]:
        add("| 触发条件 | 替换哪段 | 从哪接回 | 做什么 | 费用/时间变化 | 可否随时切换 | 核查 |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for a in it["alternatives"]:
            rep = "、".join(c.items[x]["title"] for x in a["replaces_item_ids"] if x in c.items) or "—"
            rj = c.items[a["rejoin_at_item_id"]]["title"] if a.get("rejoin_at_item_id") in c.items else "—"
            add(f"| {a['trigger']} | {rep} | {rj} | {a['description']} | {a.get('cost_delta') or '无'} / {a.get('time_delta') or '无'} | {'是' if a['instant'] else '否'}{'（需预约）' if a['requires_booking'] else ''} | {VERIFY_ZH[a['verification_status']]} |")
    else:
        add("本行程未设替代方案。")
    add("")

    add("## 八、出发前清单")
    add("")
    add("| 优先级 | 截止 | 要做的事 | 渠道 | 状态 | 没做成怎么办 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for ck in sorted(it["checklist"], key=lambda x: ["must", "should", "nice"].index(x["priority"])):
        add(f"| {PRIORITY_ZH[ck['priority']]} | {ck.get('deadline') or '—'} | {ck['action']} | {ck.get('channel') or '—'} | {({'todo': '待办', 'done': '已完成', 'na': '不适用'})[ck['status']]} | {ck.get('if_unresolved') or '—'} |")
    add("")

    add("## 九、天气与准备")
    add("")
    w = it["weather"]
    add(f"**{WEATHER_KIND_ZH[w['kind']]}**" + (f"：{w['note']}" if w.get("note") else ""))
    add("")
    for e in w["entries"]:
        src = c.sources.get(e.get("source_id") or "")
        add(f"- {fmt_date(e['date'])}：{e['summary']}" + (f"（来源：{src['title']}）" if src else ""))
    for p_ in w["prep"]:
        add(f"- 准备：{p_}")
    add("")

    add("## 十、核查与资料")
    add("")
    add("**风险**")
    add("")
    for r in it["risks"] or ["未记录"]:
        add(f"- {r}")
    add("")
    add("**本次可用能力**")
    add("")
    for cp in t["capabilities"]:
        add(f"- {CAP_ZH.get(cp['capability'], cp['capability'])}：{AVAIL_ZH[cp['availability']]}" + (f"（{cp['test_result']}）" if cp.get("test_result") else "") + (f"，降级：{cp['fallback']}" if cp["availability"] != "available" and cp.get("fallback") and cp["fallback"] != "—" else ""))
    add("")
    add("**事实记录**")
    add("")
    add("| 结论 | 状态 | 适用期 | 来源 | 抓取时间 |")
    add("| --- | --- | --- | --- | --- |")
    for f in t["facts"]:
        vf = f.get("valid_for") or {}
        vft = f"{vf.get('from') or '?'}~{vf.get('to') or '?'}" if vf else "未注明"
        srcs = "；".join(c.sources[s]["title"] for s in f["source_ids"] if s in c.sources) or "—"
        add(f"| {f['claim']} | {FACT_ZH[f['status']]} | {vft} | {srcs} | {f.get('retrieved_at') or '—'} |")
    add("")
    add("**来源**")
    add("")
    for s in t["sources"]:
        u = safe_url(s.get("url"))
        add(f"- {s['title']}" + (f"（{s.get('publisher')}）" if s.get("publisher") else "") + (f" · {u}" if u else " · 用户提供/无链接") + (f" · 发布 {s['published_at']}" if s.get("published_at") else "") + f" · 类型 {s['source_type']}")
    add("")
    if it.get("sources_note"):
        add(it["sources_note"])
        add("")
    if t["decisions"]:
        add("**关键决策**")
        add("")
        for dcs in t["decisions"]:
            add(f"- {dcs['topic']}：选择「{dcs['user_choice']}」（选项：{' / '.join(dcs['options'])}；{dcs['time']}）")
        add("")
    add("**变更记录**")
    add("")
    for ch in t["changelog"]:
        add(f"- v{ch['version']} · {ch['at']} · {ch['summary']}")
    add("")
    add("---")
    add("")
    add(f"{c.attribution} · {t['trip_id']} · v{t['plan_version']} · 生成于 {t['generated_at']} · 内容指纹 {c.hash[:12]}")
    add("")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
def li(items) -> str:
    return "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>" if items else ""


KIND_ICON = {"visit": "📍", "meal": "🍜", "transit": "🚇", "rest": "☕", "checkin": "🏨", "checkout": "🧳", "transport_major": "🚄", "free": "🌿", "buffer": "⏳"}
MODE_ICON = [("步行", "🚶"), ("地铁", "🚇"), ("轻轨", "🚈"), ("公交", "🚌"), ("大巴", "🚌"), ("出租", "🚕"), ("网约", "🚕"), ("打车", "🚕"), ("自驾", "🚗"), ("骑行", "🚲"), ("船", "⛴"), ("渡轮", "⛴"), ("缆车", "🚡"), ("高铁", "🚄"), ("动车", "🚄"), ("火车", "🚆"), ("飞机", "✈️"), ("航班", "✈️")]
BUDGET_COLORS = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)"]


def icon_for(item: dict, leg: dict | None) -> str:
    if item["kind"] not in ("transit", "transport_major"):
        return KIND_ICON.get(item["kind"], "•")
    text = ((leg or {}).get("mode") or "") + item.get("title", "")
    for k, ic in MODE_ICON:
        if k in text:
            return ic
    return KIND_ICON.get(item["kind"], "•")


def duration_label(a: str, b: str) -> str:
    try:
        s, e = datetime.fromisoformat(a.replace("Z", "+00:00")), datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return ""
    m = int((e - s).total_seconds() // 60)
    if m <= 0:
        return ""
    if m < 120:
        return f"{m} 分钟"
    half = round(m / 30) / 2
    return f"{half:g} 小时" if abs(half * 60 - m) < 8 else f"约 {half:g} 小时"


def route_svg(c: "Ctx") -> str:
    """首屏路线示意：出发 → D1 … Dn → 返程，纯装饰，数据来自 days。"""
    days = c.it["days"]
    n = len(days)
    W, H = 720, 96
    xs = [40 + i * (W - 80) / (n + 1) for i in range(n + 2)]
    ys = [58] + [(34 if i % 2 == 0 else 66) for i in range(n)] + [58]
    pts = list(zip(xs, ys))
    d = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        cx = (x0 + x1) / 2
        d += f" C{cx:.1f} {y0:.1f}, {cx:.1f} {y1:.1f}, {x1:.1f} {y1:.1f}"
    parts = [f"<svg viewBox=\"0 0 {W} {H}\" xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" aria-label=\"从{esc(c.req['origin'].get('name') or '出发地')}出发，{n} 天后返程\">"]
    parts.append(f"<path class=\"rp-ghost\" d=\"{d}\"/>")
    parts.append(f"<path class=\"rp\" pathLength=\"1\" d=\"{d}\"/>")
    delay = 0.35
    origin = c.req["origin"].get("name") or "出发"
    parts.append(f"<g class=\"node\" style=\"animation-delay:{delay:.2f}s\"><circle cx=\"{pts[0][0]:.1f}\" cy=\"{pts[0][1]:.1f}\" r=\"12\"/><text x=\"{pts[0][0]:.1f}\" y=\"{pts[0][1]:.1f}\">🏠</text><text class=\"lab\" x=\"{pts[0][0]:.1f}\" y=\"{pts[0][1] + 17:.1f}\">{esc(origin[:6])}</text></g>")
    for i, dday in enumerate(days, 1):
        x, y = pts[i]
        delay += 1.4 / max(n, 1)
        try:
            dd = date.fromisoformat(dday["date"])
            lab = f"{dd.month}/{dd.day}"
        except ValueError:
            lab = dday["date"]
        parts.append(f"<g class=\"node\" style=\"animation-delay:{delay:.2f}s\"><circle cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"12\"/><text x=\"{x:.1f}\" y=\"{y:.1f}\">D{i}</text><text class=\"lab\" x=\"{x:.1f}\" y=\"{y + 17:.1f}\">{esc(lab)}</text></g>")
    x, y = pts[-1]
    parts.append(f"<g class=\"node end\" style=\"animation-delay:{delay + 0.3:.2f}s\"><circle cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"12\"/><text x=\"{x:.1f}\" y=\"{y:.1f}\">⌂</text><text class=\"lab\" x=\"{x:.1f}\" y=\"{y + 17:.1f}\">返程</text></g>")
    parts.append("</svg>")
    return "".join(parts)


def render_html(c: Ctx) -> str:
    t, it, req = c.trip, c.it, c.req
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()

    status = t["status"]
    must = [ck for ck in it["checklist"] if ck["priority"] == "must" and ck["status"] == "todo"]
    # ---- 状态提示 -------------------------------------------------------------
    if status == "conditional":
        notice = "<div class=\"notice info\" data-reveal><strong>条件式方案。</strong>标「待你预约 / 待订 / 待确认」的事项完成后才能照着走；每一项的截止时间和没做成怎么办都在 <a href=\"#checklist\">出发前清单</a>。"
        if must:
            notice += "<div class=\"must\">" + "".join(f"<span>⏰ {esc(m.get('deadline') or '尽快')} · {esc(m['action'][:40])}{'…' if len(m['action']) > 40 else ''}</span>" for m in must) + "</div>"
        notice += "</div>"
    elif status == "draft":
        notice = "<div class=\"notice\" data-reveal><strong>探索草案。</strong>关键信息不全或实时核查未完成，请把它当作讨论稿；采用的假设见「旅行总览」。</div>"
    else:
        notice = f"<div class=\"notice ok\" data-reveal><strong>截至 {esc(t.get('checked_at') or t['generated_at'])} 核查可执行。</strong>出发前仍建议按清单再核对一次易变的事项。</div>"

    # ---- 总览 -------------------------------------------------------------------
    ov = []
    pace = req.get("pace") or {}
    pace_txt = "；".join(f"{k}：{v}" for k, v in [("早起", pace.get("early_start")), ("每日", pace.get("daily_hours")), ("步行", pace.get("walking")), ("休息", pace.get("rest_needs"))] if v)
    ints = req["interests"]
    ov.append("<div class=\"tiles\">")
    for lab, val in [
        ("出发地", req["origin"].get("name") or "未提供"),
        ("日期", f"{c.date_range()}（{len(it['days'])} 天）"),
        ("同行", c.party_brief()),
        ("预算口径", f"{c.budget_brief()}；含 {('、'.join(req['budget'].get('includes') or []) or '未说明')}；已付 {money(req['budget'].get('paid_amount'), req['budget'].get('currency', ''))}"),
        ("节奏", pace_txt or "未说明"),
        ("必去", "、".join(m["label"] for m in ints["must"]) or "无"),
        ("可选兴趣", "、".join(ints["optional"]) or "无"),
        ("特殊需求", "、".join(req["special_needs"]) or "无"),
        ("行李", req["transport"].get("luggage") or "未说明"),
        ("交通要求", req["transport"].get("notes") or "未说明"),
        ("行动便利要求", req["party"].get("mobility_notes") or "未说明"),
        ("喜欢 / 不去", f"{'、'.join(ints['like']) or '未说明'} / {'、'.join(ints['avoid']) or '无'}"),
        ("方案状态", f"<span class=\"badge b-{'verified' if status == 'executable_as_of_check' else ('conditional' if status == 'conditional' else 'unknown')}\">{esc(STATUS_ZH[status])}</span> · 核查于 {esc(t.get('checked_at') or '未核查')}"),
    ]:
        v = val if lab == "方案状态" else esc(val)
        ov.append(f"<div class=\"tile\"><span class=\"lab\">{esc(lab)}</span><span class=\"val\">{v}</span></div>")
    ov.append("</div>")
    ov.append("<h3>行程一览</h3><div class=\"strip\">")
    for n, d in enumerate(it["days"], 1):
        ov.append(f"<a class=\"dcard int-{esc(d['intensity'])}\" href=\"#{esc(d['day_id'])}\"><span class=\"dots\" aria-hidden=\"true\"><i></i><i></i><i></i></span><div class=\"dn\">D{n}</div><div class=\"dd\">{esc(fmt_date(d['date']))} · {esc(INTENSITY_ZH[d['intensity']])}</div><div class=\"dt\">{esc(d['theme'])}</div><div class=\"dr\">{esc(d.get('region') or '—')}</div><div class=\"dr small\">住 {esc(d.get('lodging_base') or '—')}</div></a>")
    ov.append("</div>")
    ov.append("<h3>锁定安排</h3>")
    if req["fixed_commitments"]:
        ov.append("<ul class=\"lock-list\">" + "".join(f"<li><span class=\"ico\">🔒</span><span>{esc(fc['description'])}<span class=\"small\"> · {esc(fmt_dt(fc.get('start'), True))} → {esc(fmt_dt(fc.get('end'), True))} · { {'confirmed': '已确认', 'pending_payment': '待支付', 'intent': '仅意向'}[fc['status']] }</span></span></li>" for fc in req["fixed_commitments"]) + "</ul>")
    else:
        ov.append("<p class=\"small\">无</p>")
    ov.append("<h3>规划依据</h3><div class=\"callouts\">")
    ov.append("<div class=\"callout assume\"><h4>采用的假设</h4>" + (li(it["assumptions"]) or "<p class=\"small\">无</p>") + "</div>")
    ov.append("<div class=\"callout trade\"><h4>重要取舍</h4>" + (li(it["tradeoffs"]) or "<p class=\"small\">无</p>") + "</div>")
    if it["unmet"]:
        ov.append("<div class=\"callout unmet\"><h4>未能满足</h4>" + li(it["unmet"]) + "</div>")
    ov.append("</div>")
    ov.append("<div class=\"table-wrap\" style=\"margin-top:14px\"><table><thead><tr><th>天</th><th>日期</th><th>主题</th><th>片区</th><th>住宿基点</th><th>强度</th><th>主要提醒</th></tr></thead><tbody>")
    for n, d in enumerate(it["days"], 1):
        ov.append(f"<tr><td>D{n}</td><td>{esc(fmt_date(d['date']))}</td><td>{esc(d['theme'])}</td><td>{esc(d.get('region') or '—')}</td><td>{esc(d.get('lodging_base') or '—')}</td><td>{esc(INTENSITY_ZH[d['intensity']])}</td><td>{esc('；'.join(d['notes']) or '—')}</td></tr>")
    ov.append("</tbody></table></div>")
    ov.append("<p class=\"small\" style=\"margin-top:10px\">时间为当地时间。标「估算」的路段时长来自资料估算而非地图工具，已按上界留时；排程时刻是规划结果，不代表交通或营业时间本身有分钟级精度。</p>")

    # ---- 每日 -------------------------------------------------------------------
    dh = []
    for n, d in enumerate(it["days"], 1):
        label = f"D{n}"
        sub = fmt_date(d["date"])
        dh.append(f"<article class=\"day\" id=\"{esc(d['day_id'])}\" data-label=\"{esc(label + ' ' + d['theme'][:8])}\" data-sub=\"{esc(sub)}\">")
        dh.append(f"<div class=\"day-head\"><div class=\"dn\">D{n}</div><h3>{esc(d['theme'])}</h3><div class=\"sub\"><span>📅 {esc(sub)}</span><span>🗺 {esc(d.get('region') or '—')}</span><span>🏨 {esc(d.get('lodging_base') or '—')}</span><span>⚡ {esc(INTENSITY_ZH[d['intensity']])}</span></div></div>")
        if d["notes"]:
            dh.append("<div class=\"day-notes\">" + "".join(f"<span>{esc(x)}</span>" for x in d["notes"]) + "</div>")
        dh.append("<ol class=\"tl\">")
        for i in d["items"]:
            leg = c.legs.get(i["incoming_leg_id"]) if i.get("incoming_leg_id") else None
            cls = f"ti kind-{esc(i['kind'])} vs-{esc(i['verification_status'])}" + (" locked" if i["locked"] else "")
            dh.append(f"<li class=\"{cls}\" data-reveal>")
            dur = duration_label(i["planned_start"], i["planned_end"])
            dh.append(f"<div class=\"ti-time\"><span class=\"t1\">{esc(fmt_dt(i['planned_start']))}</span><span>{esc(fmt_dt(i['planned_end']))}</span>" + (f"<span class=\"dur\">{esc(dur)}</span>" if dur else "") + "</div>")
            dh.append(f"<div class=\"ti-node\"><span class=\"ico\" aria-hidden=\"true\">{icon_for(i, leg)}</span></div>")
            dh.append("<div class=\"ti-card\">")
            badges = ""
            if i["locked"]:
                badges += "<span class=\"badge b-locked\">🔒 锁定</span>"
            if i["kind"] in ("visit", "meal", "checkin", "transport_major") or i["booking_status"] != "not_required" or i.get("admission_item_id"):
                badges += f"<span class=\"badge b-booking {esc(i['booking_status'])}\">{esc(booking_label(i))}</span>"
            badges += f"<span class=\"badge b-{esc(i['verification_status'])}\">{esc(VERIFY_ZH[i['verification_status']])}</span>"
            badges += f"<span class=\"badge b-kind\">{esc(KIND_ZH[i['kind']])}</span>"
            dh.append(f"<div class=\"ti-head\"><h4>{esc(i['title'])}</h4><div class=\"badges\">{badges}</div></div>")
            dh.append(f"<p class=\"ti-desc\">{esc(i['description'])}</p>")
            if leg and i["kind"] == "transit":
                tm = "时长无依据" if leg["evidence_level"] == "unknown" else f"预计 {leg['time_min']}–{leg['time_max']} 分钟，按 <b>{leg['time_max']} 分钟</b>留时"
                extra = []
                if leg.get("transfers"):
                    extra.append(f"换乘 {leg['transfers']} 次")
                if leg.get("walking_min"):
                    extra.append(f"步行约 {leg['walking_min']} 分钟")
                ev_cls = "estimate" if leg["evidence_level"] == "estimate" else ("unknown" if leg["evidence_level"] == "unknown" else "verified")
                dh.append(f"<div class=\"leg-pill\"><span>{esc(c.place_name(leg['from_id']))} → {esc(c.place_name(leg['to_id']))}</span><span>{esc(leg['mode'])}</span><span>{tm}</span><span class=\"badge b-{ev_cls}\">{esc(EVIDENCE_ZH[leg['evidence_level']])}</span>" + (f"<span>{esc('，'.join(extra))}</span>" if extra else "") + (f"<span class=\"small\">{esc(leg['note'])}</span>" if leg.get("note") else "") + "</div>")
            p = c.places.get(i["place_id"]) if i["place_id"] else None
            meta = []
            if p and i["kind"] not in ("transit", "transport_major"):
                loc = p["name"] + (f"（{p['branch']}）" if p.get("branch") else "")
                meta.append(f"<div><span class=\"k\">📍</span><span data-copy=\"{esc(p.get('address') or p['name'])}\">{esc(loc)}" + (f" · 入口/站点：{esc(p['entrance'])}" if p.get("entrance") else "") + (f" · {esc(p['address'])}" if p.get("address") else "") + "</span></div>")
                if p.get("opening_windows"):
                    ws = "；".join(f"{w['open']}–{w['close']}" + (f"（最后入场 {w['last_entry']}）" if w.get("last_entry") else "") + (f" {w['note']}" if w.get("note") else "") for w in p["opening_windows"])
                    meta.append(f"<div><span class=\"k\">🕘</span><span>开放 {esc(ws)}</span></div>")
                elif i["kind"] == "visit":
                    meta.append("<div><span class=\"k\">🕘</span><span>开放时间未查到（按未知处理）</span></div>")
                b = p.get("booking") or {}
                if b.get("required") is True:
                    scope = "场所预约规则：需要（退房不新增预约）" if i["kind"] == "checkout" else "场所入场预约：需要"
                    meta.append(f"<div><span class=\"k\">🎫</span><span>{scope} · {esc(b.get('channel') or '未找到可核实入口')}" + (f" · {esc(b['note'])}" if b.get("note") else "") + "</span></div>")
                elif b.get("required") is None:
                    meta.append("<div><span class=\"k\">🎫</span><span>场所入场预约：是否需要预约未知，待查</span></div>")
                u = safe_url(p.get("nav_link"))
                if u:
                    meta.append(f"<div><span class=\"k\">🧭</span><a href=\"{esc(u)}\" rel=\"noopener noreferrer\" target=\"_blank\">导航链接（已验证）</a></div>")
            admission_text = admission_label(i, c.items)
            if admission_text:
                meta.append(f"<div><span class=\"k\">🎫</span><span>{esc(admission_text)}</span></div>")
            if meta:
                dh.append("<div class=\"ti-meta\">" + "".join(meta) + "</div>")
            cost = i.get("cost") or {}
            if cost and not (cost.get("min") == 0 and cost.get("max") == 0 and cost.get("status") == "known" and not cost.get("unit")):
                dh.append(f"<div class=\"ti-cost\">💰 {esc(money_range(cost.get('min'), cost.get('max'), cost.get('currency', c.cur)))}" + (f"（{esc(cost['unit'])}）" if cost.get("unit") else "") + ("" if cost.get("status") == "known" else f" · {({'estimate': '估算', 'unknown': '未知'}).get(cost.get('status'), '')}") + "</div>")
            if i.get("tips") or i.get("notes"):
                dh.append("<ul class=\"ti-tips\">" + "".join(f"<li>{esc(x)}</li>" for x in i.get("tips", [])) + (f"<li>备注：{esc(i['notes'])}</li>" if i.get("notes") else "") + "</ul>")
            fs = [c.facts[f] for f in i["fact_ids"] if f in c.facts]
            if fs:
                dh.append("<details><summary>依据（" + str(len(fs)) + "）</summary><ul>" + "".join(f"<li>{esc(f['claim'])} <span class=\"badge b-{'verified' if f['status']=='verified' else 'estimate'}\">{esc(FACT_ZH[f['status']])}</span></li>" for f in fs) + "</ul></details>")
            dh.append("</div></li>")
        dh.append("</ol>")
        for a in [c.alts[x] for x in d["alternative_ids"] if x in c.alts]:
            rep = "、".join(c.items[x]["title"] for x in a["replaces_item_ids"] if x in c.items) or "—"
            rj = c.items[a["rejoin_at_item_id"]]["title"] if a.get("rejoin_at_item_id") in c.items else "—"
            dh.append(f"<div class=\"alt\" data-reveal><div class=\"t\">备用方案 · 触发：{esc(a['trigger'])}</div><div class=\"chain\"><span>替换 {esc(rep)}</span><span>→ 接回 {esc(rj)}</span></div><div>{esc(a['description'])}</div><div class=\"meta\">费用 {esc(a.get('cost_delta') or '无变化')} · 时间 {esc(a.get('time_delta') or '无变化')} · {'可随时切换' if a['instant'] else '需先满足前置条件'}{'，需预约' if a['requires_booking'] else ''} · {esc(VERIFY_ZH[a['verification_status']])}</div></div>")
        dh.append("</article>")

    # ---- 住宿 / 大交通 / 预算 -----------------------------------------------------
    st = []
    lo_ = it["lodging"]
    st.append(f"<h3>住宿</h3><p><strong>{esc(lo_['strategy'])}</strong>" + ("（含换住宿）" if lo_["change_lodging"] else "") + "</p>")
    if lo_.get("booked"):
        bk = lo_["booked"]
        st.append(f"<p>已订：{esc(bk['name'])}" + (f"（{esc(bk['region'])}）" if bk.get("region") else "") + (f" · <span data-copy=\"{esc(bk['address'])}\">{esc(bk['address'])}</span>" if bk.get("address") else "") + "</p>")
    st.append("<div class=\"regions\">")
    for idx, r in enumerate(lo_["regions"]):
        st.append(f"<div class=\"region{' rec' if idx == 0 else ''}\"><h4>{'⭐ ' if idx == 0 else ''}{esc(r['name'])}</h4><dl class=\"row\"><dt>优点</dt><dd>{esc(r.get('pros') or '—')}</dd><dt>缺点</dt><dd>{esc(r.get('cons') or '—')}</dd><dt>参考</dt><dd>{esc(r.get('budget_ref') or '未知')}</dd><dt>适合</dt><dd>{esc(r.get('suits_days') or '—')}</dd></dl></div>")
    st.append("</div>")
    st.append("<h3>大交通</h3><div class=\"tickets\">")
    for tm in it["transport_major"]:
        st.append(f"<div class=\"ticket\"><div><div class=\"ft\"><span>{esc(tm['from'])}</span><span class=\"arr\">→</span><span>{esc(tm['to'])}</span></div><div class=\"tm\"><span>出发<b>{esc(fmt_dt(tm.get('depart'), True))}</b></span><span>到达<b>{esc(fmt_dt(tm.get('arrive'), True))}</b></span><span>方式<b>{esc(tm['kind'])}</b></span></div>" + (f"<div class=\"small\" style=\"margin-top:6px\">{esc(tm.get('note') or '')}</div>" if tm.get("note") else "") + f"</div><div class=\"st\"><span class=\"badge b-{'verified' if tm['status']=='booked' else ('conditional' if tm['status']=='to_book' else 'estimate')}\">{esc(TM_STATUS_ZH[tm['status']])}</span><div>{esc(tm.get('price_ref') or '价格未知')}</div></div></div>")
    st.append("</div><p class=\"small\">待订的大交通只给出路线建议或价格参考，不代表有余票或可按该价购买。</p>")
    # 预算条
    b = it["budget"]
    lo, hi, paid, unknown = c.budget_totals()
    hard = budget_limit(t)
    scale = max([x for x in (hard, paid + hi, 1) if x is not None])
    st.append("<h3>预算</h3>")
    st.append("<div class=\"sums\">")
    st.append(f"<div class=\"sum\"><div class=\"lab\">已付</div><div class=\"val\">{esc(money(paid, c.cur))}</div></div>")
    st.append(f"<div class=\"sum hl\"><div class=\"lab\">接下来还要准备</div><div class=\"val\">{esc(money_range(lo, hi, c.cur))}</div>" + (f"<div class=\"sub\">另有 {len(unknown)} 项未知，未知不是零</div>" if unknown else "") + "</div>")
    st.append(f"<div class=\"sum\"><div class=\"lab\">总旅行成本（含已付）</div><div class=\"val\">{esc(money_range(paid + lo, paid + hi, c.cur))}</div></div>")
    if hard is not None:
        verdict = budget_verdict(lo, hi, paid, unknown, hard)
        st.append(f"<div class=\"sum\"><div class=\"lab\">预算上限 {esc(money(hard, c.cur))}</div><div class=\"val\" style=\"font-size:17px\">{esc(verdict)}</div></div>")
    if hard is None:
        st.append("<div class=\"sum\"><div class=\"lab\">预算判断</div><div>预算口径或上限待确认</div></div>")
    st.append("</div>")
    segs, legend = [], []
    known_cats = [cat for cat in b["categories"] if cat["status"] != "unknown" and cat.get("max") is not None]
    if paid > 0:
        pct = paid / scale * 100
        segs.append(f"<div class=\"seg paid\" style=\"flex:0 0 {pct:.2f}%\" title=\"已付 {esc(money(paid, c.cur))}\">{'已付' if pct >= 10 else ''}</div>")
        legend.append(f"<li><i style=\"background:#5f6d7b\"></i>已付 {esc(money(paid, c.cur))}</li>")
    for idx, cat in enumerate(known_cats):
        color = BUDGET_COLORS[idx] if idx < len(BUDGET_COLORS) else "var(--c-other)"
        pct = (cat["max"] or 0) / scale * 100
        if pct <= 0:
            legend.append(f"<li><i style=\"background:{color}\"></i>{esc(cat['name'])} {esc(money_range(cat.get('min'), cat.get('max')))}</li>")
            continue
        segs.append(f"<div class=\"seg\" style=\"flex:0 0 {pct:.2f}%;background:{color}\" title=\"{esc(cat['name'])} 上界 {esc(money(cat['max'], c.cur))}\">{esc(cat['name']) if pct >= 12 else ''}</div>")
        legend.append(f"<li><i style=\"background:{color}\"></i>{esc(cat['name'])} {esc(money_range(cat.get('min'), cat.get('max')))}</li>")
    if unknown:
        segs.append(f"<div class=\"seg unk\" style=\"flex:0 0 6%\" title=\"未知：{esc('、'.join(unknown))}\">?</div>")
        legend.append(f"<li><i style=\"background:#e2dccf\"></i>未知：{esc('、'.join(unknown))}</li>")
    limit_html = f"<div class=\"limit\" style=\"left:{hard / scale * 100:.2f}%\" data-label=\"上限 {esc(money(hard))}\"></div>" if hard is not None else ""
    st.append(f"<div class=\"bbar-wrap\"><div class=\"bbar\" role=\"img\" aria-label=\"预算构成：已付 {esc(money(paid))}，预计未付上界 {esc(money(hi))}，上限 {esc(money(hard))}\">{''.join(segs)}</div>{limit_html}<ul class=\"legend\">{''.join(legend)}</ul></div>")
    st.append("<div class=\"table-wrap\"><table><thead><tr><th>类别</th><th>已付</th><th>预计未付（低—高）</th><th>状态</th><th>说明</th></tr></thead><tbody>")
    for cat in b["categories"]:
        st.append(f"<tr><td>{esc(cat['name'])}</td><td>{esc(money(cat.get('paid') or 0))}</td><td>{esc(money_range(cat.get('min'), cat.get('max')))}</td><td>{esc(({'known': '已知', 'estimate': '估算', 'unknown': '未知'})[cat['status']])}</td><td>{esc(cat.get('note') or '—')}</td></tr>")
    st.append("</tbody></table></div>")
    if b.get("note"):
        st.append(f"<p class=\"small\">{esc(b['note'])}</p>")
    if it["alternatives"]:
        st.append("<h3>全部备用方案</h3><div class=\"table-wrap\"><table><thead><tr><th>触发条件</th><th>替换哪段</th><th>从哪接回</th><th>做什么</th><th>费用/时间</th><th>随时切换</th><th>核查</th></tr></thead><tbody>")
        for a in it["alternatives"]:
            rep = "、".join(c.items[x]["title"] for x in a["replaces_item_ids"] if x in c.items) or "—"
            rj = c.items[a["rejoin_at_item_id"]]["title"] if a.get("rejoin_at_item_id") in c.items else "—"
            st.append(f"<tr><td>{esc(a['trigger'])}</td><td>{esc(rep)}</td><td>{esc(rj)}</td><td>{esc(a['description'])}</td><td>{esc(a.get('cost_delta') or '无')} / {esc(a.get('time_delta') or '无')}</td><td>{'是' if a['instant'] else '否'}{'（需预约）' if a['requires_booking'] else ''}</td><td>{esc(VERIFY_ZH[a['verification_status']])}</td></tr>")
        st.append("</tbody></table></div>")

    # ---- 清单 -------------------------------------------------------------------
    total = len([ck for ck in it["checklist"] if ck["status"] != "na"])
    done = len([ck for ck in it["checklist"] if ck["status"] == "done"])
    prog_txt = f"已完成 {done} / {total}"
    ch = [f"<div class=\"prog\"><i data-prog style=\"width:{(done / total * 100) if total else 0:.0f}%\"></i></div><div class=\"prog-txt\" data-prog-txt>{esc(prog_txt)}</div>"]
    ch.append("<ul class=\"checklist\">")
    for ck in sorted(it["checklist"], key=lambda x: ["must", "should", "nice"].index(x["priority"])):
        cid = esc(ck["check_id"])
        checked = " checked" if ck["status"] == "done" else (" disabled" if ck["status"] == "na" else "")
        state_label = {"todo":"待办", "done":"已完成", "na":"不适用"}[ck["status"]]
        ch.append(f"<li class=\"{'done' if ck['status'] == 'done' else ''}\" data-reveal><input type=\"checkbox\" id=\"{cid}\"{checked}><label for=\"{cid}\"><span class=\"pri {esc(ck['priority'])}\">{esc(PRIORITY_ZH[ck['priority']])}</span><span class=\"act\">{esc(ck['action'])}</span><span class=\"check-state\">（{state_label}）</span><span class=\"sub\">⏰ 截止 {esc(ck.get('deadline') or '—')} · 渠道 {esc(ck.get('channel') or '—')}" + (f"<br>↪ 没做成：{esc(ck['if_unresolved'])}" if ck.get("if_unresolved") else "") + "</span></label></li>")
    ch.append("</ul>")
    ch.append("<p class=\"storage-note\">勾选只保存在你这台设备的浏览器里，关闭后可能不会保留；文件本身不会被修改。</p>")
    w = it["weather"]
    ch.append("<h3>天气与准备</h3><div class=\"weather\">")
    ch.append(f"<div class=\"wx\"><div class=\"lab\">{esc(WEATHER_KIND_ZH[w['kind']])}</div><p style=\"margin:4px 0 0;font-size:14px\">{esc(w.get('note') or '—')}</p>" + ("<ul>" + "".join(f"<li>{esc(fmt_date(e['date']))}：{esc(e['summary'])}" + (f"（来源：{esc(c.sources[e['source_id']]['title'])}）" if e.get("source_id") in c.sources else "") + "</li>" for e in w["entries"]) + "</ul>" if w["entries"] else "") + "</div>")
    if w["prep"]:
        ch.append("<div class=\"wx\"><div class=\"lab\">带什么</div><ul>" + "".join(f"<li>{esc(x)}</li>" for x in w["prep"]) + "</ul></div>")
    ch.append("</div>")

    # ---- 证据 -------------------------------------------------------------------
    ev = []
    ev.append("<h3>风险</h3>" + ("<ul class=\"risks\">" + "".join(f"<li>{esc(x)}</li>" for x in it["risks"]) + "</ul>" if it["risks"] else "<p class=\"small\">未记录</p>"))
    ev.append("<h3>本次可用能力</h3><ul class=\"caps\">" + "".join(f"<li class=\"{'on' if cp['availability']=='available' else 'off'}\">{esc(CAP_ZH.get(cp['capability'], cp['capability']))} · {esc(AVAIL_ZH[cp['availability']])}" + (f"（{esc(cp['test_result'])}）" if cp.get("test_result") else "") + (f"（{esc(cp['fallback'])}）" if cp["availability"] != "available" and cp.get("fallback") and cp["fallback"] != "—" else "") + "</li>" for cp in t["capabilities"]) + "</ul>")
    ev.append("<details class=\"acc\"><summary>事实记录（" + str(len(t["facts"])) + "）</summary><div class=\"table-wrap\"><table><thead><tr><th>结论</th><th>状态</th><th>适用期</th><th>来源</th><th>抓取</th></tr></thead><tbody>")
    for f in t["facts"]:
        vf = f.get("valid_for") or {}
        vft = f"{vf.get('from') or '?'}~{vf.get('to') or '?'}" if vf else "未注明"
        srcs = "；".join(c.sources[s]["title"] for s in f["source_ids"] if s in c.sources) or "—"
        ev.append(f"<tr><td>{esc(f['claim'])}</td><td><span class=\"badge b-{'verified' if f['status']=='verified' else 'estimate'}\">{esc(FACT_ZH[f['status']])}</span></td><td>{esc(vft)}</td><td>{esc(srcs)}</td><td>{esc(f.get('retrieved_at') or '—')}</td></tr>")
    ev.append("</tbody></table></div></details>")
    ev.append("<details class=\"acc\"><summary>来源（" + str(len(t["sources"])) + "）</summary><ul class=\"small\">")
    for s in t["sources"]:
        u = safe_url(s.get("url"))
        link = f"<a href=\"{esc(u)}\" rel=\"noopener noreferrer\" target=\"_blank\">{esc(s['title'])}</a>" if u else esc(s["title"]) + "（用户提供/无链接）"
        ev.append(f"<li>{link}" + (f" · {esc(s['publisher'])}" if s.get("publisher") else "") + (f" · 发布 {esc(s['published_at'])}" if s.get("published_at") else "") + f" · {esc(s['source_type'])}</li>")
    ev.append("</ul></details>")
    if it.get("sources_note"):
        ev.append(f"<p class=\"small\">{esc(it['sources_note'])}</p>")
    if t["decisions"]:
        ev.append("<details class=\"acc\"><summary>关键决策（" + str(len(t["decisions"])) + "）</summary><ul class=\"small\">" + "".join(f"<li>{esc(d['topic'])}：选择「{esc(d['user_choice'])}」（选项：{esc(' / '.join(d['options']))}；{esc(d['time'])}）</li>" for d in t["decisions"]) + "</ul></details>")
    ev.append("<h3>版本</h3><ul class=\"versions\">" + "".join(f"<li><b>v{ch_['version']}</b> · {esc(ch_['at'])} · {esc(ch_['summary'])}</li>" for ch_ in t["changelog"]) + "</ul>")
    ev.append(f"<p class=\"small\">编号 {esc(t['trip_id'])} · 版本 v{t['plan_version']} · 生成于 {esc(t['generated_at'])} · 核查于 {esc(t.get('checked_at') or '未核查')} · 内容指纹 {c.hash[:12]} · 渲染器 {RENDERER_VERSION}</p>")

    snapshot = json.dumps({"trip_id": t["trip_id"], "plan_version": t["plan_version"], "content_hash": c.hash, "generated_at": t["generated_at"], "trip": t}, ensure_ascii=False).replace("</", "<\\/")

    repl = {
        "@@TITLE@@": esc(it["summary"]["title"]),
        "@@TAGLINE@@": esc(it["summary"].get("tagline") or ""),
        "@@TRIP_ID@@": esc(t["trip_id"]),
        "@@PLAN_VERSION@@": esc(t["plan_version"]),
        "@@DESTINATION@@": esc(it["summary"]["destination"]),
        "@@DATE_RANGE@@": esc(c.date_range()),
        "@@DAY_COUNT@@": str(len(it["days"])),
        "@@PARTY@@": esc(c.party_brief()),
        "@@BUDGET_BRIEF@@": esc(c.budget_brief()),
        "@@STATUS_ZH@@": esc(STATUS_ZH[status]),
        "@@STATUS@@": esc(status),
        "@@ATTRIBUTION@@": esc(c.attribution),
        "@@GENERATED_AT@@": esc(t["generated_at"]),
        "@@HASH_SHORT@@": c.hash[:12],
        "@@RENDERER_VERSION@@": RENDERER_VERSION,
        "@@PROG_TXT@@": esc(prog_txt),
        "<!--@@ROUTE_SVG@@-->": route_svg(c),
        "<!--@@STATUS_NOTICE@@-->": notice,
        "<!--@@OVERVIEW@@-->": "\n".join(ov),
        "<!--@@DAYS@@-->": "\n".join(dh),
        "<!--@@STAY@@-->": "\n".join(st),
        "<!--@@CHECKLIST@@-->": "\n".join(ch),
        "<!--@@EVIDENCE@@-->": "\n".join(ev),
        "<!--@@SNAPSHOT@@-->": snapshot,
    }
    out = tpl
    for k, v in repl.items():
        out = out.replace(k, v)
    leftover = re.findall(r"@@[A-Z_]+@@", out)
    if leftover:
        raise RuntimeError(f"模板占位符未替换：{sorted(set(leftover))}")
    return out


# ----------------------------------------------------------------------------
def html_to_text(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def coverage(text: str, required: list[str]) -> dict:
    normalize = lambda v: re.sub(r"\s+", " ", v).strip()
    normalized = normalize(text)
    missing = [r for r in required if normalize(r) not in normalized]
    return {"required": len(required), "present": len(required) - len(missing), "missing": missing}


def main(argv=None) -> int:
    configure_console()
    ap = argparse.ArgumentParser(description="alun-travel-plan 同源渲染 MD + HTML")
    ap.add_argument("trip", help="trip.json 路径")
    ap.add_argument("--out-dir", help="输出目录（默认 <trip目录>/outputs）")
    ap.add_argument("--manifest", help="manifest 输出路径（默认 <trip目录>/manifest.v<N>.json）")
    ap.add_argument("--force", action="store_true", help="从相同数据重新渲染同版本文件，不覆盖历史快照")
    args = ap.parse_args(argv)

    with open(args.trip, "r", encoding="utf-8") as f:
        trip = json.load(f)
    trip_dir = os.path.dirname(os.path.realpath(args.trip))
    try:
        preflight(trip, semantic=True)
        out_dir = inside(trip_dir, args.out_dir or os.path.join(trip_dir, "outputs"))
        from trip_support import artifact_path
        man_path = artifact_path(trip_dir, args.manifest or os.path.join(trip_dir, f"manifest.v{trip['plan_version']}.json"), args.trip, 'manifest')
    except (ValueError, OSError) as e:
        print(f"✖ 无法渲染：{e}", file=sys.stderr)
        return 1
    c = Ctx(trip)
    base = f"{trip['trip_id']}_v{trip['plan_version']}"
    md_path = inside(trip_dir, os.path.join(out_dir, base + ".md"))
    html_path = inside(trip_dir, os.path.join(out_dir, base + ".html"))
    if any(os.path.normcase(p) == os.path.normcase(os.path.realpath(args.trip)) for p in (md_path, html_path)):
        print("✖ 产物不能覆盖输入文件", file=sys.stderr)
        return 1
    for p in (md_path, html_path):
        if os.path.exists(p) and not args.force:
            print(f"✖ 已存在 {p}；同版本不静默覆盖。先用 state_io.py 升版本，或加 --force。", file=sys.stderr)
            return 1

    md = render_md(c)
    page = render_html(c)
    required = c.required_strings()
    cov_md = coverage(md, required)
    cov_html = coverage(html_to_text(page), required)
    if cov_md["missing"] or cov_html["missing"]:
        print("✖ 可见业务内容覆盖不足，未写入产物", file=sys.stderr)
        return 1
    try:
        checkpoint(trip_dir, trip)
    except (ValueError, OSError) as e:
        print(f"✖ 无法保留版本：{e}", file=sys.stderr)
        return 1
    os.makedirs(out_dir, exist_ok=True)
    # Invalidate completion marker before replacing either file. A failed write
    # must not leave an old manifest claiming a completed delivery.
    if os.path.exists(man_path):
        os.unlink(man_path)
    for p, content in ((md_path, md), (html_path, page)):
        atomic_text(p, content)

    files = []
    for p, kind in ((md_path, "md"), (html_path, "html")):
        with open(p, "rb") as f:
            data = f.read()
        files.append({"path": os.path.relpath(p, trip_dir).replace(os.sep, "/"), "kind": kind, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest = {
        "trip_id": trip["trip_id"],
        "plan_version": trip["plan_version"],
        "content_hash": c.hash,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "renderer_version": RENDERER_VERSION,
        "files": files,
        "coverage": {"md": cov_md, "html": cov_html},
        "attribution": {"text": c.attribution, "md": c.attribution in md, "html": c.attribution in page},
    }
    atomic_json(man_path, manifest)

    print(f"alun-travel-plan render · {trip['trip_id']} v{trip['plan_version']}")
    print(f"  MD   {md_path} ({files[0]['bytes']} B) 覆盖 {cov_md['present']}/{cov_md['required']}")
    print(f"  HTML {html_path} ({files[1]['bytes']} B) 覆盖 {cov_html['present']}/{cov_html['required']}")
    print(f"  署名 md={manifest['attribution']['md']} html={manifest['attribution']['html']} · manifest {man_path}")
    if cov_md["missing"] or cov_html["missing"]:
        print(f"  ⚠ 覆盖缺失：md={cov_md['missing'][:5]} html={cov_html['missing'][:5]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
