"""端到端测试：模拟一条群消息，走完 触发 -> 抓取 -> 摘要 -> 渲染 全链路。

跑法：python _e2e.py
"""

from __future__ import annotations

import asyncio
import copy
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


class _Plain:
    def __init__(self, text):
        self.text = text


class _Image:
    def __init__(self, file=""):
        self.file = file

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)


class _Node:
    """合并转发节点：真框架里是 pydantic 模型，这里只要能取值就行。"""

    def __init__(self, content, name="", uin="0"):
        self.content = content
        self.name = name
        self.uin = uin


class _Nodes:
    def __init__(self, nodes):
        self.nodes = nodes


def _install_components_stub() -> None:
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = _Plain
    components.Image = _Image
    components.Node = _Node
    components.Nodes = _Nodes
    sys.modules["astrbot.api.message_components"] = components
    sys.modules["astrbot.api"].message_components = components


_install_astrbot_stub()
_install_components_stub()

from yys_core import aliases  # noqa: E402


class FakeEvent:
    def __init__(self, text: str, platform: str = "aiocqhttp"):
        self.message_str = text
        self.unified_msg_origin = "test:FriendMessage:10001"
        self.platform_name = platform

    def get_platform_name(self) -> str:
        return self.platform_name

    def get_self_id(self) -> str:
        return "10000"

    def plain_result(self, text: str) -> str:
        return "TEXT:" + text

    def image_result(self, url: str) -> str:
        return "IMAGE:" + url

    def chain_result(self, chain) -> str:
        """把合并转发压成可断言的字符串：NODE[昵称]正文|IMG:n。"""
        parts = []
        for comp in chain:
            for node in getattr(comp, "nodes", []):
                body = "".join(
                    getattr(p, "text", "") for p in node.content if hasattr(p, "text")
                )
                images = [p for p in node.content if hasattr(p, "file")]
                tail = f"|IMG:{len(images)}" if images else ""
                parts.append(f"NODE[{node.name}]{body}{tail}")
        return "FORWARD:" + "\n@@@\n".join(parts)

    def forward_nodes(self, raw: str) -> list[str]:
        return raw.split("FORWARD:", 1)[1].split("\n@@@\n")


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
    "render": {
        "style": "card",
        "show_cover": False,
        "combine_forward": True,
        "forward_name": "阴阳师攻略",
        "forward_qr": True,
        "split_messages": True,
        "items_per_message": 1,
        "show_team_code": True,
        "show_qr": True,
        "max_qr": 2,
    },
    "cache": {"ttl_minutes": 180, "max_rows": 2000},
    "warmup": {"enabled": False, "times": [], "push_umo": []},
    "cookies": {"bilibili": "", "weibo": "", "nga": ""},
    "search": {"engine": "so360", "searxng_url": ""},
}


async def run_case(
    name: str,
    context,
    text: str,
    platform: str = "aiocqhttp",
    config: dict | None = None,
) -> list[str]:
    from main import YysGuidePlugin

    print(f"\n{'=' * 60}\n场景：{name}\n输入：{text}\n平台：{platform}\n{'=' * 60}")

    match = aliases.match(text, "strict")
    print(f"匹配结果：{match}")
    if match is None:
        print("  -> 未触发，符合预期")
        return []

    plugin = YysGuidePlugin(context, copy.deepcopy(config or DEFAULT_CONFIG))
    await plugin._ensure_ready()

    event = FakeEvent(text, platform)
    outputs: list[str] = []
    async for chunk in plugin._process(event, match):
        outputs.append(chunk)

    for chunk in outputs:
        if chunk.startswith("IMAGE:"):
            print(f"\n--- 图片：{chunk.split(':', 1)[1]}")
            continue
        if chunk.startswith("FORWARD:"):
            nodes = event.forward_nodes(chunk)
            print(f"\n--- 合并转发（{len(nodes)} 个节点）---")
            for index, node in enumerate(nodes, 1):
                head, _, body = node.partition("]")
                print(f"\n  【节点 {index}】{head}]")
                print(body)
            continue
        print("\n--- 文字消息 ---")
        print(chunk.split(":", 1)[1])

    texts = [o for o in outputs if o.startswith("TEXT:")]
    forwards = [o for o in outputs if o.startswith("FORWARD:")]
    print(f"\n共 {len(texts)} 条文字消息、{len(forwards)} 条合并转发")

    await plugin.terminate()
    return outputs


async def main() -> None:
    print("### 场景 A：没配大模型，应该走规则降级")
    outputs = await run_case(
        "规则降级（真实抓取 B站 + 360）", NoLLMContext(), "阴阳师御魂怎么配"
    )
    assert outputs, "没有任何输出"
    forwards = [o for o in outputs if o.startswith("FORWARD:")]
    assert len(forwards) == 1, f"应该只发一条合并转发，实际 {len(forwards)} 条"
    nodes = FakeEvent("").forward_nodes(forwards[0])
    assert len(nodes) >= 2, f"合集里至少要有概况 + 攻略，实际 {len(nodes)} 个节点"
    assert "阴阳师 · 御魂怎么配" in nodes[0], f"概况标题不对：{nodes[0][:40]}"
    joined = "\n".join(nodes)
    assert "http" in joined, "合集里没有链接"
    assert all("NODE[阴阳师攻略]" in node for node in nodes), "节点昵称没设上"
    print("\n[ok] 规则降级 + 合并转发链路通过")

    print("\n\n### 场景 B：配了模型且返回合法 JSON")
    outputs = await run_case(
        "模型摘要（真实抓取 + 假模型返回）", FakeLLMContext(), "yys 魂土阵容推荐"
    )
    forwards = [o for o in outputs if o.startswith("FORWARD:")]
    assert len(forwards) == 1, "应该走合并转发"
    nodes = FakeEvent("").forward_nodes(forwards[0])
    assert "魂土阵容" in nodes[0], f"概况标题应回显用户说法：{nodes[0][:40]}"
    assert "狂骨" in "\n".join(nodes), "模型字段没渲染出来"
    print("\n[ok] 模型摘要链路通过")

    print("\n\n### 场景 C：非 QQ 平台应自动回落成分段发送")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    outputs = await run_case(
        "Telegram 不支持合并转发",
        NoLLMContext(),
        "阴阳师御魂怎么配",
        platform="telegram",
        config=cfg,
    )
    forwards = [o for o in outputs if o.startswith("FORWARD:")]
    texts = [o for o in outputs if o.startswith("TEXT:")]
    assert not forwards, "非 QQ 平台不该发合并转发"
    assert len(texts) >= 2, f"应该分段发送，实际只有 {len(texts)} 条文字"
    assert any("▎阴阳师" in t for t in texts), "分段里没有概况"
    print("\n[ok] 平台回落通过")

    print("\n\n### 场景 D：手动关掉合并转发，应回到分段")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["render"]["combine_forward"] = False
    outputs = await run_case(
        "关掉 combine_forward", NoLLMContext(), "阴阳师御魂怎么配", config=cfg
    )
    forwards = [o for o in outputs if o.startswith("FORWARD:")]
    texts = [o for o in outputs if o.startswith("TEXT:")]
    assert not forwards, "关掉后不该再发合并转发"
    assert len(texts) >= 2, "应该分段发送"
    print("\n[ok] 开关生效")

    print("\n\n### 场景 E：不相关的消息不应触发")
    outputs = await run_case("不该触发", NoLLMContext(), "今天中午吃什么")
    assert outputs == [], "不该触发却触发了"
    print("[ok] 误触发防护通过")

    print("\n\n" + "=" * 60)
    print("端到端测试全部通过")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
