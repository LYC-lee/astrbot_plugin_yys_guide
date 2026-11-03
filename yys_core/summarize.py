"""摘要层。

策略：LLM 优先。模型没配、超时、返回格式不对，一律退回规则抽取，
保证「一定有东西返回」，而不是让用户在群里干等。

规则抽取不编造内容：只截原文，没有的字段就空着。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.api import logger

from yys_core import teamcode
from yys_core.models import GuideItem, fmt_human_time, one_line, source_label


@dataclass
class CardEntry:
    """卡片里的一条，渲染层只认这个结构。"""

    title: str
    url: str = ""
    source: str = ""
    author: str = ""
    published: datetime | None = None
    team: str = ""
    souls: str = ""
    howto: str = ""
    note: str = ""
    # 阵容码（|TA|开头那串）。不交给模型提炼：它经常整串抄错，
    # 而错一个字符游戏就导不进去，所以直接从原文正则提取。
    codes: list[str] = field(default_factory=list)
    degraded: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def source_text(self) -> str:
        parts = []
        if self.source:
            parts.append(source_label(self.source))
        if self.author:
            parts.append(f"@{self.author}")
        if self.published:
            parts.append(fmt_human_time(self.published))
        return " · ".join(parts)


SYSTEM_PROMPT = """你是《阴阳师》资深攻略编辑，擅长把一手攻略原文压成一页能用的信息。

收到若干条攻略原文后，请逐条提炼，输出 JSON 数组。

硬性要求：
1. 只使用原文中出现的信息。原文没写的字段，一律给空字符串，绝对不要用你自己的游戏知识补全。
2. 每条对象的字段固定为：title、team、souls、howto、note。
   - title：不超过 20 字，概括这条攻略在讲什么
   - team：式神阵容，如「须佐之男 / 玉藻前 / 追月神 / 座敷童子」
   - souls：御魂配置，如「须佐：狂骨 4 + 鬼灵歌伎 2」
   - howto：打法要点，1-2 句，写清楚起手、节奏、关键操作
   - note：注意事项或前提条件（练度、皮肤、版本限制），没有就空字符串
   - 原文里「|TA|」开头的那串阵容码不要抄进任何字段，程序会单独提取。
