"""卡片渲染。

三种风格：
- card：分块 + 分隔线，适合 QQ / 微信这类纯文本渠道。
- markdown：Discord / Telegram / 飞书 / Slack 这类支持 MD 的渠道。
- plain：最朴素，任何渠道都不会出错。

缩进统一用全角空格 U+3000，因为 OneBot 适配器会对纯文本做 strip，
半角缩进在部分渠道会被吃掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yys_core import fieldsplit
from yys_core.models import CN_TZ, fmt_human_time, source_label
from yys_core.summarize import CardEntry

INDENT = "\u3000"
DIVIDER = "─" * 14
MAX_LENGTH = 3600
TRUNCATED_TAIL = "\n…内容过长已截断，完整内容看上方链接"

_FIELD_LABELS = (
    ("team", "式神"),
    ("souls", "御魂"),
    ("howto", "打法"),
    ("note", "注意"),
)

#: 式神 / 御魂这两栏即使原文没写也要占位。用户拿到的信息是全的，
#: 「原文没提」四个字比静默省略有用：一眼就知道这篇没料，该换一条看。
_REQUIRED_FIELDS = ("team", "souls")


def _header(topic: str, count: int, generated_at: datetime) -> str:
    stamp = generated_at.astimezone(CN_TZ).strftime("%m-%d %H:%M")
    return f"▎阴阳师 · {topic}\n{INDENT}{count} 条 · 整理于 {stamp}"


def _footer(entries: list[CardEntry]) -> str:
    if entries and all(entry.degraded for entry in entries):
        return "（大模型摘要不可用，以下为原文节选，点链接看完整内容）"
    return ""


CODE_HINT = "阵容码（式神录→阵容助手→粘贴下面这行）："
#: 博主没贴码时说的那句话。魂土、觉醒这类刷本攻略本来就不发码，
#: 不写清楚用户会以为是插件把码弄丢了。
NO_CODE_NOTE = "博主没贴（可看视频简介或评论区）"


def _code_lines(codes: list[Any], limit: int) -> list[str]:
    """把码排成「码独占一行」，名字另起一行放在它前头。

    名字行带缩进、码行顶格：长按复制码那一行不会把名字和全角空格一起带走。
    一条道馆攻略能贴 9 个码，全列出来用户也不知道挑哪个，所以按 limit 截断，
    超出的提示去原文看。
    """
    lines: list[str] = []
    last_label = ""
    for item in codes[: max(1, limit)]:
        code = getattr(item, "code", str(item))
        label = getattr(item, "label", "")
        if label and label != last_label:
            lines.append(f"{INDENT}· {label}")
            last_label = label
        lines.append(code)

    hidden = len(codes) - max(1, limit)
    if hidden > 0:
        lines.append(f"{INDENT}…原文还有 {hidden} 个码，点链接看全部")
    return lines


def render_header(
    topic: str,
    count: int,
    generated_at: datetime | None = None,
    notice: str = "",
) -> str:
    """分段发送时的第一条：只交代这次查了什么、拿到几条。"""
    generated_at = generated_at or datetime.now(CN_TZ)
    lines = [_header(topic, count, generated_at)]
    if notice:
        lines.append(f"{INDENT}{notice}")
    return "\n".join(lines)


def _field_rows(entry: CardEntry) -> list[tuple[str, str]]:
    """(标签, 值)。三种卡片风格共用，保证缺字段时占位一致。"""
    rows: list[tuple[str, str]] = []
    for attr, name in _FIELD_LABELS:
        value = getattr(entry, attr, "")
        if not value:
            if attr in _REQUIRED_FIELDS:
                rows.append((name, "原文没提"))
            continue
        if attr == "souls" and not fieldsplit.has_soul(value):
            # 「兔子＞158＞其他＞128＞鬼使黑」只有配速没有御魂，
            # 挂在「御魂」下面会让人以为这是御魂配置
            name = "配速"
        rows.append((name, value))
    return rows


def render_entry_block(
    entry: CardEntry,
    index: int,
    total: int = 0,
    show_codes: bool = True,
    max_codes: int = 3,
) -> str:
    """单条攻略。分段发送时一条消息装一条，就是它。

    阵容码顶格独立成行，不加任何缩进或编号前缀——
    手机上长按复制整行会把这些一起带走，贴进游戏就识别失败。
    宁可排版上跳一点，也要保证复制出来是干净的 36 个字符。
    """
    blocks: list[str] = []
    label = f"{index}/{total}" if total and total > 1 else str(index)
    blocks.append(f"{INDENT}{label}. {entry.title}")

    meta = entry.source_text
    if meta:
        blocks.append(f"{INDENT * 2}博主：{meta}")

    for name, value in _field_rows(entry):
        blocks.append(f"{INDENT * 2}{name}：{value}")

    if entry.url:
        blocks.append(f"{INDENT * 2}链接：{entry.url}")

    if show_codes:
        if entry.codes:
            blocks.append("")
            blocks.append(CODE_HINT)
            blocks.extend(_code_lines(entry.codes, max_codes))
        else:
            blocks.append(f"{INDENT * 2}阵容码：{NO_CODE_NOTE}")

    text = "\n".join(blocks)
    # 单条也可能超长（有的博主把整篇攻略塞进简介）。
    # 合并转发是一个节点一条消息，节点本身也有长度上限，
    # 与其被协议端从中间砍掉，不如自己截断并留个指路的话。
    # 尾巴的长度要算进去，否则截完还是超。
    if len(text) > MAX_LENGTH:
        tail = TRUNCATED_TAIL
        text = text[: MAX_LENGTH - len(tail)].rstrip() + tail
    return text


def render_card(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
    show_codes: bool = True,
    max_codes: int = 3,
) -> str:
    generated_at = generated_at or datetime.now(CN_TZ)
    if not entries:
        return f"▎阴阳师 · {topic}\n{INDENT}没找到近期相关攻略，换个说法或稍后再试。"

    blocks: list[str] = [_header(topic, len(entries), generated_at)]
    if notice:
        blocks.append(f"{INDENT}{notice}")
    blocks.append("")

    for index, entry in enumerate(entries, 1):
        blocks.append(
            render_entry_block(
                entry, index, len(entries), show_codes=show_codes, max_codes=max_codes
            )
        )
        if index != len(entries):
            blocks.append(DIVIDER)

    footer = _footer(entries)
    if footer:
        blocks.append("")
        blocks.append(footer)

    text = "\n".join(blocks)
    if len(text) > MAX_LENGTH:
        text = text[: MAX_LENGTH - len(TRUNCATED_TAIL)].rstrip() + TRUNCATED_TAIL
    return text


def render_split(
    topic: str,
    entries: list[CardEntry],
    per_message: int = 1,
    generated_at: datetime | None = None,
    notice: str = "",
    show_codes: bool = True,
    max_codes: int = 3,
) -> list[str]:
    """拆成多条消息：第一条交代概况，之后每 per_message 条一条消息。

    群里发一条几百字的长卡片，手机上要滑好几屏才能看完，
    而且中途有人插话就找不到看哪儿了。拆开之后每条都短，
    哪条有阵容码就在它自己那条里，跟着往下翻就是。
    """
    if not entries:
        return [render_card(topic, [], generated_at, notice, show_codes, max_codes)]

    per_message = max(1, int(per_message))
    chunks: list[str] = [render_header(topic, len(entries), generated_at, notice)]

    total = len(entries)
    for start in range(0, total, per_message):
        group = entries[start : start + per_message]
        body = [
            render_entry_block(
                entry, start + offset + 1, total,
                show_codes=show_codes, max_codes=max_codes,
            )
            for offset, entry in enumerate(group)
        ]
        chunks.append(f"\n{DIVIDER}\n".join(body))

    footer = _footer(entries)
    if footer:
        chunks.append(footer)
    return chunks


@dataclass
class ForwardNode:
    """合并转发里的一个节点，点开合集后看到的就是它。

    codes 留着给上层决定要不要在这个节点里再补一张二维码图片；
    渲染层自己不管图片，保持纯文本、可离线测试。
    """

    text: str
    codes: list[Any] = field(default_factory=list)


def render_forward(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
    show_codes: bool = True,
    max_codes: int = 3,
) -> list[ForwardNode]:
    """把结果组装成「合并转发聊天记录」的节点列表。

    比起把五条攻略拆成五条消息，合并转发在会话里只占一行，
    不刷屏、不掉顺序，想看的人点开才展开。节点顺序就是阅读顺序：
    概况 -> 第 1 条 ... 第 n 条 -> 脚注。
    """
    generated_at = generated_at or datetime.now(CN_TZ)
    if not entries:
        return [ForwardNode(text=render_card(topic, [], generated_at, notice))]

    head = [render_header(topic, len(entries), generated_at, notice)]
    if any(entry.codes for entry in entries):
        head.append(f"{INDENT}点开往下翻，每条攻略一个节点，阵容码就在它自己那条里。")
    else:
        # 魂土、觉醒这类刷本攻略本来就不发码：打法就是配速 + 御魂，
        # 照着文字配比导入一套阵容还准。提前说一声，省得用户以为码丢了。
        head.append(
            f"{INDENT}点开往下翻，每条攻略一个节点。"
            "这几条博主都没贴阵容码（刷本类通常不发），"
            "式神和御魂直接列在每条里，照着配就行。"
        )
    nodes: list[ForwardNode] = [ForwardNode(text="\n".join(head))]

    total = len(entries)
    for index, entry in enumerate(entries, 1):
        nodes.append(
            ForwardNode(
                text=render_entry_block(
                    entry, index, total, show_codes=show_codes, max_codes=max_codes
                ),
                # 只把正文里真的展示出来的码带上去，附图才和文字对得上
                codes=list(entry.codes or [])[: max(1, max_codes)]
                if show_codes
                else [],
            )
        )

    footer = _footer(entries)
    if footer:
        nodes.append(ForwardNode(text=footer))
    return nodes


def render_markdown(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
    show_codes: bool = True,
    max_codes: int = 3,
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
            lines.append(f"`博主：{meta}`")
        for label, value in _field_rows(entry):
            lines.append(f"- **{label}**：{value}")
        if entry.url:
            lines.append(f"[原文]({entry.url})")
        if show_codes:
            if entry.codes:
                lines.append("")
                lines.append(CODE_HINT)
                lines.extend(_code_lines(entry.codes, max_codes))
            else:
                lines.append(f"- **阵容码**：{NO_CODE_NOTE}")
        lines.append("")

    footer = _footer(entries)
    if footer:
        lines.append(f"_{footer}_")

    text = "\n".join(lines).strip()
    if len(text) > MAX_LENGTH:
        text = text[: MAX_LENGTH - len("\n…已截断")].rstrip() + "\n…已截断"
    return text


def render_plain(
    topic: str,
    entries: list[CardEntry],
    generated_at: datetime | None = None,
    notice: str = "",
    show_codes: bool = True,
    max_codes: int = 3,
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
            lines.append(f"   博主：{meta}")
        for label, value in _field_rows(entry):
            lines.append(f"   {label}：{value}")
        if entry.url:
            lines.append(f"   链接：{entry.url}")
        if show_codes:
            if entry.codes:
                lines.append(CODE_HINT)
                lines.extend(_code_lines(entry.codes, max_codes))
            else:
                lines.append(f"   阵容码：{NO_CODE_NOTE}")
        lines.append("")
    return "\n".join(lines).strip()


def render(
    topic: str,
    entries: list[CardEntry],
    style: str = "card",
    generated_at: datetime | None = None,
    notice: str = "",
    show_codes: bool = True,
    max_codes: int = 3,
) -> str:
    style = (style or "card").lower()
    if style == "markdown":
        return render_markdown(topic, entries, generated_at, notice, show_codes, max_codes)
    if style == "plain":
        return render_plain(topic, entries, generated_at, notice, show_codes, max_codes)
    return render_card(topic, entries, generated_at, notice, show_codes, max_codes)


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
