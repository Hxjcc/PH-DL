"""Lightweight event-loop latency monitor."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import Any, Deque, Dict, Optional

from .events import EventBus


class EventLoopMonitor:
    """Sample event-loop scheduling delay without creating a background thread."""

    def __init__(
        self,
        events: EventBus,
        *,
        interval_seconds: float = 1.0,
        warning_threshold_ms: float = 250.0,
        warning_cooldown_seconds: float = 10.0,
        window_size: int = 60,
    ) -> None:
        self.events = events
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.warning_threshold_ms = max(1.0, float(warning_threshold_ms))
        self.warning_cooldown_seconds = max(0.0, float(warning_cooldown_seconds))
        self._samples: Deque[float] = deque(maxlen=max(1, int(window_size)))
        self._task: Optional[asyncio.Task] = None
        self._last_warning_at: Optional[float] = None
        self._warning_count = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="phdl-loop-monitor")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval_seconds
        while True:
            await asyncio.sleep(max(0.0, expected - loop.time()))
            now = loop.time()
            lag_ms = max(0.0, (now - expected) * 1000.0)
            self._samples.append(lag_ms)

            if (
                lag_ms >= self.warning_threshold_ms
                and (
                    self._last_warning_at is None
                    or now - self._last_warning_at >= self.warning_cooldown_seconds
                )
            ):
                self._last_warning_at = now
                self._warning_count += 1
                self.events.emit_log(
                    "WARNING",
                    f"事件循环延迟 {lag_ms:.0f}ms（阈值 {self.warning_threshold_ms:.0f}ms）",
                )

            expected += self.interval_seconds
            if expected <= now:
                expected = now + self.interval_seconds

    def snapshot(self) -> Dict[str, Any]:
        samples = list(self._samples)
        return {
            "current_ms": round(samples[-1], 2) if samples else 0.0,
            "avg_ms": round(sum(samples) / len(samples), 2) if samples else 0.0,
            "max_ms": round(max(samples), 2) if samples else 0.0,
            "samples": len(samples),
            "interval_ms": round(self.interval_seconds * 1000.0, 2),
            "warning_threshold_ms": self.warning_threshold_ms,
            "warning_count": self._warning_count,
        }
