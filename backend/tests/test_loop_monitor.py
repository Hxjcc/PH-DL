"""Tests for event-loop latency monitoring."""

from __future__ import annotations

import asyncio
import time

from app.engine.events import EventBus
from app.engine.loop_monitor import EventLoopMonitor


async def test_loop_monitor_records_lag_and_emits_warning():
    events = EventBus(max_logs=50)
    monitor = EventLoopMonitor(
        events,
        interval_seconds=0.02,
        warning_threshold_ms=80.0,
        warning_cooldown_seconds=1.0,
        window_size=20,
    )
    monitor.start()
    try:
        await asyncio.sleep(0.03)
        time.sleep(0.15)
        await asyncio.sleep(0.05)
    finally:
        await monitor.stop()

    snapshot = monitor.snapshot()
    assert snapshot["samples"] > 0
    assert snapshot["max_ms"] >= 80.0
    assert snapshot["warning_count"] >= 1
    assert any("事件循环延迟" in entry["message"] for entry in events.recent_logs())
