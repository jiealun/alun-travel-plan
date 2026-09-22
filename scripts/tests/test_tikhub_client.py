#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tikhub_client.py 的**模拟响应**测试（不联网、不消耗任何余额）。
覆盖：T12 认证失败即停、T13 200 但内层服务异常、T14 游标不前进/结构变化即停、预算硬上限、令牌脱敏、
二级评论 cursor 对象拆分、重定向到非允许域名中止。
⚠️ 这些不是真实接口测试；真实返回结构需上线前用真实令牌核对。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tikhub_client as tc  # noqa: E402


def make_transport(script):
    """script: list of (status, body_dict_or_str)；按调用顺序返回。记录请求 URL。"""
    calls = []

    def transport(url, headers):
        calls.append((url, headers))
        if not script:
            raise AssertionError("超出预设响应数量")
        status, body = script.pop(0)
        if isinstance(body, Exception):
            raise body
        return status, body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    transport.calls = calls
    return transport


def ok(inner):
    return (200, {"code": 200, "data": {"data": inner}})


class TikHubClientTests(unittest.TestCase):
    def test_search_pagination_passes_search_ids(self):
        tr = make_transport([
            ok({"search_id": "S1", "search_session_id": "SS1", "items": [{"note_id": "n1", "title": "a", "desc": "x"}]}),
            ok({"search_id": "S1", "search_session_id": "SS1", "items": [{"note_id": "n2", "title": "b", "desc": "y"}]}),
        ])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        res = c.search("示例 关键词", pages=2)
        self.assertEqual([i["note_id"] for i in res["items"]], ["n1", "n2"])
        self.assertIn("search_id=S1", tr.calls[1][0])
        self.assertIn("search_session_id=SS1", tr.calls[1][0])
        self.assertNotIn("search_id", tr.calls[0][0])
        self.assertEqual(res["usage_record"]["requests"], 2)

    def test_search_stops_on_duplicate_page(self):
        tr = make_transport([
            ok({"search_id": "S1", "search_session_id": "SS1", "items": [{"note_id": "n1", "desc": ""}]}),
            ok({"search_id": "S1", "search_session_id": "SS1", "items": [{"note_id": "n1", "desc": ""}]}),
            ok({"items": [{"note_id": "n9"}]}),
        ])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        res = c.search("k", pages=3)
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(len(tr.calls), 2)  # 第三页不再请求
        self.assertTrue(any("无新内容" in w for w in res["warnings"]))

    def test_T12_auth_failure_stops_everything(self):
        tr = make_transport([(401, {"detail": "Unauthorized"}), ok({"items": []})])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        with self.assertRaises(tc.AuthError) as ctx:
            c.search("k")
        self.assertNotIn("tok_abcdefgh1234", str(ctx.exception))  # 脱敏
        self.assertNotIn("1234", str(ctx.exception))  # 连令牌末尾也不进入错误输出
        with self.assertRaises(tc.TikHubError):
            c.comments("n1")  # 已停止，不再发请求
        self.assertEqual(len(tr.calls), 1)

    def test_T13_inner_service_error_is_billable_and_not_retried(self):
        tr = make_transport([ok({"message": "服务异常", "comments": []})])
        b = tc.Budget(10)
        c = tc.Client("tok_abcdefgh1234", b, transport=tr)
        with self.assertRaises(tc.TikHubError) as ctx:
            c.comments("bad-id")
        self.assertIn("可能已计费", str(ctx.exception))
        self.assertEqual(b.billable_estimate, 1)
        self.assertEqual(len(tr.calls), 1)

    def test_T14_cursor_not_advancing_stops(self):
        tr = make_transport([
            ok({"comments": [{"id": "c1", "content": "a"}], "cursor": "X", "index": 1, "has_more": True}),
            ok({"comments": [{"id": "c2", "content": "b"}], "cursor": "X", "index": 2, "has_more": True}),
            ok({"comments": [{"id": "c3", "content": "c"}], "cursor": "Y", "index": 3, "has_more": True}),
        ])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        res = c.comments("n1", pages=3)
        self.assertEqual(len(tr.calls), 2)
        self.assertTrue(any("游标未前进" in w for w in res["warnings"]))

    def test_T14_structure_change_raises(self):
        tr = make_transport([ok({"comments": "not-a-list"})])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        with self.assertRaises(tc.StructureError):
            c.comments("n1")

    def test_budget_hard_limit(self):
        tr = make_transport([ok({"search_id": "S", "search_session_id": "SS", "items": [{"note_id": f"n{i}"}]}) for i in range(5)])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(2), transport=tr)
        with self.assertRaises(tc.BudgetExceeded):
            c.search("k", pages=5)
        self.assertEqual(len(tr.calls), 2)

    def test_retry_on_5xx_counts_budget(self):
        tr = make_transport([(503, "busy"), ok({"items": [{"note_id": "n1"}]})])
        b = tc.Budget(10)
        c = tc.Client("tok_abcdefgh1234", b, transport=tr)
        tc.time.sleep = lambda s: None  # 不等待
        res = c.search("k")
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(b.used, 2)

    def test_sub_comments_cursor_object_split(self):
        tr = make_transport([
            ok({"comments": [{"id": "r1", "content": "x"}], "cursor": {"cursor": "CUR2", "index": 3}, "has_more": True}),
            ok({"comments": [{"id": "r2", "content": "y"}], "cursor": {"cursor": "", "index": 4}, "has_more": False}),
        ])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        res = c.sub_comments("n1", "c1", pages=2)
        self.assertEqual(len(res["items"]), 2)
        self.assertIn("cursor=CUR2", tr.calls[1][0])
        self.assertIn("index=3", tr.calls[1][0])
        self.assertNotIn("%7B", tr.calls[1][0])  # 没有把对象整个编码进去

    def test_no_token_means_skip_not_prompt(self):
        c = tc.Client(None, tc.Budget(10), transport=make_transport([]))
        with self.assertRaises(tc.AuthError) as ctx:
            c.search("k")
        self.assertIn("不要求用户明文提供令牌", str(ctx.exception))

    def test_dry_run_makes_no_request(self):
        tr = make_transport([])
        logs = []
        c = tc.Client(None, tc.Budget(10), transport=tr, dry_run=True, log=logs.append)
        res = c.search("k", pages=2)
        self.assertEqual(tr.calls, [])
        self.assertEqual(res["usage_record"]["requests"], 0)
        self.assertTrue(logs and "未发送请求" in logs[0])

    def test_comment_output_has_no_author_fields(self):
        tr = make_transport([ok({"comments": [{"id": "c1", "content": "a", "user_info": {"nickname": "张三", "user_id": "u1"}}], "cursor": "", "has_more": False})])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=tr)
        res = c.comments("n1")
        text = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("张三", text)
        self.assertNotIn("u1", text)
        self.assertNotIn("tok_abcdefgh1234", text)

    def test_non_allowed_host_rejected(self):
        # 只允许 api.tikhub.io；基础地址被改到其他域名时必须拒绝发请求
        self.assertEqual(tc.ALLOWED_HOST, "api.tikhub.io")
        c = tc.Client("tok_abcdefgh1234", tc.Budget(10), transport=make_transport([]))
        tc.BASE_URL_BACKUP = tc.BASE_URL
        tc.BASE_URL = "https://evil.example.com"
        try:
            with self.assertRaises(tc.TikHubError):
                c.search("k")
        finally:
            tc.BASE_URL = tc.BASE_URL_BACKUP


