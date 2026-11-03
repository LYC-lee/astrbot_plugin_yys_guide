"""抖音：只做「链接 -> 文案/封面」解析，不做视频转写。

抖音的 web 接口需要 a_bogus 签名，插件里不实现这套逆向，
所以策略很明确：
- try_meta：能解析出文案、作者、封面就用上，解析失败不报错，退回只给链接；
- link_only：直接只给标题和链接。

视频内容本身（画面/口播）不转写，token 消耗不值得。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger

from yys_core.models import GuideItem, clean_text
from yys_fetchers.base import BaseFetcher

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_URL_RE = re.compile(r"https?://[^\s，,。；;、）)\]]+")
_AWEME_ID_RE = re.compile(r"(?:/video/|/note/|modal_id=|/share/video/)(\d{8,})")
_DESC_RE = re.compile(r'"desc"\s*:\s*"((?:[^"\\]|\\.){4,400})"')
_NICK_RE = re.compile(r'"nickname"\s*:\s*"((?:[^"\\]|\\.){1,60})"')
_COVER_RE = re.compile(r'"cover"\s*:\s*\{[^{}]*?"url_list"\s*:\s*\[\s*"(https?:[^"]+)"')
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


class DouyinFetcher(BaseFetcher):
    source = "douyin"

    @property
    def mode(self) -> str:
        return str(self.conf.get("douyin_mode") or "try_meta").strip()

    # ---------------- 对外 ----------------

    def extract_links(self, text: str) -> list[str]:
        return _URL_RE.findall(text or "")

    async def fetch_by_url(self, url: str) -> GuideItem | None:
        aweme_id = await self._resolve_aweme_id(url)
        if not aweme_id:
            if self.mode == "link_only":
                return self._link_only(url)
            logger.debug(f"[yys-guide] 抖音链接无法解析出 aweme_id：{url}")
            return self._link_only(url)

        if self.mode == "link_only":
            return self._link_only(f"https://www.douyin.com/video/{aweme_id}", aweme_id)

        item = await self._fetch_meta(aweme_id)
        return item or self._link_only(f"https://www.douyin.com/video/{aweme_id}", aweme_id)

    async def fetch_author(self, target: dict[str, Any], limit: int,
        keywords: list[str] | None = None,
    ) -> list[GuideItem]:
        # 主页作品列表需要签名，这里只在配置里给了作品链接时解析单条
        url = str(target.get("url") or target.get("share_url") or "").strip()
        if not url:
            return []
        item = await self.fetch_by_url(url)
        return [item] if item else []

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        # 抖音搜索接口需要签名，不实现
        return []

    # ---------------- 内部 ----------------

    async def _resolve_aweme_id(self, url: str) -> str | None:
        match = _AWEME_ID_RE.search(url)
        if match:
            return match.group(1)

        if "v.douyin.com" not in url and "douyin.com" not in url:
            return None

        try:
            resp = await self.client.request(
                "GET",
                url,
                follow_redirects=False,
                headers={"User-Agent": _MOBILE_UA},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] 抖音短链请求失败：{exc!r}")
            return None

        location = resp.headers.get("location") or ""
        match = _AWEME_ID_RE.search(location)
        if match:
            return match.group(1)

        # 少数情况返回的是带 js 跳转的页面
        match = _AWEME_ID_RE.search(resp.text or "")
        return match.group(1) if match else None

    async def _fetch_meta(self, aweme_id: str) -> GuideItem | None:
        candidates = [
            f"https://www.iesdouyin.com/share/video/{aweme_id}/",
            f"https://www.douyin.com/video/{aweme_id}",
        ]
        for page_url in candidates:
            try:
                resp = await self.client.request(
                    "GET", page_url, headers={"User-Agent": _MOBILE_UA}
                )
                if resp.status_code != 200:
                    continue
                html = unquote(resp.text or "")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[yys-guide] 抖音页面抓取失败 {page_url}：{exc!r}")
                continue

            desc = ""
            match = _DESC_RE.search(html)
            if match:
                desc = self._unescape(match.group(1))

            nick = ""
            match = _NICK_RE.search(html)
            if match:
                nick = self._unescape(match.group(1))

            cover = ""
            match = _COVER_RE.search(html)
            if match:
                cover = match.group(1).replace("\\u002F", "/")

            if not desc:
                match = _TITLE_TAG_RE.search(html)
                if match:
                    desc = clean_text(match.group(1)).replace("- 抖音", "").strip()

            if desc:
                return GuideItem(
                    source=self.source,
                    item_id=aweme_id,
                    title=desc[:80],
                    url=f"https://www.douyin.com/video/{aweme_id}",
                    author=clean_text(nick),
                    published=None,
                    digest=desc,
                    cover=cover,
                    meta={"type": "video", "mode": "meta"},
                )
        return None

    def _link_only(self, url: str, aweme_id: str = "") -> GuideItem:
        return GuideItem(
            source=self.source,
            item_id=aweme_id or url,
            title="抖音视频（未能解析文案，点开看原视频）",
            url=url,
            author="",
            published=None,
            digest="",
            meta={"type": "video", "mode": "link_only"},
        )

    @staticmethod
    def _unescape(raw: str) -> str:
        text = raw.replace("\\u002F", "/").replace('\\"', '"').replace("\\n", " ")
        return clean_text(text)
