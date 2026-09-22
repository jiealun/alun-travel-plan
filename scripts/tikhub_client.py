#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tikhub_client.py · TikHub 小红书 App V2 适配器（可选增强；只用标准库）

按 2026-09-18 拉取的官方 OpenAPI 描述实现，并于同日用真实令牌做过最小查询核对（见 references/tikhub.md §7 实测记录）。
结构不符时本适配器会明确报错并停止，不会去猜多条 JSON 路径。

规则（对应 references/tikhub.md）：
  - 真实调用从 TIKHUB_API_KEY 或 --token-env 读取令牌；日志不输出令牌，dry-run 不读令牌。
  - 只向 https://api.tikhub.io 发请求；拒绝重定向。
  - 统一预算（Budget / PersistentBudget）：CLI 通过 --usage-file 跨命令计数；超预算拒绝。
  - 401/403 立即停止；429/5xx/网络错误最多重试 2 次；超时不重试、费用记 unknown。
  - HTTP 200 但内层"服务异常"→ 不当有效内容，记"可能计费"。
  - 分页：重复游标 / 空页 / 重复内容立即停止。
  - 统一输出：{items, next_page_token, source_meta, warnings, usage_record}

用法见 references/tikhub.md §6；--dry-run 只打印请求计划。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from functools import wraps
from trip_support import atomic_json, configure_console
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_URL = "https://api.tikhub.io"
ALLOWED_HOST = "api.tikhub.io"
EP = {
    "search": "/api/v1/xiaohongshu/app_v2/search_notes",
    "image_note": "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
    "video_note": "/api/v1/xiaohongshu/app_v2/get_video_note_detail",
    "comments": "/api/v1/xiaohongshu/app_v2/get_note_comments",
    "sub_comments": "/api/v1/xiaohongshu/app_v2/get_note_sub_comments",
}
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 2


class TikHubError(Exception):
    pass


class AuthError(TikHubError):
    pass


class BudgetExceeded(TikHubError):
    pass


