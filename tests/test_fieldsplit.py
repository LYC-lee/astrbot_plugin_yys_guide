"""式神 / 御魂 / 其余 三段切分的离线自测。

跑法：python tests/test_fieldsplit.py

这些用例都来自真实抓到的原文，尤其是魂土那几条：
UP 主把配速、御魂缩写、式神名写在同一行，
切不出来用户就只能看到一段流水账。
"""

from __future__ import annotations

import os
import sys
import types

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

from yys_core import fieldsplit  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [ok] {name}")
    else:
        FAILED.append(name)
        print(f"  [fail] {name}" + (f" -> {detail}" if detail else ""))


def split(text: str, title: str = "") -> tuple[str, str, str]:
    return fieldsplit.split_fields(text, title)


# ---------------------------------------------------------------- 魂土混写


HUNDU = """魂土11s双白蛋针女
阵容：
大于158狂歌雪
狂荒紧那罗（全队爆伤最高）
火灵生生生食灵
针女契灵"""


def test_hundu_mixed_lines() -> None:
    team, souls, rest = split(HUNDU, "魂土11s双白蛋食雪紧")
    check("混写：认出带式神名的行", "紧那罗" in team, team)
    check("混写：食灵也进式神栏", "食灵" in team, team)
    check("混写：纯御魂行不进式神栏", "针女契灵" not in team, team)
    check("混写：纯御魂行进御魂栏", "针女契灵" in souls, souls)
    check("混写：配速行不进式神栏", "大于158狂歌雪" not in team, team)
    check("混写：配速行进御魂栏", "大于158狂歌雪" in souls, souls)
    check("混写：标题复读不占一行", "双白蛋针女" not in team, team)


def test_hundu_speed_only() -> None:
    _, souls, rest = split("配速：兔子＞158＞其他＞128＞鬼使黑＞110.6", "无SSR鬼使黑魂土")
    check("纯配速：进御魂栏", "兔子" in souls, souls)
    check("纯配速：不塞打法", rest == "", rest)


def test_hundu_no_config() -> None:
    text = "面板其实早就够了，一直在拼sp紧\n感慨一下数值的膨胀，以前哪有这条件"
    team, souls, rest = split(text, "百日萌新拿下11s双白蛋魂土")
    check("纯思路：不编式神", team == "", team)
    check("纯思路：不编御魂", souls == "", souls)
    check("纯思路：原文进其余", "面板" in rest, rest)


# ---------------------------------------------------------------- 标准写法


STANDARD = """阵容：天照 / 须佐之男 / 座敷童子
御魂：天照 狂骨4+鬼灵歌伎2
须佐 狂骨4+荒骷髅2
打法：先手天照开大压血线，须佐收尾。"""


def test_standard_labels() -> None:
    team, souls, rest = split(STANDARD, "道馆进攻")
    check("标准：式神栏完整", team == "天照 / 须佐之男 / 座敷童子", team)
    check("标准：御魂栏两套", "狂骨4+鬼灵歌伎2" in souls and "狂骨4+荒骷髅2" in souls, souls)
    check("标准：御魂段里的式神名不搬去式神栏", "须佐 狂骨4+荒骷髅2" not in team, team)
    check("标准：打法不混进御魂", "先手" not in souls, souls)
    check("标准：打法进其余", "先手天照" in rest, rest)


def test_label_with_inline_value() -> None:
    team, souls, _ = split("阵容：酒吞童子 / 追月神", "")
    check("行内标签：式神认得出", team == "酒吞童子 / 追月神", team)
    check("行内标签：御魂为空", souls == "", souls)


def test_short_label_line() -> None:
    team, souls, _ = split("御魂\n狂骨4\n荒骷髅2", "")
    check("单独成行的标签也认", "狂骨4" in souls and "荒骷髅2" in souls, souls)
    check("单独成行的标签不吃式神", team == "", team)


# ---------------------------------------------------------------- 边界


def test_title_not_eaten() -> None:
    # 「魂土…」开头不能被当成「魂土：」标签，否则会变成「标签=魂土，内容=11s…」，
    # 标题被啃掉半截
    team, souls, _ = split("魂土11s双白蛋针女", "")
    whole = (team + souls).strip()
    check("标题开头的魂土不是标签", whole == "魂土11s双白蛋针女", whole)


def test_empty() -> None:
    check("空文本三段都空", split("") == ("", "", ""))
    check("纯空白三段都空", split("   \n\n  ") == ("", "", ""))


def test_long_sentence_not_team() -> None:
    text = "强如雪御前石长姬，也无法在不带增伤的情况下直接秒杀魂土。"
    team, _, rest = split(text, "")
    check("长句里的式神名不把整句拖进式神栏", team == "", team)
    check("长句进其余", "雪御前" in rest, rest)


def test_team_capped() -> None:
    text = "\n".join(f"阵容{chr(0x3000)}{i}" for i in range(20))
    team, _, _ = split(text, "")
    check("式神栏有条数上限", team.count("/") <= fieldsplit.MAX_LINES, team)


def test_title_repeat_detected() -> None:
    check(
        "标题复读能认出来",
        fieldsplit.looks_like_title("魂土11s双白蛋针女", "魂土11s双白蛋食雪紧"),
    )
    check(
        "不同的标题不算复读",
        not fieldsplit.looks_like_title("雪御前11s稳定版", "鬼使黑无SSR魂土"),
    )


def test_soul_word_detection() -> None:
    check("认得狂骨", fieldsplit.has_soul("狂骨4"))
    check("认得火灵", fieldsplit.has_soul("火灵生生生"))
    check("配速不算御魂", not fieldsplit.has_soul("兔子＞158＞其他"))


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
