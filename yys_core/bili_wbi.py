"""B站 WBI 签名。

2023 年之后 B站的空间投稿、搜索等 web 接口都要求 w_rid + wts 签名，
不签名会直接返回 -352 / -403 风控错误。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

from astrbot.api import logger

from yys_core.http import HttpClient

_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# 官方前端里固定的混淆表，长度为 64
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_KEY_TTL = 6 * 3600.0


def _extract_key(url: str) -> str:
    """https://i0.hdslb.com/bfs/wbi/7cd0849413XXXX.png -> 7cd0849413XXXX"""
    if not url:
        return ""
    return url.rsplit("/", 1)[-1].split(".")[0]


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    if len(raw) < 64:
        return ""
    return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


class WbiSigner:
    """懒加载并缓存 img_key/sub_key，避免每次请求都打 nav 接口。"""

    def __init__(self, client: HttpClient):
        self._client = client
        self._img_key = ""
        self._sub_key = ""
        self._fetched_at = 0.0

    async def ensure_keys(self) -> bool:
        if self._img_key and time.time() - self._fetched_at < _KEY_TTL:
            return True
        try:
            data = await self._client.get_json(
                _NAV_URL,
                params={"web_location": "333.999"},
                headers={"Referer": "https://www.bilibili.com/"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[yys-guide] B站 nav 接口请求失败，WBI 签名不可用：{exc}")
            return False

        wbi_img = (data or {}).get("data", {}).get("wbi_img") or {}
        img_key = _extract_key(wbi_img.get("img_url", ""))
        sub_key = _extract_key(wbi_img.get("sub_url", ""))
        if not img_key or not sub_key:
            logger.warning("[yys-guide] B站 nav 接口未返回 wbi_img，跳过签名")
            return False

        self._img_key, self._sub_key = img_key, sub_key
        self._fetched_at = time.time()
        return True

    async def sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """返回带 wts / w_rid 的新参数字典；拿不到 key 时原样返回（让它去失败）。"""
        if not await self.ensure_keys():
            params.setdefault("wts", int(time.time()))
            return params

        mixin = _mixin_key(self._img_key, self._sub_key)
        if not mixin:
            params.setdefault("wts", int(time.time()))
            return params

        signed = dict(params)
        signed["wts"] = int(time.time())

        cleaned: list[tuple[str, str]] = []
        for key in sorted(signed):
            value = signed[key]
            if value is None:
                continue
            text = str(value)
            # 官方要求过滤 !'()* 这几个字符，否则签名对不上
            text = "".join(ch for ch in text if ch not in "!'()*")
            cleaned.append((key, text))

        query = urllib.parse.urlencode(cleaned)
        w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()

        result: dict[str, Any] = dict(cleaned)
        result["w_rid"] = w_rid
        return result