class RealShapeTests(unittest.TestCase):
    """按 2026-09-18 真实令牌实测到的响应形状构造的夹具（已脱敏，内容为虚构）。"""

    def test_search_real_shape(self):
        body = {"code": 200, "data": {"success": True, "code": 0, "msg": "成功", "search_id": "S-REAL", "search_session_id": "SS-REAL", "page": 1, "next_page": 2,
                "data": {"items": [
                    {"model_type": "search_agent", "mix_track_id": "DQA"},
                    {"model_type": "note", "note": {"id": "n-real-1", "title": "示例标题", "desc": "示例正文", "type": "normal", "liked_count": 0, "comments_count": 0, "collected_count": 3, "timestamp": 1789270938, "images_list": [{}, {}], "user": {"nickname": "某人", "userid": "u1"}}},
                ]}}}
        tr = make_transport([(200, body), (200, body)])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(5), transport=tr)
        res = c.search("k", pages=2)
        self.assertEqual(len(res["items"]), 1)  # search_agent 被过滤，第二页重复被去重
        it = res["items"][0]
        self.assertEqual(it["liked_count"], 0)  # 0 不能被吞成 None
        self.assertEqual(it["image_count"], 2)
        self.assertTrue(it["published_at"].startswith("2026-"))
        self.assertIn("search_id=S-REAL", tr.calls[1][0])
        self.assertNotIn("某人", json.dumps(res, ensure_ascii=False))

    def test_note_detail_real_shape(self):
        body = {"code": 200, "data": {"success": True, "code": 0, "msg": "成功", "data": [{"model_type": "note", "note_list": [{"id": "n-real-1", "title": "T", "desc": "D", "type": "normal", "time": 1789270938, "liked_count": 10, "comments_count": 9, "collected_count": 1, "images_list": [{}], "hash_tag": [{"name": "示例话题"}], "ip_location": "Shanghai", "user": {"nickname": "某人"}}]}]}}
        c = tc.Client("tok_abcdefgh1234", tc.Budget(5), transport=make_transport([(200, body)]))
        it = c.note_detail("n-real-1")["items"][0]
        self.assertEqual(it["title"], "T")
        self.assertEqual(it["hashtags"], ["示例话题"])
        self.assertEqual(it["comment_count"], 9)

    def test_comments_real_shape_cursor_string(self):
        body = {"code": 200, "data": {"success": True, "code": 0, "msg": "成功", "data": {
            "comments": [{"id": "c1", "content": "x", "time": 1789272756, "like_count": 0, "sub_comment_count": 8, "ip_location": "Shanghai",
                          "sub_comments": [{"id": "r1", "content": "y", "time": 1789272784, "like_count": 0, "user": {"nickname": "某人"}}],
                          "sub_comment_cursor": "{\"cursor\":\"r1\",\"index\":1}", "user": {"nickname": "某人"}}],
            "cursor": "{\"contextId\":\"ctx\",\"index\":2,\"pageArea\":\"ALL\"}", "has_more": True, "comment_count": 9}}}
        body2 = {"code": 200, "data": {"success": True, "code": 0, "msg": "成功", "data": {"comments": [{"id": "c2", "content": "z", "time": 1789272800, "like_count": 1}], "cursor": "", "has_more": False}}}
        tr = make_transport([(200, body), (200, body2)])
        c = tc.Client("tok_abcdefgh1234", tc.Budget(5), transport=tr)
        res = c.comments("n", pages=2)
        self.assertEqual(len(res["items"]), 2)
        first = res["items"][0]
        self.assertEqual(first["sub_comment_cursor"], {"cursor": "r1", "index": 1})
        self.assertEqual(first["sub_comments_inline"][0]["comment_id"], "r1")
        # 第二页把整个 cursor 字符串原样回传，并从其中取 index / pageArea
        self.assertIn("contextId", tr.calls[1][0])
        self.assertIn("index=2", tr.calls[1][0])
        self.assertIn("pageArea=ALL", tr.calls[1][0])
        self.assertNotIn("某人", json.dumps(res, ensure_ascii=False))

    def test_inner_business_failure_is_billable(self):
        body = {"code": 200, "data": {"success": False, "code": -1, "msg": "参数错误", "data": None}}
        b = tc.Budget(5)
        c = tc.Client("tok_abcdefgh1234", b, transport=make_transport([(200, body)]))
        with self.assertRaises(tc.TikHubError):
            c.comments("bad")
        self.assertEqual(b.billable_estimate, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
