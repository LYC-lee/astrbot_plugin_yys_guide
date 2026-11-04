"""图片卡片的离线自测。

跑法：python tests/test_card_image.py

只验证「结构拆分」和「能不能画出来」，不判像素级美观——
版式改一次截图看一眼就行，写进断言里太脆。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
PLUGIN_DIR = (
    _PARENT
    if os.path.isfile(os.path.join(_PARENT, "main.py"))
    else os.path.join(_PARENT, "astrbot_plugin_yys_guide")
)
sys.path.insert(0, PLUGIN_DIR)

from yys_core import card_image, teamcode  # noqa: E402
from yys_core.render import render_code_list  # noqa: E402
from yys_core.summarize import CardEntry  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [ok] {name}")
    else:
        FAILED.append(f"{name}{'：' + detail if detail else ''}")
        print(f"  [!!] {name} {detail}")


def make_entry(**kwargs) -> CardEntry:
    base = dict(
        title="赛事训练①高配：约35秒",
        source="bilibili",
        author="姝酱的小本本",
        team="大天狗 / 神无月 / 伊邪那美 / SP缘结神 / 不见岳",
        souls=(
            "大天狗：攻攻爆·满暴 狂士\n"
            "神无月：速生生 轮入道\n"
            "伊邪那美：速命X 元兴寺夜荒魂\n"
            "SP缘结神：速X暴/爆·满暴 火灵\n"
            "不见岳：防防防 涂佛/散件"
        ),
        howto="配速：神无月＞190＞那美＞SP缘＞170＞系统UR狗。通关时间35秒左右。",
        note="大天狗可换成其他群体输出。",
    )
    base.update(kwargs)
    return CardEntry(**base)


# ------------------------------------------------------------------ 拆分


def test_slots_pair_by_name() -> None:
    """御魂前写了式神名，就该挂到那个式神下面，而不是按顺序硬塞。"""
    slots = card_image.parse_slots(
        "须佐之男 / 玉藻前 / 追月神",
        "追月神：招财猫 4\n须佐之男：狂骨 4\n玉藻前：破势 4",
    )
    check("三个式神三个槽", len(slots) == 3, str(len(slots)))
    check(
        "按名字对齐御魂",
        [s.name for s in slots] == ["须佐之男", "玉藻前", "追月神"]
        and slots[0].souls == ["狂骨 4"]
        and slots[2].souls == ["招财猫 4"],
        str([(s.name, s.souls) for s in slots]),
    )


def test_slots_fallback_order() -> None:
    """没写名字时按出现顺序补位，补不上就空着。"""
    slots = card_image.parse_slots("A / B / C", "狂骨 4\n破势 4")
    check("按顺序补位", slots[0].souls == ["狂骨 4"] and slots[1].souls == ["破势 4"])
    check("补不上就空着", slots[2].souls == [])


def test_soul_rows_split_attr() -> None:
    """「攻攻爆·满暴 狂士」要拆成两行：上属性下御魂名。"""
    slots = card_image.parse_slots("大天狗", "大天狗：攻攻爆·满暴 狂士")
    check("属性和御魂名分两行", slots[0].souls == ["攻攻爆·满暴", "狂士"], str(slots[0].souls))


def test_soul_rows_keep_plus() -> None:
    """「狂骨 4 + 鬼灵歌伎 2」是一条御魂，不能从加号前后劈开。"""
    slots = card_image.parse_slots("须佐之男", "须佐之男：狂骨 4 + 鬼灵歌伎 2")
    check("加号不被拆行", slots[0].souls == ["狂骨 4 + 鬼灵歌伎 2"], str(slots[0].souls))


def test_slots_limit() -> None:
    """一条阵容列太多列会挤成一团，最多取 5 个。"""
    slots = card_image.parse_slots("一 / 二 / 三 / 四 / 五 / 六 / 七", "")
    check("最多五列", len(slots) == 5, str(len(slots)))


def test_slots_ignore_placeholder() -> None:
    """「原文没提」是渲染层自己补的占位词，不能画成一列式神。"""
    slots = card_image.parse_slots("原文没提", "原文没提")
    check("占位词不占用式神位", slots == [], str([(s.name, s.souls) for s in slots]))


def test_speed_line() -> None:
    """配速要单独拎出来画，混在打法里会被忽略。"""
    entry = make_entry()
    speed = card_image._speed_line(entry)
    check("抽出配速行", "配速" in speed, speed)
    rest = card_image._howto_lines(entry, speed)
    check("打法里不再重复配速", all("配速" not in line for line in rest), str(rest))


def test_note_not_split_by_semicolon() -> None:
    """备注里的分号是并列项，拆开就成两条独立备注。"""
    entry = make_entry(note="神无月→千姬；那美→雪女\nSP缘→铁鼠")
    notes = card_image._note_lines(entry, "")
    check("分号不拆条", len(notes) == 2, str(notes))
    check("行内分号保留", "；" in notes[0] or ";" in notes[0], notes[0])


def test_note_dedupe_with_howto() -> None:
    """摘要把同一句写进打法和注意时，图里只留一份。"""
    entry = make_entry(
        howto="配速：A＞B。大天狗全队攻击最高，可以换成其他群体输出。",
        note="大天狗全队攻击最高，可以换成其他群体输出：UR大天狗/石长姬",
    )
    notes = card_image._note_lines(entry, entry.howto)
    check("重复的备注被丢掉", notes == [], str(notes))


# ------------------------------------------------------------------ 画图


def test_render_creates_png() -> None:
    """能画出一张正常尺寸的图。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = card_image.render_card_image(
            make_entry(), 1, 5, qr_path=None, code="",
            avatar_dir=os.path.join(tmp, "avatars"),
            out_dir=os.path.join(tmp, "cards"),
            generated_at="11-04 13:30",
        )
        check("返回了路径", bool(path))
        if not path:
            return
        check("文件存在", os.path.exists(path))
        check("文件不是空图", os.path.getsize(path) > 20000, str(os.path.getsize(path)))
        from PIL import Image

        with Image.open(path) as img:
            check("宽度固定 1080", img.width == 1080, str(img.width))
            check("高度撑开了", img.height > 600, str(img.height))
            check("比例是竖图", img.height > img.width * 0.7, f"{img.width}x{img.height}")


