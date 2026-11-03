"""端到端测试：模拟一条群消息，走完 触发 -> 抓取 -> 摘要 -> 渲染 全链路。

跑法：python _e2e.py
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
        warning=lambda *a, **k: None,
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

from yys_core import aliases  # noqa: E402


class FakeEvent:
    def __init__(self, text: str):
        self.message_str = text
        self.unified_msg_origin = "test:FriendMessage:10001"

    def plain_result(self, text: str) -> str:
        return "TEXT:" + text

    def image_result(self, url: str) -> str:
        return "IMAGE:" + url


class NoLLMContext:
    """模拟 AstrBot 没配模型的情况，应该自动降级到规则抽取。"""

    def __init__(self):
        self.sent = []

    async def get_current_chat_provider_id(self, umo=None):
        return None

    def get_using_provider(self, umo=None):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class FakeLLMResp:
    def __init__(self, text):
        self.completion_text = text


class FakeLLMContext(NoLLMContext):
    """模拟配了模型且返回合法 JSON。"""

    async def get_current_chat_provider_id(self, umo=None):
        return "fake-provider"

    async def llm_generate(self, chat_provider_id=None, prompt=None, system_prompt=None):
        return FakeLLMResp(
            '[{"title":"须佐御魂配法","team":"须佐之男 / 玉藻前 / 追月神",'
            '"souls":"须佐：狂骨4+鬼灵歌伎2","howto":"起手拉条，二技能接大招收掉",'
            '"note":"需要一定练度"}]'
        )


DEFAULT_CONFIG = {
    "enabled": True,
    "trigger_mode": "strict",
    "cooldown_seconds": 0,
    "show_progress": True,
    "max_items": 5,
    "core_authors": [],
    "topics": ["阴阳师 御魂", "阴阳师 阵容"],
    "sources": {
        "bilibili": True,
        "nga": False,
        "weibo": False,
        "douyin": True,
        "web_search": True,
    },
    "douyin_mode": "try_meta",
    "summary": {
        "use_llm": True,
        "llm_max_items": 5,
        "item_max_chars": 1800,
        "llm_timeout": 40,
    },
    "render": {"style": "card", "show_cover": False},
    "cache": {"ttl_minutes": 180, "max_rows": 2000},
    "warmup": {"enabled": False, "times": [], "push_umo": []},
    "cookies": {"bilibili": "", "weibo": "", "nga": ""},
    "search": {"engine": "so360", "searxng_url": ""},
}


async def run_case(name: str, context, text: str) -> list[str]:
    from main import YysGuidePlugin

    print(f"\n{'=' * 60}\n场景：{name}\n输入：{text}\n{'=' * 60}")

    match = aliases.match(text, "strict")
    print(f"匹配结果：{match}")
    if match is None:
        print("  -> 未触发，符合预期")
        return []

    plugin = YysGuidePlugin(context, dict(DEFAULT_CONFIG))
    await plugin._ensure_ready()

    event = FakeEvent(text)
    outputs: list[str] = []
    async for chunk in plugin._process(event, match):
        outputs.append(chunk)

    for chunk in outputs:
        body = chunk.split(":", 1)[1]
        print(f"\n--- 输出（{chunk[:5]}）---")
        print(body)

    await plugin.terminate()
    return outputs


async def main() -> None:
    print("### 场景 A：没配大模型，应该走规则降级")
    outputs = await run_case(
        "规则降级（真实抓取 B站 + 360）", NoLLMContext(), "阴阳师御魂怎么配"
    )
    assert outputs, "没有任何输出"
    assert any(o.startswith("TEXT:") for o in outputs), "没有文本输出"
    final = outputs[-1]
    assert "阴阳师 · 御魂" in final, "卡片标题不对"
    assert "http" in final, "卡片里没有链接"
    print("\n[ok] 规则降级链路通过")

    print("\n\n### 场景 B：配了模型且返回合法 JSON")
    outputs = await run_case(
        "模型摘要（真实抓取 + 假模型返回）", FakeLLMContext(), "yys 魂土阵容推荐"
    )
    final = outputs[-1]
    assert "阵容" in final or "御魂" in final, "模型字段没渲染出来"
    print("\n[ok] 模型摘要链路通过")

    print("\n\n### 场景 C：不相关的消息不应触发")
    outputs = await run_case("不该触发", NoLLMContext(), "今天中午吃什么")
    assert outputs == [], "不该触发却触发了"
    print("[ok] 误触发防护通过")

    print("\n\n" + "=" * 60)
    print("端到端测试全部通过")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
