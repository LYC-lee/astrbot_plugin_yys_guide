"""从 biligame wiki 拉式神立绘，裁成方形头像给卡片用。

跑法：
    python tools/fetch_avatars.py --limit 10   # 先试几个看效果
    python tools/fetch_avatars.py              # 全量

素材来源是 wiki 的 `Special:FilePath/<式神名>头像.png`（游戏内方形头像，180×180），
没有头像文件的退回 `<式神名>.png` 立绘自己裁。
插件不内置整张立绘（一张 0.5~1MB，两百多个式神就是几百兆），
只留裁好的 256px 方形头像，一张二十来KB。

为什么不用整张立绘直接圆裁：立绘是全身像，缩进 150px 的圆里
脸只有十几个像素，糊成一团。所以按 alpha 找人物包围盒，
取头顶往下 46% 那一块（头 + 肩），视觉上就是头像。

版权归网易所有，仅供个人自用；仓库里只放缩略图，不放原画。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys

import httpx
from PIL import Image

WIKI = "https://wiki.biligame.com/yys"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets", "avatars")


def normalize_avatar(raw: bytes, size: int = 180) -> Image.Image | None:
    """头像文件本来就是方形脸，直接缩放；顺带处理非方形的情况。"""
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
    if img.width < 40 or img.height < 40:
        return None
    ratio = img.width / img.height
    if 0.9 <= ratio <= 1.1:
        return img.resize((size, size), Image.Resampling.LANCZOS)
    return head_crop(raw, size)


def head_crop(raw: bytes, size: int = 256) -> Image.Image | None:
    """把透明底立绘裁成「头 + 肩」的方形头像，铺白底。"""
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
    if img.width < 60 or img.height < 60:
        return None

    alpha = img.getchannel("A")
    bbox = alpha.point(lambda v: 255 if v > 12 else 0).getbbox()
    if not bbox:
        bbox = (0, 0, img.width, img.height)
    left, top, right, bottom = bbox
    height = max(1, bottom - top)

    # 头部水平位置：只取最上面那一小条不透明像素的中位数。
    # 用包围盒中心会把长兵器、翅膀算进去，脸就偏到一边了。
    band = alpha.crop((left, top, right, min(bottom, top + max(8, int(height * 0.22)))))
    try:
        import numpy as np

        arr = np.array(band)
        ys, xs = np.nonzero(arr > 20)
        cx = left + int(np.median(xs)) if len(xs) else (left + right) // 2
    except Exception:  # noqa: BLE001
        cx = (left + right) // 2

    side = int(height * 0.46)
    x0 = cx - side // 2
    y0 = top - int(side * 0.06)
    crop = img.crop((x0, y0, x0 + side, y0 + side))

    # 立绘是透明底，卡片是白底：先合到白底，免得圆裁后边缘发黑
    canvas = Image.new("RGB", crop.size, (255, 255, 255))
    canvas.paste(crop, mask=crop.split()[-1])
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


async def fetch_one(client: httpx.AsyncClient, name: str, sem: asyncio.Semaphore) -> tuple[str, str]:
    path = os.path.join(OUT_DIR, f"{name}.png")
    if os.path.exists(path) and os.path.getsize(path) > 4000:
        return name, "skip"

    async with sem:
        # 先要「XX头像.png」——这是游戏内的方形头像，脸就在正中间。
        # 没有的话退回「XX.png」全身立绘，自己按 alpha 裁头。
        for suffix in ("头像", ""):
            try:
                resp = await client.get(f"{WIKI}/Special:FilePath/{name}{suffix}.png")
            except Exception:  # noqa: BLE001
                return name, "net"
            if resp.status_code != 200 or not resp.content:
                continue
            avatar = (
                normalize_avatar(resp.content) if suffix else head_crop(resp.content)
            )
            if avatar is None:
                continue
            try:
                avatar.save(path, "PNG", optimize=True)
            except Exception:  # noqa: BLE001
                return name, "write"
            return name, "ok" if suffix else "ok-立绘"
    return name, "404"


async def main_async(names: list[str]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    sem = asyncio.Semaphore(6)
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    stats: dict[str, int] = {}
    async with httpx.AsyncClient(
        timeout=30, headers={"User-Agent": UA}, follow_redirects=True, limits=limits
    ) as client:
        tasks = [fetch_one(client, n, sem) for n in names]
        for index, coro in enumerate(asyncio.as_completed(tasks), 1):
            name, status = await coro
            stats[status] = stats.get(status, 0) + 1
            if status not in ("ok", "skip"):
                print(f"  [miss] {name}: {status}")
            if index % 25 == 0:
                print(f"  ... {index}/{len(names)}")
    print(f"\n完成：{stats}")
    total = sum(
        os.path.getsize(os.path.join(OUT_DIR, f))
        for f in os.listdir(OUT_DIR)
        if f.endswith(".png")
    )
    print(f"目录 {OUT_DIR} 共 {len(os.listdir(OUT_DIR))} 个文件，{total / 1024 / 1024:.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--names", default="", help="逗号分隔，指定要抓的名字")
    args = parser.parse_args()

    if args.names:
        names = [n.strip() for n in args.names.split(",") if n.strip()]
    else:
        list_path = os.path.join(ROOT, "tools", "avatar_names.json")
        with open(list_path, encoding="utf-8") as fh:
            names = json.load(fh)
    if args.limit:
        names = names[: args.limit]

    asyncio.run(main_async(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
