"""NGA 阴阳师版。

NGA 的内容接口用 `__output=11` 返回结构化 JSON，游客态需要 cookie 里的
guestJs，否则会被重定向到验证页。按作者抓取需要登录 Cookie，游客态下
这里退化为「拉版块 + 本地按标题过滤」。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger

from yys_core.http import parse_cookie
from yys_core.models import CN_TZ, GuideItem, clean_text
from yys_fetchers.base import BaseFetcher

_BOARD_URL = "https://bbs.nga.cn/thread.php"
_READ_URL = "https://bbs.nga.cn/read.php"

# 阴阳师主讨论区（582 是互助招募，703 是伊吹茶馆，都不是攻略区）
_DEFAULT_FID = "538"

# 这些关键词太宽，等于不过滤
_GENERIC_KEYWORDS = {"阴阳师", "yys", "痒痒鼠", "onmyoji", "攻略"}


def _strip_title_tags(subject: str) -> str:
    """NGA 标题常带 [攻略] [数据] 这种前缀，保留但压平空白。"""
    return clean_text(subject)


def _parse_postdate(value: Any) -> datetime | None:
    """postdate 有时是 int，有时是 {"0": ts, ...}，都兼容一下。"""
    if isinstance(value, dict):
        for key in ("0", 0):
            if key in value:
                value = value[key]
                break
        else:
            if value:
                value = next(iter(value.values()))
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, CN_TZ)


class NgaFetcher(BaseFetcher):
    source = "nga"
    enrich_limit = 2
    pages = 3

    def __init__(self, client, conf: dict[str, Any] | None = None):
        super().__init__(client, conf)
        self.fid = str(self.conf.get("nga_fid") or _DEFAULT_FID)
        self._cookies: dict[str, str] | None = None

    async def _get_cookies(self) -> dict[str, str]:
        if self._cookies is not None:
            return self._cookies
        raw = str(self.conf.get("nga") or "").strip()
        if raw:
            self._cookies = parse_cookie(raw)
        else:
            self._cookies = {"guestJs": str(int(time.time()))}
        return self._cookies

    # ---------------- 抓取 ----------------

    async def fetch_author(self, target: dict[str, Any], limit: int) -> list[GuideItem]:
        # NGA 不提供游客可用的作者检索，退化成按话题抓版块
        topic = (target.get("name") or "").strip()
        return await self.search(topic or "阴阳师", limit)

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        keyword = (keyword or "").strip()
        cookies = await self._get_cookies()
        collected: dict[str, GuideItem] = {}

        # 传进来的可能是「魂土 御魂」这种多词组合，命中任意一个就算相关
        tokens = [
            token
            for token in re.split(r"[\s,，]+", keyword)
            if token and token not in _GENERIC_KEYWORDS
        ]

        for page in range(1, self.pages + 1):
            try:
                data = await self.client.get_json(
                    _BOARD_URL,
                    params={"fid": self.fid, "page": page, "__output": "11"},
                    cookies=cookies,
                    headers={"Referer": f"https://bbs.nga.cn/thread.php?fid={self.fid}"},
                )
            except Exception as exc:  # noqa: BLE001
                message = repr(exc)
                if "403" in message or "401" in message:
                    logger.warning(
                        "[yys-guide] NGA 拒绝了游客访问（403「访客不能直接访问」）。"
                        "请把浏览器里 bbs.nga.cn 的登录 Cookie 填到插件配置的 cookies.nga。"
                    )
                else:
                    logger.warning(f"[yys-guide] NGA 版块第 {page} 页抓取失败：{exc!r}")
                break

            entries = self._parse_threads(data)
            if not entries:
                break

            for entry in entries:
                item = self._to_item(entry)
                if item is None:
                    continue
                if tokens:
                    haystack = f"{item.title} {item.digest}".lower()
                    if not any(token.lower() in haystack for token in tokens):
                        continue
                collected[item.item_id] = item

            if len(collected) >= limit:
                break

        items = sorted(collected.values(), key=lambda i: i.published_ts, reverse=True)
        return items[:limit]

    def _parse_threads(self, data: Any) -> list[dict[str, Any]]:
        payload = (data or {}).get("data")
        if not isinstance(payload, dict):
            return []
        order = payload.get("__T") or []
        entries: list[dict[str, Any]] = []
        for row_id in order:
            node = payload.get(str(row_id))
            if not isinstance(node, dict):
                continue
            # 结构可能是 {tid:..., ...} 也可能是 {"0": {...}}
            entry = node
            if "tid" not in entry and isinstance(node.get("0"), dict):
                entry = node["0"]
            if entry.get("tid"):
                entries.append(entry)
        return entries

    def _to_item(self, entry: dict[str, Any]) -> GuideItem | None:
        tid = entry.get("tid")
        subject = _strip_title_tags(str(entry.get("subject") or ""))
        if not tid or not subject:
            return None

        author = entry.get("author") or ""
        if isinstance(author, dict):
            author = author.get("username") or author.get("uid") or ""

        replies = entry.get("replies")
        digest = ""
        if replies not in (None, ""):
            try:
                digest = f"NGA 讨论帖 · {int(replies)} 条回复"
            except (TypeError, ValueError):
                digest = "NGA 讨论帖"

        return GuideItem(
            source=self.source,
            item_id=str(tid),
            title=subject,
            url=f"https://bbs.nga.cn/read.php?tid={tid}",
            author=clean_text(str(author)),
            published=_parse_postdate(entry.get("postdate") or entry.get("lastpost")),
            digest=digest,
            meta={"type": "thread"},
        )

    # ---------------- 补正文 ----------------

    async def enrich(self, items: list[GuideItem]) -> list[GuideItem]:
        targets = [item for item in items if not item.content][: self.enrich_limit]
        for item in targets:
            try:
                data = await self.client.get_json(
                    _READ_URL,
                    params={"tid": item.item_id, "__output": "11"},
                    cookies=await self._get_cookies(),
                    headers={"Referer": item.url},
                )
                content = self._extract_first_post(data)
                if content:
                    item.content = content[:4000]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[yys-guide] NGA 帖子正文抓取失败 {item.item_id}：{exc!r}")
        return items

    def _extract_first_post(self, data: Any) -> str:
        payload = (data or {}).get("data") or {}
        posts = payload.get("__R")
        if isinstance(posts, dict):
            first = posts.get("0") or posts.get(0)
            if isinstance(first, dict):
                return clean_text(
                    str(first.get("post_content") or first.get("content") or "")
                )
        if isinstance(posts, list) and posts:
            first = posts[0]
            if isinstance(first, dict):
                return clean_text(
                    str(first.get("post_content") or first.get("content") or "")
                )
        return ""
