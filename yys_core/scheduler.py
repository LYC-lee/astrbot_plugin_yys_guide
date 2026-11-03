"""定时预热。

不依赖框架的调度 API（不同版本差异大），自己维护一个 asyncio 循环。
休眠分片进行，容器暂停/系统时间跳变后能自己校正回正确时间点。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from astrbot.api import logger

_SLEEP_SLICE = 300.0


def parse_times(raw: list[str] | None) -> list[tuple[int, int]]:
    """["09:00", "20:30"] -> [(9, 0), (20, 30)]，非法值直接丢掉。"""
    parsed: list[tuple[int, int]] = []
    for entry in raw or []:
        text = str(entry).strip()
        if ":" not in text:
            continue
        hour_text, minute_text = text.split(":", 1)
        try:
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.append((hour, minute))
    return sorted(set(parsed))


class WarmupScheduler:
    def __init__(self, times: list[tuple[int, int]], callback: Callable[[], Awaitable[None]]):
        self.times = times
        self.callback = callback
        self._task: asyncio.Task | None = None
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not self.times:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="yys-guide-warmup")
        logger.info(
            f"[yys-guide] 定时预热已启动：{['%02d:%02d' % t for t in self.times]}"
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def next_run(self, now: datetime | None = None) -> datetime | None:
        if not self.times:
            return None
        now = now or datetime.now()
        candidates: list[datetime] = []
        for hour, minute in self.times:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates) if candidates else None

    async def _loop(self) -> None:
        while not self._stopped:
            target = self.next_run()
            if target is None:
                return
            logger.debug(f"[yys-guide] 下次预热时间：{target:%Y-%m-%d %H:%M}")
            await self._sleep_until(target)
            if self._stopped:
                return
            try:
                await self.callback()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[yys-guide] 定时预热执行失败：{exc!r}")
            # 避免同一分钟重复触发
            await asyncio.sleep(61)

    async def _sleep_until(self, target: datetime) -> None:
        while not self._stopped:
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, _SLEEP_SLICE))
