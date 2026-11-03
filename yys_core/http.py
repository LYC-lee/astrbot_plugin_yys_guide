"""统一的 HTTP 客户端：复用连接、带 UA、失败重试、JSON 容错。"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from astrbot.api import logger

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def parse_cookie(raw: str | None) -> dict[str, str]:
    """把 "a=1; b=2" 解析成 dict。"""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in str(raw).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


class HttpClient:
    """所有抓取器共用一个实例，避免每次请求都新建连接池。"""

    def __init__(self, timeout: float = 15.0, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                headers={
                    "User-Agent": DEFAULT_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        client = await self._ensure_client()
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    cookies=cookies,
                    **kwargs,
                )
                if resp.status_code in _RETRY_STATUS and attempt < self.retries:
                    await asyncio.sleep(0.6 * (attempt + 1) + random.random() * 0.3)
                    continue
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.6 * (attempt + 1) + random.random() * 0.3)
                    continue
                raise

        raise last_error or RuntimeError(f"请求失败：{url}")

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> str:
        resp = await self.request("GET", url, headers=headers, params=params, cookies=cookies)
        resp.raise_for_status()
        return resp.text

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> Any:
        resp = await self.request("GET", url, headers=headers, params=params, cookies=cookies)
        resp.raise_for_status()
        return resp.json()

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> Any:
        resp = await self.request(
            "POST", url, headers=headers, json=payload, cookies=cookies
        )
        resp.raise_for_status()
        return resp.json()

    async def get_cookies(self, url: str) -> dict[str, str]:
        """访问一次首页，把 Set-Cookie 收下来（B站/NGA 的游客态靠这个）。"""
        client = await self._ensure_client()
        try:
            await client.get(url)
        except (httpx.HTTPError, httpx.TransportError) as exc:
            logger.debug(f"[yys-guide] 获取游客 cookie 失败 {url}: {exc}")
            return {}
        return {k: v for k, v in client.cookies.items()}

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
