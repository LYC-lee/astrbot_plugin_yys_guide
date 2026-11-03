"""搜索引擎兜底。

实测结论（2026-11）：
- 360 搜索（so.com）对无 cookie 的请求返回正常中文结果，且 <a> 上带 data-mdurl
  给出真实地址，所以作为默认引擎；
- Bing 对无 cookie 的中文查询会退化成完全不相关的结果（搜「阴阳师御魂攻略」
  返回「阴阳」百科），因此只保留作可选，并在结果里做一次基本的相关性过滤；
- SearXNG 需要自建，作为不想用商业引擎时的替代。

核心博主当天没更新、或者用户问的话题谁都没写过时，用这里补上。
"""

from __future__ import annotations

import html
import re
from typing import Any

from astrbot.api import logger

from yys_core.models import GuideItem, clean_text
from yys_fetchers.base import BaseFetcher

_H3_RE = re.compile(r"<h3[^>]*class=\"res-title", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')
_MDURL_RE = re.compile(r'data-mdurl="([^"]+)"')
_DESC_RE = re.compile(r'<p[^>]*class="res-desc"[^>]*>(.*?)</p>', re.S)
_SUMMARY_RE = re.compile(r"data-result-summary[^>]*>(.*?)</span>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

_BING_BLOCK_SEP = '<li class="b_algo"'
_BING_TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_DESC_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 结果标题里至少要沾一点这些词，否则判定为引擎跑偏（Bing 常见）
_RELEVANCE_WORDS = ("阴阳师", "御魂", "式神", "yys", "副本", "阵容", "逢魔", "秘闻")


def _strip_tags(raw: str) -> str:
    return clean_text(html.unescape(_TAG_RE.sub("", raw)))


class WebSearchFetcher(BaseFetcher):
    source = "web"

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        engine = str(self.conf.get("engine") or "so360").lower()
        if engine == "none":
            return []

        if engine == "searxng":
            return await self._searxng(keyword, limit)
        if engine == "bing":
            return await self._bing(keyword, limit)
        return await self._so360(keyword, limit)

    # ---------------- 360 搜索 ----------------

    async def _so360(self, keyword: str, limit: int) -> list[GuideItem]:
        query = self._build_query(keyword)
        try:
            resp = await self.client.request(
                "GET", "https://www.so.com/s", params={"q": query}, headers=_HEADERS
            )
            resp.raise_for_status()
            page = resp.text
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] 360 搜索失败「{query}」：{exc!r}")
            return []

        starts = [match.start() for match in _H3_RE.finditer(page)]
        items: list[GuideItem] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(page)
            block = page[start:end]

            head = block[: block.find("</h3>") + 5] if "</h3>" in block else block[:1200]
            title = _strip_tags(head)
            if not title:
                continue

            url = ""
            md_match = _MDURL_RE.search(head)
            if md_match:
                url = html.unescape(md_match.group(1))
            if not url:
                href_match = _HREF_RE.search(head)
                if href_match:
                    url = html.unescape(href_match.group(1))
            if not url.startswith("http"):
                continue

            digest = ""
            summary_match = _SUMMARY_RE.search(block)
            if summary_match:
                digest = _strip_tags(summary_match.group(1))
            if not digest:
                desc_match = _DESC_RE.search(block)
                if desc_match:
                    digest = _strip_tags(desc_match.group(1))

            items.append(
                GuideItem(
                    source=self.source,
                    item_id=url,
                    title=title,
                    url=url,
                    author="",
                    published=None,
                    digest=digest,
                    meta={"type": "web", "engine": "so360"},
                )
            )
            if len(items) >= limit:
                break
        return items

    # ---------------- Bing（可选） ----------------

    async def _bing(self, keyword: str, limit: int) -> list[GuideItem]:
        query = self._build_query(keyword)
        try:
            resp = await self.client.request(
                "GET",
                "https://cn.bing.com/search",
                params={"q": query, "count": max(10, limit * 3), "mkt": "zh-CN"},
                headers=_HEADERS,
            )
            resp.raise_for_status()
            page = resp.text
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] Bing 搜索失败「{query}」：{exc!r}")
            return []

        items: list[GuideItem] = []
        dropped = 0
        for block in page.split(_BING_BLOCK_SEP)[1:]:
            title_match = _BING_TITLE_RE.search(block)
            if not title_match:
                continue
            url = html.unescape(title_match.group(1)).strip()
            title = _strip_tags(title_match.group(2))
            if not title or not url.startswith("http"):
                continue

            digest = ""
            desc_match = _BING_DESC_RE.search(block)
            if desc_match:
                digest = _strip_tags(desc_match.group(1))

            # Bing 在无 cookie 时会把中文查询降级成完全无关的结果，先挡掉
            if not any(word in title for word in _RELEVANCE_WORDS):
                dropped += 1
                continue

            items.append(
                GuideItem(
                    source=self.source,
                    item_id=url,
                    title=title,
                    url=url,
                    author="",
                    published=None,
                    digest=digest,
                    meta={"type": "web", "engine": "bing"},
                )
            )
            if len(items) >= limit:
                break

        if dropped and not items:
            logger.warning(
                "[yys-guide] Bing 返回的结果与查询无关（无 cookie 时常见），"
                "建议把 search.engine 换成 so360"
            )
        return items

    # ---------------- SearXNG ----------------

    async def _searxng(self, keyword: str, limit: int) -> list[GuideItem]:
        base = str(self.conf.get("searxng_url") or "").strip().rstrip("/")
        if not base:
            logger.warning("[yys-guide] 选了 SearXNG 但没填 searxng_url")
            return []
        query = self._build_query(keyword)
        try:
            data = await self.client.get_json(
                f"{base}/search",
                params={"q": query, "format": "json", "language": "zh-CN"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] SearXNG 搜索失败：{exc!r}")
            return []

        results = (data or {}).get("results") or []
        items: list[GuideItem] = []
        for entry in results:
            url = entry.get("url")
            title = _strip_tags(entry.get("title") or "")
            if not url or not title:
                continue
            items.append(
                GuideItem(
                    source=self.source,
                    item_id=url,
                    title=title,
                    url=url,
                    author=clean_text(entry.get("author")),
                    published=None,
                    digest=_strip_tags(entry.get("content") or ""),
                    meta={"type": "web", "engine": "searxng"},
                )
            )
            if len(items) >= limit:
                break
        return items

    # ---------------- 工具 ----------------

    @staticmethod
    def _build_query(keyword: str) -> str:
        if "阴阳师" in keyword or "yys" in keyword.lower():
            return keyword
        return f"阴阳师 {keyword} 攻略"