class StructureError(TikHubError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def first_present(*vals):
    """第一个不为 None 的值（0 也算有值；避免 `or` 链把 0 吞成 None）。"""
    for v in vals:
        if v is not None:
            return v
    return None


def parse_json_string(v):
    """小红书把游标编码成 JSON 字符串（如 '{"cursor":"...","index":1}'）；解析失败原样返回。"""
    if isinstance(v, str) and v.startswith("{"):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def redact(token: str | None) -> str:
    if not token:
        return "(未设置)"
    return "****" + token[-4:] if len(token) >= 8 else "****"


class Budget:
    """跨模块共享的请求预算。每一次 HTTP 请求（含重试）都计数。"""

    def __init__(self, limit: int):
        self.limit = int(limit)
        if self.limit < 0:
            raise ValueError("请求上限不能为负数")
        self.stopped_reason = None
        self.used = 0
        self.billable_estimate = 0  # 可能计费的请求数（含内层服务异常）
        self.unknown_cost = 0       # 超时等费用状态未知
        self.notes: list[str] = []

    def reserve(self, n: int = 1) -> None:
        if n < 1:
            raise ValueError("预留次数必须为正数")
        if self.stopped_reason:
            raise TikHubError("本次调研的 TikHub 调用已停止")
        if self.used + n > self.limit:
            raise BudgetExceeded(f"请求预算已用 {self.used}/{self.limit}，不再发起新请求")
        self.used += n
        self.persist()

    def persist(self):
        pass

    def close(self):
        pass

    def record(self) -> dict:
        return {"requests": self.used, "limit": self.limit, "billable_estimate": self.billable_estimate, "unknown_cost": self.unknown_cost, "notes": self.notes, "stopped_reason": self.stopped_reason}


class PersistentBudget(Budget):
    """One locked ledger per trip across CLI commands; reserve before HTTP."""
    def __init__(self, limit, path):
        super().__init__(limit)
        self.path = os.path.abspath(path)
        self.lock_path = self.path + ".lock"
        self.lock_fd = None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            self.lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise TikHubError("消费账本正在使用或存在中断锁；未发起请求，请先检查持锁进程") from None
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    saved = json.load(f)
                fields = ("requests", "limit", "billable_estimate", "unknown_cost")
                if not isinstance(saved, dict) or any(type(saved.get(k)) is not int or saved[k] < 0 for k in fields):
                    raise ValueError("消费账本结构损坏")
                if saved["requests"] > saved["limit"] or saved["billable_estimate"] > saved["requests"]:
                    raise ValueError("消费账本计数不一致")
                if limit > saved["limit"]:
                    raise ValueError("不能通过重复命令提高已记录上限；需另行确认新的调研预算")
                self.limit = min(limit, saved["limit"])
                if saved["requests"] > self.limit:
                    raise BudgetExceeded("已用请求数超过新上限，未发起请求")
                self.used = saved["requests"]
                self.billable_estimate = saved["billable_estimate"]
                # A process may have died after reserve and before response.
                self.unknown_cost = max(saved["unknown_cost"], self.used - self.billable_estimate)
                self.stopped_reason = saved.get("stopped_reason")
                self.notes = []  # Never copy untrusted ledger text into logs.
            self.persist()
        except Exception:
            self.close()
            raise

    def persist(self):
        atomic_json(self.path, self.record())

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
            os.unlink(self.lock_path)


def guarded_endpoint(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (StructureError, TypeError, AttributeError, KeyError, IndexError, ValueError) as e:
            self.stopped_reason = "响应结构与已支持契约不一致"
            self.budget.stopped_reason = self.stopped_reason
            self.budget.persist()
            raise StructureError(self.stopped_reason + "；已停止适配器，请使用普通联网调研") from None
    return call


class Client:
    def __init__(self, token: str | None, budget: Budget, transport=None, timeout: int = DEFAULT_TIMEOUT, dry_run: bool = False, log=print):
        self.token = token
        self.budget = budget
        self.transport = transport or self._http
        self.response_mode = "mock" if transport is not None else "live"
        self.timeout = timeout
        self.dry_run = dry_run
        self.log = log
        self.stopped_reason: str | None = None

    # ---- HTTP ---------------------------------------------------------------
    def _http(self, url: str, headers: dict) -> tuple[int, str]:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                raise TikHubError("服务返回重定向，已中止；认证请求不自动跟随跳转")

        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")

    def request(self, key: str, params: dict) -> dict:
        try:
            return self._request(key, params)
        except AuthError:
            self.budget.stopped_reason = self.stopped_reason or "认证失败"
            raise
        finally:
            self.budget.persist()

    def _request(self, key: str, params: dict) -> dict:
        if self.stopped_reason or self.budget.stopped_reason:
            raise TikHubError("适配器已停止；请先解决认证或结构问题")
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        url = BASE_URL + EP[key] + "?" + urllib.parse.urlencode(clean)
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.hostname != ALLOWED_HOST or parsed_url.scheme != "https" or parsed_url.port not in (None, 443):
            raise TikHubError("目标域名不在允许列表")
        if self.dry_run:
            self.log(f"[dry-run] GET {url}  (未发送请求)")
            return {"_dry_run": True, "code": 200, "data": {}}
        if not self.token:
            raise AuthError("未配置 TIKHUB_API_KEY；本次跳过小红书调研，不要求用户明文提供令牌")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": "alun-travel-plan/1.0"}
        attempt = 0
        while True:
            self.budget.reserve(1)
            attempt += 1
            try:
                status, body = self.transport(url, headers)
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                is_timeout = isinstance(e, TimeoutError) or "timed out" in str(e).lower()
                if is_timeout:
                    self.budget.unknown_cost += 1
                    self.budget.notes.append(f"{key} 超时，费用状态未知，不自动重试")
                    raise TikHubError(f"{key} 请求超时（费用状态未知）") from e
                if attempt <= MAX_RETRIES:
                    time.sleep(1.5 * attempt)
                    continue
                raise TikHubError(f"{key} 网络错误，请检查连接") from None
            if status in (401, 403):
                self.stopped_reason = f"认证失败或余额不足（HTTP {status}）"
                raise AuthError(self.stopped_reason + "；已停止全部 TikHub 请求，请检查令牌与余额")
            if status == 429 or status >= 500:
                if attempt <= MAX_RETRIES:
                    time.sleep(2.0 * attempt)
                    continue
                raise TikHubError(f"{key} 持续 {status}，已放弃（重试 {MAX_RETRIES} 次）")
            if status != 200:
                raise TikHubError(f"{key} HTTP {status}，未输出上游响应正文")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as e:
                raise StructureError(f"{key} 返回不是 JSON") from e
            return self._check_payload(key, payload)

    # ---- 响应校验 -------------------------------------------------------------
    def _check_payload(self, key: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise StructureError(f"{key} 外层不是对象")
        self.budget.billable_estimate += 1  # HTTP 200 may be billable even when business data fails.
        code = payload.get("code")
        data = payload.get("data")
        inner_code = data.get("code") if isinstance(data, dict) else None
        messages = [payload.get(k) for k in ("message", "detail", "msg")]
        if isinstance(data, dict):
            messages.extend(data.get(k) for k in ("message", "detail", "msg"))
        text = " ".join(v for v in messages if isinstance(v, str)).lower()
        auth_codes = (401, 402, 403, "401", "402", "403")
        if code in auth_codes or inner_code in auth_codes or any(v in text for v in ("余额不足", "insufficient balance", "insufficient credit", "unauthorized", "invalid api key")):
            self.stopped_reason = "认证失败或余额不足（业务状态）"
            raise AuthError(self.stopped_reason + "；已停止全部请求，此次可能计费")
        if code not in (None, 200):
            raise TikHubError(f"{key} 外层业务失败（可能计费），未输出上游正文")
        if not isinstance(data, dict):
            raise StructureError(f"{key} 缺少有效 data 对象")
        inner = data.get("data")
        error_text = " ".join(str(inner.get(k) or "") for k in ("message", "msg", "detail")) if isinstance(inner, dict) else ""
        if data.get("success") is False or inner_code not in (None, 0, 200) or any(k in (text + " " + error_text).lower() for k in ("服务异常", "service error")):
            raise TikHubError(f"{key} 内层返回异常：此请求可能已计费，不重试")
        return payload

    @staticmethod
    def _outer(payload: dict) -> dict:
        """$.data：业务外层（search_id / search_session_id / success / code / msg 在这里）。"""
        d = payload.get("data")
        if not isinstance(d, dict):
            raise StructureError("data 不是对象，无法解析")
        return d

    @staticmethod
    def _inner(payload: dict) -> dict:
        """$.data.data：业务内层（实测 2026-09-18：搜索的 items、评论的列表都在这里）；$.data 本身是业务对象时也接受。"""
        d = payload.get("data")
        if isinstance(d, dict) and isinstance(d.get("data"), (dict, list)):
            return d["data"] if isinstance(d["data"], dict) else {"items": d["data"]}
        if isinstance(d, dict):
            return d
        raise StructureError("data 结构不是对象，无法解析")

    def _list(self, inner, names):
        # Explicit supported variants, never mistake an absent field for empty.
        for name in names:
            if name in inner:
                value = inner[name]
                if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
                    raise StructureError("列表字段类型不符")
                return value
        raise StructureError("响应缺少已支持的列表字段")

    # ---- 端点 -------------------------------------------------------------------
    @guarded_endpoint
    def search(self, keyword: str, pages: int = 1, sort_type: str = "general", time_filter: str = "不限", note_type: str = "不限") -> dict:
        items, warnings, seen = [], [], set()
        search_id = search_session_id = None
        next_token = None
        for page in range(1, pages + 1):
            params = {"keyword": keyword, "page": page, "sort_type": sort_type, "time_filter": time_filter, "note_type": note_type}
            if page > 1:
                if not (search_id and search_session_id):
                    warnings.append("首页未返回 search_id/search_session_id，停止翻页")
                    break
                params.update({"search_id": search_id, "search_session_id": search_session_id})
            payload = self.request("search", params)
            if payload.get("_dry_run"):
                continue
            outer, inner = self._outer(payload), self._inner(payload)
            # 实测：search_id / search_session_id 在 $.data 层，不在 $.data.data
            search_id = outer.get("search_id") or inner.get("search_id") or search_id
            search_session_id = outer.get("search_session_id") or inner.get("search_session_id") or search_session_id
            notes = self._list(inner, ("items", "notes", "note_list"))
            if not isinstance(notes, list):
                raise StructureError("搜索结果中找不到笔记列表（items/notes/note_list）")
            new = 0
            for n in notes:
                # 实测：items 里混有 model_type=search_agent（AI 一站式回答卡）等非笔记项，只取 note
                if isinstance(n, dict) and n.get("model_type") not in (None, "note"):
                    continue
                nid = self._note_id(n)
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                new += 1
                items.append(self._slim_note(n, nid))
            if new == 0:
                warnings.append(f"第 {page} 页无新内容，停止翻页")
                next_token = None
                break
            if outer.get("next_page") is None and outer.get("has_more") is False:
                next_token = None
                break
            next_token = {"page": page + 1, "search_id": search_id, "search_session_id": search_session_id}
        return self._result(items, next_token, {"endpoint": "search", "keyword": keyword, "sort_type": sort_type, "time_filter": time_filter}, warnings)

    @guarded_endpoint
    def note_detail(self, note_id: str, video: bool = False) -> dict:
        key = "video_note" if video else "image_note"
        payload = self.request(key, {"note_id": note_id})
        if payload.get("_dry_run"):
            return self._result([], None, {"endpoint": key, "note_id": note_id}, [])
        # 实测（2026-09-18）：$.data.data 是列表，[0].note_list[0] 才是笔记对象（含 desc/time/images_list/hash_tag/topics）
        raw = self._outer(payload).get("data")
        note = None
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            nl = raw[0].get("note_list")
            if isinstance(nl, list) and nl and isinstance(nl[0], dict):
                note = nl[0]
        elif isinstance(raw, dict):
            note = raw.get("note") or raw.get("note_card") or (raw.get("note_list") or [None])[0]
        if not isinstance(note, dict) or not (note.get("id") or note.get("note_id")):
            raise StructureError("详情响应里找不到笔记对象（期望 $.data.data[0].note_list[0]）")
        if str(note.get("id") or note.get("note_id")) != note_id:
            raise StructureError("详情响应 ID 与请求不一致")
        item = self._slim_note(note, note_id, full=True)
        item["hashtags"] = [h.get("name") for h in (note.get("hash_tag") or []) if isinstance(h, dict) and h.get("name")]
        item["ip_location"] = note.get("ip_location")
        warnings = []
        if video:
            warnings.append("视频笔记只取得文字/可读字段，未观看视频内容")
        return self._result([item], None, {"endpoint": key, "note_id": note_id}, warnings)

    @guarded_endpoint
    def comments(self, note_id: str, pages: int = 1, sort_strategy: str = "latest_v2") -> dict:
        items, warnings, seen = [], [], set()
        cursor, index, page_area = "", 0, "UNFOLDED"
        next_token = None
        last_cursor = None
        for page in range(1, pages + 1):
            payload = self.request("comments", {"note_id": note_id, "cursor": cursor, "index": index, "pageArea": page_area, "sort_strategy": sort_strategy})
            if payload.get("_dry_run"):
                continue
            inner = self._inner(payload)
            comments = self._list(inner, ("comments", "items"))
            if not isinstance(comments, list):
                raise StructureError("评论响应中找不到评论列表（comments/items）")
            new = 0
            for cmt in comments:
                cid = str(cmt.get("id") or cmt.get("comment_id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                new += 1
                items.append(self._slim_comment(cmt, cid))
            # 实测（2026-09-18）：cursor 是 JSON 字符串 '{"contextId":"…","index":2,"pageArea":"ALL"}'；
            # 翻页时 cursor 原样回传，index / pageArea 从其中取（文档写法）。has_more 在同层。
            raw_cursor = inner.get("cursor")
            cur_obj = parse_json_string(raw_cursor)
            if isinstance(cur_obj, dict):
                cursor = raw_cursor if isinstance(raw_cursor, str) else (cur_obj.get("cursor") or "")
                index = first_present(cur_obj.get("index"), inner.get("index"), index)
                page_area = first_present(cur_obj.get("pageArea"), inner.get("pageArea"), page_area)
            else:
                cursor = raw_cursor if isinstance(raw_cursor, str) else ""
                index = inner.get("index", index)
                page_area = inner.get("pageArea", page_area)
            has_more = inner.get("has_more", bool(cursor))
            if new == 0 or not has_more or not cursor or cursor == last_cursor:
                if cursor and cursor == last_cursor:
                    warnings.append("游标未前进，停止翻页")
                next_token = None
                break
            last_cursor = cursor
            next_token = {"cursor": cursor, "index": index, "pageArea": page_area}
        return self._result(items, next_token, {"endpoint": "comments", "note_id": note_id, "sort_strategy": sort_strategy}, warnings)

    @guarded_endpoint
    def sub_comments(self, note_id: str, comment_id: str, pages: int = 1, start_cursor: str = "", start_index: int = 1) -> dict:
        """start_cursor / start_index 可直接取自一级评论的 sub_comment_cursor（{"cursor","index"}），跳过已内联返回的那几条。"""
        items, warnings, seen = [], [], set()
        cursor, index = start_cursor or "", start_index or 1
        next_token = None
        last_cursor = None
        for page in range(1, pages + 1):
            payload = self.request("sub_comments", {"note_id": note_id, "comment_id": comment_id, "cursor": cursor, "index": index})
            if payload.get("_dry_run"):
                continue
            inner = self._inner(payload)
            comments = self._list(inner, ("comments", "items"))
            if not isinstance(comments, list):
                raise StructureError("二级评论响应中找不到列表")
            new = 0
            for cmt in comments:
                cid = str(cmt.get("id") or cmt.get("comment_id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                new += 1
                items.append(self._slim_comment(cmt, cid))
            cur_obj = parse_json_string(inner.get("cursor"))
            if isinstance(cur_obj, dict):
                cursor, index = cur_obj.get("cursor", "") or "", first_present(cur_obj.get("index"), index)
            elif isinstance(cur_obj, str):
                cursor = cur_obj
            else:
                cursor = ""
            has_more = inner.get("has_more", bool(cursor))
            if new == 0 or not has_more or not cursor or cursor == last_cursor:
                next_token = None
                break
            last_cursor = cursor
            next_token = {"cursor": cursor, "index": index}
        return self._result(items, next_token, {"endpoint": "sub_comments", "note_id": note_id, "comment_id": comment_id}, warnings)

    # ---- 精简与脱敏 -------------------------------------------------------------
    @staticmethod
    def _note_id(n: dict) -> str | None:
        for k in ("note_id", "id"):
            if n.get(k):
                return str(n[k])
        nc = n.get("note_card") or n.get("note") or {}
        for k in ("note_id", "id"):
            if isinstance(nc, dict) and nc.get(k):
                return str(nc[k])
        return None

    @staticmethod
    def _epoch_to_iso(v):
        """小红书时间戳：秒或毫秒；无法解析返回原值。"""
        try:
            v = int(v)
        except (TypeError, ValueError):
            return v
        if v > 10**12:
            v //= 1000
        try:
            return datetime.fromtimestamp(v).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return v

    @staticmethod
    def _slim_note(n: dict, nid: str, full: bool = False) -> dict:
        # 实测（2026-09-18）：搜索项为 {model_type:"note", note:{id,title,desc,type,liked_count,comments_count,
        # collected_count,shared_count,timestamp,last_update_time,images_list,user,...}}；详情结构见 note_detail
        if isinstance(n.get("note"), dict):
            nc = n["note"]
        elif isinstance(n.get("note_card"), dict):
            nc = n["note_card"]
        else:
            nc = n
        desc = nc.get("desc") or nc.get("content") or ""
        interact = nc.get("interact_info") or {}
        ts = nc.get("timestamp") or nc.get("time") or nc.get("publish_time") or nc.get("last_update_time")
        return {
            "note_id": nid,
            "title": nc.get("title") or nc.get("display_title") or "",
            "type": nc.get("type") or nc.get("note_type") or "",
            "excerpt": desc if full else desc[:200],
            "published_at": Client._epoch_to_iso(ts) if ts else None,
            "liked_count": first_present(interact.get("liked_count"), nc.get("liked_count"), nc.get("likes")),
            "comment_count": first_present(interact.get("comment_count"), nc.get("comments_count"), nc.get("comment_count")),
            "collected_count": first_present(interact.get("collected_count"), nc.get("collected_count")),
            "url": f"https://www.xiaohongshu.com/explore/{nid}",
            "image_count": len(nc.get("image_list") or nc.get("images_list") or []),
            "_license_note": "图片地址不构成转载许可；未保存图片 URL；未保存作者昵称/ID",
        }

    @staticmethod
    def _slim_comment(cmt: dict, cid: str) -> dict:
        # 实测：一级评论自带 sub_comments（内联几条）+ sub_comment_cursor（JSON 字符串 {"cursor","index"}）+ sub_comment_count
        ts = first_present(cmt.get("create_time"), cmt.get("time"))
        sub_cur = parse_json_string(cmt.get("sub_comment_cursor"))
        inline_subs = []
        for sc in cmt.get("sub_comments") or []:
            if isinstance(sc, dict) and (sc.get("id") or sc.get("comment_id")):
                inline_subs.append({
                    "comment_id": str(sc.get("id") or sc.get("comment_id")),
                    "content": (sc.get("content") or "")[:300],
                    "published_at": Client._epoch_to_iso(first_present(sc.get("create_time"), sc.get("time"))) if first_present(sc.get("create_time"), sc.get("time")) else None,
                    "liked_count": first_present(sc.get("like_count"), sc.get("liked_count")),
                })
        out = {
            "comment_id": cid,
            "content": (cmt.get("content") or "")[:300],
            "published_at": Client._epoch_to_iso(ts) if ts else None,
            "liked_count": first_present(cmt.get("like_count"), cmt.get("liked_count")),
            "sub_comment_count": first_present(cmt.get("sub_comment_count"), cmt.get("reply_count")),
            "ip_location": cmt.get("ip_location"),
            # 不保留评论者昵称、头像、用户 ID
        }
        if inline_subs:
            out["sub_comments_inline"] = inline_subs
        if isinstance(sub_cur, dict):
            out["sub_comment_cursor"] = {"cursor": sub_cur.get("cursor"), "index": sub_cur.get("index")}
        return out

    def _result(self, items, next_token, meta, warnings) -> dict:
        meta = dict(meta)
        meta["retrieved_at"] = now_iso()
        meta["series"] = "app_v2"
        meta["verified_against_docs"] = "2026-09-18 OpenAPI 描述；各端点历史实测范围见 references/tikhub.md §7"
        meta["response_mode"] = "dry_run" if self.dry_run else self.response_mode
        return {"items": items, "next_page_token": next_token, "source_meta": meta, "warnings": warnings, "usage_record": self.budget.record()}


# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    configure_console()
    ap = argparse.ArgumentParser(description="TikHub 小红书 App V2 适配器")
    ap.add_argument("cmd", choices=["search", "note", "video", "comments", "replies"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--budget", type=int, default=40, help="本次请求上限（用户已同意的数字）")
    ap.add_argument("--usage-file", help="本次旅行共享消费账本路径；真实调用必填")
    ap.add_argument("--sort", default=None)
    ap.add_argument("--time-filter", default="不限")
    ap.add_argument("--cursor", default="", help="replies：起始游标（取自一级评论的 sub_comment_cursor.cursor）")
    ap.add_argument("--index", type=int, default=1, help="replies：起始 index（取自 sub_comment_cursor.index）")
    ap.add_argument("--token-env", default="TIKHUB_API_KEY")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    token = None if a.dry_run else os.environ.get(a.token_env)
    needed = 2 if a.cmd == "replies" else 1
    if len(a.args) < needed or a.pages < 1 or a.budget < 0:
        ap.error("参数不足或页数/预算无效")
    if not a.dry_run and not a.usage_file:
        ap.error("真实调用必须指定同一行程共享的 --usage-file")
    if a.out and a.usage_file:
        out_path = os.path.normcase(os.path.realpath(a.out))
        if out_path in {os.path.normcase(os.path.realpath(a.usage_file)), os.path.normcase(os.path.realpath(a.usage_file + '.lock'))}:
            ap.error("结果文件不能覆盖消费账本或其锁文件")
    try:
        budget = Budget(a.budget) if a.dry_run else PersistentBudget(a.budget, a.usage_file)
    except (ValueError, OSError, TikHubError):
        print("✖ 消费账本不可用、已被锁定或预算不一致；未发起请求", file=sys.stderr)
        return 4
    client = Client(token, budget, dry_run=a.dry_run, log=lambda m: print(m, file=sys.stderr))
    try:
        if a.cmd == "search":
            if not a.args:
                raise SystemExit("需要关键词")
            res = client.search(" ".join(a.args), pages=a.pages, sort_type=a.sort or "general", time_filter=a.time_filter)
        elif a.cmd in ("note", "video"):
            res = client.note_detail(a.args[0], video=(a.cmd == "video"))
        elif a.cmd == "comments":
            res = client.comments(a.args[0], pages=a.pages, sort_strategy=a.sort or "latest_v2")
        else:
            res = client.sub_comments(a.args[0], a.args[1], pages=a.pages, start_cursor=a.cursor, start_index=a.index)
    except AuthError as e:
        print(f"✖ {e}", file=sys.stderr)
        res = {"items": [], "next_page_token": None, "source_meta": {"endpoint": a.cmd}, "warnings": [str(e)], "usage_record": budget.record()}
        _emit(res, a.out)
        return 3
    except BudgetExceeded as e:
        print(f"✖ {e}", file=sys.stderr)
        _emit({"items": [], "warnings": [str(e)], "usage_record": budget.record()}, a.out)
        return 4
    except TikHubError as e:
        print(f"✖ {e}", file=sys.stderr)
        res = {"items": [], "next_page_token": None, "source_meta": {"endpoint": a.cmd}, "warnings": [str(e)], "usage_record": budget.record()}
        _emit(res, a.out)
        return 5
    finally:
        budget.close()
    _emit(res, a.out)
    print(f"✔ {a.cmd}: {len(res['items'])} 条 · 请求 {budget.used}/{budget.limit} · 可能计费 {budget.billable_estimate} · 费用未知 {budget.unknown_cost}", file=sys.stderr)
    return 0


def _emit(res: dict, out: str | None) -> None:
    text = json.dumps(res, ensure_ascii=False, indent=2)
    if out:
        atomic_json(out, res)
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
