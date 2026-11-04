"""合并转发（聊天记录合集）的离线自测。

跑法：python tests/test_forward.py

不联网、不依赖 AstrBot：只验证节点切分和节点里的文本，
上层的 Node/Nodes 组装由 test_end_to_end 用 stub 覆盖。
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime

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
    logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    api.logger = logger
    api.AstrBotConfig = dict
    astrbot.api = api
    astrbot.logger = logger
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_astrbot_stub()

from yys_core.models import CN_TZ, GuideItem  # noqa: E402
from yys_core.render import INDENT, render_forward  # noqa: E402
from yys_core.summarize import CardEntry, rule_summarize  # noqa: E402
from yys_core.teamcode import TeamCode  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


NOW = datetime(2026, 11, 4, 9, 40, tzinfo=CN_TZ)

CODE_A = "|TA|50a1656ca3b50b51ec76450cac224d52"
CODE_B = "|TA|7fbefcd26141af939af808589ecfb854"
CODE_C = "|TA|e4ee8dc36f633641f93cb935dcc60785"
CODE_D = "|TA|26ece6403b2d3ba0c7c6420c96720873"


def make_entry(index: int, codes: list[TeamCode] | None = None) -> CardEntry:
    return CardEntry(
        title=f"第 {index} 套阵容怎么打",
        url=f"https://example.com/{index}",
        source="bilibili",
        author=f"作者{index}",
        published=NOW,
        team="须佐之男 / 玉藻前 / 追月神",
        howto="开局拉条，二技能接大打收尾",
        codes=codes or [],
    )


def test_node_count() -> None:
    nodes = render_forward("魂土阵容", [make_entry(i) for i in range(1, 6)], generated_at=NOW)
    # 概况 1 + 5 条攻略，没有 degraded 所以没有脚注节点
    check("节点数 = 概况 + 条目", len(nodes) == 6, str(len(nodes)))
    check("首个节点是概况", "▎阴阳师 · 魂土阵容" in nodes[0].text)
    check("概况里带条数", "5 条" in nodes[0].text)


def test_node_order() -> None:
    nodes = render_forward("魂土阵容", [make_entry(i) for i in range(1, 4)], generated_at=NOW)
    check("第 1 条排在概况之后", "第 1 套" in nodes[1].text)
    check("第 2 条紧随其后", "第 2 套" in nodes[2].text)
    check("第 3 条在最后", "第 3 套" in nodes[3].text)
    check("节点编号带总数", "1/3." in nodes[1].text, nodes[1].text[:20])


def test_node_body_keeps_link() -> None:
    nodes = render_forward("魂土阵容", [make_entry(1)], generated_at=NOW)
    body = nodes[1].text
    check("节点里有原文链接", "https://example.com/1" in body)
    check("节点里有式神字段", "式神：须佐之男" in body)
    check("节点里有打法字段", "打法：" in body)


def test_codes_are_standalone() -> None:
    entry = make_entry(1, [TeamCode(CODE_A, "速度位1️⃣"), TeamCode(CODE_B, "输出位1️⃣")])
    nodes = render_forward("道馆阵容", [entry], generated_at=NOW, max_codes=3)
    body = nodes[1].text
    check("节点的码在正文里", CODE_A in body and CODE_B in body)
    check("节点把码带上去给附图用", [c.code for c in nodes[1].codes] == [CODE_A, CODE_B])

    code_lines = [line for line in body.split("\n") if line.startswith("|TA|")]
    check("码行数对", len(code_lines) == 2, str(len(code_lines)))
    check(
        "码行顶格、零前缀（长按复制要干净）",
        all(not line.startswith(INDENT) for line in code_lines),
    )
    check("码行不含名字", all("位1️⃣" not in line for line in code_lines))


def test_max_codes() -> None:
    entry = make_entry(
        1,
        [
            TeamCode(CODE_A, "一队"),
            TeamCode(CODE_B, "二队"),
            TeamCode(CODE_C, "三队"),
            TeamCode(CODE_D, "四队"),
        ],
    )
    nodes = render_forward("道馆阵容", [entry], generated_at=NOW, max_codes=2)
    body = nodes[1].text
    check("只显示 max_codes 个码", body.count("|TA|") == 2, str(body.count("|TA|")))
    check("提示还有几个没显示", "原文还有 2 个码" in body)
    check("附图只按显示出来的算", len(nodes[1].codes) == 2, str(len(nodes[1].codes)))


def test_no_codes_when_disabled() -> None:
    entry = make_entry(1, [TeamCode(CODE_A, "速度位1️⃣")])
    nodes = render_forward("道馆阵容", [entry], generated_at=NOW, show_codes=False)
    body = nodes[1].text
    check("关掉后正文没有码", "|TA|" not in body)
    check("关掉后也不带图", nodes[1].codes == [])


def test_empty() -> None:
    nodes = render_forward("魂土阵容", [], generated_at=NOW)
    check("没结果也只有一个节点", len(nodes) == 1, str(len(nodes)))
    check("兜底提示可读", "没找到" in nodes[0].text)


def test_degraded_footer() -> None:
    items = [
        GuideItem(
            item_id="b1",
            source="bilibili",
            title="魂土 11s",
            url="https://example.com/a",
            digest="原文摘要",
        ),
    ]
    entries = rule_summarize(items)
    nodes = render_forward("魂土阵容", entries, generated_at=NOW)
    check("降级时末尾加脚注节点", "原文节选" in nodes[-1].text)
    check("脚注节点没有码", nodes[-1].codes == [])


def test_notice() -> None:
    nodes = render_forward(
        "魂土阵容", [make_entry(1)], generated_at=NOW, notice="本次接口被限流，以下为缓存内容。"
    )
    check("限流提示在概况节点里", "限流" in nodes[0].text)


def test_node_length_capped() -> None:
    """单个节点太长会让协议端截断，节点正文不该超过卡片上限。"""
    long_entry = make_entry(1)
    long_entry.howto = "打" * 5000
    nodes = render_forward("魂土阵容", [long_entry], generated_at=NOW)
    check("超长节点会被截断", len(nodes[1].text) <= 3600, str(len(nodes[1].text)))


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()

    print(f"通过 {PASSED} 项")
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
