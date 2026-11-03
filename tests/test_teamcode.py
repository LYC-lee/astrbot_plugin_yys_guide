"""阵容码提取 + 分段渲染的离线自测。

跑法：python tests/test_teamcode.py
"""

from __future__ import annotations

import os
import sys
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

from yys_core import teamcode  # noqa: E402
from yys_core.models import CN_TZ, GuideItem  # noqa: E402
from yys_core.render import INDENT, render_card, render_split  # noqa: E402
from yys_core.summarize import CardEntry, rule_summarize  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


NOW = datetime(2026, 11, 4, 2, 40, tzinfo=CN_TZ)

CODE_A = "|TA|2795b9ab4651ee39c37cfa9d79c4fc38"
CODE_B = "|TA|a352c5e746fb7765bbc66244597c59fe"


def codes_of(*codes: str) -> list[teamcode.TeamCode]:
    return [teamcode.TeamCode(code) for code in codes]


def flat(result: list[teamcode.TeamCode]) -> list[str]:
    return [item.code for item in result]


# ---------------------------------------------------------------- 提取


def test_extract_basic() -> None:
    text = f"【文字码】\n{CODE_A}\n{CODE_B}\n【式神录】→左上【阵容助手】"
    check("基本提取：两条都认出来", flat(teamcode.extract_codes(text)) == [CODE_A, CODE_B])


def test_extract_fullwidth_pipe() -> None:
    """手机复制常见全角竖线，认不出来用户就白贴了。"""
    codes = teamcode.extract_codes("｜TA｜2795B9AB4651EE39C37CFA9D79C4FC38")
    check("全角竖线 + 大写：归一成小写", flat(codes) == [CODE_A], str(flat(codes)))


def test_extract_with_spaces() -> None:
    """博主排版时会在码里插空格，原文也提醒过「有空格记得去掉」。"""
    codes = teamcode.extract_codes("|TA| 2795 b9ab 4651 ee39 c37c fa9d 79c4 fc38")
    check("码里带空格：压掉空格后仍能识别", flat(codes) == [CODE_A], str(flat(codes)))


def test_extract_dedup_keeps_order() -> None:
    codes = teamcode.extract_codes(f"{CODE_B}\n{CODE_A}\n{CODE_B}")
    check("去重且保持出现顺序", flat(codes) == [CODE_B, CODE_A], str(flat(codes)))


def test_extract_ignores_wrong_length() -> None:
    short = "|TA|2795b9ab4651ee39c37cfa9d79c4fc3"  # 31 位
    long = "|TA|2795b9ab4651ee39c37cfa9d79c4fc380"  # 33 位
    check("位数不足不认", teamcode.extract_codes(short) == [])
    check("位数超出不认（不截断成 32 位）", teamcode.extract_codes(long) == [])


def test_extract_ignores_unrelated_hex() -> None:
    """普通 32 位 hex（图片名、MD5）不能被当成阵容码。"""
    text = "封面图 a352c5e746fb7765bbc66244597c59fe.png 尺寸 1200x630"
    check("没有 |TA| 前缀的 hex 不认", teamcode.extract_codes(text) == [])


def test_extract_multiple_fields() -> None:
    """标题里有码的，正文里也有码的，都要能拿到。"""
    codes = teamcode.extract_codes("", f"正文 {CODE_A}", f"结尾 {CODE_B}")
    check("跨多个字段提取", flat(codes) == [CODE_A, CODE_B], str(flat(codes)))


def test_extract_cap() -> None:
    body = "\n".join(f"|TA|{index:032x}" for index in range(1, 40))
    codes = teamcode.extract_codes(body)
    check(
        f"最多记 {teamcode.MAX_CODES} 个（展示数量另有限制）",
        len(codes) == teamcode.MAX_CODES,
        str(len(codes)),
    )


def test_normalize() -> None:
    check(
        "normalize 输出 36 字符",
        len(teamcode.normalize("2795B9AB4651EE39C37CFA9D79C4FC38")) == 36,
    )
    check(
        "normalize 小写",
        teamcode.normalize("2795B9AB4651EE39C37CFA9D79C4FC38") == CODE_A,
    )


