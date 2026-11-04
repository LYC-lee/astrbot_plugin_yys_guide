"""把攻略原文切成「式神 / 御魂 / 其余说明」。

只切分，不补全。原文没写的绝不猜，猜错了用户照着配会直接翻车。

为什么需要这一步：B站刷本攻略（魂土/觉醒/周末本）的写法是这样的——

    魂土11s双白蛋针女
    阵容：
    大于158狂歌雪
    狂荒紧那罗（全队爆伤最高）
    火灵生生生食灵
    针女契灵

式神名、配速、御魂缩写混在同一行，整段塞进「打法」，
用户看完还是不知道该上谁、带什么御魂。按行分类之后至少能读出
「紧那罗 / 食灵」是式神，「大于158狂歌雪 / 针女契灵」是御魂。

判定顺序（宁可归到「其余」，也不要把打法硬塞进式神栏）：
1. 同时出现式神名和御魂名 -> 这一行在讲「某个式神带某套御魂」，归式神
2. 只出现御魂名 -> 归御魂
3. 只出现式神名 -> 归式神
4. 带配速写法（>158、速度、配速）-> 归御魂
5. 其余 -> 说明性文字
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from yys_core.aliases import SPIRIT_NAMES

# aliases 里的式神名是给搜索用的，只收常见老式神。
# 刷本攻略里高频出现的新式神和俗称补在这里，不动搜索那边，
# 免得「白蛋」这种词把搜索关键词带偏。
_EXTRA_SPIRITS: tuple[str, ...] = (
    "雪御前", "食灵", "因幡辉夜姬", "因幡", "歌留多", "封阳君", "香音",
    "狮驼", "大黑天", "禅心云外镜", "白蛋", "黑蛋", "达摩", "山风",
    "神堕八岐大蛇", "须佐之男", "天照", "伊邪那美", "月读", "猫川",
)

# 御魂全名。缩写靠里面的全名命中：「狂歌雪」命中「狂骨」，
# 「狂荒紧那罗」同时命中「狂骨」和式神「紧那罗」。
SOUL_NAMES: tuple[str, ...] = (
    "狂骨", "针女", "破势", "心眼", "网切", "地藏像", "木魅", "遗念火",
    "招财猫", "火灵", "蚌精", "荒骷髅", "鬼灵歌伎", "歌伎", "地震鲶",
    "土蜘蛛", "日女", "鸣屋", "狰", "薙魂", "伤魂鸟", "狂气", "镇魂兽",
    "树妖", "阴摩罗", "被服", "涅槃之火", "无刀取", "骰子鬼", "三味",
    "雪幽魂", "魍魉之匣", "返魂香", "珍珠", "恶楼", "涂佛", "古笼火",
    "夜鸣", "兵主部", "蜃气楼", "狂怒", "锤", "散件四号位", "六号位",
)

_TEAM_LABELS = ("阵容", "式神", "队伍", "配队", "上阵", "式神配置")
_SOUL_LABELS = ("御魂", "御魂配置", "御魂搭配")
_SPEED_LABELS = ("配速", "速度", "速度位")
_OTHER_LABELS = (
    "打法", "思路", "注意", "备注", "面板", "练度", "阵容码", "文字码",
    "操作", "手法", "要求", "说明", "前提",
)

_ALL_LABELS = (
    _TEAM_LABELS + _SOUL_LABELS + _SPEED_LABELS + _OTHER_LABELS
)

# 标签必须带冒号，或者整行只有标签词。
# 「魂土11s双白蛋针女」这种标题如果按前缀硬匹配，
# 会变成「标签=魂土，内容=11s双白蛋针女」，把标题啃掉半截。
_LABEL_ALT = "|".join(sorted(_ALL_LABELS, key=len, reverse=True))
_LABEL_RE = re.compile(
    r"^[【\[（(]?\s*(" + _LABEL_ALT + r")\s*[】\]）)]?\s*[:：]\s*(.*)$"
)
_LABEL_ONLY_RE = re.compile(
    r"^[【\[（(]?\s*(" + _LABEL_ALT + r")\s*[】\]）)]?\s*$"
)

_SPEED_RE = re.compile(r"(?:[>＞]\s*\d{2,3}|\d{2,3}\s*[>＞]|速度|配速|一速|二速)")
# 「大于158狂歌雪」这种配速行很少带式神名，靠数字兜住
_SPEED_NUM_RE = re.compile(r"\d{2,3}")

# 超过这个长度的行基本是说明性文字，不往式神栏里塞
TEAM_LINE_MAX = 20
# 再长就是整段叙述了，御魂栏也不收
LONG_LINE_MAX = 40

# 「文字阵容码 速度位1️⃣ 输出位1️⃣」这类行是码的位置名，不是配速也不是御魂。
# 不单独挑出来，「速度位」里的「速度」会被当成配速写进御魂栏。
_CODE_HINT_RE = re.compile(r"(文字码|阵容码|文字阵容码|扫码|码见)")

# 「速度位1️⃣」「辅助位1️⃣+输出位2️⃣」是阵容码的位置名。
# 「速度位」里的「速度」会被 _SPEED_RE 认成配速，写进御魂栏，
# 用户会以为那是要卡的速度。
_POS_LABEL_RE = re.compile(r"^(速度位|输出位|辅助位|一速位|二速位|替补位)")

# 一行最多收几个，收多了会把正文整篇搬进字段
MAX_LINES = 6
MAX_CHARS = 150


def has_soul(line: str) -> bool:
    return any(name in line for name in SOUL_NAMES)


def has_spirit(line: str) -> bool:
    return any(name in line for name in SPIRIT_NAMES) or any(
        name in line for name in _EXTRA_SPIRITS
    )


def _classify(line: str) -> str:
    if _CODE_HINT_RE.search(line) or _POS_LABEL_RE.match(line):
        return "other"
    if len(line) > LONG_LINE_MAX:
        # 整段叙述里顺带提到式神名和御魂名，别整段搬进配置栏
        return "other"
    speed = bool(_SPEED_RE.search(line))
    soul = has_soul(line)
    spirit = has_spirit(line)
    if len(line) <= TEAM_LINE_MAX and _SPEED_NUM_RE.search(line) and not spirit:
        # 「大于158狂歌雪」：配速 + 御魂缩写，没写式神名
        return "souls"
    if soul and spirit:
        # 「狂荒紧那罗」：在说某个式神带什么，归式神
        return "team"
    if soul or speed:
        return "souls"
    if spirit:
        # 长句里往往顺带提一句式神名，别把整段说明塞进式神栏
        return "other" if len(line) > TEAM_LINE_MAX else "team"
    return "other"


def _bucket_of(label: str) -> str:
    if label in _TEAM_LABELS:
        return "team"
    if label in _SOUL_LABELS or label in _SPEED_LABELS:
        return "souls"
    return "other"


def _squash(line: str) -> str:
    return " ".join(line.split()).strip(" ·-—|")


def _looks_like_title(line: str, title: str) -> bool:
    """第一行常常是视频标题的复读，别再占一行式神栏。"""
    if not title:
        return False
    a = _squash(line)
    b = _squash(title)
    if not a or not b:
        return False
    if a == b or (len(a) >= 6 and (a in b or b in a)):
        return True
    # 「魂土11s双白蛋针女」和标题「魂土11s双白蛋食雪紧」只差尾巴两个字，
    # 是同一句话被改了形态，不算配置
    return len(a) >= 8 and SequenceMatcher(None, a, b).ratio() >= 0.75


#: 给上层用：判断某段文字是不是标题的复读
looks_like_title = _looks_like_title


def _join(lines: list[str]) -> str:
    seen: list[str] = []
    for line in lines:
        text = _squash(line)
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= MAX_LINES:
            break
    joined = " / ".join(seen)
    if len(joined) > MAX_CHARS:
        joined = joined[:MAX_CHARS].rstrip(" /·") + "…"
    return joined


def split_fields(text: str, title: str = "") -> tuple[str, str, str]:
    """返回 (式神, 御魂, 其余说明)。三段都可能是空字符串。"""
    body = (text or "").strip()
    if not body:
        return "", "", ""

    team: list[str] = []
    souls: list[str] = []
    other: list[str] = []
    bucket = {"team": team, "souls": souls, "other": other}

    current = ""
    for raw in body.split("\n"):
        line = raw.strip().lstrip("-·•*").strip()
        if not line:
            continue

        match = _LABEL_RE.match(line)
        if match:
            label, rest = match.group(1), (match.group(2) or "").strip()
            current = _bucket_of(label)
            if rest and not _looks_like_title(rest, title):
                # 标签后面还跟着内容：「御魂：狂骨4+歌伎2」。
                # 归到标签自己的桶里，不要再按内容猜：写法标签的行
                # 常常带式神名（「打法：先手天照开大」），重新分类会把它塞进式神栏。
                bucket[current].append(rest)
            continue

        only = _LABEL_ONLY_RE.match(line)
        if only:
            current = _bucket_of(only.group(1))
            continue

        if _looks_like_title(line, title):
            continue

        # 没有标签的行按内容判断。
        # 已经进入某个标签段落时，只在「明显是另一类」的情况下改判：
        # 「御魂：」下面写「须佐 狂骨4+荒骷髅2」，虽然带式神名，
        # 讲的还是御魂，不能因为认出「须佐」就搬去式神栏。
        kind = _classify(line)
        if current == "souls":
            kind = "souls"
        elif current == "team" and kind == "other":
            kind = "team"
        bucket[kind].append(line)

    return _join(team), _join(souls), " ".join(other).strip()
