"""用 _conf_schema.json 里的真实默认配置跑一遍完整流程。

用途：确认「装好插件之后、什么都不改」的默认体验是什么样的。
跑法：python tests/verify_default_config.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_end_to_end import (  # noqa: E402
    FakeEvent,
    NoLLMContext,
    PLUGIN_DIR,
    _install_astrbot_stub,
)

_install_astrbot_stub()

from yys_core import aliases  # noqa: E402


def build_config() -> dict:
    """把 schema 里的 default 递归展开成一份可直接用的配置。"""
    path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
    with open(path, encoding="utf-8") as fp:
        schema = json.load(fp)

    def walk(node: dict) -> object:
        kind = node.get("type")
        if kind == "object":
            return {key: walk(sub) for key, sub in (node.get("items") or {}).items()}
        return node.get("default")

    return {key: walk(node) for key, node in schema.items()}


async def main() -> None:
    config = build_config()
    # 测试环境不预热、不冷却，避免干扰
    config["warmup"]["enabled"] = False
    config["cooldown_seconds"] = 0
    config["summary"]["use_llm"] = False  # 没接真模型，走规则降级

    authors = config.get("core_authors") or []
    print("=" * 60)
    print("默认配置里的核心博主：")
    for target in authors:
        key = target.get("__template_key")
        ident = target.get("mid") or target.get("uid")
        print(f"  - {key:<9} {ident:<18} {target.get('name')}")
    print("=" * 60)

    from main import YysGuidePlugin

    plugin = YysGuidePlugin(NoLLMContext(), config)
    await plugin._ensure_ready()

    for text in ("阴阳师 御魂怎么配", "阴阳师 道馆阵容"):
        match = aliases.match(text, config["trigger_mode"])
        print(f"\n{'=' * 60}\n输入：{text}\n匹配：{match}\n{'=' * 60}")
        if match is None:
            print("  -> 未触发")
            continue

        event = FakeEvent(text)
        async for chunk in plugin._process(event, match):
            body = chunk[5:] if isinstance(chunk, str) and chunk.startswith("TEXT:") else chunk
            print(body)

    await plugin._cache.close() if hasattr(plugin, "_cache") else None


asyncio.run(main())
