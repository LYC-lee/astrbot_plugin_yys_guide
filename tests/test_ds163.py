"""网易大神抓取器测试。

分两层：
1. 离线：时间解析、标题拆分、HTML 解析（用手写的 mini 片段，模拟真实页面结构）
2. 联网：真实抓一位作者，验证接口是否还有效

跑法：python tests/test_ds163.py          离线 + 联网
     python tests/test_ds163.py --offline  只跑离线
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
# tests 可能和插件目录同级（开发工作区），也可能就在插件目录里（git 仓库布局）
PLUGIN_DIR = (
    _PARENT
    if os.path.isfile(os.path.join(_PARENT, "main.py"))
    else os.path.join(_PARENT, "astrbot_plugin_yys_guide")
)
sys.path.insert(0, PLUGIN_DIR)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def _install_stub() -> None:
    """ds163 只依赖 astrbot.api.logger，桩可以很短。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_stub()

from yys_core.http import HttpClient  # noqa: E402
from yys_core.models import CN_TZ  # noqa: E402
from yys_fetchers.ds163 import Ds163Fetcher, _split_title, parse_ds_time  # noqa: E402

NOW = datetime(2026, 11, 4, 0, 30, tzinfo=CN_TZ)


# ---------------------------------------------------------------- 1. 时间解析

print("\n=== 1. 时间解析 ===")

check("刚刚", parse_ds_time("刚刚", NOW) == NOW)
check(
    "13分钟前",
    parse_ds_time("13分钟前", NOW) == NOW.replace(minute=17),
    str(parse_ds_time("13分钟前", NOW)),
)
check(
    "2小时前",
    # 00:30 往前 2 小时要跨到前一天，这里故意不用 replace(hour=) 免得写错
    parse_ds_time("2小时前", NOW) == datetime(2026, 11, 3, 22, 30, tzinfo=CN_TZ),
    str(parse_ds_time("2小时前", NOW)),
)
check(
    "3天前",
    parse_ds_time("3天前", NOW).day == 1,
    str(parse_ds_time("3天前", NOW)),
)
check(
    "昨天23:00",
    parse_ds_time("昨天23:00", NOW) == datetime(2026, 11, 3, 23, 0, tzinfo=CN_TZ),
    str(parse_ds_time("昨天23:00", NOW)),
)
check(
    "今天09:43",
    parse_ds_time("今天09:43", NOW) == datetime(2026, 11, 4, 9, 43, tzinfo=CN_TZ),
    str(parse_ds_time("今天09:43", NOW)),
)
check(
    "前天12:00",
    parse_ds_time("前天12:00", NOW) == datetime(2026, 11, 2, 12, 0, tzinfo=CN_TZ),
    str(parse_ds_time("前天12:00", NOW)),
)
check(
    "11/1 当年",
    parse_ds_time("11/1", NOW) == datetime(2026, 11, 1, tzinfo=CN_TZ),
    str(parse_ds_time("11/1", NOW)),
)
check(
    "10/30 当年",
    parse_ds_time("10/30", NOW) == datetime(2026, 10, 30, tzinfo=CN_TZ),
    str(parse_ds_time("10/30", NOW)),
)
check(
    "2026-10-25 12:00",
    parse_ds_time("2026-10-25 12:00", NOW) == datetime(2026, 10, 25, 12, 0, tzinfo=CN_TZ),
    str(parse_ds_time("2026-10-25 12:00", NOW)),
)
check(
    "2021/09/24 斜杠日期",
    parse_ds_time("2021/09/24", NOW) == datetime(2021, 9, 24, tzinfo=CN_TZ),
    str(parse_ds_time("2021/09/24", NOW)),
)
check(
    "2021/07/09 12:30 斜杠带时间",
    parse_ds_time("2021/07/09 12:30", NOW)
    == datetime(2021, 7, 9, 12, 30, tzinfo=CN_TZ),
    str(parse_ds_time("2021/07/09 12:30", NOW)),
)
check("空字符串 -> None", parse_ds_time("", NOW) is None)
check("乱七八糟 -> None", parse_ds_time("刚才在看", NOW) is None)

# 跨年：1 月看到「12/30」，必须推到去年，否则会变成未来时间
jan_now = datetime(2027, 1, 5, 12, 0, tzinfo=CN_TZ)
cross = parse_ds_time("12/30", jan_now)
check(
    "跨年 12/30 -> 上一年",
    cross == datetime(2026, 12, 30, tzinfo=CN_TZ),
    str(cross),
)
check("跨年结果不在未来", cross < jan_now, str(cross))