def test_label_from_line_prefix() -> None:
    """码前面的短语就是它的名字：多个码时用户靠这个区分。"""
    text = f"速度位1️⃣ {CODE_A}\n输出位1️⃣ {CODE_B}"
    labels = [item.label for item in teamcode.extract_codes(text)]
    check("行内前缀作为名字", labels == ["速度位1️⃣", "输出位1️⃣"], str(labels))


def test_label_with_brackets() -> None:
    text = f"【阶段一文字码】 {CODE_A}"
    labels = [item.label for item in teamcode.extract_codes(text)]
    check("【】包裹的名字保留", labels == ["【阶段一文字码】"], str(labels))


def test_label_empty_when_sentence() -> None:
    """一整段正文后面直接跟码时不留名字——那是句子不是标签。"""
    text = (
        "这套阵容的思路是先手拉条然后用玉藻前补刀，"
        "适合练度不高又想稳定通关的玩家，下面直接给码： "
        f"{CODE_A}"
    )
    labels = [item.label for item in teamcode.extract_codes(text)]
    check("长句子不当名字", labels == [""], str(labels))


def test_label_empty_when_alone_on_line() -> None:
    labels = [item.label for item in teamcode.extract_codes(f"阵容码合集\n{CODE_A}")]
    check("码独占一行时没有名字", labels == [""], str(labels))


# ---------------------------------------------------------------- 正文里的码


def test_strip_removes_without_placeholder() -> None:
    """原文节选里不能留占位符：一串占位符比原文还难读。"""
    text = f"🌸文字阵容码 速度位1️⃣ {CODE_A} 输出位1️⃣ {CODE_B}"
    cleaned = teamcode.strip_codes(text)
    check("码被删干净", "|TA|" not in cleaned, cleaned)
    check("不留占位符", "码见下方" not in cleaned and "【" not in cleaned, cleaned)
    check("位名保留下来", "速度位1️⃣" in cleaned and "输出位1️⃣" in cleaned, cleaned)


def test_strip_keeps_other_text() -> None:
    text = f"龙胆狩日首领战攻略\n{CODE_A}\n每天前 20 局有积分"
    cleaned = teamcode.strip_codes(text)
    check("正文其他内容不受影响", "每天前 20 局有积分" in cleaned, cleaned)


def test_strip_no_code() -> None:
    text = "荒川百战 3+2 打法"
    check("没有码时原样返回", teamcode.strip_codes(text) == text)


def test_strip_partial_code() -> None:
    """抓取时正文可能在码中间被截断，剩下的半截也要清掉。

    真实案例：B站简介 254 字截断，尾巴上留着「|TA|8e77f5ea81cd4f5f8900a235405」。
    """
    text = "文字阵容码 速度位1️⃣ |TA|8e77f5ea81cd4f5f8900a235405"
    cleaned = teamcode.strip_codes(text)
    check("半截码也清掉", "|TA|" not in cleaned, cleaned)
    check("半截码不当作有效码", teamcode.extract_codes(text) == [])


# ---------------------------------------------------------------- 摘要层接码


def test_rule_summarize_picks_codes() -> None:
    item = GuideItem(
        source="ds163",
        item_id="1",
        title="【日陨此时】平将门首领战攻略",
        url="https://m.ds.163.com/feed/1",
        digest="5 套阵容",
        content=f"高中低配阵容\n{CODE_A}\n{CODE_B}",
    )
    entries = rule_summarize([item])
    check("规则摘要也会带出阵容码", flat(entries[0].codes) == [CODE_A, CODE_B])
    check("摘要正文里不再重复整串码", CODE_A not in entries[0].howto, entries[0].howto)


# ---------------------------------------------------------------- 渲染


def _entry(**kwargs) -> CardEntry:
    base = dict(
        title="超鬼王提速阵容",
        url="https://www.bilibili.com/video/BV1xx",
        source="bilibili",
        author="流沙河底",
        published=NOW - timedelta(hours=5),
        team="酒吞 / 追月神",
        howto="开局叠被动",
    )
    base.update(kwargs)
    return CardEntry(**base)


