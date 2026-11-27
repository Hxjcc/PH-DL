"""Task status delivery and scheduler regressions."""

from datetime import datetime
from types import SimpleNamespace

from app.config import RuntimeSettings
from app.engine import disk_scheduler, scheduler
from app.engine.events import EventBus
from app.services.downloader import DownloadDiskBudget


def test_event_queue_keeps_job_structure_and_resyncs_only_overrun_subscribers():
    events = EventBus()
    slow = events.subscribe()
    fast = events.subscribe()

    def publish(kind, data):
        events.broadcast(kind, data)
        assert fast.get_nowait()["type"] == kind
        fast.task_done()

    for index in range(slow.maxsize - 1):
        publish("job_updated", {"id": index})
    publish("item_progress", {"item_id": 1})
    publish("log", {})
    publish("jobs_deleted", {"job_ids": [1000]})
    pending = []
    while not slow.empty():
        pending.append(slow.get_nowait())
        slow.task_done()
        slow.enqueue_resync_if_needed()
    assert len(pending) == slow.maxsize
    assert [item["data"]["id"] for item in pending[:-1]] == list(range(slow.maxsize - 1))
    assert pending[-1]["type"] == "jobs_deleted"

    for index in range(slow.maxsize + 1):
        publish("job_updated", {"id": index})
    fast.enqueue_resync_if_needed()
    assert fast.empty()
    pending = []
    while not slow.empty():
        pending.append(slow.get_nowait())
        slow.task_done()
        slow.enqueue_resync_if_needed()
    assert [item["data"]["id"] for item in pending[:-1]] == list(range(1, slow.maxsize + 1))
    assert pending[-1] == {"type": "resync_required", "data": {}}


async def test_progress_status_probes_disk_once_per_snapshot_and_throttles_unchanged_states(tmp_path, monkeypatch):
    probes, emitted = [], []
    now = [10.0]
    monkeypatch.setattr(disk_scheduler, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def disk_usage(_path):
        probes.append(True)
        return SimpleNamespace(free=4 * disk_scheduler.GIB)

    manager = disk_scheduler.AdaptiveDiskScheduler(
        tmp_path, DownloadDiskBudget(tmp_path, 0), max_downloads=3, max_uploads=1,
        min_free_disk_gb=1, disk_usage_fn=disk_usage, on_status=emitted.append,
    )
    snapshot = manager.snapshot()
    assert len(probes) == 1 and snapshot["prefer_largest_upload"]
    manager.emit_status()
    for _ in range(100):
        manager.emit_status()
    assert len(probes) == 2 and len(emitted) == 1
    now[0] += 1
    manager.emit_status()
    assert len(probes) == 3 and len(emitted) == 2
    manager.active_downloads += 1
    manager.emit_status()
    assert len(probes) == 4 and emitted[-1]["active_downloads"] == 1


async def test_scheduler_reports_overdue_run_today_with_offset_and_then_tomorrow(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 20, 18, 0, 0)

    monkeypatch.setattr(scheduler, "datetime", FixedDateTime)
    config = RuntimeSettings(schedule_enabled=True, schedule_time="04:30")
    triggered = []

    async def get_settings():
        return config

    async def last_date():
        return "2026-10-19"

    async def trigger(date):
        triggered.append(date)
        manager._stop_event.set()
        return 100

    manager = scheduler.UpdateScheduler(
        SimpleNamespace(get_settings=get_settings, get_scheduler_last_run_date=last_date), EventBus(), trigger,
    )
    due = datetime.fromisoformat(await manager.next_run_iso())
    assert due.date().isoformat() == "2026-10-20" and due.utcoffset() is not None
    await manager._loop()
    assert triggered == ["2026-10-20"]
    following = datetime.fromisoformat(await manager.next_run_iso())
    assert following.date().isoformat() == "2026-10-21" and (following.hour, following.minute) == (4, 30)
