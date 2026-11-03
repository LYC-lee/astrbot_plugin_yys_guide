"""微博：按 uid 拉时间线 + 关键词搜索。

走 m.weibo.cn 的 container 接口，游客态大部分情况可读；
isLongText 的条目再补一次 extend 接口拿全文。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

from yys_core.http import parse_cookie
from yys_core.models import CN_TZ, GuideItem, clean_text
from yys_fetchers.base import BaseFetcher

_INDEX_URL = "https://m.weibo.cn/api/container/getIndex"
_EXTEND_URL = "https://m.weibo.cn/statuses/extend"

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Wed Nov 04 12:00:00 +0800 2025
_FULL_TIME_RE = re.compile(
    r"([A-Z][a-z]{2})\s+(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\s+([+-]\d{4})\s+(\d{4})"
)
_MINUTE_AGO_RE = re.compile(r"(\d+)\s*分钟前")
_HOUR_AGO_RE = re.compile(r"(\d+)\s*小时前")


def parse_weibo_time(raw: str | None, now: datetime | None = None) -> datetime | None:
    """微博的 created_at 有四五种写法，逐个兼容。"""
    if not raw:
        return None
    raw = str(raw).strip()
    now = now or datetime.now(CN_TZ)

    match = _FULL_TIME_RE.search(raw)
    if match:
        mon, day, hour, minute, second, tz, year = match.groups()
        try:
            offset_hours = int(tz[1:3])
            tzinfo = timezone(timedelta(hours=offset_hours if tz[0] == "+" else -offset_hours))
            return datetime(
                int(year), _MONTHS[mon], int(day), int(hour), int(minute), int(second),
                tzinfo=tzinfo,
            )
        except (KeyError, ValueError):
            return None

    if raw == "刚刚":
        return now
    match = _MINUTE_AGO_RE.search(raw)
    if match:
        return now - timedelta(minutes=int(match.group(1)))
    match = _HOUR_AGO_RE.search(raw)
    if match:
        return now - timedelta(hours=int(match.group(1)))

    if raw.startswith("今天"):
        clock = re.search(r"(\d{1,2}):(\d{2})", raw)
        if clock:
            return now.replace(
                hour=int(clock.group(1)), minute=int(clock.group(2)), second=0, microsecond=0
            )
    if raw.startswith("昨天"):
        clock = re.search(r"(\d{1,2}):(\d{2})", raw)
        base = now - timedelta(days=1)
        if clock:
            return base.replace(
                hour=int(clock.group(1)), minute=int(clock.group(2)), second=0, microsecond=0
            )

    short = re.search(r"(\d{1,2})-(\d{1,2})", raw)
    if short:
        try:
            candidate = now.replace(
                month=int(short.group(1)), day=int(short.group(2)),
                hour=0, minute=0, second=0, microsecond=0,
            )
            if candidate > now:
                candidate = candidate.replace(year=candidate.year - 1)
            return candidate
        except ValueError:
            return None
    return None


class WeiboFetcher(BaseFetcher):
    source = "weibo"
    enrich_limit = 2

    def _cookies(self) -> dict[str, str]:
        return parse_cookie(str(self.conf.get("weibo") or ""))

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "Referer": referer,
            "MWeibo-Pwa": "1",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _log_blocked(self, exc: Exception, label: str) -> None:
        """微博对无 cookie 的请求返回 432，直接说清楚要填什么。"""
        message = repr(exc)
        if any(code in message for code in ("432", "403", "418", "401")):
            logger.warning(
                f"[yys-guide] 微博拒绝了游客请求（{label}，需要登录态）。"
                "请把 m.weibo.cn 的 SUB 值填到插件配置的 cookies.weibo。"
            )
        else:
            logger.warning(f"[yys-guide] 微博{label}失败：{exc!r}")

    # ---------------- 按博主 ----------------

    async def fetch_author(self, target: dict[str, Any], limit: int,
        keywords: list[str] | None = None,
    ) -> list[GuideItem]:
        uid = str(target.get("uid") or "").strip()
        if not uid.isdigit():
            logger.warning(f"[yys-guide] 微博 uid 无效：{uid!r}（需要数字 uid），已跳过")
            return []

        try:
            data = await self.client.get_json(
                _INDEX_URL,
                params={"containerid": f"107603{uid}", "page": 1},
                cookies=self._cookies(),
                headers=self._headers(f"https://m.weibo.cn/u/{uid}"),
            )
        except Exception as exc:  # noqa: BLE001
            self._log_blocked(exc, f"时间线 uid={uid}", "https://m.weibo.cn/u/" + uid)
            return []

        if (data or {}).get("ok") != 1:
            logger.warning(f"[yys-guide] 微博接口未返回数据 uid={uid}")
            return []

        cards = ((data.get("data") or {}).get("cards")) or []
        items: list[GuideItem] = []
        for card in cards:
            mblog = card.get("mblog") if isinstance(card, dict) else None
            if not isinstance(mblog, dict):
                continue
            item = self._to_item(mblog, uid)
            if item:
                items.append(item)
            if len(items) >= limit:
                break
        return items

    def _to_item(self, mblog: dict[str, Any], uid: str) -> GuideItem | None:
        mid = mblog.get("id") or mblog.get("mid")
        if not mid:
            return None
        text = clean_text(mblog.get("text"))
        if not text:
            return None

        title = text.split("\n", 1)[0][:80]
        pics = mblog.get("pics") or []
        cover = ""
        if pics and isinstance(pics[0], dict):
            cover = pics[0].get("large", {}).get("url") or pics[0].get("url") or ""

        user = mblog.get("user") or {}
        return GuideItem(
            source=self.source,
            item_id=str(mid),
            title=title,
            url=f"https://m.weibo.cn/detail/{mid}",
            author=clean_text(user.get("screen_name")) if isinstance(user, dict) else "",
            published=parse_weibo_time(mblog.get("created_at")),
            digest=text,
            content="",
            cover=cover,
            meta={
                "type": "mblog",
                "long": bool(mblog.get("isLongText")),
                "reposts": mblog.get("reposts_count"),
                "comments": mblog.get("comments_count"),
            },
        )

    # ---------------- 搜索 ----------------

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        if not keyword.strip():
            return []
        containerid = f"100103type=1&q={keyword}"
        try:
            data = await self.client.get_json(
                _INDEX_URL,
                params={"containerid": containerid, "page_type": "searchall"},
                cookies=self._cookies(),
                headers=self._headers("https://m.weibo.cn/"),
            )
        except Exception as exc:  # noqa: BLE001
            self._log_blocked(exc, f"搜索「{keyword}」")
            return []

        if (data or {}).get("ok") != 1:
            logger.debug(f"[yys-guide] 微博搜索无结果「{keyword}」（多数情况下需要 Cookie）")
            return []

        cards = ((data.get("data") or {}).get("cards")) or []
        items: list[GuideItem] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            # 搜索结果可能包在 card_group 里
            group = card.get("card_group") or [card]
            for sub in group:
                mblog = sub.get("mblog") if isinstance(sub, dict) else None
                if not isinstance(mblog, dict):
                    continue
                uid = str((mblog.get("user") or {}).get("id") or "")
                item = self._to_item(mblog, uid)
                if item:
                    items.append(item)
                if len(items) >= limit:
                    return items
        return items

    # ---------------- 补长文 ----------------

    async def enrich(self, items: list[GuideItem]) -> list[GuideItem]:
        targets = [item for item in items if item.meta.get("long")][: self.enrich_limit]
        for item in targets:
            try:
                data = await self.client.get_json(
                    _EXTEND_URL,
                    params={"id": item.item_id},
                    cookies=self._cookies(),
                    headers=self._headers(item.url),
                )
                long_text = clean_text((data or {}).get("data", {}).get("longTextContent"))
                if long_text:
                    item.content = long_text
                    item.digest = long_text
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[yys-guide] 微博长文补全失败 {item.item_id}：{exc!r}")
        return items
