"""把一条攻略画成图片卡片。

为什么要有这个：QQ 里的纯文字卡片，五条就要滑十几屏，而且「式神 / 御魂」
是左对齐的一列列文字，看不出哪个御魂配哪个式神。博主们发的攻略图之所以
好用，是因为它把式神横着排开、御魂挂在各自下面，一眼就能对上。

版式（1080 宽，高度按内容撑开）：

    ┌ 标题条（渐变蓝，左侧序号圆标）────────────┐
    │  ①  式神头像  头像  头像  头像  头像        │
    │     名字 / 技能 / 定位                      │
    │     [御魂块] [御魂块] [御魂块] ...          │
    │  配速：A > 190 > B > 170 > C                │
    │  ─ ─ ─ ─ 虚线 ─ ─ ─ ─                      │
    │  [黄底黑框] 通关时间 35 秒左右               │
    │  ①备注一  ②备注二                          │
    │  ┌ 深色块：一键导入 + 二维码 + 文字码 ┐      │
    │  └────────────────────────────────┘        │
    │  博主 · 平台 · 时间                         │
    └────────────────────────────────────────┘

画布高度先给足再裁，省得为了算高度把所有排版量两遍。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

# 图片卡是给手机看的，1080 宽在 QQ 里点开不糊、文件又不会大到发不出去。
WIDTH = 1080
MARGIN = 26
RADIUS = 10

# 配色照搬攻略图的习惯：蓝底标题 + 黄底结论 + 黑底御魂块。
BLUE_DARK = (30, 58, 108)
BLUE = (56, 100, 170)
BLUE_LIGHT = (150, 186, 232)
INK = (26, 26, 30)
GRAY = (108, 114, 126)
LIGHT = (238, 241, 246)
GOLD = (255, 206, 92)
RED = (198, 42, 42)
YELLOW_BG = (255, 244, 176)
SOUL_BG = (24, 24, 28)
DEEP_BG = (20, 30, 52)

#: 头像目录里放 <式神名>.png 就会用真图，没有就用配色圆占位。
#: 素材涉及版权，插件不内置，留个口子让人自己丢进去。
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp")

#: 占位头像的底色，按名字取，保证同一个式神每次颜色一样。
_AVATAR_COLORS = [
    (92, 124, 190), (168, 108, 168), (198, 116, 96), (96, 156, 140),
    (186, 152, 84), (120, 132, 196), (156, 96, 120), (108, 148, 108),
]

_FONT_CANDIDATES = {
    "bold": [
        "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
    "regular": [
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/Deng.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
}


@dataclass
class Slot:
    """一个式神位：名字 + 御魂，左右对齐后就是图里的一列。"""

    name: str
    souls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 字体


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES["bold" if bold else "regular"]:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    # 一个中文字体都没有时不让整张图黑着发出去，退回 Pillow 内置位图字体：
    # 中文会是方块，但至少版式和二维码还在，能看清结构。
    return ImageFont.load_default()


# ---------------------------------------------------------------- 文本工具


def _text_w(draw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    """按像素宽断行。中文没有词边界，只能逐字堆。"""
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        if _text_w(draw, current + char, font) <= max_w:
            current += char
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines or [""]


def _ellipsis(draw, text: str, font, max_w: int) -> str:
    if _text_w(draw, text, font) <= max_w:
        return text
    ell = "…"
    while text and _text_w(draw, text + ell, font) > max_w:
        text = text[:-1]
    return text + ell


# ---------------------------------------------------------------- 解析


# 式神之间的分隔：斜杠、顿号、逗号、加号、换行、竖线。
# 不按空格拆——「SP 缘结神」这种写法拆开就成两个式神了。
_TEAM_SPLIT = re.compile(r"[/／、,，;；|\n\r]+|\s{2,}")
_SOUL_SPLIT = re.compile(r"[\n;；|]+")
_NAME_SEP = re.compile(r"^\s*([^：:]{1,12})[：:]\s*(.+)$")
# 「攻攻爆·满暴 狂士」= 主属性 + 御魂名，图里分两行写才看得清。
# 拆点只在空格后面紧跟着汉字时成立：「狂骨 4 + 鬼灵歌伎 2」里的数字和加号
# 属于同一条御魂，拆开就成了「狂骨」/「4 + 鬼灵歌伎 2」，读着别扭。
_ATTR_SEP = re.compile(r"(?<![+＋/、])\s+(?=[\u4e00-\u9fff])")

# 「原文没提」这类占位词是卡片渲染层自己补的，跑回解析时不能当成式神名，
# 否则图上会多出一列叫「原文没提」的式神。
_PLACEHOLDERS = {"原文没提", "未提", "暂无", "无", "-", "—", "空"}


def _clean(text: str) -> str:
    return re.sub(r"[\s\u3000]+", " ", str(text or "")).strip(" /／、,，;；|")


def _soul_rows(chunk: str) -> list[str]:
    """把一段御魂配置拆成图里要显示的行。

    「攻攻爆·满暴 狂士」-> ['攻攻爆·满暴', '狂士']，一行属性一行御魂名，
    就是攻略图里那种上白下金的写法。只有一段时原样返回。
    """
    parts = [p for p in _ATTR_SEP.split(chunk) if p]
    if len(parts) <= 1:
        return [chunk]
    return [parts[0], " ".join(parts[1:])]


def parse_slots(team: str, souls: str, limit: int = 5) -> list[Slot]:
    """把「式神」和「御魂」两段文字拆成一一对应的列。

    式神段长得像「须佐之男 / 玉藻前 / 追月神」，御魂段长得像
    「须佐之男：狂骨 4 + 鬼灵歌伎 2」——名字写在前面就能精确挂上去；
    有的博主写成「狂骨 4 + 鬼灵歌伎 2 / 招财猫 4」只给顺序，
    那就按出现顺序依次填。
    """
    names = [
        n[:12]
        for n in (_clean(p) for p in _TEAM_SPLIT.split(_clean(team)))
        if n and n not in _PLACEHOLDERS
    ][:limit]

    chunks = [
        c
        for c in (_clean(p) for p in _SOUL_SPLIT.split(str(souls or "")))
        if c and c not in _PLACEHOLDERS
    ]
    named: dict[str, list[str]] = {}
    loose: list[list[str]] = []
    for chunk in chunks:
        match = _NAME_SEP.match(chunk)
        if match:
            owner = _clean(match.group(1))
            body = match.group(2).strip()
            hit = next(
                (n for n in names if owner and (owner in n or n in owner)), None
            )
            if hit:
                named.setdefault(hit, []).extend(_soul_rows(body))
                continue
        loose.append(_soul_rows(chunk))

    slots: list[Slot] = []
    cursor = 0
    for name in names:
        own = named.get(name, [])
        if own:
            slots.append(Slot(name, own))
            continue
        # 没名字的御魂按顺序补位，补完就空着——宁可空，也别张冠李戴
        if cursor < len(loose):
            slots.append(Slot(name, loose[cursor]))
            cursor += 1
        else:
            slots.append(Slot(name, []))
    return slots


def _speed_line(entry: Any) -> str:
    """从「打法」里挑出配速那一行，画在式神下面。

    「配速」是这套阵容能不能跑起来的关键，混在打法正文里会被忽略。
    """
    text = str(getattr(entry, "howto", "") or "")
    for line in re.split(r"[\n。;；]", text):
        line = line.strip()
        if not line:
            continue
        if "配速" in line or "速度" in line:
            return _clean(line)[:60]
    return ""


def _howto_lines(entry: Any, speed: str) -> list[str]:
    """打法正文去掉配速那一行，剩下的按句切。"""
    text = str(getattr(entry, "howto", "") or "")
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"[\n。;；]+", text) if p.strip()]
    out: list[str] = []
    for part in parts:
        if speed and part.replace(" ", "") == speed.replace(" ", ""):
            continue
        out.append(part)
    return out[:4]


def _note_lines(entry: Any, howto: str = "") -> list[str]:
    """注意事项。

    只按换行拆：行内的「；」是并列项（「神无月→千姬；那美→雪女」），
    拆开就变成两条独立备注，编号一多反而乱。
    """
    text = str(getattr(entry, "note", "") or "")
    if not text:
        return []
    out: list[str] = []
    for part in text.split("\n"):
        part = part.strip().lstrip("①②③④⑤⑥·-• ").strip()
        if not part:
            continue
        # 摘要偶尔把同一句同时写进打法和注意，图里重复一遍很蠢
        if howto and part[:14] in howto:
            continue
        out.append(part)
    return out[:6]


# ---------------------------------------------------------------- 头像


def _avatar(name: str, size: int, directory: str):
    from PIL import Image, ImageDraw

    for ext in AVATAR_EXTS:
        path = os.path.join(directory, f"{name}{ext}")
        if not os.path.exists(path):
            continue
        try:
            img = Image.open(path).convert("RGB")
            w, h = img.size
            side = min(w, h)
            img = img.crop(((w - side) // 2, 0, (w - side) // 2 + side, side))
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            return img, mask
        except Exception:  # noqa: BLE001
            continue

    # 没素材就画个配色圆 + 名字头一个字。比起空白圆，能让人一眼数清几个式神。
    color = _AVATAR_COLORS[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(_AVATAR_COLORS)]
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # 竖向渐变，比纯色圆少一点塑料感
    for y in range(size):
        ratio = y / max(1, size - 1)
        draw.line(
            [(0, y), (size, y)],
            fill=(
                int(color[0] + (255 - color[0]) * ratio * 0.45),
                int(color[1] + (255 - color[1]) * ratio * 0.45),
                int(color[2] + (255 - color[2]) * ratio * 0.45),
            ),
        )
    font = _font(int(size * 0.42), bold=True)
    char = name.replace("SP", "").replace("UR", "").strip()[:1] or "?"
    box = draw.textbbox((0, 0), char, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        char,
        font=font,
        fill=(255, 255, 255),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    return img, mask


# ---------------------------------------------------------------- 画图


def prune_cards(directory: str, keep: int = 120) -> None:
    """清掉旧图，只留最近 keep 张。

    文件名是按标题哈希的，同一篇攻略反复查只会覆盖同一个文件，
    但天天查不同攻略总会攒下来。一张 200KB，不清能长到几百兆。
    """
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".png")]
    except OSError:
        return
    if len(names) <= keep:
        return
    paths = [os.path.join(directory, n) for n in names]
    try:
        paths.sort(key=os.path.getmtime)
    except OSError:
        return
    for path in paths[: len(paths) - keep]:
        try:
            os.remove(path)
        except OSError:
            continue


def render_card_image(
    entry: Any,
    index: int = 1,
    total: int = 1,
    qr_path: str | None = None,
    code: str = "",
    avatar_dir: str = "",
    out_dir: str = "",
    generated_at: str = "",
) -> str | None:
    """画一张卡片，返回 PNG 绝对路径；Pillow 缺失时返回 None。

    任何一步出错都返回 None，让调用方回落到文字卡片——
    图片是锦上添花，不能因为它把整条回复搞没。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None

    try:
        canvas = Image.new("RGB", (WIDTH, 4200), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        title = _clean(getattr(entry, "title", "")) or "阴阳师攻略"
        slots = parse_slots(
            getattr(entry, "team", ""), getattr(entry, "souls", "")
        )
        speed = _speed_line(entry)
        howto = _howto_lines(entry, speed)
        notes = _note_lines(entry, str(getattr(entry, "howto", "") or ""))

        inner = WIDTH - MARGIN * 2
        y = MARGIN

        # ---- 标题条 ----
        bar_h = 108
        bar_w = WIDTH - MARGIN * 2
        bar = Image.new("RGB", (bar_w, bar_h))
        bar_draw = ImageDraw.Draw(bar)
        for i in range(bar_h):
            ratio = i / max(1, bar_h - 1)
            bar_draw.line(
                [(0, i), (bar_w, i)],
                fill=(
                    int(BLUE_LIGHT[0] + (BLUE_DARK[0] - BLUE_LIGHT[0]) * ratio * 1.55),
                    int(BLUE_LIGHT[1] + (BLUE_DARK[1] - BLUE_LIGHT[1]) * ratio * 1.55),
                    int(BLUE_LIGHT[2] + (BLUE_DARK[2] - BLUE_LIGHT[2]) * ratio * 1.55),
                ),
            )
        bar_mask = Image.new("L", (bar_w, bar_h), 0)
        ImageDraw.Draw(bar_mask).rounded_rectangle(
            (0, 0, bar_w - 1, bar_h - 1), radius=26, fill=255
        )
        canvas.paste(bar, (MARGIN, y), bar_mask)

        badge_r = 36
        cx, cy = MARGIN + badge_r + 12, y + bar_h // 2
        draw.ellipse((cx - badge_r, cy - badge_r, cx + badge_r, cy + badge_r),
                     fill=(255, 255, 255), outline=BLUE_DARK, width=5)
        num_font = _font(42, bold=True)
        num = str(index)
        nb = draw.textbbox((0, 0), num, font=num_font)
        draw.text((cx - (nb[2] - nb[0]) / 2 - nb[0], cy - (nb[3] - nb[1]) / 2 - nb[1]),
                  num, font=num_font, fill=BLUE_DARK)

        title_x = cx + badge_r + 24
        title_font = _font(46, bold=True)
        right_pad = 96 if total > 1 else MARGIN
        shown = _ellipsis(draw, title, title_font, WIDTH - MARGIN - title_x - right_pad)
        tb = draw.textbbox((0, 0), shown, font=title_font)
        draw.text((title_x, cy - (tb[3] - tb[1]) / 2 - tb[1]), shown,
                  font=title_font, fill=(255, 255, 255))
        if total > 1:
            tag_font = _font(24)
            tag = f"{index}/{total}"
            tw = _text_w(draw, tag, tag_font)
            draw.text((WIDTH - MARGIN - tw - 22, cy - 12), tag, font=tag_font,
                      fill=(214, 226, 244))
        y += bar_h + 26

        # ---- 式神列 ----
        if slots:
            count = len(slots)
            # 列宽给个上限：只有一两个式神时，整宽铺过去会变成一条巨型黑条
            col_w = min(214, inner // count)
            start_x = (WIDTH - col_w * count) // 2
            avatar = min(150, col_w - 22)
            name_font = _font(27, bold=True)
            soul_font = _font(20, bold=True)
            soul_line_h = 28

            # 一列撑死三行御魂（属性 / 御魂名 / 补充），再多就把列顶开了
            for slot in slots:
                slot.souls = slot.souls[:3]
            soul_rows = max([len(s.souls) or 1 for s in slots] + [1])
            block_h = 14 + soul_rows * soul_line_h + 6
            row_h = avatar + 40 + 34 + block_h

            for i, slot in enumerate(slots):
                cx = start_x + col_w * i + col_w // 2
                img, mask = _avatar(slot.name, avatar, avatar_dir)
                px = cx - avatar // 2
                canvas.paste(img, (px, y), mask)
                # 双圈：外面浅灰压边，里面一道白环，跟攻略图里的头像框一个意思
                draw.ellipse((px, y, px + avatar, y + avatar),
                             outline=(206, 214, 228), width=3)
                draw.ellipse((px + 5, y + 5, px + avatar - 5, y + avatar - 5),
                             outline=(255, 255, 255), width=2)

                label = _ellipsis(draw, slot.name, name_font, col_w - 12)
                lw = _text_w(draw, label, name_font)
                draw.text((cx - lw / 2, y + avatar + 8), label,
                          font=name_font, fill=INK)

                # 御魂块：黑底，第一行白字（主属性），其余金色（御魂名）
                by = y + avatar + 44
                bx0, bx1 = cx - col_w // 2 + 10, cx + col_w // 2 - 10
                draw.rounded_rectangle((bx0, by, bx1, by + block_h),
                                       radius=8, fill=SOUL_BG)
                texts = slot.souls or ["原文没提"]
                for r, text in enumerate(texts):
                    line = _ellipsis(draw, text, soul_font, bx1 - bx0 - 14)
                    tw = _text_w(draw, line, soul_font)
                    if not slot.souls:
                        color = (150, 156, 170)
                    else:
                        # 第一行是主属性，往下是御魂名，上白下金才好分
                        color = (255, 255, 255) if r == 0 else GOLD
                    draw.text((cx - tw / 2, by + 12 + r * soul_line_h),
                              line, font=soul_font, fill=color)
            y += row_h + 12

        # ---- 配速 ----
        if speed:
            sp_font = _font(28, bold=True)
            draw.text((MARGIN, y), "配速：", font=sp_font, fill=RED)
            label_w = _text_w(draw, "配速：", sp_font)
            rest = _ellipsis(draw, speed.replace("配速：", "").replace("配速:", ""),
                             sp_font, inner - label_w)
            draw.text((MARGIN + label_w, y), rest, font=sp_font, fill=INK)
            y += 46

        # ---- 虚线 ----
        # 只在后面还有说明文字时才画：一条只有阵容、没打法的卡片，
        # 末尾单独一条虚线看着像图被截断了。
        if howto or notes:
            y += 6
            for x in range(MARGIN, WIDTH - MARGIN, 18):
                draw.line([(x, y), (min(x + 9, WIDTH - MARGIN), y)],
                          fill=(190, 196, 208), width=2)
            y += 20

        # ---- 打法 / 结论 ----
        if howto:
            body_font = _font(25)
            first = howto[0]
            first = _ellipsis(draw, first, _font(29, bold=True), inner - 40)
            hi_font = _font(29, bold=True)
            hh = 58
            draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + hh), radius=6,
                                   fill=YELLOW_BG, outline=INK, width=3)
            tw = _text_w(draw, first, hi_font)
            draw.text(((WIDTH - tw) / 2, y + (hh - 34) / 2), first,
                      font=hi_font, fill=INK)
            y += hh + 18

            for line in howto[1:]:
                for wrapped in _wrap(draw, line, body_font, inner - 26):
                    draw.text((MARGIN + 26, y), wrapped, font=body_font, fill=(52, 56, 66))
                    y += 36
                y += 4
            y += 8

        # ---- 备注 ----
        if notes:
            note_font = _font(24)
            for i, note in enumerate(notes, 1):
                mark = "①②③④⑤⑥"[i - 1] if i <= 6 else f"{i}."
                draw.text((MARGIN, y), mark, font=_font(24, bold=True), fill=RED)
                for j, wrapped in enumerate(_wrap(draw, note, note_font, inner - 34)):
                    draw.text((MARGIN + 34, y), wrapped, font=note_font, fill=(64, 68, 78))
                    y += 34
                y += 4
            y += 8

        # ---- 底部导入块 ----
        has_qr = bool(qr_path and os.path.exists(qr_path))
        if has_qr or code:
            y += 6
            block_h = 322 if has_qr else 156
            draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + block_h),
                                   radius=14, fill=DEEP_BG)
            left_w = inner - (330 if has_qr else 0)

            tip_font = _font(24, bold=True)
            draw.text((MARGIN + 22, y + 18), "存图 → 扫码导入，简单方便",
                      font=tip_font, fill=GOLD)
            big_font = _font(30, bold=True)
            big = _ellipsis(draw, title, big_font, left_w - 44)
            draw.text((MARGIN + 22, y + 58), big, font=big_font, fill=(255, 255, 255))

            hint_font = _font(20)
            hint = "打开「阴阳师」→「阵容助手」→「阵容码」导入"
            draw.text((MARGIN + 22, y + 104), hint, font=hint_font, fill=(186, 198, 220))

            if code:
                draw.text((MARGIN + 22, y + 140), "文字码（下条消息可长按复制）：",
                          font=hint_font, fill=(186, 198, 220))
                # 码单独占一行、前面不加任何前缀，对齐着看就知道是 36 个字符
                draw.rounded_rectangle(
                    (MARGIN + 18, y + 168, MARGIN + 18 + left_w - 44, y + 212),
                    radius=6, fill=(38, 52, 82),
                )
                draw.text((MARGIN + 30, y + 178), code, font=_font(21),
                          fill=(255, 226, 150))

            if has_qr:
                qr_size = 244
                qx = WIDTH - MARGIN - qr_size - 30
                qy = y + 26
                draw.rounded_rectangle((qx - 10, qy - 10, qx + qr_size + 10, qy + qr_size + 10),
                                       radius=8, fill=(255, 255, 255))
                qr_img = Image.open(qr_path).convert("RGB").resize(
                    (qr_size, qr_size), Image.Resampling.LANCZOS
                )
                canvas.paste(qr_img, (qx, qy))
                tag_font = _font(22, bold=True)
                tw = _text_w(draw, "阵容码", tag_font)
                draw.text((qx + (qr_size - tw) / 2, qy + qr_size + 14),
                          "阵容码", font=tag_font, fill=(232, 236, 244))
            y += block_h + 18
        else:
            # 魂土、觉醒这类刷本攻略本来就不发码，打法就是配速 + 御魂。
            # 留一句说明，省得用户以为插件把码弄丢了。
            hint_font = _font(22)
            draw.rounded_rectangle((MARGIN, y + 4, WIDTH - MARGIN, y + 80),
                                   radius=10, fill=(246, 247, 250))
            draw.text((MARGIN + 20, y + 30),
                      "博主没贴阵容码 —— 照上面的式神、配速和御魂配就行",
                      font=hint_font, fill=GRAY)
            y += 86

        # ---- 页脚 ----
        meta = str(getattr(entry, "source_text", "") or "")
        foot_font = _font(21)
        parts = [p for p in (meta, generated_at) if p]
        if parts:
            draw.text((MARGIN, y), _ellipsis(draw, " · ".join(parts), foot_font, inner),
                      font=foot_font, fill=GRAY)
            y += 34

        y += MARGIN
        canvas = canvas.crop((0, 0, WIDTH, min(y, canvas.height)))

        # 白底卡片四周描一圈浅边，在深色聊天背景上不至于糊成一团
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 0, WIDTH - 1, canvas.height - 1), outline=(222, 226, 234), width=1)

        os.makedirs(out_dir, exist_ok=True)
        digest = hashlib.md5(
            (title + code + str(index)).encode("utf-8")
        ).hexdigest()[:16]
        path = os.path.abspath(os.path.join(out_dir, f"card_{digest}.png"))
        canvas.save(path, "PNG", optimize=True)
        return path
    except Exception as exc:  # noqa: BLE001
        # 渲染失败只会让用户收到文字卡，不会报错，所以默认只留一行 debug。
        # 排查版式问题（字体缺失、内容越界）时置 YYS_CARD_DEBUG=1 看完整栈。
        if os.environ.get("YYS_CARD_DEBUG"):
            import traceback

            traceback.print_exc()
        try:
            from astrbot.api import logger

            logger.debug(f"[yys-guide] 图片卡片渲染失败：{exc!r}")
        except Exception:  # noqa: BLE001
            pass
        return None
