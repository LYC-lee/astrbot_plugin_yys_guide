"""离线自测：用一个假的 astrbot 模块顶替框架，验证纯逻辑部分是否正确。

跑法：python _selftest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
# tests 可能和插件目录同级（开发工作区），也可能就在插件目录里（git 仓库布局）
PLUGIN_DIR = (
    _PARENT
    if os.path.isfile(os.path.join(_PARENT, "main.py"))
    else os.path.join(_PARENT, "astrbot_plugin_yys_guide")
)
sys.path.insert(0, PLUGIN_DIR)


# ---------------------------------------------------------------- stub astrbot


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
            def deco(func):
                func._astr_filter = ("regex", pattern)
                return func

            return deco

        @staticmethod
        def command(name):
            def deco(func):
                func._astr_filter = ("command", name)
                return func

            return deco

        @staticmethod
        def permission_type(*args, **kwargs):
            def deco(func):
                return func

            return deco

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self):
            self.parts = []

        def message(self, text):
            self.parts.append(text)
            return self

    event.filter = _Filter
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain

    class Star:
        def __init__(self, context):
            self.context = context

    class Context:
        pass

    def register(name, author, desc, version, repo=""):
        def deco(cls):
            cls._astr_meta = (name, author, desc, version, repo)
            return cls

        return deco

    star.Context = Context
    star.Star = Star
    star.register = register

    astrbot.api = api
    astrbot.logger = logger
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star


_install_astrbot_stub()

from yys_core import aliases  # noqa: E402
from yys_core.cache import GuideCache  # noqa: E402
from yys_core.filters import filter_items, is_seo_junk, is_spam  # noqa: E402
from yys_core.models import (  # noqa: E402
    CN_TZ,
    GuideItem,
    clean_text,
    fmt_human_time,
    title_core,
    title_key,
)
from yys_core.render import render  # noqa: E402
from yys_core.scheduler import WarmupScheduler, parse_times  # noqa: E402
from yys_core.summarize import _parse_json_array, rule_summarize  # noqa: E402

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


# ---------------------------------------------------------------- 1. 别名匹配

print("\n=== 1. 触发匹配 ===")

cases_should_match = [
    ("阴阳师御魂怎么配", "strict"),
    ("yys 魂土阵容推荐", "strict"),
    ("魂十一怎么速刷", "strict"),
    ("痒痒鼠 超鬼王攻略", "strict"),
    ("业原火阵容", "strict"),
    ("秘闻副本 鬼切", "strict"),
    ("须佐之男御魂带什么", "strict"),
    ("魂土阵容", "strict"),
    ("魂土", "loose"),
    ("阴阳师今天有什么活动", "strict"),
]

for text, mode in cases_should_match:
    result = aliases.match(text, mode)
    check(f"应触发：{text}", result is not None, f"-> {result}")

cases_should_not_match = [
    ("今天天气不错", "loose"),
    ("这个阵容很强", "loose"),
    ("活动什么时候结束", "loose"),
    ("我们去打游戏吧", "strict"),
    ("御魂", "strict"),
]

for text, mode in cases_should_not_match:
    result = aliases.match(text, mode)
    check(f"不应触发：{text}", result is None, f"-> topic={getattr(result, 'topic', None)}")

sample = aliases.match("yys 魂土阵容推荐", "strict")
check("topic 取到御魂", sample is not None and sample.topic == "御魂", f"-> {getattr(sample, 'topic', None)}")
check(
    "关键词包含魂土",
    sample is not None and "魂土" in sample.search_keywords,
    f"-> {getattr(sample, 'search_keywords', None)}",
)

spirit_case = aliases.match("须佐之男御魂怎么配", "strict")
check(
    "识别式神名",
    spirit_case is not None and "须佐之男" in spirit_case.search_keywords,
    f"-> {getattr(spirit_case, 'search_keywords', None)}",
)


# ---------------------------------------------------------------- 2. 文本清洗

print("\n=== 2. 文本清洗 ===")
check("去 em 标签", clean_text('<em class="keyword">御魂</em> 搭配') == "御魂 搭配")
check("解 HTML 实体", clean_text("A&amp;B&nbsp;C") == "A&B C")
check("压掉空行", "\n\n\n" not in clean_text("a\n\n\n\nb"))


# ---------------------------------------------------------------- 3. 卡片渲染

print("\n=== 3. 卡片渲染 ===")
now = datetime.now(CN_TZ)
items = [
    GuideItem(
        source="bilibili",
        item_id="BV1xx",
        title="须佐之男御魂怎么配",
        url="https://www.bilibili.com/video/BV1xx",
        author="某UP",
        published=now - timedelta(hours=2),
        digest="须佐之男推荐狂骨四件套",
    ),
    GuideItem(
        source="nga",
        item_id="12345",
        title="[攻略] 魂土稳定速刷",
        url="https://bbs.nga.cn/read.php?tid=12345",
        author="nga用户",
        published=now - timedelta(days=1),
        digest="三分钟速刷",
    ),
]
entries = rule_summarize(items)
entries[0].team = "须佐之男 / 玉藻前"
entries[0].souls = "须佐：狂骨4 + 鬼灵歌伎2"
entries[0].howto = "起手拉条，二技能接大"
entries[0].degraded = False

for style in ("card", "markdown", "plain"):
    text = render("御魂", entries, style)
    check(f"{style} 非空", len(text) > 60)
    check(f"{style} 含链接", "bilibili.com" in text)
    check(f"{style} 含阵容", "狂骨" in text)

card = render("御魂", entries, "card")
check("卡片用全角空格缩进", "\u3000" in card)
check("卡片有分隔线", "─" in card)
print("\n--- card 样式实际输出 ---")
print(card)
print("--- 输出结束 ---\n")


# ---------------------------------------------------------------- 4. JSON 解析

print("\n=== 4. 模型输出解析 ===")
payload = [{"title": "t1", "team": "a", "souls": "b", "howto": "c", "note": ""}]
check("裸 JSON", _parse_json_array(json.dumps(payload)) == payload)
check(
    "带 ```json 包裹",
    _parse_json_array("```json\n" + json.dumps(payload) + "\n```") == payload,
)
check("前后有废话", _parse_json_array("好的，结果如下：" + json.dumps(payload)) == payload)
check("非法 JSON 返回 None", _parse_json_array("完全不是 JSON") is None)
check("空字符串返回 None", _parse_json_array("") is None)


# ---------------------------------------------------------------- 5. 缓存

print("\n=== 5. 缓存 ===")


async def check_cache() -> None:
    tmp = tempfile.mkdtemp()
    cache = GuideCache(os.path.join(tmp, "test.db"), ttl_minutes=60, max_rows=100)
    await cache.init()

    await cache.upsert(items)
    rows = await cache.query(["御魂"], limit=5)
    check("写入后能按关键词查到", len(rows) == 1, f"-> {len(rows)}")

    rows = await cache.query(["不存在的词"], limit=5)
    check("不相关关键词查不到", len(rows) == 0, f"-> {len(rows)}")

    rows = await cache.query(None, limit=5)
    check("无条件查询返回全部", len(rows) == 2, f"-> {len(rows)}")

    stats = await cache.stats()
    check("统计 total=2", stats["total"] == 2, f"-> {stats}")

    # 重复写入同一 key 不应产生新行
    await cache.upsert(items)
    stats = await cache.stats()
    check("重复写入不新增行", stats["total"] == 2, f"-> {stats}")

    # 过期数据：ttl 缩到 0 秒后查不到
    stale = GuideCache(os.path.join(tmp, "stale.db"), ttl_minutes=1, max_rows=100)
    await stale.init()
    await stale.upsert(items)
    stale.ttl_seconds = 1
    await asyncio.sleep(1.2)
    rows = await stale.query(None, limit=5)
    check("TTL 过期后查不到", len(rows) == 0, f"-> {len(rows)}")
    rows = await stale.query(None, limit=5, allow_stale=True)
    check("allow_stale 能查到过期数据", len(rows) == 2, f"-> {len(rows)}")

    await cache.close()
    await stale.close()


asyncio.run(check_cache())


# ---------------------------------------------------------------- 6. 调度

print("\n=== 6. 定时调度 ===")
check("解析时间点", parse_times(["09:00", "20:30"]) == [(9, 0), (20, 30)])
check("忽略非法值", parse_times(["25:00", "abc", "08:70"]) == [])
check("重复时间去重", parse_times(["09:00", "09:00"]) == [(9, 0)])

scheduler = WarmupScheduler([(9, 0), (20, 0)], lambda: asyncio.sleep(0))
fixed_now = datetime(2026, 11, 3, 21, 0, 0)
check(
    "21:00 时下次执行是次日 09:00",
    scheduler.next_run(fixed_now) == datetime(2026, 11, 4, 9, 0),
    f"-> {scheduler.next_run(fixed_now)}",
)
fixed_now = datetime(2026, 11, 3, 8, 0, 0)
check(
    "08:00 时下次执行是当天 09:00",
    scheduler.next_run(fixed_now) == datetime(2026, 11, 3, 9, 0),
    f"-> {scheduler.next_run(fixed_now)}",
)


# ---------------------------------------------------------------- 7. 时间格式化

print("\n=== 7. 时间格式化 ===")
check("刚刚", fmt_human_time(now) == "刚刚")
check("分钟前", fmt_human_time(now - timedelta(minutes=30)) == "30 分钟前")
check("小时前", fmt_human_time(now - timedelta(hours=3)) == "3 小时前")
check("天前", fmt_human_time(now - timedelta(days=2)) == "2 天前")
check("未知时间", fmt_human_time(None) == "时间未知")


# ---------------------------------------------------------------- 8. 内容过滤

print("\n=== 8. 内容过滤 ===")

check("代练广告", is_spam("满级御魂8400+，精调方案，私信优惠"))
check("调号黑话", is_spam("专业调号 稳定不封"))
check("组合词", is_spam("加群有优惠"))
check("正常攻略不误伤", not is_spam("【阴阳师】道馆一套阵容怒斩600功勋！"))

# 搜索引擎落地页：标题堆砌站点名和 slogan
check(
    "官网SEO尾巴",
    is_seo_junk(
        "阴阳师:当前版本道馆突破阵容教学 _《阴阳师》手游官网 "
        "《阴阳师》手游官网_唯美奇幻 匠心巨制,开启唯美奇幻之旅"
    ),
)
check("手游官网", is_seo_junk("阴阳师攻略_阴阳师手游官网"))
check("关键词堆砌", is_seo_junk("阴阳师_阴阳师攻略_阴阳师式神"))
check(
    "B站站点尾巴",
    is_seo_junk("【阴阳师】道馆阵容?看这一个视频就够了!_哔哩哔哩bilibili_阴阳师"),
)
check("知乎站点尾巴", is_seo_junk("阴阳师御魂怎么配_知乎"))
check("九游手机游戏", is_seo_junk("阴阳师道馆什么阵容强势道馆强势阵容推荐__九游手机游戏"))
check("双下划线站点尾巴", is_seo_junk("阴阳师魂土阵容推荐__17173"))
check("正常标题不误伤", not is_seo_junk("【阴阳师】咲耶&ur大天狗版本最新道馆进攻阵容（附图文方案码）"))
check("带官网的攻略也不算", not is_seo_junk("阴阳师官网更新公告解读"))
check("大神标题不误伤", not is_seo_junk("【秘闻副本】荒川百战挂机前百思路—3+2通关和2+3通关，轻松拿满奖励"))
check("空标题不报错", not is_seo_junk(""))

_seo_item = GuideItem(
    source="web",
    item_id="x1",
    title="阴阳师套娃_阴阳师手游官网_阴阳师攻略",
    url="https://example.com/1",
)
_ok_item = GuideItem(
    source="web",
    item_id="x2",
    title="【阴阳师】魂土11秒阵容",
    url="https://example.com/2",
)
_kept, _dropped = filter_items([_seo_item, _ok_item])
check("filter_items 丢弃SEO页", _dropped == 1, f"dropped={_dropped}")
check("filter_items 保留正常条目", len(_kept) == 1 and _kept[0].item_id == "x2")


# ---------------------------------------------------------------- 结合插件主体

import main as plugin_main  # noqa: E402


# ---------------------------------------------------------------- 9. 近似标题去重

print("\n=== 9. 近似标题去重 ===")
check("title_core 去掉开头标签", title_core("【秘闻副本】荒川百战") == "荒川百战")
check(
    "title_key 抹掉「图片版」",
    title_key("【UR大天狗活动】飞羽凌云图片版：三套阵容+规则奖励！")
    == title_key("【UR大天狗活动】飞羽凌云：三套阵容+规则奖励！"),
    title_key("【UR大天狗活动】飞羽凌云图片版：三套阵容+规则奖励！"),
)
check(
    "同系列不同阵容不合并",
    title_key("【秘闻副本】荒川百战3+2") != title_key("【秘闻副本】荒川百战2+3"),
)


def _dup_fixture() -> list[GuideItem]:
    """姝酱的小本本 在大神上把同一篇攻略拆成「图片版 / 普通版」发了两条。"""
    return [
        GuideItem(
            source="ds163",
            item_id="a",
            title="【UR大天狗活动】飞羽凌云图片版：三套阵容+规则奖励！",
            url="u1",
            author="姝酱的小本本",
            published=datetime(2026, 10, 28, tzinfo=CN_TZ),
        ),
        GuideItem(
            source="ds163",
            item_id="b",
            title="【UR大天狗活动】飞羽凌云：三套阵容+规则奖励！",
            url="u2",
            author="姝酱的小本本",
            published=datetime(2026, 10, 27, tzinfo=CN_TZ),
        ),
        GuideItem(
            source="ds163",
            item_id="c",
            title="【咲耶首领】神眸垂世：4套满收益阵容",
            url="u3",
            author="姝酱的小本本",
        ),
        GuideItem(  # 同一个标题但换了个作者，不是同一个人，不该合并
            source="ds163",
            item_id="d",
            title="【UR大天狗活动】飞羽凌云：三套阵容+规则奖励！",
            url="u4",
            author="别人",
        ),
    ]


_dup_kept = plugin_main.YysGuidePlugin._drop_near_duplicates(_dup_fixture())
check(
    "图片版/普通版合并成一条",
    [item.item_id for item in _dup_kept] == ["a", "c", "d"],
    f"-> {[item.item_id for item in _dup_kept]}",
)
check("不同作者的相同标题保留", any(item.item_id == "d" for item in _dup_kept))


# ---------------------------------------------------------------- 汇总

print(f"\n{'=' * 40}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 40)
sys.exit(1 if FAIL else 0)
