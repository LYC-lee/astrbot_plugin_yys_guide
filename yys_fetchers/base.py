"""抓取器基类。

约定：任何抓取器都不允许把异常抛给上层，失败就返回空列表并记 warning，
整个插件不能因为某一个平台挂了就整体不可用。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from yys_core.http import HttpClient
from yys_core.models import GuideItem


class BaseFetcher:
    source: str = "base"
    #: 单次抓取最多补抓多少条正文，控制请求数防止风控
    enrich_limit: int = 3

    def __init__(self, client: HttpClient, conf: dict[str, Any] | None = None):
        self.client = client
        self.conf = conf or {}

    async def fetch_author(self, target: dict[str, Any], limit: int) -> list[GuideItem]:
        """按博主抓取。target 来自 core_authors 配置项。"""
        return []

    async def search(self, keyword: str, limit: int) -> list[GuideItem]:
        """按关键词搜索。"""
        return []

    async def enrich(self, items: list[GuideItem]) -> list[GuideItem]:
        """补充正文/详情，默认不做。"""
        return items

    # 子类用来统一兜底
    async def safe(self, coro, label: str) -> list[GuideItem]:
        try:
            result = await coro
            return result or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] {self.source} {label} 抓取失败：{exc!r}")
            return []
