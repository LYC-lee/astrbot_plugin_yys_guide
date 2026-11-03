"""B站：空间投稿视频 + 专栏 + 全站搜索。

实测结论（2026-11）：
- 空间投稿：`x/space/arc/search`（不带 wbi）只要有游客会话 cookie 就能用，
  返回 200；而 `x/space/wbi/arc/search` 对游客几乎必然 412 / -352。
  所以按作者抓取优先走旧接口，失败再降级到 wbi 签名版。
- 全站搜索：`x/web-interface/search/type` 只需 buvid3，风控宽松。
- 空间接口单位时间配额很低，连续请求会返回 -799（请求过于频繁），
  因此同一抓取器内强制冷却 _SPACE_COOLDOWN 秒。
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime
from typing import Any

from astrbot.api import logger

from yys_core.bili_wbi import WbiSigner
from yys_core.http import parse_cookie
from yys_core.models import CN_TZ, GuideItem, clean_text
from yys_fetchers.base import BaseFetcher

_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_SPACE_VIDEO_PLAIN_URL = "https://api.bilibili.com/x/space/arc/search"
_SPACE_VIDEO_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
_SPACE_ARTICLE_URL = "https://api.bilibili.com/x/space/wbi/article"
_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

# 空间接口是 IP 级配额，实测连续请求十几秒内就会 -799 / 412，
# 两次之间至少隔这么久；抓取器里还会用搜索接口兜底。
_SPACE_COOLDOWN = 8.0
# 排队超过这么久就别等了，直接走搜索兜底：宁可少拿几条，
# 也不能让一次聊天查询卡 30 秒
_SPACE_MAX_WAIT = 3.0


def _norm_url(url: str | None) -> str:
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def _ts_to_dt(value: Any) -> datetime | None:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, CN_TZ)


class BilibiliFetcher(BaseFetcher):
    source = "bilibili"
    enrich_limit = 3

    def __init__(self, client, conf: dict[str, Any] | None = None):
        super().__init__(client, conf)
        self.signer = WbiSigner(client)
        self._cookies: dict[str, str] | None = None
        self._last_space_at = 0.0

    async def _space_throttle(self) -> bool:
        """空间接口有调用配额，两次之间强制冷却，避免 -799。

        返回 False 表示前面还有别人在排队、这轮轮不到：此时不要干等，
        让上层直接走「搜索作者名 + 按 mid 过滤」的兜底路径。
        4 个 B站博主串着等 8 秒冷却是 30 秒起步，用户根本等不了；
        搜索接口风控宽松，同样能拿到这位 UP 的最新投稿。
        """
        import time

        gap = time.monotonic() - self._last_space_at
        if 0 < gap < _SPACE_COOLDOWN:
            wait = _SPACE_COOLDOWN - gap
            if wait > _SPACE_MAX_WAIT:
                logger.debug(f"[yys-guide] B站空间接口还要排 {wait:.1f}s，这次改走搜索兜底")
                return False
            await asyncio.sleep(wait)
        self._last_space_at = time.monotonic()
        return True

    @staticmethod
    def _vlist(data: Any) -> list[dict[str, Any]]:
        return (((data or {}).get("data") or {}).get("list") or {}).get("vlist") or []

    async def _get_cookies(self) -> dict[str, str]:
        if self._cookies is not None:
            return self._cookies

        raw = str(self.conf.get("bilibili") or "").strip()
        if raw:
            self._cookies = parse_cookie(raw)
            logger.debug("[yys-guide] B站使用配置里的 Cookie")
            return self._cookies

        cookies: dict[str, str] = {}
        # 先访问首页建立游客会话，buvid3/b_nut 会通过 Set-Cookie 下发，
        # 空间旧接口靠这个会话才认（只调 spi 拿不到完整会话）。
        try:
            session = await self.client.get_cookies("https://www.bilibili.com/")
            for key in ("buvid3", "buvid4", "b_nut", "_uuid"):
                if session.get(key):
                    cookies[key] = session[key]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] B站首页会话建立失败：{exc!r}")

        try:
            data = await self.client.get_json(_SPI_URL)
            payload = (data or {}).get("data") or {}
            if payload.get("b_3"):
                cookies.setdefault("buvid3", payload["b_3"])
            if payload.get("b_4"):
                cookies.setdefault("buvid4", payload["b_4"])
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] B站游客 cookie 获取失败：{exc!r}")

        self._cookies = cookies
        return cookies

    # ---------------- 按博主 ----------------

    async def fetch_author(self, target: dict[str, Any], limit: int,
        keywords: list[str] | None = None,
    ) -> list[GuideItem]:
        mid = str(target.get("mid") or "").strip()
        if not mid.isdigit():
            logger.warning(f"[yys-guide] B站博主 mid 无效：{mid!r}，已跳过")
            return []
        label = target.get("name") or mid

        videos = await self.safe(self._space_videos(mid, limit), f"空间视频 {label}")
        # 空间接口有 IP 级配额，很容易 -799 / 412。
        # 拿不到就用「搜索作者名 + 按 mid 过滤」兜底——搜索接口风控宽松得多。
        if not videos and target.get("name"):
            logger.debug(f"[yys-guide] B站空间接口不可用，改用搜索兜底：{label}")
            videos = await self.safe(
                self._author_via_search(str(target["name"]), mid, limit), f"搜索兜底 {label}"
            )
        if len(videos) < limit:
            articles = await self.safe(
                self._space_articles(mid, limit - len(videos)), f"空间专栏 {label}"
            )
            videos.extend(articles)
        return videos[:limit]

    async def _author_via_search(self, name: str, mid: str, limit: int) -> list[GuideItem]:
        """按作者名搜索，再用 mid 精确过滤，绕开空间接口的配额限制。"""
        cookies = await self._get_cookies()
        try:
            data = await self.client.get_json(
                _SEARCH_URL,
                params={
                    "search_type": "video",
                    "keyword": name,
                    "page": 1,
                    "order": "pubdate",
                },
                cookies=cookies,
                headers={"Referer": "https://search.bilibili.com/"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] B站搜索兜底失败 {name}：{exc!r}")
            return []

        results = (data or {}).get("data") or {}
        entries = [x for x in (results.get("result") or []) if str(x.get("mid")) == mid]
        entries.sort(key=lambda x: x.get("pubdate") or 0, reverse=True)

        items: list[GuideItem] = []
        for entry in entries[: max(1, limit)]:
            bvid = entry.get("bvid")
            if not bvid:
                continue
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=bvid,
                    title=clean_text(entry.get("title")),
                    url=f"https://www.bilibili.com/video/{bvid}",
                    author=clean_text(entry.get("author")) or name,
                    published=_ts_to_dt(entry.get("pubdate")),
                    digest=clean_text(entry.get("description")),
                    cover=_norm_url(entry.get("pic")),
                    meta={"type": "video", "play": entry.get("play"), "via": "search"},
                )
            )
        return items

    async def _space_videos(self, mid: str, limit: int) -> list[GuideItem]:
        cookies = await self._get_cookies()
        size = min(30, max(1, limit))
        headers = {"Referer": f"https://space.bilibili.com/{mid}/video"}

        data: Any = None
        # 优先走不带 wbi 的旧接口：游客态可用，风控明显更松
        if not await self._space_throttle():
            return []
        try:
            data = await self.client.get_json(
                _SPACE_VIDEO_PLAIN_URL,
                params={"mid": mid, "ps": size, "pn": 1, "order": "pubdate"},
                cookies=cookies,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] B站空间旧接口失败，转 wbi：{exc!r}")
            data = None

        if not ((data or {}).get("code") == 0 and self._vlist(data)):
            # 降级：wbi 签名版（有登录 Cookie 时更稳）
            if not await self._space_throttle():
                return []
            params = {
                "mid": mid,
                "ps": size,
                "pn": 1,
                "order": "pubdate",
                "index": 0,
                "platform": "web",
                "web_location": "1550101",
            }
            signed = await self.signer.sign(params)
            try:
                data = await self.client.get_json(
                    _SPACE_VIDEO_URL, params=signed, cookies=cookies, headers=headers
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] B站空间视频接口请求失败 mid={mid}：{exc!r}")
                return []

        code = (data or {}).get("code")
        if code != 0:
            logger.warning(
                f"[yys-guide] B站空间视频接口返回 code={code}：{(data or {}).get('message')}"
            )
            return []

        vlist = self._vlist(data)
        items: list[GuideItem] = []
        for entry in vlist:
            bvid = entry.get("bvid")
            if not bvid:
                continue
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=bvid,
                    title=clean_text(entry.get("title")),
                    url=f"https://www.bilibili.com/video/{bvid}",
                    author=clean_text(entry.get("author")),
                    published=_ts_to_dt(entry.get("created")),
                    digest=clean_text(entry.get("description")),
                    cover=_norm_url(entry.get("pic")),
                    meta={"type": "video", "play": entry.get("play")},
                )
            )
        return items

    async def _space_articles(self, mid: str, limit: int) -> list[GuideItem]:
        cookies = await self._get_cookies()
        params = {
            "mid": mid,
            "pn": 1,
            "ps": min(30, max(1, limit)),
            "sort": "publish_time",
        }
        signed = await self.signer.sign(params)
        data = await self.client.get_json(
            _SPACE_ARTICLE_URL,
            params=signed,
            cookies=cookies,
            headers={"Referer": f"https://space.bilibili.com/{mid}/article"},
        )
        code = (data or {}).get("code")
        if code != 0:
            logger.debug(f"[yys-guide] B站专栏接口返回 code={code}（通常需要登录态），跳过")
            return []

        articles = (data.get("data") or {}).get("articles") or []
        items: list[GuideItem] = []
        for entry in articles:
            cv_id = entry.get("id")
            if not cv_id:
                continue
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=f"cv{cv_id}",
                    title=clean_text(entry.get("title")),
                    url=f"https://www.bilibili.com/read/cv{cv_id}",
                    author=clean_text(
                        ((entry.get("author") or {}).get("name")) or entry.get("author_name")
                    ),
                    published=_ts_to_dt(entry.get("publish_time") or entry.get("ctime")),
                    digest=clean_text(entry.get("summary")),
                    cover=_norm_url(entry.get("banner_url")),
                    meta={"type": "article"},
                )
            )
        return items

    # ---------------- 搜索 ----------------

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        if not keyword.strip():
            return []
        cookies = await self._get_cookies()
        params = {
            "search_type": "video",
            "keyword": keyword,
            "order": "pubdate",
            "page": 1,
            "page_size": min(30, max(1, limit * 2)),
        }
        signed = await self.signer.sign(params)
        referer = "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(keyword)

        data = await self.client.get_json(
            _SEARCH_URL, params=signed, cookies=cookies, headers={"Referer": referer}
        )
        code = (data or {}).get("code")
        if code != 0:
            logger.warning(f"[yys-guide] B站搜索接口返回 code={code}：{data.get('message')}")
            return []

        results = (data.get("data") or {}).get("result") or []
        items: list[GuideItem] = []
        for entry in results:
            bvid = entry.get("bvid")
            # 优先用 bvid 拼标准链接，arcurl 是老式的 av 号地址
            url = (
                f"https://www.bilibili.com/video/{bvid}"
                if bvid
                else _norm_url(entry.get("arcurl") or "")
            )
            if not url:
                continue
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=bvid or url,
                    title=clean_text(entry.get("title")),
                    url=_norm_url(url),
                    author=clean_text(entry.get("author")),
                    published=_ts_to_dt(entry.get("pubdate")),
                    digest=clean_text(entry.get("description")),
                    cover=_norm_url(entry.get("pic")),
                    meta={"type": "video", "play": entry.get("play")},
                )
            )
        return items

    # ---------------- 补正文 ----------------

    async def enrich(self, items: list[GuideItem]) -> list[GuideItem]:
        targets = [
            item
            for item in items
            if item.meta.get("type") == "video" and not item.content
        ][: self.enrich_limit]
        if not targets:
            return items
        await asyncio.gather(
            *(self._fill_video_desc(item) for item in targets), return_exceptions=True
        )
        return items

    async def _fill_video_desc(self, item: GuideItem) -> None:
        try:
            cookies = await self._get_cookies()
            data = await self.client.get_json(
                _VIEW_URL,
                params={"bvid": item.item_id},
                cookies=cookies,
                headers={"Referer": item.url},
            )
            payload = (data or {}).get("data") or {}
            desc = clean_text(payload.get("desc"))
            if desc:
                item.content = desc
            if not item.published:
                item.published = _ts_to_dt(payload.get("pubdate"))
            if not item.digest and payload.get("title"):
                item.digest = clean_text(payload.get("title"))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] B站视频详情补充失败 {item.item_id}：{exc!r}")