def test_code_line_is_copyable() -> None:
    """最关键的一条：码必须顶格、无前缀，长按复制整行就是干净的 36 字符。"""
    text = render_card("超鬼王", [_entry(codes=codes_of(CODE_A))], NOW)
    lines = text.split("\n")
    code_lines = [line for line in lines if line.strip() == CODE_A]
    check("码单独成行", len(code_lines) == 1, repr(code_lines))
    check(
        "码行没有任何缩进/前缀（复制即用）",
        bool(code_lines) and code_lines[0] == CODE_A,
        repr(code_lines[0]) if code_lines else "缺失",
    )
    check("码前有解释文字", "阵容码" in text)


def test_no_code_no_hint() -> None:
    text = render_card("超鬼王", [_entry()], NOW)
    check("没有码时不出现阵容码提示行", "阵容码" not in text)


def test_multiple_codes_each_own_line() -> None:
    text = render_card("超鬼王", [_entry(codes=codes_of(CODE_A, CODE_B))], NOW)
    lines = text.split("\n")
    check("多个码各自独占一行", CODE_A in lines and CODE_B in lines, repr(lines))


def test_code_limit_and_hint() -> None:
    many = codes_of(*(f"|TA|{i:032x}" for i in range(1, 7)))
    text = render_card("道馆进攻", [_entry(codes=many)], NOW, max_codes=3)
    shown = [line for line in text.split("\n") if line.startswith("|TA|")]
    check("只展示 max_codes 个", len(shown) == 3, str(len(shown)))
    check("提示原文还有几个", "还有 3 个码" in text, text)


def test_code_label_lines() -> None:
    """有名字的码：名字行带缩进，码行顶格。"""
    codes = [
        teamcode.TeamCode(CODE_A, "速度位1️⃣"),
        teamcode.TeamCode(CODE_B, "输出位1️⃣"),
    ]
    text = render_card("道馆进攻", [_entry(codes=codes)], NOW)
    lines = text.split("\n")
    check("名字行带缩进", f"{INDENT}· 速度位1️⃣" in lines, repr(lines[-6:]))
    check("码行仍然顶格", CODE_A in lines and CODE_B in lines)


def test_show_codes_off() -> None:
    text = render_card("超鬼王", [_entry(codes=codes_of(CODE_A))], NOW, show_codes=False)
    check("关掉开关后不显示码", CODE_A not in text)


def test_split_message_count() -> None:
    entries = [_entry(title=f"第{i}条") for i in range(1, 4)]
    chunks = render_split("超鬼王", entries, 1, NOW)
    check("3 条攻略 + 1 条概况 = 4 条消息", len(chunks) == 4, str(len(chunks)))
    check("第一条是概况", chunks[0].startswith("▎阴阳师 · 超鬼王"))
    check("概况里交代条数", "3 条" in chunks[0])
    check(
        "后面每条不再重复标题行",
        all("▎阴阳师" not in chunk for chunk in chunks[1:]),
    )
    check(
        "编号带总数，翻到哪条心里有数",
        chunks[1].startswith(f"{INDENT}1/3.") and chunks[3].startswith(f"{INDENT}3/3."),
        chunks[1][:12],
    )


def test_split_two_per_message() -> None:
    entries = [_entry(title=f"第{i}条") for i in range(1, 5)]
    chunks = render_split("超鬼王", entries, 2, NOW)
    check("4 条按 2 条一组 = 概况 + 2 条", len(chunks) == 3, str(len(chunks)))
    check("组内用分隔线相连", "─" in chunks[1])


def test_split_keeps_notice_once() -> None:
    entries = [_entry(), _entry()]
    chunks = render_split("超鬼王", entries, 1, NOW, notice="注：本次限流")
    check("限流提示只在概况出现一次", sum("限流" in c for c in chunks) == 1)


def test_split_degraded_footer() -> None:
    entries = [_entry(degraded=True), _entry(degraded=True)]
    chunks = render_split("超鬼王", entries, 1, NOW)
    check(
        "降级说明单独一条，不重复贴在每条后面",
        sum("原文节选" in c for c in chunks) == 1,
    )


def test_split_empty() -> None:
    chunks = render_split("魂土阵容", [], 1, NOW)
    check("没有结果时只发一条", len(chunks) == 1, str(len(chunks)))
    check("空结果提示可读", "没找到" in chunks[0])


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
