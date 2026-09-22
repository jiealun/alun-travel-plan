#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_trip.py 的回归测试：用示例数据做突变，确认每类缺陷都能被拦。
运行：python3 scripts/tests/test_validate.py
这些是**模拟数据测试**，不是真实旅行数据或真实接口测试。
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)

import validate_trip as vt  # noqa: E402

SAMPLE = os.path.join(ROOT, "assets", "examples", "sample_trip.json")


def load():
    with open(SAMPLE, "r", encoding="utf-8") as f:
        return json.load(f)


def run(trip):
    with open(vt.SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)
    audit = vt.Audit(trip)
    ms = vt.MiniSchema(schema)
    ok = ms.check(trip, schema, "$")
    if not ok:
        audit.issue("blocking", [], "结构错误：" + "; ".join(ms.errors[:5]), "修结构")
        return audit, ms.errors
    vt.semantic_checks(trip, audit)
    return audit, []


def item(trip, item_id):
    for d in trip["itinerary"]["days"]:
        for i in d["items"]:
            if i["item_id"] == item_id:
                return i
    raise KeyError(item_id)


def has_issue(audit, severity, keyword):
    return any(i["severity"] == severity and keyword in i["evidence"] for i in audit.issues)


class ValidateTests(unittest.TestCase):
    def test_sample_has_no_blocking(self):
        audit, errs = run(load())
        self.assertEqual(errs, [])
        self.assertEqual(audit.summary()["blocking"], 0, audit.issues)
        self.assertEqual(audit.recommended_status(), "conditional")

    def test_schema_rejects_unknown_enum(self):
        t = load()
        t["status"] = "perfect"
        audit, errs = run(t)
        self.assertTrue(errs)

    def test_T05_closed_day_is_blocking(self):
        t = load()
        # 把第一天改到周一（2026-10-12），博物馆常规闭馆
        d = t["itinerary"]["days"][0]
        d["date"] = "2026-10-12"
        for i in d["items"]:
            i["planned_start"] = i["planned_start"].replace("2026-10-10", "2026-10-12")
            i["planned_end"] = i["planned_end"].replace("2026-10-10", "2026-10-12")
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "没有适用的开放窗口"), audit.issues)

    def test_last_entry_enforced(self):
        t = load()
        m = item(t, "item-d1-museum")
        m["planned_start"] = "2026-10-10T16:30:00+08:00"
        m["planned_end"] = "2026-10-10T17:30:00+08:00"
        # 把后面的项也挪开，避免重叠干扰
        for iid in ("item-d1-transit-3", "item-d1-checkin", "item-d1-oldstreet", "item-d1-dinner"):
            x = item(t, iid)
            x["planned_start"] = x["planned_start"].replace("T14:", "T18:").replace("T16:", "T19:").replace("T19:", "T20:")
            x["planned_end"] = x["planned_end"].replace("T14:", "T18:").replace("T16:", "T19:").replace("T19:", "T20:").replace("T20:30", "T21:30")
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "不在 示例博物馆 的开放窗口内"), audit.issues)

    def test_overlap_is_blocking(self):
        t = load()
        item(t, "item-d1-museum")["planned_start"] = "2026-10-10T10:30:00+08:00"
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "时间重叠"))

    def test_leg_time_not_respected(self):
        t = load()
        item(t, "item-d1-transit-2")["planned_end"] = "2026-10-10T10:20:00+08:00"  # 只留 10 分钟，上界 35
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "少于路段上界"))

    def test_unknown_leg_with_number_is_blocking(self):
        t = load()
        leg = t["legs"][1]
        leg["evidence_level"] = "unknown"  # 但保留数字
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "证据等级 unknown 却填了时间"))

    def test_T07_booking_entry_is_not_confirmed(self):
        t = load()
        m = item(t, "item-d1-museum")
        m["booking_status"] = "not_required"
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "需要预约，但"))

    def test_verified_requires_verified_facts(self):
        t = load()
        m = item(t, "item-d1-museum")
        m["booking_status"] = "confirmed"
        m["verification_status"] = "verified"
        t["facts"][1]["status"] = "unknown"  # 目标日期公告未查
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "不是 verified"))

    def test_T04_must_go_removed_is_blocking(self):
        t = load()
        d = t["itinerary"]["days"][0]
        d["items"] = [i for i in d["items"] if i["item_id"] != "item-d1-museum"]
        t["itinerary"]["alternatives"][0]["replaces_item_ids"] = ["item-d1-transit-2", "item-d1-transit-3"]
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "必去项目"))

    def test_locked_item_cannot_disappear(self):
        t = load()
        d = t["itinerary"]["days"][1]
        d["items"] = [i for i in d["items"] if i["item_id"] != "item-rail-back"]
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "没有对应的 locked 行程项"))

    def test_return_boundary(self):
        t = load()
        t["request"]["dates"]["return_by"] = "2026-10-11T19:00:00+08:00"
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "晚于最晚返回"))

    def test_T16_budget_over_limit(self):
        t = load()
        t["itinerary"]["budget"]["hard_limit"] = 1000
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "已超硬上限"))

    def test_budget_unknown_not_zero(self):
        t = load()
        cats = t["itinerary"]["budget"]["categories"]
        cats[-1]["min"] = 0
        cats[-1]["max"] = 0
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "info", "标 unknown 却有数字"))

    def test_status_executable_with_conditions_is_blocking(self):
        t = load()
        t["status"] = "executable_as_of_check"
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "方案标为 executable_as_of_check"))

    def test_instant_alternative_cannot_require_booking(self):
        t = load()
        t["itinerary"]["alternatives"][0]["requires_booking"] = True
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "即时备用却需要预约"))

    def test_T24_secret_in_data(self):
        t = load()
        t["itinerary"]["sources_note"] = "调试：TIKHUB_API_KEY=abcdef1234567890abcdef"
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "疑似凭据"))

    def test_T24_javascript_url(self):
        t = load()
        t["places"][2]["nav_link"] = "javascript:alert(1)"
        audit, errs = run(t)
        # schema 层就应拒绝（httpUrl pattern）
        self.assertTrue(errs or has_issue(audit, "blocking", "危险协议"))

    def test_T10_seasonal_with_temperature_flagged(self):
        t = load()
        t["itinerary"]["weather"]["entries"] = [{"date": "2026-10-10", "summary": "晴 24°C", "source_id": None}]
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "conditional", "含具体温度"))

    def test_dangling_reference(self):
        t = load()
        item(t, "item-d2-garden")["fact_ids"].append("fact-does-not-exist")
        audit, _ = run(t)
        self.assertTrue(has_issue(audit, "blocking", "悬空引用"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
