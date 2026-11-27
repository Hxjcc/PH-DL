"""Built-in daily auto-update scheduler (no external cron required)."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
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


def _next_schedule(now: datetime, hhmm: Tuple[int, int], last_run: Optional[str]) -> Tuple[datetime, bool]:
    today = now.date()
    already_ran = last_run == today.isoformat()
    day = today + timedelta(days=1) if already_ran else today
    scheduled = datetime.combine(day, time(*hhmm)).astimezone()
    return scheduled, not already_ran and scheduled <= now


class UpdateScheduler:
    def __init__(
        self,
        repo: Repository,
        events: EventBus,
        trigger_auto_update: Callable[[str], Awaitable[Optional[int]]],
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
        now = datetime.now().astimezone()
        scheduled, due = _next_schedule(now, parsed, last_run)
        return (now if due else scheduled).isoformat(timespec="seconds")

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = await self.repo.get_settings()
                if settings.schedule_enabled:
                    parsed = parse_hhmm(settings.schedule_time)
                    if parsed:
                        last_run = await self._ensure_last_run_loaded()
                        now = datetime.now().astimezone()
                        _, due = _next_schedule(now, parsed, last_run)
                        if due:
                            today_str = now.date().isoformat()
                            job_id = await self._trigger_auto_update(today_str)
                            self._last_run_date = today_str
                            if job_id is not None:
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