# ---------------------------------------------------------------- 2. 标题拆分

print("\n=== 2. 标题拆分 ===")

t, d = _split_title("【极·胧车】葛叶出站，体服挂机前三\n■ 感谢大佬提供账号测试\n■ 后续还会优化", [])
check("取首行当标题", t == "【极·胧车】葛叶出站，体服挂机前三", t)
check("其余行当摘要", d.startswith("■ 感谢大佬"), d)

t, d = _split_title("『关于今年超鬼王』攻略安排+叠加申明\n#阴阳师# #刹那之卷#", ["#阴阳师#", "#刹那之卷#"])
check("话题标签被剔除", t == "『关于今年超鬼王』攻略安排+叠加申明", t)
check("剔除话题后摘要为空", d == "", d)

t, d = _split_title("【攻略】\nUR大天狗活动6套阵容推荐", [])
check("首行过短时拼第二行", "UR大天狗" in t, t)

t, d = _split_title("", ["#阴阳师#"])
check("正文为空时回退话题名", t == "阴阳师", t)

t, d = _split_title("a" * 200, [])
check("超长标题被截断", len(t) <= 81, f"len={len(t)}")


# ---------------------------------------------------------------- 3. HTML 解析

print("\n=== 3. HTML 解析（离线样本）===")

MINI = '''
<main class="user-page__main"><div class="user-page__feeds">
<div id="6ae707b3a9b3f049ca166ac4" class="feed-card" style="display:none;">
  <a href="/feed/6ae707b3a9b3f049ca166ac4/" class="feed-card__link-hide"></a>
  <div class="feed-card__header"><div class="userbar">
    <div class="user-name user-name--famous"><a href="/user/x/"><span>Mico林木森</span></a></div>
    <div class="time-location"><time class="time-location__time">     11/1   </time>
      <span class="time-location__location">发布于 湖南</span></div>
  </div></div>
  <div class="feed-card__body">
    <div class="feed-card__subject"><div class="feed-card-ellipsis__text feed-text">『关于今年超鬼王』攻略安排+叠加申明<br class="br"/><br class="br"/><font class="highlight" color="#5778a1">#阴阳师#</font> <font class="highlight" color="#5778a1">#刹那之卷#</font></div></div>
    <div class="feed-card__rich"><figure><img src="https://img.166.net/reunionpub/1_20261101_abc.jpeg?imageView&amp;thumbnail=240y240" /></figure></div>
  </div>
</div>
<div id="6ae5da6739537f33142f7763" class="feed-card" style="display:none;">
  <a href="/feed/6ae5da6739537f33142f7763/" class="feed-card__link-hide"></a>
  <div class="feed-card__header"><div class="userbar">
    <div class="user-name"><a href="/user/x/"><span>yys雯雯</span></a></div>
    <div class="time-location"><time class="time-location__time">昨天23:00</time></div>
  </div></div>
  <div class="feed-card__body">
    <div class="feed-card__subject"><div class="feed-card-ellipsis__text feed-text">【道馆进攻】ur大天狗版本全套阵容<br class="br"/>🌸攻略来自流沙河底，视频他还在剪<br class="br"/>🌸此次全套共9个阵容<font class="highlight" color="#5778a1">#阴阳师#</font></div></div>
  </div>
</div>
</div></main>
'''

fetcher = Ds163Fetcher(HttpClient())
items = fetcher.parse_user_page(MINI, "b6b5bc8277e34f69aeca018db0081397")

check("解析出 2 条", len(items) == 2, f"实际 {len(items)}")
if len(items) == 2:
    a, b = items
    check("第一条 id 正确", a.item_id == "6ae707b3a9b3f049ca166ac4", a.item_id)
    check("第一条作者", a.author == "Mico林木森", a.author)
    check("第一条标题", a.title == "『关于今年超鬼王』攻略安排+叠加申明", a.title)
    check("第一条链接", a.url == "https://ds.163.com/feed/6ae707b3a9b3f049ca166ac4/", a.url)
    check("第一条时间", a.published == datetime(2026, 11, 1, tzinfo=CN_TZ), str(a.published))
    check("第一条话题", a.meta.get("topics") == ["#阴阳师#", "#刹那之卷#"], str(a.meta.get("topics")))
    check("第一条封面", "img.166.net" in a.cover and "?" not in a.cover, a.cover)
    check("source 标记", a.source == "ds163", a.source)

    check("第二条作者", b.author == "yys雯雯", b.author)
    check("第二条标题", b.title == "【道馆进攻】ur大天狗版本全套阵容", b.title)
    check("第二条摘要含正文", "流沙河底" in b.digest, b.digest)
    check("第二条时间含时分", b.published == datetime(2026, 11, 3, 23, 0, tzinfo=CN_TZ), str(b.published))
    check("br 被转成换行而非空格", "\n" not in b.title, b.title)

