"""关键词匹配。

核心思路：把关键词分成「实体词」和「意图词」。

- 实体词：魂土、业原火、秘闻、超鬼王、结界…… 决定了「问的是哪块内容」，
  有实体词时话题就由它定，因为它的信息量最大；
- 意图词：阵容、打法、推荐、配队…… 只说明「想看什么形式」，不能单独定话题。

早期版本按字符串长度选主词，导致「魂土阵容推荐」被判成「阵容」，
所以这里改成实体优先。

再加一层锚点判定，解决群里随口说个「阵容」就触发的问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# topic -> 别名列表
TOPIC_ALIASES: dict[str, list[str]] = {
    "御魂": [
        "御魂搭配", "御魂怎么带", "御魂", "魂土", "魂十一", "魂11", "业原火",
        "招财", "魍魉", "心眼", "狰", "针女", "破势", "网切", "狂骨", "幽谷响",
        "蚌精", "珍珠", "遗念火", "涂佛", "火灵",
    ],
    "阵容": [
        "阵容推荐", "挂机阵容", "阵容", "配队", "组队", "速刷", "逢魔", "打法", "阵容怎么配",
    ],
    "活动": [
        "活动攻略", "限时活动", "剧情活动", "新活动", "活动",
    ],
    "秘闻": [
        "秘闻副本", "秘闻本", "秘闻",
    ],
    "爬塔": [
        "超鬼王", "爬塔", "荒川", "业火", "鬼王",
    ],
    # 道馆和结界是两套玩法：道馆是打别人的道馆，结界是突破/结界卡，不能混
    "道馆": [
        "道馆阵容", "道馆突破", "道馆",
    ],
    "结界": [
        "结界突破", "结界卡", "结界", "阴阳寮",
    ],
    "斗技": [
        "斗技阵容", "斗技", "竞技场", "御魂觉醒",
    ],
    "抽卡": [
        "新式神", "式神推荐", "抽卡建议", "抽卡", "狗粮", "觉醒材料",
    ],
    "通用": [
        "阴阳师攻略", "痒痒鼠", "onmyoji", "阴阳师", "yys",
    ],
}

#: 能决定话题的「实体」类 topic
ENTITY_TOPICS: frozenset[str] = frozenset(
    {"御魂", "秘闻", "爬塔", "结界", "道馆", "斗技", "活动"}
)

#: 只表示「想看什么形式」的 topic
INTENT_TOPICS: frozenset[str] = frozenset({"阵容", "抽卡", "通用"})

# 这些词单独出现时太泛，必须搭配锚点或实体词
WEAK_ALIASES: frozenset[str] = frozenset(
    {
        "活动", "阵容", "配队", "组队", "打法", "抽卡", "速刷", "斗技",
        "结界", "御魂觉醒", "阵容推荐", "挂机阵容", "阵容怎么配", "鬼王",
    }
)

ANCHOR_WORDS: tuple[str, ...] = ("阴阳师", "yys", "痒痒鼠", "onmyoji", "安倍晴明")

INTENT_WORDS: tuple[str, ...] = (
    "攻略", "推荐", "怎么", "如何", "思路", "打法", "配队", "速刷",
    "最新", "最近", "有没有", "求", "看看", "什么", "哪套", "哪个",
)

# 常见式神名，命中时作为额外搜索关键词
SPIRIT_NAMES: tuple[str, ...] = (
    "须佐之男", "伊邪那美", "天照", "月读", "八岐大蛇", "不知火", "酒吞童子",
    "大天狗", "茨木童子", "玉藻前", "彼岸花", "面灵气", "鬼切", "白狼",
    "追月神", "辉夜姬", "阎魔", "两面佛", "阿修罗", "帝释天", "紧那罗",
    "千姬", "铃鹿御前", "铃彦姬", "缘结神", "不见岳", "孔雀明王", "御馔津",
    "大岳丸", "荒川之主", "花鸟卷", "书翁", "座敷童子", "山兔", "镰鼬",
    "妖刀姬", "吸血姬", "青行灯", "食发鬼", "童男", "日和坊", "樱花妖",
    "雪女", "椒图", "萤草", "黑无常", "酒吞", "茨木", "大舅", "须佐",
)

_NORMALIZE_RE = re.compile(r"[\s\u3000，,。.！!？?、；;：:（）()\[\]【】\"'“”‘’~～|]+")


def _build_index(topics: frozenset[str]) -> list[tuple[str, str]]:
    """按别名长度倒序，保证「魂十一」优先于「魂土」、「御魂搭配」优先于「御魂」。"""
    pairs = [
        (alias.lower(), topic)
        for topic, aliases in TOPIC_ALIASES.items()
        if topic in topics
        for alias in aliases
    ]
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


_ENTITY_INDEX = _build_index(ENTITY_TOPICS)
_INTENT_INDEX = _build_index(INTENT_TOPICS)
_ALL_ALIASES = sorted(
    {alias for aliases in TOPIC_ALIASES.values() for alias in aliases},
    key=len,
    reverse=True,
)


def normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", (text or "")).lower()


@dataclass
class MatchResult:
    topic: str
    alias: str
    raw: str
    anchors: list[str] = field(default_factory=list)
    second_alias: str | None = None
    has_intent: bool = False
    spirits: list[str] = field(default_factory=list)
    #: 手动查询时用户原样输入的词，优先级最高
    extra_keywords: list[str] = field(default_factory=list)

    @property
    def search_keywords(self) -> list[str]:
        """给缓存查询和搜索引擎用的关键词，越靠前越重要。"""
        words: list[str] = []
        for candidate in [
            *self.extra_keywords,
            self.alias,
            self.second_alias,
            *self.spirits,
            self.topic,
        ]:
            if candidate and candidate not in words:
                words.append(candidate)
        return words


def _hits(index: list[tuple[str, str]], normalized: str) -> list[tuple[str, str]]:
    return [(alias, topic) for alias, topic in index if alias and alias in normalized]


def match(text: str, mode: str = "strict") -> MatchResult | None:
    """匹配消息，返回 None 表示不该响应。

    mode: strict 需要更强的证据；loose 只要命中实体词就响应。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    normalized = normalize(raw)
    if not normalized:
        return None

    entity_hits = _hits(_ENTITY_INDEX, normalized)
    intent_hits = _hits(_INTENT_INDEX, normalized)
    if not entity_hits and not intent_hits:
        return None

    anchors = [word for word in ANCHOR_WORDS if normalize(word) in normalized]
    has_intent_word = any(word in raw for word in INTENT_WORDS)
    spirits = [name for name in SPIRIT_NAMES if name in raw]

    # 话题由实体词决定；没有实体词才退回意图词
    if entity_hits:
        primary_alias, primary_topic = entity_hits[0]
    else:
        primary_alias, primary_topic = intent_hits[0]

    # 第二关键词：优先取不同 topic 的实体词，其次取意图词
    second_alias: str | None = None
    for alias, topic in entity_hits:
        if topic != primary_topic:
            second_alias = alias
            break
    if second_alias is None:
        for alias, topic in intent_hits:
            if topic != primary_topic:
                second_alias = alias
                break

    strong = primary_alias not in WEAK_ALIASES
    has_entity = bool(entity_hits)

    if anchors:
        # 已经明确在说这个游戏，命中任何别名都可以响应
        allowed = True
    elif has_entity and strong:
        if mode == "loose":
            allowed = True
        else:
            allowed = has_intent_word or bool(intent_hits) or bool(spirits)
    else:
        allowed = False

    if not allowed:
        return None

    return MatchResult(
        topic=primary_topic,
        alias=primary_alias,
        raw=raw,
        anchors=anchors,
        second_alias=second_alias,
        has_intent=has_intent_word,
        spirits=spirits,
    )


def describe_aliases() -> str:
    """给用户看的别名清单。"""
    lines = ["可用关键词："]
    for topic, aliases in TOPIC_ALIASES.items():
        shown = " / ".join(aliases[:8])
        lines.append(f"  {topic}：{shown}")
    return "\n".join(lines)


def all_trigger_words() -> list[str]:
    """用于构建触发热词正则：排除泛词，加上锚点词。"""
    words = {word for word in _ALL_ALIASES if word not in WEAK_ALIASES}
    words.update(ANCHOR_WORDS)
    return sorted(words, key=len, reverse=True)
