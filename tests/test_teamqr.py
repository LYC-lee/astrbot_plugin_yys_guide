"""阵容二维码的离线自测。

跑法：python tests/test_teamqr.py

这里守住一条容易搞错的边界：能扫码导入的二维码，内容是 `#TA#` 开头的
压缩数据，不是那串 `|TA|` 文字码。文字码画成二维码扫出来只是文本。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
PLUGIN_DIR = (
    _PARENT
    if os.path.isfile(os.path.join(_PARENT, "main.py"))
    else os.path.join(_PARENT, "astrbot_plugin_yys_guide")
)
sys.path.insert(0, PLUGIN_DIR)

from yys_core import teamqr  # noqa: E402

PASSED = 0
FAILED: list[str] = []

# 真实的阵容数据是 448 字符左右，这里造一条同量级的假数据
FAKE_PAYLOAD = "#TA#eJxrZ2Rhmjadi8mGYdIkDtZJ3KwHDsw6c5NNg3EyKyvrdEZGxgmTJxpMNGKYyTDptEMkAxicdsh2ADOAQgdtoEJXFj9LAwKIiskTGSYyTGJkOjCRifvArLNMIQgDWYAG8jAAzYOZkH4AakKhAQNMN/NEtolsByZwMwD1aoH1MoL0cgL1CjNgdwvUtFQHqHBaA8K0"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [ok] {name}")
    else:
        FAILED.append(f"{name}{'：' + detail if detail else ''}")
        print(f"  [!!] {name} {detail}")


def _qr_bytes(text: str, box_size: int = 8) -> bytes:
    import qrcode

    qr = qrcode.QRCode(box_size=box_size, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ 判定


def test_payload_prefix() -> None:
    """只认 #TA#；文字码、别的二维码一律不算。"""
    check("#TA# 数据算阵容码", teamqr.is_team_payload(FAKE_PAYLOAD))
    check(
        "文字码不算阵容码",
        not teamqr.is_team_payload("|TA|2795b9ab4651ee39c37cfa9d79c4fc38"),
    )
    check("公众号链接不算", not teamqr.is_team_payload("https://mp.weixin.qq.com/s/abc"))
    check("空串不算", not teamqr.is_team_payload(""))


def test_payload_length_guard() -> None:
    """太短太长都不正常，别把一张糊图当成阵容。"""
    check("太短拒绝", not teamqr.is_team_payload("#TA#abc"))
    check("太长拒绝", not teamqr.is_team_payload("#TA#" + "a" * 5000))


# ------------------------------------------------------------------ 解码


def test_decode_bytes() -> None:
    """能解出自己画的码，且只挑 #TA# 那一张。"""
    other = _qr_bytes("https://example.com/not-a-team-code")
    check("别的二维码不认", teamqr.decode_bytes(other) is None)

    data = _qr_bytes(FAKE_PAYLOAD)
    got = teamqr.decode_bytes(data)
    check("解出阵容数据", got == FAKE_PAYLOAD, str(got)[:40])


def test_decode_garbage() -> None:
    """不是图片的字节不能抛异常。"""
    check("垃圾字节返回 None", teamqr.decode_bytes(b"\x00" * 2048) is None)
    check("空字节返回 None", teamqr.decode_bytes(b"") is None)


# ------------------------------------------------------------------ 重画


def test_render_roundtrip() -> None:
    """重画的码必须能解回同一份数据，否则扫了也白扫。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = teamqr.render_payload(FAKE_PAYLOAD, tmp)
        check("生成了文件", bool(path) and os.path.exists(path or ""))
        if not path:
            return
        with open(path, "rb") as fh:
            again = teamqr.decode_bytes(fh.read())
        check("内容与原图一致", again == FAKE_PAYLOAD, str(again)[:40])
        from PIL import Image

        with Image.open(path) as img:
            check("尺寸够扫", min(img.size) >= 400, str(img.size))


def test_render_survives_shrink() -> None:
    """微信/QQ 会把图压一道，缩到卡片里那个尺寸还得能解。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = teamqr.render_payload(FAKE_PAYLOAD, tmp)
        if not path:
            check("生成失败", False)
            return
        from PIL import Image

        with Image.open(path) as img:
            small = img.convert("RGB").resize((244, 244), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=85)
        check("压到 244px 仍可解", teamqr.decode_bytes(buf.getvalue()) == FAKE_PAYLOAD)


# ------------------------------------------------------------------ 抓图


class _FakeResponse:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content


class _FakeClient:
    """按 url 返回预设结果，用来验证「翻图」的顺序与容错。"""

    def __init__(self, mapping: dict[str, _FakeResponse]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append(url)
        if url not in self.mapping:
            raise RuntimeError("boom")
        return self.mapping[url]


def test_find_payload_skips_bad() -> None:
    """前面几张挂了或不是码，要继续往后翻。"""
    good = _qr_bytes(FAKE_PAYLOAD)
    client = _FakeClient(
        {
            "https://a/1.jpg": _FakeResponse(404, b""),
            "https://a/2.jpg": _FakeResponse(200, b"not an image"),
            "https://a/3.jpg": _FakeResponse(200, good),
        }
    )
    got = asyncio.run(teamqr.find_payload(client, ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"]))
    check("跳过失败项继续找", got == FAKE_PAYLOAD, str(got)[:40])
    check("按顺序请求", client.calls == ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"])


def test_find_payload_limit() -> None:
    """一篇最多翻三张，别把整篇图都下一遍。"""
    client = _FakeClient({})
    urls = [f"https://a/{i}.jpg" for i in range(10)]
    asyncio.run(teamqr.find_payload(client, urls))
    check("最多翻 3 张", len(client.calls) == 3, str(len(client.calls)))


def test_find_payload_empty() -> None:
    """没有图就别发请求。"""
    client = _FakeClient({})
    check("空列表返回 None", asyncio.run(teamqr.find_payload(client, [])) is None)
    check("没发请求", client.calls == [])


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
