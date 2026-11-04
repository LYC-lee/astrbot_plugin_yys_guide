"""游戏生成的阵容二维码。

这里有个容易搞错的地方：游戏里「阵容助手」的二维码，装的不是那串
`|TA|` 文字码，而是一段 `#TA#` 开头的压缩数据——把 base64 解开、
zlib 解压之后是四百多字节的二进制，里面有式神位次、御魂、速度值
和作者留的说明。扫一下游戏直接算出阵容，靠的就是这份数据。

`|TA|` + 32 位十六进制是另一套东西：粘贴进游戏也能导入，但它只有
36 个字符，和二维码里的数据没有换算关系。所以拿文字码画一张二维码
是画不出「能扫码导入」的码的——扫出来只是一段文本，游戏不认。

能做的只有一件事：博主原图里本来就有真二维码，把它解出来再重画。
重画的内容完全相同，扫码效果一样，但比博主截图里那张干净得多
（作者们普遍反馈原图要裁到只剩二维码才扫得动）。
"""

from __future__ import annotations

import hashlib
import io
import os

#: 游戏二维码的固定前缀。用它精确筛选，不会把原图里的公众号二维码、
#: 活动二维码一起当成阵容码。
TA_PREFIX = "#TA#"

#: 太短的不是阵容数据，太长的是把整张图当码了——都不正常
MIN_LEN = 60
MAX_LEN = 3000

#: 一篇攻略最多翻几张图找码。大神图文一般前三张就有关键信息，
#: 全下下来既慢又费流量。
MAX_IMAGES = 3


def is_team_payload(text: str) -> bool:
    if not text or not text.startswith(TA_PREFIX):
        return False
    return MIN_LEN <= len(text) <= MAX_LEN


def _scan(image) -> str | None:
    try:
        import zxingcpp
    except Exception:  # noqa: BLE001
        return None
    try:
        results = zxingcpp.read_barcodes(image)
    except Exception:  # noqa: BLE001
        return None
    for result in results:
        text = result.text or ""
        if is_team_payload(text):
            return text
    return None


def _scan_tiles(image) -> str | None:
    """长图切块扫。

    攻略「一图流」常是 1700×5900 这种长条，二维码只占其中一小块。
    整张缩到能一次扫的尺寸，二维码就剩几十个像素，糊得认不出来；
    保持原分辨率切段扫，一块也就 1800 高，几秒钟能扫完。
    """
    tile_h = 1800
    overlap = 240
    step = tile_h - overlap
    top = 0
    while top < image.height:
        bottom = min(top + tile_h, image.height)
        hit = _scan(image.crop((0, top, image.width, bottom)))
        if hit:
            return hit
        if bottom >= image.height:
            break
        top += step
    return None


def decode_bytes(data: bytes) -> str | None:
    """从图片字节里找出阵容二维码的内容。

    一张攻略图可能同时有公众号二维码和阵容码，按前缀挑，
    挑不到就返回 None——宁可没有码，也不能贴错。
    """
    if not data or len(data) < 512:
        return None
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None

    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None

    # 常规截图先整张扫一次，命中就不用切了
    if image.width * image.height <= 12_000_000:
        hit = _scan(image)
        if hit:
            return hit
        # 整张扫不到时尺寸通常也不大，切块也没意义
        if image.height <= 2400:
            return None

    if image.width * image.height > 24_000_000:
        # 动辄几千万像素的图先压一道，不然后面的切块会很慢
        scale = (24_000_000 / (image.width * image.height)) ** 0.5
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return _scan_tiles(image)


def render_payload(payload: str, directory: str, target_px: int = 640) -> str | None:
    """把 `#TA#` 数据重新画成二维码，返回文件路径。

    尺寸按内容自适应：文字码那种短内容画 640 会稀疏得难看，
    四百多字节的阵容数据则必须给够格子才扫得动。
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except Exception:  # noqa: BLE001
        return None

    try:
        probe = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=4)
        probe.add_data(payload)
        probe.make(fit=True)
        modules = probe.modules_count + 8
        box_size = max(3, target_px // modules)
        qr = qrcode.QRCode(
            error_correction=ERROR_CORRECT_M, box_size=box_size, border=4
        )
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception:  # noqa: BLE001
        return None

    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()
    path = os.path.abspath(os.path.join(directory, f"ta_{digest}.png"))
    try:
        os.makedirs(directory, exist_ok=True)
        image.save(path, "PNG", optimize=True)
    except Exception:  # noqa: BLE001
        return None
    return path


async def find_payload(client, urls: list[str], limit: int = MAX_IMAGES) -> str | None:
    """按顺序翻图，找到第一张带阵容码的就返回它的内容。

    单张失败（404 / 超时 / 不是图）直接跳过，不影响后面几张。
    """
    if not urls:
        return None
    for url in urls[:limit]:
        try:
            # 一图流单张就六七兆，超时给宽一点
            resp = await client.request("GET", url, timeout=20.0)
            if resp.status_code != 200:
                continue
            payload = decode_bytes(resp.content)
        except Exception:  # noqa: BLE001
            continue
        if payload:
            return payload
    return None
