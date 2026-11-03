"""阴阳师攻略助手。

一条消息进来后的流程：
  别名匹配 -> 查缓存 -> （缓存不够才）抓取 -> 写缓存 -> 补正文
  -> LLM 摘要（失败降级规则抽取）-> 渲染成卡片 -> 发送

设计上所有外部依赖都允许失败：某个平台挂了、模型超时、缓存没命中，
最终一定还能给用户一个能看的结果。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, AsyncGenerator

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import AstrBotConfig, logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.star import Context, Star, register  # noqa: E402

from yys_core import aliases  # noqa: E402
from yys_core.cache import GuideCache  # noqa: E402
from yys_core.filters import filter_items  # noqa: E402
from yys_core.http import HttpClient  # noqa: E402
from yys_core.models import (  # noqa: E402
    CN_TZ,
    GuideItem,
    fmt_human_time,
    source_label,
    title_key,
)
from yys_core.render import pick_cover, render  # noqa: E402
from yys_core.scheduler import WarmupScheduler, parse_times  # noqa: E402
from yys_core.summarize import summarize  # noqa: E402
from yys_fetchers import (  # noqa: E402
    BilibiliFetcher,
    DouyinFetcher,
    Ds163Fetcher,
    NgaFetcher,
    WeiboFetcher,
    WebSearchFetcher,
)

PLUGIN_NAME = "astrbot_plugin_yys_guide"

# 泛词不进正则：它们单独出现本来也不会触发，放进去只会增加无谓的 handler 调用
_GENERIC_FOR_QUERY = {"阴阳师", "yys", "痒痒鼠", "onmyoji", "攻略", "最新", "最近"}


def _build_trigger_pattern() -> str:
    words = aliases.all_trigger_words()
    return "(?i)(?:" + "|".join(re.escape(word) for word in words) + ")"


TRIGGER_PATTERN = _build_trigger_pattern()


def _data_dir() -> str:
    return os.path.join("data", "plugin_data", PLUGIN_NAME)


@register(
    PLUGIN_NAME,
    "yys-guide",
    "阴阳师攻略助手：关键词触发，自动抓取博主攻略（B站/NGA/微博/抖音）并整理成结构化卡片",
    "1.0.0",
    "https://github.com/your-name/astrbot_plugin_yys_guide",
)
class YysGuidePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = dict(config) if config else {}

        self._http = HttpClient()
        self._cache = GuideCache(
            db_path=os.path.join(_data_dir(), "guide.db"),
            ttl_minutes=int(self._cfg("cache.ttl_minutes", 180)),
            max_rows=int(self._cfg("cache.max_rows", 2000)),
        )
        self._fetchers = self._build_fetchers()

        self._cooldown: dict[str, float] = {}
        self._scheduler: WarmupScheduler | None = None
        self._ready = asyncio.Event()
        self._bootstrap_task: asyncio.Task | None = None
        self._warmup_lock = asyncio.Lock()
        self._warned_weibo = False

        try:
            self._bootstrap_task = asyncio.get_running_loop().create_task(self._bootstrap())
        except RuntimeError:
            # 没有运行中的 loop 时留到首次处理消息再初始化
            self._bootstrap_task = None

    # ------------------------------------------------------------ 配置

    def _cfg(self, path: str, default: Any = None) -> Any:
        node: Any = self.config
        for key in path.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return default if node is None else node

    def _build_fetchers(self) -> dict[str, Any]:
        cookies = self._cfg("cookies", {}) or {}
        search = self._cfg("search", {}) or {}
        shared = {
            "bilibili": cookies.get("bilibili", ""),
            "weibo": cookies.get("weibo", ""),
            "nga": cookies.get("nga", ""),
            "nga_fid": str(self._cfg("nga_fid", "") or ""),
            "douyin_mode": self._cfg("douyin_mode", "try_meta"),
            "engine": search.get("engine", "so360"),
            "searxng_url": search.get("searxng_url", ""),
        }
        return {
            "bilibili": BilibiliFetcher(self._http, shared),
            "ds163": Ds163Fetcher(self._http, shared),
            "nga": NgaFetcher(self._http, shared),
            "weibo": WeiboFetcher(self._http, shared),
            "douyin": DouyinFetcher(self._http, shared),
            "web": WebSearchFetcher(self._http, shared),
        }

    def _source_enabled(self, name: str) -> bool:
        enabled = self._cfg("sources", {}) or {}
        key = "web_search" if name == "web" else name
        return bool(enabled.get(key, True))

    # ------------------------------------------------------------ 生命周期

    async def _bootstrap(self) -> None:
        try:
            await self._cache.init()
            await self._start_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[yys-guide] 初始化失败：{exc!r}")
        finally:
            self._ready.set()

    async def _ensure_ready(self) -> None:
        if self._ready.is_set():
            return
        if self._bootstrap_task is None or self._bootstrap_task.done():
            self._bootstrap_task = asyncio.create_task(self._bootstrap())
        try:
            await asyncio.wait_for(asyncio.shield(self._bootstrap_task), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("[yys-guide] 初始化超时，继续尝试处理请求")

    async def _start_scheduler(self) -> None:
        warmup = self._cfg("warmup", {}) or {}
        if not warmup.get("enabled", True):
            return
        times = parse_times(warmup.get("times"))
        if not times:
            return
        self._scheduler = WarmupScheduler(times, self._warmup_once)
        self._scheduler.start()

    async def terminate(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
        await self._http.close()
        await self._cache.close()
        logger.info("[yys-guide] 插件已卸载")

    # ------------------------------------------------------------ 触发

    @filter.regex(TRIGGER_PATTERN)
    async def on_message_trigger(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """闲聊里提到阴阳师相关关键词时自动整理攻略。"""
        if not self._cfg("enabled", True):
            return

        match = aliases.match(
            event.message_str or "", str(self._cfg("trigger_mode", "strict"))
        )
        if match is None:
            return

        async for result in self._process(event, match):
            yield result

    @filter.command("yys")
    async def cmd_yys(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """手动查询：/yys 御魂 须佐之男"""
        raw = (event.message_str or "").strip()
        argument = re.sub(r"^\S+\s*", "", raw, count=1).strip()

        if not argument:
            yield event.plain_result(
                "用法：/yys <关键词>\n例：/yys 魂土阵容、/yys 须佐之男御魂\n\n"
                + aliases.describe_aliases()
            )
            return

        if argument in {"状态", "status"}:
            async for result in self.cmd_status(event):
                yield result
            return

        if argument in {"刷新", "refresh"}:
            async for result in self.cmd_refresh(event):
                yield result
            return

        await self._ensure_ready()
        matched = aliases.match(f"阴阳师 {argument}", "loose")
        if matched is not None:
            matched.raw = argument
            match = matched
        else:
            # 用户输入的是别名表里没有的新词，直接拿它当查询词
            match = aliases.MatchResult(
                topic=argument, alias=argument, raw=argument, extra_keywords=[argument]
            )
        async for result in self._process(event, match, force_refresh=False):
            yield result

    @filter.command("yys刷新")
    async def cmd_refresh(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """手动预热一轮缓存。"""
        await self._ensure_ready()
        yield event.plain_result("开始预热缓存，可能需要一会儿…")
        try:
            count = await self._warmup_once(force=True)
            yield event.plain_result(f"预热完成，写入/更新 {count} 条。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[yys-guide] 手动预热失败：{exc!r}")
            yield event.plain_result("预热失败了，看下日志里 [yys-guide] 开头的报错。")

    @filter.command("yys状态")
    async def cmd_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看缓存状态。"""
        await self._ensure_ready()
        try:
            stats = await self._cache.stats()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"读取缓存状态失败：{exc!r}")
            return

        by_source = stats.get("by_source") or {}
        detail = "、".join(
            f"{source_label(name)} {count}" for name, count in by_source.items()
        ) or "空"

        lines = [
            "▎阴阳师攻略助手 · 状态",
            f"\u3000缓存条目：{stats.get('total', 0)}（有效 {stats.get('fresh', 0)}）",
            f"\u3000按来源：{detail}",
            f"\u3000数据源：" + "、".join(
                source_label(name)
                for name in ("bilibili", "ds163", "nga", "weibo", "douyin", "web")
                if self._source_enabled(name)
            ),
            f"\u3000核心博主：{len(self._cfg('core_authors', []) or [])} 个",
        ]
        if self._scheduler is not None:
            next_run = self._scheduler.next_run()
            if next_run:
                lines.append(f"\u3000下次预热：{next_run:%m-%d %H:%M}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 主流程

    async def _process(
        self,
        event: AstrMessageEvent,
        match: "aliases.MatchResult",
        *,
        force_refresh: bool = False,
    ) -> AsyncGenerator[Any, None]:
        umo = event.unified_msg_origin
        topic = match.topic
        keywords = match.search_keywords or [match.alias]
        limit = max(1, int(self._cfg("max_items", 5)))

        if not force_refresh and not self._check_cooldown(umo):
            return

        if bool(self._cfg("show_progress", True)):
            yield event.plain_result(f"正在检索「{topic}」相关攻略，稍等一下…")

        items: list[GuideItem] = []
        notice = ""
        try:
            items, notice = await self._gather(
                topic, keywords, limit, use_cache=not force_refresh
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[yys-guide] 抓取阶段异常：{exc!r}")

        if not items:
            yield event.plain_result(
                f"▎阴阳师 · {topic}\n"
                "\u3000没找到近期相关攻略。可以试试换个说法，或者检查核心博主配置是否正确。"
            )
            return

        await self._enrich(items)

        entries: list[Any] = []
        try:
            entries = await summarize(
                self.context,
                umo,
                topic,
                match.raw,
                items,
                use_llm=bool(self._cfg("summary.use_llm", True)),
                llm_max_items=int(self._cfg("summary.llm_max_items", 5)),
                max_chars=int(self._cfg("summary.item_max_chars", 1800)),
                timeout=float(self._cfg("summary.llm_timeout", 40)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[yys-guide] 摘要阶段异常，改为纯原文输出：{exc!r}")

        if not entries:
            from yys_core.summarize import rule_summarize

            entries = rule_summarize(items)

        text = render(
            topic,
            entries,
            str(self._cfg("render.style", "card")),
            notice=notice,
        )
        yield event.plain_result(text)

        if bool(self._cfg("render.show_cover", False)):
            covers = pick_cover(entries, [item.cover for item in items])
            for cover in covers:
                try:
                    yield event.image_result(cover)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[yys-guide] 封面发送失败：{exc!r}")
                    break

    def _check_cooldown(self, umo: str) -> bool:
        cooldown = int(self._cfg("cooldown_seconds", 60))
        if cooldown <= 0:
            return True
        now = time.time()
        if now - self._cooldown.get(umo, 0.0) < cooldown:
            return False
        self._cooldown[umo] = now
        return True

    # ------------------------------------------------------------ 数据收集

    async def _gather(
        self,
        topic: str,
        keywords: list[str],
        limit: int,
        *,
        use_cache: bool = True,
    ) -> tuple[list[GuideItem], str]:
        merged: dict[str, GuideItem] = {}
        # 多取一些，过滤掉营销内容后仍够数
        fetch_limit = max(limit * 2, limit + 3)

        if use_cache:
            cached = await self._cache.query(keywords, limit=fetch_limit)
            for item in cached:
                merged[item.key] = item
            if len(merged) >= fetch_limit:
                logger.debug(f"[yys-guide] 缓存命中 {len(merged)} 条：{keywords}")
                return self._finalize(merged, limit, keywords), ""

        fresh, notice = await self._fetch(topic, keywords, fetch_limit)
        if fresh:
            try:
                await self._cache.upsert(fresh)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] 写缓存失败：{exc!r}")
            for item in fresh:
                merged.setdefault(item.key, item)

        # 抓取失败但缓存里还有东西，提示用户「这是旧内容」比直接少给信息更有用
        if notice and not fresh and merged:
            notice = "注：博主与搜索接口本次都被限流，以下为缓存内容。"

        return self._finalize(merged, limit, keywords), notice

    def _finalize(
        self,
        merged: dict[str, GuideItem],
        limit: int,
        keywords: list[str] | None = None,
    ) -> list[GuideItem]:
        """排序 + 去重 + 最后一道过滤。缓存里可能残留旧版本写进去的营销内容。"""
        ordered = self._sort_items(merged, keywords)
        kept, dropped = filter_items(ordered)
        if dropped:
            logger.debug(f"[yys-guide] 输出前过滤 {dropped} 条营销/无关内容")

        # 搜索引擎结果只做兜底：站内（大神/B站/NGA/微博/抖音）够数时就不用它。
        # 这些页面标题常被 SEO 拼成「xxx__九游手机游戏」「xxx - 游戏鸟」，
        # 一条条加规则永远追不完，从源头少用才是解法。
        in_site = [item for item in kept if item.source != "web"]
        if len(in_site) >= limit:
            kept = in_site

        # 同一平台、同一标题只留一条。
        # 大神/B站的作者经常把一篇攻略拆成几条发（正文只改个开头），
        # 按 item_id 去重拦不住，卡片里会出现两条几乎一样的条目。
        # 跨平台不去重：同一篇内容在 B站是视频、在大神是图文，两种形态都留着。
        unique: dict[str, GuideItem] = {}
        for item in kept:
            unique.setdefault(item.dedup_hash(), item)
        if len(unique) < len(kept):
            logger.debug(f"[yys-guide] 标题去重，合并 {len(kept) - len(unique)} 条")

        deduped = self._drop_near_duplicates(list(unique.values()))
        return self._balance_authors(deduped, limit)

    @staticmethod
    def _drop_near_duplicates(items: list[GuideItem]) -> list[GuideItem]:
        """同一作者、标题几乎一样的，只留信息量最大的那条。

        作者常把同一篇攻略发两遍：一条「视频使用教学，一键解决爆仓烦恼」、
        另一条「一键解决爆仓烦恼」，标题不同但说的是同一件事。
        按完整标题去重拦不住，所以去掉开头的【标签】和「图片版/视频版」
        这类形态词后再比，一个标题是另一个的子串就认为是重复。

        要求互相包含而不是「前缀相同」，是为了不误伤同系列的不同攻略：
        「【秘闻副本】荒川百战3+2」和「【秘闻副本】荒川百战2+3」不该被合并。
        """
        kept: list[GuideItem] = []
        for item in items:  # 已按优先级排好序，先到的保留
            item_key = title_key(item.title)
            duplicate = False
            for existing in kept:
                if (existing.source, existing.author) != (item.source, item.author):
                    continue
                other_key = title_key(existing.title)
                if not item_key or not other_key:
                    continue
                shorter, longer = sorted((item_key, other_key), key=len)
                if len(shorter) >= 6 and shorter in longer:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(item)
        return kept

    @staticmethod
    def _balance_authors(
        items: list[GuideItem], limit: int, per_author: int = 2
    ) -> list[GuideItem]:
        """限制单个作者占用的条数，避免最活跃的那位霸榜。

        用户配了 7 个博主是想看不同人的思路，如果卡片 5 条全来自同一个人，
        那另外 6 个就白配了。所以先按「每个作者最多 2 条」挑，
        挑不满再拿剩下的补齐——只有一个作者有内容时，仍然能填满卡片。
        """
        picked: list[GuideItem] = []
        overflow: list[GuideItem] = []
        counts: dict[tuple[str, str], int] = {}

        for item in items:
            key = (item.source, (item.author or "").strip())
            if counts.get(key, 0) < per_author:
                counts[key] = counts.get(key, 0) + 1
                picked.append(item)
            else:
                overflow.append(item)

        if len(picked) < limit:
            picked.extend(overflow[: limit - len(picked)])
            # 补齐后顺序会乱，重新按原顺序排一次
            order = {id(item): idx for idx, item in enumerate(items)}
            picked.sort(key=lambda it: order.get(id(it), 0))

        return picked[:limit]

    async def _fetch(
        self, topic: str, keywords: list[str], limit: int
    ) -> tuple[list[GuideItem], str]:
        per_source = max(2, limit)

        author_items, notice = await self._fetch_authors(per_source)
        if author_items and not self._is_stale(author_items):
            relevant = self._filter_relevant(author_items, keywords)
            if len(relevant) >= limit:
                return relevant[:limit], ""
        elif author_items:
            logger.debug("[yys-guide] 核心博主近期没更新，转向话题搜索")

        query = self._build_query(topic, keywords)
        search_items = await self._search_all(query, keywords, per_source)

        combined, dropped = filter_items(author_items + search_items)
        if dropped:
            logger.debug(f"[yys-guide] 过滤掉 {dropped} 条营销/无关内容")
        return combined, notice

    async def _fetch_authors(self, limit: int) -> tuple[list[GuideItem], str]:
        """返回 (条目, 提示语)。提示语非空表示博主接口这次没拿到东西。"""
        targets = self._cfg("core_authors", []) or []
        if not isinstance(targets, list):
            return [], ""

        tasks = []
        skipped_no_cookie = 0
        for target in targets:
            if not isinstance(target, dict):
                continue
            platform = (
                target.get("__template_key") or target.get("platform") or ""
            ).strip()
            fetcher = self._fetchers.get(platform)
            if fetcher is None:
                logger.warning(f"[yys-guide] 未知的平台标记：{platform!r}")
                continue
            if not self._source_enabled(platform):
                continue
            # 微博没 Cookie 必然 432，直接跳过，省一次无用请求
            if platform == "weibo" and not str(self._cfg("cookies.weibo", "")).strip():
                skipped_no_cookie += 1
                continue
            label = target.get("name") or target.get("mid") or target.get("uid") or platform
            tasks.append(fetcher.safe(fetcher.fetch_author(target, limit), f"博主 {label}"))

        if skipped_no_cookie and not self._warned_weibo:
            self._warned_weibo = True
            logger.info(
                f"[yys-guide] 有 {skipped_no_cookie} 个微博博主因未填 Cookie 被跳过；"
                "填 cookies.weibo（m.weibo.cn 的 SUB）后才会生效"
            )

        if not tasks:
            return [], ""

        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[GuideItem] = []
        succeeded = 0
        for result in results:
            if isinstance(result, list):
                items.extend(result)
                if result:
                    succeeded += 1
            elif isinstance(result, Exception):
                logger.warning(f"[yys-guide] 博主抓取异常：{result!r}")

        notice = ""
        if not succeeded:
            notice = (
                f"注：{len(tasks)} 个博主的接口本次被限流，以下为缓存/全网搜索结果，"
                "稍后再试可拿到博主原文。"
            )
            logger.info(f"[yys-guide] 博主抓取全部失败（{len(tasks)} 个），返回提示语")
        return items, notice

    async def _search_all(
        self, query: str, keywords: list[str], limit: int
    ) -> list[GuideItem]:
        tasks = []
        for name in ("bilibili", "nga", "weibo", "web"):
            if not self._source_enabled(name):
                continue
            fetcher = self._fetchers.get(name)
            if fetcher is None:
                continue
            # NGA 是版块内按标题过滤，给它短词更合适
            keyword = " ".join(self._content_keywords(keywords)) if name == "nga" else query
            if not keyword:
                keyword = query
            tasks.append(fetcher.safe(fetcher.search(keyword, limit), f"搜索 {name}"))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[GuideItem] = []
        for result in results:
            if isinstance(result, list):
                items.extend(result)
        return items

    async def _enrich(self, items: list[GuideItem]) -> None:
        grouped: dict[str, list[GuideItem]] = {}
        for item in items:
            grouped.setdefault(item.source, []).append(item)

        tasks = []
        for source, group in grouped.items():
            fetcher = self._fetchers.get(source)
            if fetcher is not None:
                tasks.append(fetcher.enrich(group))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------ 工具方法

    def _core_signatures(self) -> tuple[set[str], set[str]]:
        """核心博主的「备注名」和「平台 id」。

        只用名字匹配不够稳：用户可能把备注名写成「森森」，而大神返回的作者名是
        「Mico林木森」，加权就失效了。所以再拿 uid/mid 兜一次。
        """
        names: set[str] = set()
        ids: set[str] = set()
        for target in self._cfg("core_authors", []) or []:
            if not isinstance(target, dict):
                continue
            name = str(target.get("name") or "").strip()
            if name:
                names.add(name)
            for key in ("uid", "mid"):
                value = str(target.get(key) or "").strip()
                if value:
                    ids.add(value.lower())
        return names, ids

    def _sort_items(
        self,
        items: dict[str, GuideItem] | list[GuideItem],
        keywords: list[str] | None = None,
    ) -> list[GuideItem]:
        pool = list(items.values()) if isinstance(items, dict) else list(items)
        core_names, core_ids = self._core_signatures()
        content_keywords = self._content_keywords(keywords or [])

        def hits(item: GuideItem) -> bool:
            if not content_keywords:
                return True
            haystack = f"{item.title} {item.digest}".lower()
            return any(word.lower() in haystack for word in content_keywords)

        def is_core(item: GuideItem) -> bool:
            if (item.author or "").strip() in core_names:
                return True
            for key in ("uid", "mid"):
                value = str(item.meta.get(key) or "").strip().lower()
                if value and value in core_ids:
                    return True
            return False

        # 排序优先级：核心博主且对题 > 对题 > 不对题的博主 ≥ 其他人。
        # 用户配博主是为了优先看他们，但博主在聊别的东西时不该霸占卡片。
        # 组内仍按时间倒序。
        # source_kind 用来把搜索引擎结果压到最后：它是兜底，不该和有作者的站内内容平级。
        def rank(item: GuideItem) -> tuple[int, int, int, float]:
            core = 1 if is_core(item) else 0
            relevant = 1 if hits(item) else 0
            in_site = 0 if item.source == "web" else 1
            return (core * relevant, relevant, in_site, item.published_ts)

        return sorted(pool, key=rank, reverse=True)

    def _is_stale(self, items: list[GuideItem], hours: int = 24) -> bool:
        newest = max((item.published_ts for item in items), default=0.0)
        if newest <= 0:
            return True
        return (time.time() - newest) > hours * 3600

    def _content_keywords(self, keywords: list[str]) -> list[str]:
        filtered = [k for k in keywords if k not in _GENERIC_FOR_QUERY]
        return filtered or keywords

    def _filter_relevant(
        self, items: list[GuideItem], keywords: list[str]
    ) -> list[GuideItem]:
        content_keywords = self._content_keywords(keywords)
        if not content_keywords:
            return items
        relevant = [
            item
            for item in items
            if any(
                keyword.lower() in f"{item.title} {item.digest}".lower()
                for keyword in content_keywords
            )
        ]
        # 过滤太狠就退回全部，宁可给多也不给空
        return relevant or items

    def _build_query(self, topic: str, keywords: list[str]) -> str:
        parts = ["阴阳师"]
        for word in self._content_keywords(keywords)[:2]:
            if word not in parts:
                parts.append(word)
        if topic and topic not in parts:
            parts.append(topic)
        return " ".join(parts)

    # ------------------------------------------------------------ 定时预热

    async def _warmup_once(self, force: bool = False) -> int:
        async with self._warmup_lock:
            targets = self._warmup_targets()
            if not targets:
                logger.info("[yys-guide] 没配置预热话题，跳过预热")
                return 0

            limit = max(1, int(self._cfg("max_items", 5)))
            total = 0
            for topic, keywords in targets:
                try:
                    items, notice = await self._gather(
                        topic, keywords, limit, use_cache=not force
                    )
                    if notice:
                        logger.info(f"[yys-guide] 预热「{topic}」提示：{notice}")
                    total += len(items)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[yys-guide] 预热「{topic}」失败：{exc!r}")

            try:
                await self._cache.purge()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[yys-guide] 清理缓存失败：{exc!r}")

            logger.info(f"[yys-guide] 预热完成，共 {total} 条")
            await self._push_digest()
            return total

    def _warmup_targets(self) -> list[tuple[str, list[str]]]:
        raw_topics = self._cfg("topics", []) or []
        if not isinstance(raw_topics, list):
            return []

        targets: list[tuple[str, list[str]]] = []
        for raw in raw_topics:
            text = str(raw).strip()
            if not text:
                continue
            words = [w for w in re.split(r"[\s,，]+", text) if w]
            if not words:
                continue
            topic = words[-1]
            keywords = [w for w in words if w not in _GENERIC_FOR_QUERY] or [topic]
            targets.append((topic, keywords))
        return targets

    async def _push_digest(self) -> None:
        """预热完可选推一份简报。push_umo 为空时什么都不做，不会在群里刷消息。"""
        push_list = self._cfg("warmup.push_umo", []) or []
        if not push_list:
            return

        try:
            items = await self._cache.query(None, limit=max(1, int(self._cfg("max_items", 5))))
        except Exception:  # noqa: BLE001
            return
        if not items:
            return

        from yys_core.summarize import rule_summarize

        entries = rule_summarize(items)
        text = render("今日更新", entries, str(self._cfg("render.style", "card")))

        from astrbot.api.event import MessageChain

        for umo in push_list:
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] 简报推送到 {umo} 失败：{exc!r}")
