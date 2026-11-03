"""搜索准确度回归测试（离线）。

锁住两个真实踩过的坑：

1. 问「魂土阵容」，卡片里却有「本周活动日程表」——因为相关性是用
   「阵容」这种意图词判断的，而它出现在几乎每篇攻略标题里；
   并且不够 5 条时会拿博主其他动态硬补。
2. 卡片标题显示成「御魂」（内部话题名），用户以为自己搜错了。

跑法：python tests/test_accuracy.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
PLUGIN_DIR = (
    _PARENT
    if os.path.isfile(os.path.join(_PARENT, "main.py"))
    else os.path.join(_PARENT, "astrbot_plugin_yys_guide")
)
sys.path.insert(0, PLUGIN_DIR)


def _install_astrbot_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    api.logger = logger
    api.AstrBotConfig = dict

    class _Filter:
        @staticmethod
        def regex(pattern):
            return lambda func: func

        @staticmethod
        def command(name):
            return lambda func: func

    class AstrMessageEvent:
        pass

    class MessageChain:
        def message(self, text):
            return self

    event.filter = _Filter
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = Star
    star.register = lambda *a, **k: (lambda cls: cls)

    astrbot.api = api
    astrbot.logger = logger
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star


_install_astrbot_stub()

from yys_core import aliases  # noqa: E402
from yys_core.models import CN_TZ, GuideItem  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {detail}")


# ---------------------------------------------------------------- 关键词分层

print("\n=== 1. 关键词分层 ===")

m = aliases.match("@大肥鱼 阴阳师魂土阵容", "strict")
check("能匹配「阴阳师魂土阵容」", m is not None)
kw = m.keywords
check("强词只有魂土", kw.strong == ("魂土",), f"-> {kw.strong}")
check("御魂降为弱词", kw.weak == ("御魂",), f"-> {kw.weak}")
check("阵容不进关键词", "阵容" not in kw.all, f"-> {kw.all}")
check("搜索串只用具体词", kw.query() == "阴阳师 魂土", f"-> {kw.query()}")
check("卡片标题回显用户说法", m.display == "魂土阵容", f"-> {m.display}")

m2 = aliases.match("须佐之男御魂怎么配", "strict")
check(
    "式神名排在最前",
    m2 is not None and m2.keywords.strong[0] == "须佐之男",
    f"-> {getattr(m2, 'keywords', None)}",
)
check(
    "式神 + 御魂 都进搜索串",
    m2 is not None and m2.keywords.query() == "阴阳师 须佐之男 御魂",
    f"-> {m2.keywords.query() if m2 else None}",
)

m3 = aliases.match("阴阳师今天有什么活动", "strict")
check(
    "「活动」这种泛词降为弱词",
    m3 is not None and m3.keywords.strong == () and m3.keywords.weak == ("活动",),
    f"-> {getattr(m3, 'keywords', None)}",
)
check(
    "弱词也能进搜索串",
    m3 is not None and m3.keywords.query() == "阴阳师 活动",
    f"-> {m3.keywords.query() if m3 else None}",
)

# ---------------------------------------------------------------- 相关性分级

print("\n=== 2. 相关性分级 ===")

from main import YysGuidePlugin  # noqa: E402


def _cfg() -> dict:
    return {
        "enabled": True,
        "max_items": 5,
        "strict_relevance": True,
        "author_fetch_limit": 12,
        "core_authors": [
            {"__template_key": "ds163", "uid": "a" * 32, "name": "槐夏三十"},
            {"__template_key": "ds163", "uid": "b" * 32, "name": "yys雯雯"},
        ],
        "topics": [],
        "sources": {},
        "summary": {},
        "render": {"style": "card"},
        "cache": {"ttl_minutes": 180, "max_rows": 100},
        "warmup": {"enabled": False, "times": [], "push_umo": []},
        "cookies": {},
        "search": {},
    }


plugin = YysGuidePlugin(None, _cfg())
now = datetime.now(CN_TZ)


def _item(title: str, author: str = "槐夏三十", source: str = "ds163", hours: int = 1):
    return GuideItem(
        source=source,
        item_id=title[:8],
        title=title,
        url="https://example.com/" + title[:6],
        author=author,
        published=now - timedelta(hours=hours),
        meta={"uid": "a" * 32},
    )


kw_soil = aliases.match("阴阳师魂土阵容", "strict").keywords
check("命中魂土 → 2 级", plugin._relevance(_item("魂土13秒双白蛋"), kw_soil) == 2)
check("只命中御魂 → 1 级", plugin._relevance(_item("御魂整理16.0版"), kw_soil) == 1)
check(
    "只命中阵容 → 0 级",
    plugin._relevance(_item("五式神不变阵挂机名仕合集，共九套阵容推荐"), kw_soil) == 0,
)
check("什么都没命中 → 0 级", plugin._relevance(_item("本周活动日程表"), kw_soil) == 0)


# ---------------------------------------------------------------- 卡片不再凑数

print("\n=== 3. 卡片不再拿无关内容凑数 ===")

# 复刻截图那一轮的真实返回：5 条博主最新动态里一条魂土都没有
screenshot = {
    it.key: it
    for it in [
        _item("【体验服式神改动】葛叶削弱方案—狐族庇佑大改", hours=2),
        _item("【UR大天狗版本】五式神不变阵挂机名仕合集—共九套阵容推荐", hours=11),
        _item("【秘闻副本】荒川百战挂机前百思路—3+2通关", hours=14),
        _item("【契灵篇】万言羊阵容推荐", author="Mico林木森", hours=20),
        _item("本周活动日程表 10.28-11.3", author="yys雯雯", hours=26),
    ]
}
kept = plugin._finalize(dict(screenshot), 5, kw_soil)
check("一条无关内容都不返回", kept == [], f"-> {[i.title for i in kept]}")

# 有一条魂土、一条御魂整理：都要，魂土排前面
mixed = dict(screenshot)
mixed.update(
    {
        "soil": _item("魂土15s超低配阵容", author="某UP", source="bilibili", hours=30),
        "souls": _item("御魂整理16.0版", author="yys方寒", hours=40),
    }
)
kept = plugin._finalize(mixed, 5, kw_soil)
check(
    "相关的都保留且魂土在前",
    [i.title for i in kept] == ["魂土15s超低配阵容", "御魂整理16.0版"],
    f"-> {[i.title for i in kept]}",
)
check("无关内容仍然被丢弃", len(kept) == 2, f"-> {len(kept)}")

# 关掉严格相关性 → 退回旧行为（会凑满 5 条）
plugin.config["strict_relevance"] = False
loose = plugin._finalize(dict(screenshot), 5, kw_soil)
check("关闭严格模式后允许凑数", len(loose) == 5, f"-> {len(loose)}")
plugin.config["strict_relevance"] = True

# 没有关键词（纯「阴阳师」触发）时不做相关性过滤
check(
    "无强词时不误杀",
    len(plugin._finalize(dict(screenshot), 5, aliases.Keywords())) == 5,
)


# ---------------------------------------------------------------- 不再抢别的插件的话

print("\n=== 4. 答完就终止事件传播 ===")


class _FakeEvent:
    def __init__(self):
        self.call_llm = True
        self.stopped = False

    def should_call_llm(self, value):
        self.call_llm = value

    def stop_event(self):
        self.stopped = True


ev = _FakeEvent()
YysGuidePlugin._halt(ev)
check("禁止默认 LLM 再答一遍", ev.call_llm is False)
check("终止事件传播", ev.stopped is True)


class _BareEvent:
    """老版本 AstrBot 没有这两个方法，不能因此报错。"""


YysGuidePlugin._halt(_BareEvent())
check("方法缺失时不抛异常", True)


# ---------------------------------------------------------------- 无结果时的提示

print("\n=== 5. 没找到时的回复 ===")
import asyncio  # noqa: E402


async def _no_result() -> str:
    return await plugin._no_result_text("魂土阵容", kw_soil)


text = asyncio.run(_no_result())
check("回显用户说法", "魂土阵容" in text, text)
check("给出更具体的建议", "魂土 御魂" in text, text)

print(f"\n{'=' * 40}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 40)
sys.exit(1 if FAIL else 0)