def test_render_with_qr() -> None:
    """带二维码时图里要真的贴进去，且不越界。"""
    with tempfile.TemporaryDirectory() as tmp:
        qr = teamcode.qr_image("|TA|2795b9ab4651ee39c37cfa9d79c4fc38", tmp)
        check("二维码能生成", bool(qr))
        path = card_image.render_card_image(
            make_entry(), 1, 5, qr_path=qr,
            code="|TA|2795b9ab4651ee39c37cfa9d79c4fc38",
            avatar_dir=os.path.join(tmp, "avatars"),
            out_dir=os.path.join(tmp, "cards"),
        )
        check("带码也能画", bool(path))
        from PIL import Image

        with Image.open(path) as img:
            check("带码时更高", img.height > 900, str(img.height))


def test_render_degenerate_entry() -> None:
    """只有标题的条目不能把渲染搞崩（斗技长文摘要失败时就是这样）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = card_image.render_card_image(
            CardEntry(title="只有标题"), 3, 5, qr_path=None, code="",
            avatar_dir=tmp, out_dir=os.path.join(tmp, "cards"),
        )
        check("空条目也能出图", bool(path))


def test_render_no_avatar_dir() -> None:
    """头像目录不存在时用配色圆占位，不该抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = card_image.render_card_image(
            make_entry(), 1, 1, avatar_dir="/nonexistent/avatars",
            out_dir=os.path.join(tmp, "cards"),
        )
        check("缺头像目录不崩", bool(path))


# ------------------------------------------------------------------ 头像


def _make_avatar(directory: str, name: str, color=(200, 60, 60)) -> None:
    from PIL import Image

    os.makedirs(directory, exist_ok=True)
    Image.new("RGBA", (160, 160), color + (255,)).save(
        os.path.join(directory, f"{name}.webp"), "WEBP"
    )


def test_avatar_multi_dir() -> None:
    """用户自己的头像目录优先，插件自带的兜底。"""
    with tempfile.TemporaryDirectory() as tmp:
        user = os.path.join(tmp, "user")
        bundled = os.path.join(tmp, "bundled")
        _make_avatar(user, "大天狗")
        _make_avatar(bundled, "神无月")
        img, _ = card_image._avatar("大天狗", 80, [user, bundled])
        check("优先用用户目录", img.getpixel((40, 40))[0] > 150, str(img.getpixel((40, 40))))
        img2, _ = card_image._avatar("神无月", 80, [user, bundled])
        check("回落到自带目录", img2.size == (80, 80))


def test_avatar_sp_alias() -> None:
    """SP 简称要落到正式名，不能命中同名 SSR。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_avatar(tmp, "缘结神", (10, 10, 10))
        _make_avatar(tmp, "纺愿缘结神", (250, 250, 250))
        img, _ = card_image._avatar("SP缘结神", 60, [tmp])
        pixel = img.getpixel((30, 30))
        check("SP 用正式名头像", pixel[0] > 200, str(pixel))


def test_avatar_missing_falls_back() -> None:
    """名字对不上就画占位圆，不能崩，也不能返回空图。"""
    img, mask = card_image._avatar("完全不存在的式神名", 64, ["/nonexistent"])
    check("占位圆尺寸正确", img.size == (64, 64) and mask.size == (64, 64))


def test_avatar_transparent_edges() -> None:
    """透明底头像要合到白底，否则圆裁后边缘发黑。"""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(tmp, exist_ok=True)
        img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
        # 中间画一块不透明的
        for x in range(40, 80):
            for y in range(40, 80):
                img.putpixel((x, y), (30, 120, 200, 255))
        img.save(os.path.join(tmp, "透明式神.webp"), "WEBP")
        got, _ = card_image._avatar("透明式神", 60, [tmp])
        corner = got.getpixel((2, 2))
        check("透明处铺成白色", min(corner) > 240, str(corner))


# ------------------------------------------------------------------ 文字码


def test_code_list_text() -> None:
    """图片模式下的纯文字码：一条不落、码顶格、没码时不发。"""
    empty = render_code_list([make_entry()], 3)
    check("没码时返回空串", empty == "", empty[:30])

    entry = make_entry(
        codes=[
            teamcode.TeamCode("|TA|2795b9ab4651ee39c37cfa9d79c4fc38", "主力"),
            teamcode.TeamCode("|TA|a352c5e746fb7765bbc66244597c59fe", "替补"),
        ]
    )
    text = render_code_list([entry], 3)
    check("两条码都在", text.count("|TA|") == 2, str(text.count("|TA|")))
    for line in text.splitlines():
        if line.startswith("|TA|"):
            check("码行零前缀", line.startswith("|TA|") and len(line) == 36, line)
            break
    check("码行不带全角缩进", "\u3000|TA|" not in text)


def test_code_list_respects_max() -> None:
    """一条贴了九个码的攻略，只发前面几个，别把消息塞满。"""
    codes = [teamcode.TeamCode(f"|TA|{'a' * 31}{i}", "") for i in range(9)]
    text = render_code_list([make_entry(codes=codes)], 3)
    check("按 max_codes 截断", text.count("|TA|") == 3, str(text.count("|TA|")))


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