check("空 HTML 不报错", fetcher.parse_user_page("", "x" * 32) == [])
check("无卡片页面返回空", fetcher.parse_user_page("<html><body>404</body></html>", "x" * 32) == [])


# ---------------------------------------------------------------- 4. 参数校验

print("\n=== 4. 参数校验 ===")


async def _check_validate() -> None:
    f = Ds163Fetcher(HttpClient())
    check("uid 太短被拒", await f.fetch_author({"uid": "abc"}, 5) == [])
    check("uid 非法字符被拒", await f.fetch_author({"uid": "z" * 32}, 5) == [])
    check("uid 为空被拒", await f.fetch_author({}, 5) == [])
    check("搜索直接返回空（大神无搜索页）", await f.search("御魂", 5) == [])


asyncio.run(_check_validate())


# ---------------------------------------------------------------- 5. 联网实测

if "--offline" in sys.argv:
    print("\n=== 5. 联网实测（已跳过）===")
else:
    print("\n=== 5. 联网实测 ===")

    AUTHORS = [
        ("槐夏三十", "a9724e98c1cb4a4e931ebc3f467ea73d"),
        ("yys雯雯", "aaa923436aa440df9ac1ee3f47387b99"),
        ("Mico林木森", "b6b5bc8277e34f69aeca018db0081397"),
    ]

    async def _live() -> None:
        client = HttpClient()
        f = Ds163Fetcher(client)
        try:
            for name, uid in AUTHORS:
                got = await f.fetch_author({"uid": uid, "name": name}, 5)
                check(f"{name} 抓到内容", len(got) > 0, f"{len(got)} 条")
                if got:
                    top = got[0]
                    check(f"{name} 标题非空", bool(top.title), top.title)
                    check(f"{name} 时间可解析", top.published is not None, "时间为 None")
                    print(f"         └ {top.published:%m-%d %H:%M} | {top.title[:40]}")
                await asyncio.sleep(1.0)
        finally:
            await client.close()

    asyncio.run(_live())


# ------------------------------------------------ 6. JSON 接口解析与翻页

print("\n=== 6. JSON 接口（翻历史）===")


def _feed(fid: str, text: str, ts: int, *, title: str = "", rtype: int = 2,
          nick: str = "槐夏三十", topics: list[str] | None = None) -> dict:
    body: dict = {"text": text, "media": []}
    if title:
        body["title"] = title
    return {
        "id": fid,
        "uid": "a9724e98c1cb4a4e931ebc3f467ea73d",
        "type": rtype,
        "createTime": ts,
        "feedContent": {"type": rtype, "body": body},
        "appRole": '{"nick":"%s"}' % nick,
        "topicInfoList": [{"topicName": t} for t in (topics or [])],
    }


_f = Ds163Fetcher(HttpClient())

# 毫秒时间戳：2026-11-03 23:00 北京时间
_ts = 1793718000000

_plain = _f.parse_api_feed(_feed("6ae9f820ca8643644ac44fa3", "【秘闻副本】荒川百战挂机思路\n 🌟正文", _ts), "u")
check("普通动态能解析", _plain is not None)
check("首行当标题", _plain is not None and _plain.title == "【秘闻副本】荒川百战挂机思路", _plain.title if _plain else "")
check("毫秒时间戳换算正确", _plain is not None and _plain.published.strftime("%m-%d %H") == "11-03 23", str(_plain.published) if _plain else "")
check("作者取自 appRole", _plain is not None and _plain.author == "槐夏三十", _plain.author if _plain else "")

_long = _f.parse_api_feed(
    _feed("6adc11e469a7294ae34dc2b5", "🌟PVE方面包含：魂十、魂土、魂王、魂海", _ts,
          title="【咲耶版本】PVE阵容攻略和PVP阵容攻略合集", rtype=6, nick=""),
    "u",
)
check("长文有独立 title", _long is not None and _long.title.startswith("【咲耶版本】"), _long.title if _long else "")
check("长文标题不误用正文首行", _long is not None and "魂十" not in _long.title, _long.title if _long else "")
check("长文正文进摘要", _long is not None and "魂土" in _long.digest, _long.digest[:30] if _long else "")

