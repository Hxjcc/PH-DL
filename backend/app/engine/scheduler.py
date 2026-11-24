"""Built-in daily auto-update scheduler (no external cron required)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional, Tuple

from ..db.repo import Repository
from .events import EventBus


def parse_hhmm(time_str: str) -> Optional[Tuple[int, int]]:
    parts = (time_str or "").strip().split(":")
    if len(parts) != 2:
        return None
    if not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


class UpdateScheduler:
    def __init__(
        self,
        repo: Repository,
        events: EventBus,
        trigger_auto_update: Callable[[], Awaitable[int]],
    ) -> None:
        self.repo = repo
        self.events = events
        self._trigger_auto_update = trigger_auto_update
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._last_run_date: Optional[str] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop(), name="phdl-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _ensure_last_run_loaded(self) -> Optional[str]:
        if self._last_run_date is None:
            self._last_run_date = await self.repo.get_scheduler_last_run_date()
        return self._last_run_date

    async def next_run_iso(self) -> Optional[str]:
        settings = await self.repo.get_settings()
        if not settings.schedule_enabled:
            return None
        parsed = parse_hhmm(settings.schedule_time)
        if not parsed:
            return None
        last_run = await self._ensure_last_run_loaded()
        hour, minute = parsed
        now = datetime.now()
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        today_str = now.strftime("%Y-%m-%d")
        if candidate <= now or last_run == today_str:
            candidate += timedelta(days=1)
        return candidate.isoformat(timespec="seconds")

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = await self.repo.get_settings()
                if settings.schedule_enabled:
                    parsed = parse_hhmm(settings.schedule_time)
                    if parsed:
                        last_run = await self._ensure_last_run_loaded()
                        hour, minute = parsed
                        now = datetime.now()
                        today_str = now.strftime("%Y-%m-%d")
                        scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if scheduled_dt <= now and last_run != today_str:
                            # Catch up when the process starts after the scheduled
                            # time, then persist the run date only after the job
                            # has actually been enqueued.
                            job_id = await self._trigger_auto_update()
                            self._last_run_date = today_str
                            await self.repo.set_scheduler_last_run_date(today_str)
                            self.events.broadcast("overview_changed", {})
                            self.events.emit_log(
                                "INFO",
                                f"定时追更触发（{settings.schedule_time}），已创建任务 #{job_id}",
                                job_id=job_id,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.events.emit_log("ERROR", f"定时调度器检查异常: {err}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=20.0)
            except asyncio.TimeoutError:
                continue
