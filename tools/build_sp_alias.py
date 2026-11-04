"""生成 SP/UR 式神简称到正式名的映射表。

玩家和博主写的是「SP缘结神」，游戏里的正式名是「纺愿缘结神」，
头像文件也是按正式名存的。不去查这层映射，SP 阵容就会显示成
同名 SSR 的脸——比没有头像更误导。

办法是查 wiki 的重定向：`SP缘结神` 这条目的目标页就是 `纺愿缘结神`。

跑法：python tools/build_sp_alias.py
产物：assets/sp_alias.json
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

WIKI = "https://wiki.biligame.com/yys/api.php"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AVATAR_DIR = os.path.join(ROOT, "assets", "avatars")
OUT = os.path.join(ROOT, "assets", "sp_alias.json")

PREFIXES = ("SP", "UR", "SSR")


async def resolve(client: httpx.AsyncClient, name: str, prefix: str) -> tuple[str, str] | None:
    alias = f"{prefix}{name}"
    try:
        resp = await client.get(
            WIKI,
            params={
                "action": "query",
                "titles": alias,
                "redirects": 1,
                "format": "json",
            },
        )
        data = resp.json().get("query", {})
    except Exception:  # noqa: BLE001
        return None
    redirects = data.get("redirects") or []
    if not redirects:
        return None
    target = redirects[0].get("to", "").strip()
    if not target or target == alias:
        return None
    return alias, target


async def main_async() -> None:
    names = sorted(
        f.rsplit(".", 1)[0]
        for f in os.listdir(AVATAR_DIR)
        if f.endswith((".webp", ".png", ".jpg"))
    )
    sem = asyncio.Semaphore(6)
    mapping: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as client:
        async def one(name: str, prefix: str) -> None:
            async with sem:
                got = await resolve(client, name, prefix)
            if got:
                mapping[got[0]] = got[1]

        tasks = [one(n, p) for n in names for p in PREFIXES]
        for index, coro in enumerate(asyncio.as_completed(tasks), 1):
            await coro
            if index % 100 == 0:
                print(f"  ... {index}/{len(tasks)}，已命中 {len(mapping)}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(mapping.items())), fh, ensure_ascii=False, indent=1)
    print(f"\n写入 {OUT}：{len(mapping)} 条")
    for key in list(sorted(mapping))[:10]:
        print(f"  {key} -> {mapping[key]}")


if __name__ == "__main__":
    asyncio.run(main_async())
