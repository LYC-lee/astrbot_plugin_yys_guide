"""校验核心博主名单是否还能抓到。

改完 _conf_schema.json 的 core_authors 之后跑一下这个，
填错的 mid / uid、已经停更的号、接口失效，都会直接显示出来，
不用等到线上才发现卡片是空的。

跑法：
    python tests/check_authors.py               校验配置文件里的名单
    python tests/check_authors.py --only ds163  只看大神
    python tests/check_authors.py --only bilibili
"""

from __future__ import annotations

import asyncio
import datetime
import json
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
from yys_fetchers.bilibili import BilibiliFetcher  # noqa: E402
from yys_fetchers.ds163 import Ds163Fetcher  # noqa: E402


def load_targets() -> list[dict]:
    path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
    with open(path, encoding="utf-8") as fh:
        schema = json.load(fh)
    return schema.get("core_authors", {}).get("default", []) or []


async def main() -> None:
    only = ""
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 < len(sys.argv):
            only = sys.argv[idx + 1]

    targets = load_targets()
    if only:
        targets = [t for t in targets if t.get("__template_key") == only]
    if not targets:
        print("配置里没有可校验的博主。")
        return

    client = HttpClient()
    fetchers = {
        "bilibili": BilibiliFetcher(client, {}),
        "ds163": Ds163Fetcher(client, {}),
    }
    now = datetime.datetime.now(CN_TZ)
    ok = 0
    stale = 0

    for target in targets:
        key = target.get("__template_key") or ""
        fetcher = fetchers.get(key)
        label = target.get("name") or target.get("mid") or target.get("uid") or "?"

        if fetcher is None:
            print(f"  ?  {label:<12} 平台 {key!r} 不支持按作者校验，跳过")
            continue

        try:
            items = await fetcher.fetch_author(target, 3)
        except Exception as exc:  # noqa: BLE001
            print(f"  x  {label:<12} 异常 {exc!r}")
            continue

        if not items:
            print(f"  x  {label:<12} 抓不到 —— 检查 mid/uid 是否填错")
            continue

        ok += 1
        latest = items[0]
        if latest.published:
            days = (now - latest.published).days
            when = latest.published.strftime("%Y-%m-%d")
            flag = "⚠️ 停更" if days > 30 else ""
            if days > 30:
                stale += 1
            print(f"  OK {label:<12} 最新 {when}（{days}天前）{flag}")
        else:
            print(f"  OK {label:<12} 最新时间解析失败")
        print(f"       {latest.title[:46]}")

        await asyncio.sleep(1.5)

    print(f"\n成功率 {ok}/{len(targets)}" + (f"，其中 {stale} 个超过 30 天没更新" if stale else ""))
    await client.close()


asyncio.run(main())
