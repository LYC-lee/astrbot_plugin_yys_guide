"""阵容码：从攻略正文里抠出「|TA|xxxx」，并画成二维码。

阴阳师「阵容助手」分享出来的码长这样：

    【文字码】
    |TA|2795b9ab4651ee39c37cfa9d79c4fc38
    |TA|a352c5e746fb7765bbc66244597c59fe

游戏内路径是【式神录】→ 左上【阵容助手】→ 左下【阵容码】，
粘贴文字码或扫二维码都行。二维码的内容就是这串文字码本身，
所以有文字码就能自己画一张干净的码，不必去识别博主截图里的那张——
作者们普遍反馈「扫相册容易提示未识别到有效码，必须裁剪到只剩二维码」。

博主常把码拆成多段发（必备式神 / 补充包 / 阶段一阶段二），
码前面的那句短语就是它的名字。一条道馆攻略能贴 9 个甚至 27 个码，
所以提取时把名字一起带上，渲染时再按配置截断，不然卡片就成了码表。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

# |TA| 后面跟 32 位十六进制。用户从手机上复制常见全角竖线，
# 或者字符之间夹着空格，都要能认。结尾的负向前瞻用来拒绝 33 位——
# 少了它会悄悄截断成 32 位，用户拿到一个永远导不进去的码。
_CODE_RE = re.compile(
    r"[|｜]\s*T\s*A\s*[|｜]\s*((?:[0-9a-fA-F][\s\u3000]?){32})(?![0-9a-fA-F])",
    re.IGNORECASE,
)

# 一条动态里最多记这么多个。斗技/道馆合集动辄二十几个，
# 提取阶段宽松一点没关系，展示几个由 render.max_codes 决定。
MAX_CODES = 20

# 抓取时正文可能刚好在码中间被截断，剩下「|TA|8e77f5ea81cd4f5f8900a235405」
# 这种半截。它不是有效码，但留在正文里照样碍眼，用户还可能以为能复制。
_PARTIAL_RE = re.compile(r"[|｜]\s*T\s*A\s*[|｜][0-9a-fA-F\s\u3000]*", re.IGNORECASE)

# 码前面那句话最长取几个字当名字
LABEL_CHARS = 14

_LABEL_TRIM = " \t\u3000:：,，、|｜-—*#>·"
_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")


@dataclass(frozen=True)
class TeamCode:
    """一个阵容码，以及它在原文里的名字（可能为空）。"""

    code: str
    label: str = ""

    def __str__(self) -> str:
        return self.code


def normalize(raw: str) -> str:
    """统一成游戏能直接粘贴的形式：|TA| + 32 位小写十六进制。"""
    body = re.sub(r"[\s\u3000]", "", raw or "").lower()
    return f"|TA|{body}"


def _label_before(text: str, start: int, window: int = 40) -> str:
    """码同一行前面的短语，作为它的名字。

    「速度位1️⃣ |TA|xxx」-> 速度位1️⃣
    「【阶段一文字码】 |TA|xxx」-> 【阶段一文字码】

    只往同一行里找。上一行的东西猜不准，宁可留空——
    名字对错了比没有名字更误导人。
    """
    chunk = text[max(0, start - window) : start]
    line = chunk.rsplit("\n", 1)[-1]
    line = line.strip(_LABEL_TRIM)
    if not line or len(line) > LABEL_CHARS * 2:
        # 一整段正文后面直接跟码时不留名字，那不是标签是句子
        return ""
    return line[-LABEL_CHARS:]


def extract_codes(*texts: str) -> list[TeamCode]:
    """按出现顺序提取阵容码，去重。

    多个入参是为了把标题、摘要、正文一起扫一遍——
    有的博主把码写在简介里，有的写在正文末尾。
    """
    seen: set[str] = set()
    codes: list[TeamCode] = []
    for text in texts:
        if not text:
            continue
        body = str(text)
        for match in _CODE_RE.finditer(body):
            code = normalize(match.group(1))
            if len(code) != 36 or code in seen:
                continue
            seen.add(code)
            codes.append(TeamCode(code, _label_before(body, match.start())))
            if len(codes) >= MAX_CODES:
                return codes
    return codes


def strip_codes(text: str) -> str:
    """把正文里的阵容码删掉，只留下方那一份干净可复制的。

    三个原因：
    1. 摘要会截断到 160 字，截在码中间就剩半串，用户复制了也导入不了，
       看着还像插件把码弄错了；
    2. 同一串码在一条消息里出现两遍，用户不知道该复制哪个；
    3. 原文里「速度位1️⃣ |TA|xxx 输出位1️⃣ |TA|yyy」这种，码一删掉就
       只剩一串位名，比替换成占位符可读得多。
    """
    if not text:
        return ""
    cleaned = _CODE_RE.sub(" ", str(text))
    # 抓取时截断剩下的半截码也一并清掉
    cleaned = _PARTIAL_RE.sub(" ", cleaned)
    cleaned = _SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def short_code(code: str) -> str:
    """日志里用，别把整串刷屏。"""
    return f"{code[:7]}…{code[-6:]}" if len(code) > 16 else code


def qr_image(code: str, directory: str) -> str | None:
    """把阵容码画成 PNG，返回绝对路径；环境里没有 qrcode 就返回 None。

    生成失败一律返回 None，让调用方退回到「只发文字码」——
    二维码是加分项，不能让缺个依赖把整条回复搞挂。
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except Exception:  # noqa: BLE001  没装 / 装了但缺 pillow
        return None

    digest = hashlib.md5(code.encode("utf-8")).hexdigest()
    path = os.path.abspath(os.path.join(directory, f"{digest}.png"))

    # 同一个码在缓存里会反复出现，画一次就够了
    try:
        if os.path.getsize(path) > 0:
            return path
    except OSError:
        pass

    try:
        os.makedirs(directory, exist_ok=True)
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            # 白边按标准留够 4 个模块：扫码识别失败最常见的原因就是裁得太紧
            box_size=10,
            border=4,
        )
        qr.add_data(code)
        qr.make(fit=True)
        qr.make_image(fill_color="black", back_color="white").save(path)
        return path
    except Exception as exc:  # noqa: BLE001
        from astrbot.api import logger

        logger.debug(f"[yys-guide] 二维码生成失败（{short_code(code)}）：{exc!r}")
        return None
