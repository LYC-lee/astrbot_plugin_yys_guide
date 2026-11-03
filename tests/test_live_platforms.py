"""联网可用性验证：真实打一次各平台的接口，确认签名和解析能不能用。

跑法：python _livetest.py
"""

from __future__ import annotations

import asyncio
import os
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


def _install_astrbot_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: print("   [warn]", *a),
        error=lambda *a, **k: print("   [err]", *a),
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

    class Context:
        pass

    def register(*args, **kwargs):
        return lambda cls: cls

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

from yys_core.http import HttpClient  # noqa: E402
from yys_fetchers import (  # noqa: E402
    BilibiliFetcher,
    DouyinFetcher,
    NgaFetcher,
    WebSearchFetcher,
    WeiboFetcher,
)


def show(name: str, items) -> None:
    print(f"  -> {len(items)} 条")
    for item in items[:3]:
        print(f"     [{item.source}] {item.title[:36]} | {item.author} | {item.published}")


async def main() -> None:
    client = HttpClient(timeout=20)
    conf = {
        "bilibili": "",
        "weibo": "",
        "nga": "",
        "nga_fid": "",
        "douyin_mode": "try_meta",
        "engine": "so360",
        "searxng_url": "",
    }

    print("\n=== B站：WBI 密钥 ===")
    bili = BilibiliFetcher(client, conf)
    try:
        ok = await bili.signer.ensure_keys()
        print(f"  取到 key: {ok}")
        if ok:
            print(f"  img_key = {bili.signer._img_key[:16]}…")
            print(f"  sub_key = {bili.signer._sub_key[:16]}…")
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== B站：游客 cookie ===")
    try:
        cookies = await bili._get_cookies()
        print(f"  cookies: {list(cookies.keys())}")
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== B站：搜索「阴阳师 御魂」 ===")
    try:
        items = await bili.search("阴阳师 御魂", 5)
        show("bilibili", items)
        if not items:
            print("  （空结果，通常意味着签名或风控有问题）")
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== B站：搜索「阴阳师 须佐之男」 ===")
    try:
        items = await bili.search("阴阳师 须佐之男", 3)
        show("bilibili", items)
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== NGA：阴阳师版 ===")
    nga = NgaFetcher(client, conf)
    try:
        items = await nga.search("御魂", 5)
        show("nga", items)
        if not items:
            print("  （空结果：可能是游客态被拦或 fid 不对）")
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== 微博：关键词搜索「阴阳师 御魂」 ===")
    weibo = WeiboFetcher(client, conf)
    try:
        items = await weibo.search("阴阳师 御魂", 3)
        show("weibo", items)
        if not items:
            print("  （空结果：微博搜索多数情况需要 Cookie）")
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== Bing：兜底搜索 ===")
    web = WebSearchFetcher(client, conf)
    try:
        items = await web.search("御魂", 5)
        show("web", items)
    except Exception as exc:
        print(f"  失败：{exc!r}")

    print("\n=== 抖音：链接解析 ===")
    douyin = DouyinFetcher(client, conf)
    try:
        item = await douyin.fetch_by_url("https://v.douyin.com/iF1XyYqQ/")
        print(f"  -> {item}")
    except Exception as exc:
        print(f"  失败：{exc!r}")

    await client.close()


asyncio.run(main())
