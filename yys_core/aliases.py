"""关键词匹配。

核心思路：把关键词分成三类。

- 实体词：魂土、业原火、秘闻、超鬼王、结界…… 决定了「问的是哪块内容」，
  有实体词时话题就由它定，因为它的信息量最大；
- 意图词：阵容、打法、推荐、配队…… 只说明「想看什么形式」，不能单独定话题，
  更**不能拿来判断相关性**——几乎每篇攻略标题里都有「阵容」，
  拿它做匹配等于没匹配（这也是早期版本「问魂土却全是无关内容」的根因）；
- 锚点词：阴阳师 / yys / 痒痒鼠，用来确认「确实在聊这个游戏」。

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
        "魂王", "魂十", "魂海", "魂十三", "魂九", "日轮", "御灵", "痴念之卷",
        "招财", "魍魉", "心眼", "狰", "针女", "破势", "网切", "狂骨", "幽谷响",
        "蚌精", "珍珠", "遗念火", "涂佛", "火灵",
    ],
    # 真·八岐大蛇是每周轮换的独立副本，跟御魂不是一回事（掉的是首领御魂胚子）
    "真蛇": [
        "真蛇", "真八岐大蛇", "真·八岐大蛇", "真八",
    ],
    # 逢魔之时那一类限时首领
    "首领": [
        "极荒骷髅", "荒骷髅", "极土蜘蛛", "土蜘蛛", "地震鲶", "地域鬼王",
        "麒麟", "首领退治", "阴界之门", "狭间暗域", "狭间", "逢魔之时", "逢魔",
    ],
    "阵容": [
        "阵容推荐", "挂机阵容", "阵容", "配队", "组队", "速刷", "打法", "阵容怎么配",
    ],
    "活动": [
        "活动攻略", "限时活动", "剧情活动", "新活动", "花合战", "英杰试炼", "活动",
    ],
    "秘闻": [
        "秘闻副本", "秘闻本", "秘闻竞速", "秘闻百战", "秘闻",
    ],
    "爬塔": [
        "超鬼王", "爬塔", "荒川", "业火",
    ],
    # 道馆和结界是两套玩法：道馆是打别人的道馆，结界是突破/结界卡，不能混
    "道馆": [
        "道馆阵容", "道馆突破", "道馆",
    ],
    "结界": [
        "结界突破", "结界卡", "结界", "阴阳寮",
    ],
    "斗技": [
        "斗技阵容", "斗技", "竞技场", "御魂觉醒", "名仕", "为崽而战", "百鬼奕",
    ],
    "契灵": [
        "契灵", "术印",
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
    {
        "御魂", "真蛇", "首领", "秘闻", "爬塔", "结界", "道馆", "斗技",
        "活动", "契灵",
    }
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

#: 只在说明「想看什么形式」的别名，判断相关性时必须排除
INTENT_ALIASES: frozenset[str] = frozenset(
    {alias for topic, aliases in TOPIC_ALIASES.items() if topic in INTENT_TOPICS
     for alias in aliases}
    | set(INTENT_WORDS)
)

#: 泛词：留空也能触发，但完全不能用来判断相关性
GENERIC_WORDS: frozenset[str] = frozenset(
    {"阴阳师", "yys", "痒痒鼠", "onmyoji", "安倍晴明", "攻略", "最新", "最近", "通用"}
)

_NORMALIZE_RE = re.compile(r"[\s\u3000，,。.！!？?、；;：:（）()\[\]【】\"'“”‘’~～|]+")
_AT_RE = re.compile(r"@\S+")
_DISPLAY_STRIP = " \t\r\n，,。.!！?？、；;：:/／~～@#【】[]（）()"


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
#: 从短到长，用来把组合词收回成核心词
_SHORT_ALIASES = sorted({alias for alias in _ALL_ALIASES if len(alias) >= 2}, key=len)


def normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", (text or "")).lower()


def is_content_word(word: str) -> bool:
    """这个词能不能用来判断「这条内容是不是用户在问的」。"""
    word = (word or "").strip()
    if not word:
        return False
    return word not in INTENT_ALIASES and word not in GENERIC_WORDS


def strip_intent(word: str) -> str:
    """从「斗技阵容」「阵容推荐」里剥掉意图部分，只留「斗技」。

    剥完什么都不剩，说明这个词本身只是意图（比如「挂机阵容」），不能当关键词。
    """
    for alias in sorted(INTENT_ALIASES, key=len, reverse=True):
        if len(alias) >= 2 and alias != word and alias in word:
            word = word.replace(alias, "")
    return word.strip()


def canonical_word(word: str) -> str:
    """组合词收回成核心词：「御魂搭配」-> 御魂，「斗技阵容」-> 斗技。

    不做这一步的话，用户说「御魂搭配」，标题里写的是「御魂怎么配」，两边永远对不上。
    """
    word = strip_intent((word or "").strip())
    if not word:
        return ""
    for alias in _SHORT_ALIASES:
        if len(alias) < len(word) and alias in word:
            return alias
    return word


@dataclass(frozen=True)
class Keywords:
    """抓取与排序用的关键词集合，分强弱两档。

    strong：能单独定位内容的词，比如「魂土」「须佐之男」；
    weak：只圈定大方向的词，比如「魂土」问题里的「御魂」——
          讲御魂整理的帖子跟魂土沾边，但优先级要低一档。

    意图词（阵容 / 打法 / 攻略 / 推荐）一律不进这里。原因见模块开头。
    """

    strong: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.strong or self.weak)

    @property
    def all(self) -> list[str]:
        """给缓存查询用的扁平列表，强词在前。"""
        words = list(self.strong)
        for word in self.weak:
            if word not in words:
                words.append(word)
        return words

    @property
    def label(self) -> str:
        return self.all[0] if self.all else "攻略"

    def query(self) -> str:
        """给搜索引擎/B站用的查询串。

        只留强词：实测「阴阳师 魂土」比「阴阳师 魂土 阵容 御魂」精准得多，
        多加词会把「咲耶版本PVE阵容合集」这种大杂烩顶上来。
        """
        parts = ["阴阳师"]
        for word in list(self.strong[:2]) or list(self.weak[:2]):
            if word not in parts:
                parts.append(word)
        return " ".join(parts)


def keywords_from_words(words: list[str]) -> Keywords:
    """给「兜底搜索话题词」这类纯文字配置生成 Keywords。"""
    strong: list[str] = []
    weak: list[str] = []
    for raw in words:
        word = (raw or "").strip()
        if not word or word in GENERIC_WORDS or word in INTENT_ALIASES:
            continue
        if word in strong or word in weak:
            continue
        (weak if word in WEAK_ALIASES else strong).append(word)
    return Keywords(tuple(strong), tuple(weak))


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
    def keywords(self) -> Keywords:
        """强词 = 具体别名 + 式神名；弱词 = 话题级词（活动 / 斗技 / 御魂）。"""
        strong: list[str] = []
        weak: list[str] = []

        # 式神名放前面：用户点名了某个式神，它就是最具体的线索
        for word in [*self.spirits, self.alias]:
            word = canonical_word(word)
            if not word or word in strong or word in weak or not is_content_word(word):
                continue
            (weak if word in WEAK_ALIASES else strong).append(word)

        # 手输的词（/yys xxx）原样保留，用户自己知道想要什么
        for word in self.extra_keywords:
            word = (word or "").strip()
            if word and word not in strong and word not in weak and is_content_word(word):
                strong.append(word)

        # 话题名永远是「大方向」，哪怕它自己没被列为泛词：
        # 问「魂土」时，「御魂」只能当弱词，不然「御魂整理」会跟「魂土阵容」平起平坐
        for word in [self.topic, self.second_alias]:
            word = canonical_word(word)
            if not word or word in strong or word in weak:
                continue
            if is_content_word(word):
                weak.append(word)

        return Keywords(tuple(strong), tuple(weak))

    @property
    def search_keywords(self) -> list[str]:
        """给缓存查询和搜索引擎用的关键词，越靠前越重要。"""
        return self.keywords.all

    @property
    def display(self) -> str:
        """卡片标题里显示的话题，用用户自己的说法，别换词。"""
        text = _AT_RE.sub(" ", self.raw)
        for word in sorted(ANCHOR_WORDS, key=len, reverse=True):
            text = re.sub(re.escape(word), " ", text, flags=re.I)
        text = re.sub(r"\s+", "", text).strip(_DISPLAY_STRIP)
        # 只说「阴阳师」两个字时没有具体话题，标题就写「最新攻略」，
        # 别把内部的「通用」这种分类名直接甩给用户
        return text[:14] or ("最新攻略" if self.topic == "通用" else self.topic)


def _hits(index: list[tuple[str, str]], normalized: str) -> list[tuple[str, str]]:
    return [(alias, topic) for alias, topic in index if alias and alias in normalized]


def _dedupe_spirits(spirits: list[str]) -> list[str]:
    """「须佐之男」和「须佐」会同时命中，只留长的那个。"""
    kept: list[str] = []
    for name in sorted(spirits, key=len, reverse=True):
        if any(name in longer for longer in kept):
            continue
        kept.append(name)
    return kept


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
    spirits = _dedupe_spirits([name for name in SPIRIT_NAMES if name in raw])

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
