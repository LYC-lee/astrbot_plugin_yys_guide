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

from yys_core import aliases, teamcode  # noqa: E402
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
from yys_core.render import pick_cover, render, render_split  # noqa: E402
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

def _build_trigger_pattern() -> str:
    words = aliases.all_trigger_words()
    return "(?i)(?:" + "|".join(re.escape(word) for word in words) + ")"


TRIGGER_PATTERN = _build_trigger_pattern()


def _data_dir() -> str:
    return os.path.join("data", "plugin_data", PLUGIN_NAME)


@register(
    PLUGIN_NAME,
    "yys-guide",
    "阴阳师攻略助手：关键词触发，自动抓取博主攻略（网易大神/B站/NGA/微博/抖音）并整理成结构化卡片",
    "1.4.0",
    "https://github.com/LYC-lee/astrbot_plugin_yys_guide",
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
        ds163 = self._cfg("ds163", {}) or {}
        shared = {
            "bilibili": cookies.get("bilibili", ""),
            "weibo": cookies.get("weibo", ""),
            "nga": cookies.get("nga", ""),
            "nga_fid": str(self._cfg("nga_fid", "") or ""),
            "douyin_mode": self._cfg("douyin_mode", "try_meta"),
            "engine": search.get("engine", "so360"),
            "searxng_url": search.get("searxng_url", ""),
            "ds163_scan_pages": ds163.get("scan_pages", 4),
            "ds163_scan_days": ds163.get("scan_days", 180),
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

        text = (event.message_str or "").strip()
        # 别的插件的 /指令 不归我们管，避免把别人的命令也答一遍
        if text[:1] in ("/", "／") and "yys" not in text[:6].lower():
            return

        match = aliases.match(text, str(self._cfg("trigger_mode", "strict")))
        if match is None:
            return

        answered = False
        async for result in self._process(event, match):
            answered = True
            yield result

        # 答完就把事件掐掉：不然默认 LLM / 别的关键词插件会再回一遍同一条消息
        if answered:
            self._halt(event)

    @staticmethod
    def _halt(event: AstrMessageEvent) -> None:
        """告诉 AstrBot：这条我已经答了，后面的 handler 和默认 LLM 都别再说话。"""
        should_call_llm = getattr(event, "should_call_llm", None)
        if callable(should_call_llm):
            try:
                should_call_llm(False)
            except Exception:  # noqa: BLE001
                pass
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[yys-guide] 终止事件传播失败：{exc!r}")

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
        self._halt(event)

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
        # 卡片标题用用户自己的说法。「魂土阵容」显示成「御魂」会让人以为搜错了
        display = match.display or match.topic
        keywords = match.keywords
        limit = max(1, int(self._cfg("max_items", 5)))

        if not force_refresh and not self._check_cooldown(umo):
            return

        if bool(self._cfg("show_progress", True)):
            yield event.plain_result(f"正在检索「{display}」相关攻略，稍等一下…")

        items: list[GuideItem] = []
        notice = ""
        try:
            items, notice = await self._gather(
                display, keywords, limit, use_cache=not force_refresh
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[yys-guide] 抓取阶段异常：{exc!r}")

        if not items:
            yield event.plain_result(await self._no_result_text(display, keywords))
            return

        await self._enrich(items)

        entries: list[Any] = []
        try:
            entries = await summarize(
                self.context,
                umo,
                display,
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

        now = datetime.now(CN_TZ)
        style = str(self._cfg("render.style", "card") or "card")
        show_codes = bool(self._cfg("render.show_team_code", True))
        # 分段只对 card 风格做：markdown/plain 是给 Telegram、Discord 那类
        # 支持富文本的渠道准备的，一屏能显示完，不需要拆。
        split = bool(self._cfg("render.split_messages", True)) and style.lower() == "card"
        per_message = max(1, int(self._cfg("render.items_per_message", 1)))
        # 一条道馆攻略能贴 9 个码，全列出来用户也不知道挑哪个
        max_codes = max(1, int(self._cfg("render.max_codes", 3)))

        if split:
            for chunk in render_split(
                display, entries, per_message, now, notice, show_codes, max_codes
            ):
                yield event.plain_result(chunk)
            qr_order = per_message
        else:
            yield event.plain_result(
                render(display, entries, style, now, notice, show_codes, max_codes)
            )
            qr_order = 0

        # 二维码紧跟在它的文字码后面：翻到哪条就扫哪条，不用回头找。
        if show_codes and bool(self._cfg("render.show_qr", True)):
            async for result in self._qr_results(event, entries, qr_order, max_codes):
                yield result

        if bool(self._cfg("render.show_cover", False)):
            covers = pick_cover(entries, [item.cover for item in items])
            for cover in covers:
                try:
                    yield event.image_result(cover)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[yys-guide] 封面发送失败：{exc!r}")
                    break

    async def _qr_results(
        self,
        event: AstrMessageEvent,
        entries: list[Any],
        per_message: int,
        max_codes: int = 3,
    ) -> AsyncGenerator[Any, None]:
        """给带阵容码的条目画二维码。

        只画文字码派生出来的码，不去识别博主截图里那张：
        作者们普遍反馈「扫相册提示未识别到有效码，得先裁图」，
        自己按文字码重画一张干净的反而更好扫，而且能保证码没错字。
        """
        budget = max(0, int(self._cfg("render.max_qr", 2)))
        if budget <= 0:
            return

        directory = os.path.join(_data_dir(), "qr")
        sent = 0
        for index, entry in enumerate(entries, 1):
            codes = list(getattr(entry, "codes", []) or [])
            # 只画正文里实际展示出来的那几个，多画了也和文字对不上
            for item in codes[:max_codes]:
                if sent >= budget:
                    return
                code = getattr(item, "code", str(item))
                label = getattr(item, "label", "")
                path = await asyncio.to_thread(teamcode.qr_image, code, directory)
                if not path:
                    # 装不上 qrcode 就走这条路：文字码已经在正文里了，够用
                    logger.info(
                        "[yys-guide] 二维码生成不可用（缺 qrcode 依赖？），本次只发文字码"
                    )
                    return
                # 一条消息里好几个码时，不给图配句话就对不上是哪一个
                if per_message != 1 or len(codes) > 1:
                    tag = f"（{label}）" if label else ""
                    yield event.plain_result(f"第 {index} 条{tag}的阵容码，扫这个：")
                try:
                    yield event.image_result(path)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[yys-guide] 二维码发送失败：{exc!r}")
                    return
                sent += 1

    async def _no_result_text(self, display: str, keywords: "aliases.Keywords") -> str:
        """一条相关的都没有时，说清楚查了哪儿、给出更具体的说法。

        比硬塞几条无关内容好：用户看一眼就知道不是搜漏了，而是确实没有。
        """
        lines = [
            f"▎阴阳师 · {display}",
            f"\u3000没找到「{display}」相关的近期攻略。",
        ]

        latest = await self._latest_titles(2)
        if latest:
            lines.append(f"\u3000博主最近在聊：{' / '.join(latest)}")

        word = keywords.label
        lines.append(
            f"\u3000可以换个更具体的说法，比如「{word} 御魂」「{word} 阵容」，"
            "或者加上式神名。"
        )
        return "\n".join(lines)

    async def _latest_titles(self, count: int) -> list[str]:
        """缓存里最新的几条标题，只用来做「最近在聊」的提示。"""
        try:
            items = await self._cache.query(None, limit=count * 4)
        except Exception:  # noqa: BLE001
            return []

        titles: list[str] = []
        seen: set[str] = set()
        for item in items:
            key = title_key(item.title)
            if key in seen:
                continue
            seen.add(key)
            titles.append(item.title[:18])
            if len(titles) >= count:
                break
        return titles

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
        keywords: "aliases.Keywords",
        limit: int,
        *,
        use_cache: bool = True,
    ) -> tuple[list[GuideItem], str]:
        merged: dict[str, GuideItem] = {}
        # 多取一些，过滤掉营销内容后仍够数
        fetch_limit = max(limit * 2, limit + 3)

        if use_cache:
            cached = await self._cache.query(keywords.all, limit=fetch_limit * 2)
            for item in cached:
                merged[item.key] = item
            # 缓存里条数够不等于「够用」：还要看有几条真的对题
            enough = self._finalize(merged, limit, keywords)
            if len(enough) >= limit:
                logger.debug(f"[yys-guide] 缓存命中 {len(enough)} 条：{keywords.all}")
                return enough, ""

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
        keywords: "aliases.Keywords | None" = None,
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

        # 关键词一个都没命中的，直接不要。
        # 以前这里是「不够 5 条就拿博主的其他动态补」，于是问「魂土阵容」
        # 会收到「本周活动日程表」——凑满五格，但没有一条是用户问的。
        if keywords and bool(self._cfg("strict_relevance", True)):
            kept = self._relevant_only(kept, keywords)

        # 同一平台、同一标题只留一条。
        # 大神/B站的作者经常把一篇攻略拆成几条发（正文只改个开头），
        # 按 item_id 去重拦不住，卡片里会出现两条几乎一样的条目。
        unique: dict[str, GuideItem] = {}
        for item in kept:
            unique.setdefault(item.dedup_hash(), item)
        if len(unique) < len(kept):
            logger.debug(f"[yys-guide] 标题去重，合并 {len(kept) - len(unique)} 条")

        # 跨平台的同名内容也只留一条：同一篇攻略常被博主同步发到大神和 B站，
        # 卡片里两条一模一样的标题看着像出了 bug。排序已经把「对题 + 站内 + 新」
        # 排在前面，所以保留先到的那条即可。
        deduped = self._drop_duplicate_titles(list(unique.values()))

        return self._balance_authors(self._drop_near_duplicates(deduped), limit)

    @staticmethod
    def _drop_duplicate_titles(items: list[GuideItem]) -> list[GuideItem]:
        """跨平台去重：标题完全一样、或者一个是另一个的「主体」，只留一条。

        「【逢魔演武蜃气楼】图片版：4套阵容码，掉首领御魂」和
        「【阴阳师】逢魔演武-蜃气楼：4套阵容码，掉首领御魂」是同一篇，
        前者是从神发的图文、后者是 B站视频，标题不同但说的是同一件事。
        """
        kept: list[GuideItem] = []
        for item in items:
            key = title_key(item.title)
            duplicate = False
            for existing in kept:
                other = title_key(existing.title)
                if not key or not other:
                    continue
                if key == other:
                    duplicate = True
                    break
                if existing.source == item.source:
                    continue  # 同平台的近似标题交给 _drop_near_duplicates
                shorter, longer = sorted((key, other), key=len)
                if len(shorter) >= 8 and shorter in longer:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(item)
        if len(kept) < len(items):
            logger.debug(f"[yys-guide] 跨平台同名去重，合并 {len(items) - len(kept)} 条")
        return kept

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
        self, topic: str, keywords: "aliases.Keywords", limit: int
    ) -> tuple[list[GuideItem], str]:
        per_source = max(2, limit)
        # 博主那边多拿一点：B站翻页便宜，大神现在也能翻历史了，
        # 多拿才有机会在里面翻到一条真的讲「魂土」的。
        author_limit = max(per_source, int(self._cfg("author_fetch_limit", 12)))

        # 把强词交给抓取器：大神据此决定要不要往回翻页找历史内容。
        # 只给强词，别把「阵容」「攻略」这种意图词递下去，不然翻页永远命不中要害。
        author_items, notice = await self._fetch_authors(
            author_limit, list(keywords.strong) if keywords else None
        )
        strict = bool(self._cfg("strict_relevance", True))
        author_pool = (
            self._relevant_only(author_items, keywords) if strict else author_items
        )

        # 博主那边的内容够用就直接返回，不再打搜索接口。
        # 注意这里必须自己排一次序：抓取是并发的，谁先回来谁排在前面，
        # 不排序的话「取前 5 条」等于「最早返回的那位博主霸榜」。
        if len(author_pool) >= limit:
            ordered = self._sort_items(author_pool, keywords)
            return self._balance_authors(ordered, limit), ""

        if author_items and not author_pool:
            logger.debug(f"[yys-guide] 博主近期没有「{keywords.label}」相关内容，转向站内搜索")

        # 不够就补搜索。注意这里补的是「搜到的结果」，不是把博主其他动态塞进来
        search_items = await self._search_all(keywords, per_source)

        combined, dropped = filter_items(author_pool + search_items)
        if dropped:
            logger.debug(f"[yys-guide] 过滤掉 {dropped} 条营销/无关内容")
        return combined, notice

    async def _fetch_authors(
        self, limit: int, keywords: list[str] | None = None
    ) -> tuple[list[GuideItem], str]:
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
            tasks.append(
                fetcher.safe(
                    fetcher.fetch_author(target, limit, keywords), f"博主 {label}"
                )
            )

        if skipped_no_cookie and not self._warned_weibo:
            self._warned_weibo = True
            logger.info(
                f"[yys-guide] 有 {skipped_no_cookie} 个微博博主因未填 Cookie 被跳过；"
                "填 cookies.weibo（m.weibo.cn 的 SUB）后才会生效"
            )

        if not tasks:
            return [], ""

        # 边收边判断：已经拿到够用的相关条目就取消还没回来的博主。
        # B站四个博主共用一个空间接口配额，排队能排 20 秒；而卡片最多 5 条，
        # 大神的图文那边通常 2~3 秒就能给出答案，没必要陪着干等。
        items: list[GuideItem] = []
        succeeded = 0
        words = [w.lower() for w in (keywords or []) if w]
        goal = max(2, min(limit, 5))
        pending = {asyncio.ensure_future(coro): idx for idx, coro in enumerate(tasks)}

        for future in asyncio.as_completed(list(pending)):
            try:
                result = await future
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] 博主抓取异常：{exc!r}")
                continue
            if isinstance(result, list):
                items.extend(result)
                if result:
                    succeeded += 1

            if words and sum(
                1 for it in items if any(w in it.title.lower() for w in words)
            ) >= goal:
                for task in pending:
                    if not task.done():
                        task.cancel()
                break

        notice = ""
        if not succeeded:
            notice = (
                f"注：{len(tasks)} 个博主的接口本次被限流，以下为缓存/全网搜索结果，"
                "稍后再试可拿到博主原文。"
            )
            logger.info(f"[yys-guide] 博主抓取全部失败（{len(tasks)} 个），返回提示语")
        return items, notice

    async def _search_all(
        self, keywords: "aliases.Keywords", limit: int
    ) -> list[GuideItem]:
        query = keywords.query()
        tasks = []
        for name in ("bilibili", "nga", "weibo", "web"):
            if not self._source_enabled(name):
                continue
            fetcher = self._fetchers.get(name)
            if fetcher is None:
                continue
            # NGA 是版块内按标题过滤，给它短词更合适
            keyword = " ".join(keywords.strong or keywords.weak) if name == "nga" else query
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

    @staticmethod
    def _relevance(item: GuideItem, keywords: "aliases.Keywords") -> int:
        """这条内容和问题的相关度。

        2 = 标题命中具体词（魂土 / 须佐之男），就是用户问的那件事；
        1 = 标题只命中大方向（御魂），沾边但排后面；
        0 = 只命中「阵容」这类意图词，或者什么都没命中，等于答非所问。

        **只看标题，不看摘要**。大神的「摘要」其实是整篇正文，
        一篇「PVE 内容合集」的正文里必然列出「魂十、魂土、魂王、结界突破……」，
        拿摘要判断的话，它会被当成魂土攻略排到第一位——这正是截图里
        「问魂土却收到葛叶式神改动」的原因。
        """
        if not keywords:
            return 1
        title = (item.title or "").lower()
        if any(word.lower() in title for word in keywords.strong):
            return 2
        if any(word.lower() in title for word in keywords.weak):
            return 1
        return 0

    def _relevant_only(
        self, items: list[GuideItem], keywords: "aliases.Keywords"
    ) -> list[GuideItem]:
        if not keywords:
            return list(items)
        return [item for item in items if self._relevance(item, keywords) > 0]

    def _sort_items(
        self,
        items: dict[str, GuideItem] | list[GuideItem],
        keywords: "aliases.Keywords | None" = None,
    ) -> list[GuideItem]:
        pool = list(items.values()) if isinstance(items, dict) else list(items)
        core_names, core_ids = self._core_signatures()
        kw = keywords or aliases.Keywords()

        def is_core(item: GuideItem) -> bool:
            if (item.author or "").strip() in core_names:
                return True
            for key in ("uid", "mid"):
                value = str(item.meta.get(key) or "").strip().lower()
                if value and value in core_ids:
                    return True
            return False

        # 排序优先级：对题 > 核心博主 > 站内 > 时间。
        # 「对题」必须排第一：用户配博主是为了优先看他们，但博主在聊别的东西时
        # 不该顶掉一条真的讲魂土的视频——这正是「搜索不准确」的观感来源。
        # source 用来把搜索引擎结果压在站内内容后面：它是兜底，不该和有作者的内容平级。
        def rank(item: GuideItem) -> tuple[int, int, int, float]:
            tier = self._relevance(item, kw)
            core = 1 if is_core(item) else 0
            in_site = 0 if item.source == "web" else 1
            return (tier, core, in_site, item.published_ts)

        return sorted(pool, key=rank, reverse=True)

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

    def _warmup_targets(self) -> list[tuple[str, "aliases.Keywords"]]:
        raw_topics = self._cfg("topics", []) or []
        if not isinstance(raw_topics, list):
            return []

        targets: list[tuple[str, "aliases.Keywords"]] = []
        for raw in raw_topics:
            text = str(raw).strip()
            if not text:
                continue
            words = [w for w in re.split(r"[\s,，]+", text) if w]
            if not words:
                continue
            topic = words[-1]
            targets.append((topic, aliases.keywords_from_words(words)))
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
        # 简报是主动推送到群里的，塞阵容码会把它撑得很长，
        # 想看细节的人自己触发一次查询就行
        text = render(
            "今日更新", entries, str(self._cfg("render.style", "card")), show_codes=False
        )

        from astrbot.api.event import MessageChain

        for umo in push_list:
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] 简报推送到 {umo} 失败：{exc!r}")