3. 输出必须是纯 JSON 数组，不要 ```json 包裹，不要任何解释文字。
4. 数组长度与输入条目数一致，顺序也保持一致。
"""


# ---------------------------------------------------------------- LLM


def _resp_text(resp: Any) -> str:
    if resp is None:
        return ""
    text = getattr(resp, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    if isinstance(resp, str):
        return resp.strip()
    if isinstance(resp, dict):
        value = resp.get("completion_text") or resp.get("text") or ""
        return str(value).strip()
    return ""


async def call_llm(
    context: Any,
    umo: str,
    system_prompt: str,
    prompt: str,
    timeout: float = 40.0,
) -> str | None:
    """兼容 AstrBot 新旧两套 LLM 调用方式。"""
    provider_id = None
    get_provider_id = getattr(context, "get_current_chat_provider_id", None)
    if callable(get_provider_id):
        try:
            provider_id = await get_provider_id(umo=umo)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[yys-guide] 获取会话 provider 失败：{exc!r}")
            provider_id = None

    llm_generate = getattr(context, "llm_generate", None)
    if provider_id and callable(llm_generate):
        try:
            resp = await asyncio.wait_for(
                llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                ),
                timeout=timeout,
            )
            text = _resp_text(resp)
            if text:
                return text
        except asyncio.TimeoutError:
            logger.warning("[yys-guide] 大模型调用超时，降级为规则抽取")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] llm_generate 调用失败：{exc!r}")

    # 旧版接口回退
    get_using_provider = getattr(context, "get_using_provider", None)
    if callable(get_using_provider):
        provider = None
        try:
            provider = get_using_provider(umo=umo)
        except TypeError:
            try:
                provider = get_using_provider()
            except Exception:  # noqa: BLE001
                provider = None
        except Exception:  # noqa: BLE001
            provider = None

        if provider is not None:
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt, system_prompt=system_prompt, contexts=[]
                    ),
                    timeout=timeout,
                )
                return _resp_text(resp) or None
            except asyncio.TimeoutError:
                logger.warning("[yys-guide] 大模型（旧接口）调用超时，降级为规则抽取")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] text_chat 调用失败：{exc!r}")
    return None


# ---------------------------------------------------------------- 解析


def _parse_json_array(text: str) -> list[dict[str, Any]] | None:
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        logger.debug(f"[yys-guide] 模型返回的 JSON 解析失败：{exc!r}")
        return None

    if not isinstance(parsed, list):
        return None
    return [entry for entry in parsed if isinstance(entry, dict)]


# ---------------------------------------------------------------- 主流程


def rule_summarize(items: list[GuideItem]) -> list[CardEntry]:
    """不调模型时的兜底：只截原文，不编造。

    原文里的换行必须压掉，否则视频简介那种多行文本会把卡片排版撑坏。
    """
    entries: list[CardEntry] = []
    for item in items:
        # 先抹掉正文里的码再截断：160 字截在码中间会剩半串，
        # 用户复制了导入失败，还以为是插件把码弄错了。
        digest = one_line(teamcode.strip_codes(item.digest or item.content), 160)
        entries.append(
            CardEntry(
                title=one_line(item.title, 60),
                url=item.url,
                source=item.source,
                author=item.author,
                published=item.published,
                howto=digest,
                codes=teamcode.extract_codes(item.title, item.digest, item.content),
                degraded=True,
            )
        )
    return entries


def _build_prompt(topic: str, raw: str, items: list[GuideItem], max_chars: int) -> str:
    lines = [f"话题：{topic}"]
    if raw:
        lines.append(f"提问者原话：{raw[:120]}")
    lines.append(f"共 {len(items)} 条攻略原文：")
    lines.append("")

    for index, item in enumerate(items, 1):
        body = item.text_for_llm
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        header = (
            f"[{index}] 来源：{source_label(item.source)}"
            f" | 作者：{item.author or '未知'}"
            f" | 时间：{fmt_human_time(item.published)}"
            f" | 标题：{item.title}"
        )
        lines.append(header)
        lines.append(f"正文：{body or '（无正文，仅标题）'}")
        lines.append("---")

    return "\n".join(lines)


async def summarize(
    context: Any,
    umo: str,
    topic: str,
    raw: str,
    items: list[GuideItem],
    *,
    use_llm: bool = True,
    llm_max_items: int = 5,
    max_chars: int = 1800,
    timeout: float = 40.0,
) -> list[CardEntry]:
    if not items:
        return []

    fallback = rule_summarize(items)
    if not use_llm:
        return fallback

    llm_items = items[: max(1, llm_max_items)]
    prompt = _build_prompt(topic, raw, llm_items, max_chars)
    response = await call_llm(context, umo, SYSTEM_PROMPT, prompt, timeout=timeout)
    if not response:
        return fallback

    parsed = _parse_json_array(response)
    if not parsed:
        logger.warning("[yys-guide] 模型输出不是合法 JSON 数组，降级为规则抽取")
        return fallback

    entries: list[CardEntry] = []
    for index, item in enumerate(llm_items):
        data = parsed[index] if index < len(parsed) else {}
        if not isinstance(data, dict):
            data = {}
        entry = CardEntry(
            title=(str(data.get("title") or item.title)).strip()[:40] or item.title,
            url=item.url,
            source=item.source,
            author=item.author,
            published=item.published,
            team=str(data.get("team") or "").strip(),
            souls=str(data.get("souls") or "").strip(),
            howto=str(data.get("howto") or "").strip(),
            note=str(data.get("note") or "").strip(),
            # 码从原文取，不用模型给的那份
            codes=fallback[index].codes,
        )
        # 模型偶尔无视提示词把码抄进字段，抄错一个字符就用不了，统一抹掉
        for attr in ("team", "souls", "howto", "note"):
            setattr(entry, attr, teamcode.strip_codes(getattr(entry, attr)))
        if not (entry.team or entry.souls or entry.howto or entry.note):
            # 模型这条基本没提炼出东西，用规则版本兜住
            entry.howto = fallback[index].howto
            entry.degraded = True
        entries.append(entry)

    # 超出模型处理条数的部分，直接走规则
    for extra in fallback[len(llm_items) :]:
        entries.append(extra)

    return entries
