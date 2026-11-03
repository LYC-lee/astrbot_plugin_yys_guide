"""挖一批网易大神上的阴阳师作者，用来扩充 core_authors。

大神站内没有可用的搜索接口（实测 404），但外链路径是通的：
360 搜索能搜到 `ds.163.com/feed/<24位id>/`，而 feed 详情页的 SSR 里带
`/user/<32位uid>/`，昵称就在旁边的 <span> 里 —— 于是「搜 feed → 反查作者」。

捞到 uid 后统一回抓主页，验证「能不能解析出动态 + 最近更新时间」，
只留下真能出内容的，按更新新鲜度排序打印。

跑法：
    python tests/discover_ds_authors.py
    python tests/discover_ds_authors.py --pages 3
"""

from __future__ import annotations

import asyncio
import datetime
import html as _html
import os
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
# tests 可能和插件目录同级（开发工作区），也可能就在插件目录里（git 仓库布局）
PLUGIN_DIR = (
    _PARENT
    if os.path.isfile(os.path.join(_PARENT, "main.py"))
    else os.path.join(_PARENT, "astrbot_plugin_yys_guide")
)
sys.path.insert(0, PLUGIN_DIR)


def _stub() -> None:
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
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        command=lambda *a, **k: (lambda f: f),
        regex=lambda *a, **k: (lambda f: f),
        event_message_type=lambda *a, **k: (lambda f: f),
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )
    star.Context = object
    star.Star = object
    star.register = lambda *a, **k: (lambda cls: cls)
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )


_stub()

from yys_core.http import HttpClient  # noqa: E402
from yys_core.models import CN_TZ  # noqa: E402
from yys_fetchers.ds163 import Ds163Fetcher  # noqa: E402

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_SEARCH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_FEED_ID_RE = re.compile(r"ds\.163\.com/feed/([0-9a-f]{24})")
_AUTHOR_RE = re.compile(
    r'user-name[^"]*"><a href="/user/([0-9a-f]{32})/"[^>]*><span>(.*?)</span>', re.S
)
_TAG_RE = re.compile(r"<[^>]+>")

QUERIES = [
    "网易大神 阴阳师 攻略",
    "网易大神 阴阳师 阵容",
    "网易大神 阴阳师 御魂",
    "网易大神 阴阳师 超鬼王",
    "网易大神 阴阳师 逢魔之时",
    "网易大神 阴阳师 秘闻",
    "网易大神 阴阳师 活动 攻略",
    "网易大神 阴阳师 式神 培养",
    "网易大神 阴阳师 斗技 阵容",
    "网易大神 阴阳师 御魂 搭配",
    "网易大神 阴阳师 平安奇谭",
    "大神 阴阳师 攻略 分享",
]


def _clean(raw: str) -> str:
    return _html.unescape(_TAG_RE.sub("", raw)).strip()


async def collect_feeds(client: HttpClient, pages: int) -> set[str]:
    """360 搜索里翻出 feed id。"""
    feeds: set[str] = set()
    for query in QUERIES:
        for page in range(1, pages + 1):
            try:
                resp = await client.request(
                    "GET",
                    "https://www.so.com/s",
                    params={"q": query, "pn": page},
                    headers=_SEARCH_HEADERS,
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print(f"  !  搜索失败「{query}」p{page}: {exc!r}")
                continue
            got = set(_FEED_ID_RE.findall(resp.text))
            fresh = got - feeds
            feeds |= got
            print(f"  ·  {query} p{page} → +{len(fresh)}（累计 {len(feeds)} 条 feed）")
            await asyncio.sleep(1.2)
    return feeds


async def feed_author(client: HttpClient, fid: str) -> tuple[str, str]:
    """feed 详情页 → (uid, 昵称)。拿不到就返回空。"""
    try:
        page = await client.get_text(
            f"https://ds.163.com/feed/{fid}/",
            headers={"User-Agent": _MOBILE_UA, "Referer": "https://m.ds.163.com/"},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  !  feed {fid[:8]} 打不开：{exc!r}")
        return "", ""
    match = _AUTHOR_RE.search(page)
    if not match:
        return "", ""
    return match.group(1).lower(), _clean(match.group(2))


async def verify(fetcher: Ds163Fetcher, uid: str, hint: str) -> dict | None:
    try:
        items = await fetcher.fetch_author({"uid": uid, "name": hint}, 3)
    except Exception as exc:  # noqa: BLE001
        print(f"  x  {hint or uid[:8]} 异常 {exc!r}")
        return None
    if not items:
        return None
    latest = items[0]
    return {
        "uid": uid,
        "name": latest.author or hint or uid[:8],
        "latest": latest.published,
        "title": latest.title,
    }


async def main() -> None:
    pages = 2
    if "--pages" in sys.argv:
        idx = sys.argv.index("--pages")
        if idx + 1 < len(sys.argv):
            pages = max(1, int(sys.argv[idx + 1]))

    client = HttpClient()
    fetcher = Ds163Fetcher(client, {})
    now = datetime.datetime.now(CN_TZ)

    print("① 360 搜索捞 feed 链接")
    feeds = await collect_feeds(client, pages)

    print(f"\n② 逐条 feed 反查作者（{len(feeds)} 条）")
    authors: dict[str, str] = {}
    for fid in sorted(feeds):
        uid, name = await feed_author(client, fid)
        if uid:
            authors.setdefault(uid, name)
        await asyncio.sleep(1.0)
    print(f"  → 去重后 {len(authors)} 位作者")

    print("\n③ 回抓主页验证")
    alive: list[dict] = []
    for uid, hint in authors.items():
        info = await verify(fetcher, uid, hint)
        if info:
            alive.append(info)
            stamp = info["latest"].strftime("%Y-%m-%d") if info["latest"] else "时间?"
            days = (now - info["latest"]).days if info["latest"] else 9999
            print(f"  OK {info['name']:<16} {stamp}（{days}天前）{info['title'][:32]}")
        else:
            print(f"  -- {hint or uid[:8]} 主页无内容 / 解析失败")
        await asyncio.sleep(1.3)

    alive.sort(
        key=lambda x: x["latest"] or datetime.datetime.min.replace(tzinfo=CN_TZ),
        reverse=True,
    )
    print("\n===== 可用作者（按更新新鲜度）=====")
    for info in alive:
        stamp = info["latest"].strftime("%Y-%m-%d") if info["latest"] else "?"
        print(f"{info['name']}\t{info['uid']}\t{stamp}")
    print(f"\n可用 {len(alive)} / 候选 {len(authors)}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
