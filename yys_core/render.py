"""卡片渲染。

三种风格：
- card：分块 + 分隔线，适合 QQ / 微信这类纯文本渠道。
- markdown：Discord / Telegram / 飞书 / Slack 这类支持 MD 的渠道。
- plain：最朴素，任何渠道都不会出错。

缩进统一用全角空格 U+3000，因为 OneBot 适配器会对纯文本做 strip，
半角缩进在部分渠道会被吃掉。
"""

from __future__ import annotations

from datetime import datetime

from yys_core.models import CN_TZ, fmt_human_time, source_label
from yys_core.summarize import CardEntry

INDENT = "\u3000"
DIVIDER = "─" * 14
MAX_LENGTH = 3600

_FIELD_LABELS = (
    ("team", "阵容"),
    ("souls", "御魂"),
    ("howto", "打法"),
    ("note", "注意"),
)


def _header(topic: str, count: int, generated_at: datetime) -> str:
    stamp = generated_at.astimezone(CN_TZ).strftime("%m-%d %H:%M")
    return f"▎阴阳师 · {topic}\n{INDENT}{count} 条 · 整理于 {stamp}"


def _footer(entries: list[CardEntry]) -> str:
    if entries and all(entry.degraded for entry in entries):
        return "（大模型摘要不可用，以下为原文节选，点链接看完整内容）"
    return ""


def render_card(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
) -> str:
    generated_at = generated_at or datetime.now(CN_TZ)
    if not entries:
        return f"▎阴阳师 · {topic}\n{INDENT}没找到近期相关攻略，换个说法或稍后再试。"

    blocks: list[str] = [_header(topic, len(entries), generated_at)]
    if notice:
        blocks.append(f"{INDENT}{notice}")
    blocks.append("")

    for index, entry in enumerate(entries, 1):
        blocks.append(f"{INDENT}{index}. {entry.title}")

        meta = entry.source_text
        if meta:
            blocks.append(f"{INDENT * 2}{meta}")

        for attr, label in _FIELD_LABELS:
            value = getattr(entry, attr, "")
            if value:
                blocks.append(f"{INDENT * 2}{label}：{value}")

        if entry.url:
            blocks.append(f"{INDENT * 2}链接：{entry.url}")

        if index != len(entries):
            blocks.append(DIVIDER)

    footer = _footer(entries)
    if footer:
        blocks.append("")
        blocks.append(footer)

    text = "\n".join(blocks)
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH].rstrip() + "\n…内容过长已截断，完整内容看上方链接"
    return text


def render_markdown(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
) -> str:
    generated_at = generated_at or datetime.now(CN_TZ)
    if not entries:
        return f"### 阴阳师 · {topic}\n没找到近期相关攻略。"

    stamp = generated_at.astimezone(CN_TZ).strftime("%m-%d %H:%M")
    lines = [f"### 阴阳师 · {topic}", f"> {len(entries)} 条 · 整理于 {stamp}"]
    if notice:
        lines.append(f"> {notice}")
    lines.append("")

    for index, entry in enumerate(entries, 1):
        lines.append(f"**{index}. {entry.title}**")
        meta = entry.source_text
        if meta:
            lines.append(f"`{meta}`")
        for attr, label in _FIELD_LABELS:
            value = getattr(entry, attr, "")
            if value:
                lines.append(f"- **{label}**：{value}")
        if entry.url:
            lines.append(f"[原文]({entry.url})")
        lines.append("")

    footer = _footer(entries)
    if footer:
        lines.append(f"_{footer}_")

    text = "\n".join(lines).strip()
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH].rstrip() + "\n…已截断"
    return text


def render_plain(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
) -> str:
    generated_at = generated_at or datetime.now(CN_TZ)
    if not entries:
        return f"阴阳师 · {topic}：没找到近期相关攻略。"

    lines = [f"阴阳师 · {topic}（{len(entries)} 条）"]
    if notice:
        lines.append(notice)
    lines.append("")
    for index, entry in enumerate(entries, 1):
        lines.append(f"{index}. {entry.title}")
        meta = entry.source_text
        if meta:
            lines.append(f"   {meta}")
        for attr, label in _FIELD_LABELS:
            value = getattr(entry, attr, "")
            if value:
                lines.append(f"   {label}：{value}")
        if entry.url:
            lines.append(f"   {entry.url}")
        lines.append("")
    return "\n".join(lines).strip()


def render(
    topic: str,
    entries: list[CardEntry],
    style: str = "card",
    generated_at: datetime | None = None,
    notice: str = "",
) -> str:
    style = (style or "card").lower()
    if style == "markdown":
        return render_markdown(topic, entries, generated_at, notice)
    if style == "plain":
        return render_plain(topic, entries, generated_at, notice)
    return render_card(topic, entries, generated_at, notice)


def pick_cover(entries: list[CardEntry], covers: list[str], limit: int = 2) -> list[str]:
    """按条目顺序挑封面图，去重。"""
    picked: list[str] = []
    seen: set[str] = set()
    for cover in covers:
        if not cover or cover in seen:
            continue
        if not cover.startswith("http"):
            continue
        seen.add(cover)
        picked.append(cover)
        if len(picked) >= limit:
            break
    return picked