check("没有正文的帖子跳过", _f.parse_api_feed(_feed("6ae9f820ca8643644ac44fa3", "   ", _ts), "u") is None)
check("id 不合法跳过", _f.parse_api_feed(_feed("bad-id", "正文", _ts), "u") is None)


class _FakeClient:
    """按调用次数依次返回预设页，同时记录请求体。"""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls: list[dict] = []

    async def post_json(self, url: str, payload: dict, **_kw) -> dict:
        self.calls.append(dict(payload))
        idx = len(self.calls) - 1
        if idx < len(self.pages):
            return self.pages[idx]
        return {"code": 200, "result": {"feeds": []}}


def _page(feeds: list[dict]) -> dict:
    return {"code": 200, "result": {"feeds": feeds}}


async def _paging_cases() -> None:
    import yys_fetchers.ds163 as mod

    old_gap = mod._PAGE_GAP
    mod._PAGE_GAP = 0  # 测试里别真等
    try:
        # 第 1 页没命中，命中在第 2 页：应该自动翻页，并把游标传下去
        page1 = [
            _feed("aaaaaaaaaaaaaaaaaaaaaaaa", "葛叶削弱方案", _ts),
            _feed("bbbbbbbbbbbbbbbbbbbbbbbb", "荒川百战挂机", _ts - 3600_000),
        ]
        page2 = [
            _feed("cccccccccccccccccccccccc", "【魂土】双白蛋11s阵容", _ts - 7200_000),
            _feed("dddddddddddddddddddddddd", "魂土带萌新", _ts - 9000_000),
            _feed("eeeeeeeeeeeeeeeeeeeeeeee", "魂土鬼使黑无SSR", _ts - 9000_100),
        ]
        client = _FakeClient([_page(page1), _page(page2)])
        got = await Ds163Fetcher(client, {"ds163_scan_pages": 4})._fetch_api(
            "u", 5, ["魂土"], 4, "测试"
        )
        check("翻到命中就停在第 2 页", len(client.calls) == 2, f"请求 {len(client.calls)} 次")
        check("第 2 页带 maxTime 游标", client.calls[1].get("maxTime") == _ts - 3600_000,
              str(client.calls[1]))
        check("两页内容都在", got is not None and len(got) == 5, str(len(got or [])))
        check(
            "命中 3 条",
            got is not None
            and sum(1 for i in got if "魂土" in i.title) == 3,
            str([i.title for i in got or []]),
        )

        # 第 1 页就够 3 条命中：不该再翻第 2 页
        rich = [
            _feed("e" * 24, "魂土阵容A", _ts),
            _feed("f" * 24, "魂土阵容B", _ts - 1000),
            _feed("1" * 24, "魂土阵容C", _ts - 2000),
        ]
        client = _FakeClient([_page(rich)])
        await Ds163Fetcher(client, {})._fetch_api("u", 12, ["魂土"], 4, "测试")
        check("命中够数不翻页", len(client.calls) == 1, f"请求 {len(client.calls)} 次")

        # 没有关键词：只取第一页
        client = _FakeClient([_page(page1), _page(page2)])
        await Ds163Fetcher(client, {})._fetch_api("u", 12, None, 4, "测试")
        check("无关键词只请求一次", len(client.calls) == 1, f"请求 {len(client.calls)} 次")

        # 接口报错 → 返回 None，让上层退回 HTML
        client = _FakeClient([{"code": 500, "errmsg": "boom"}])
        bad = await Ds163Fetcher(client, {})._fetch_api("u", 5, ["魂土"], 4, "测试")
        check("接口异常返回 None", bad is None, str(bad))

        # 翻到时间窗口之外就停
        old_ts = _ts - 400 * 86400_000
        client = _FakeClient([_page([_feed("9" * 24, "很久以前", old_ts)])])
        await Ds163Fetcher(client, {"ds163_scan_days": 180})._fetch_api(
            "u", 5, ["魂土"], 4, "测试"
        )
        check("超出时间窗口不继续翻", len(client.calls) == 1, f"请求 {len(client.calls)} 次")
    finally:
        mod._PAGE_GAP = old_gap


asyncio.run(_paging_cases())


# ---------------------------------------------------------------- 汇总

print(f"\n{'=' * 40}")
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 40)
sys.exit(1 if FAIL else 0)
