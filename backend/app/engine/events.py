"""In-memory event bus for WebSocket real-time progress and structured logs."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Set

from ..redaction import redact_diagnostic
from ..services.hls_downloader import is_hls_url

logger = logging.getLogger("phdl")
_TELEMETRY_EVENTS = frozenset({"item_progress", "job_scheduler", "job_scan", "log"})


def public_job_item(item: Dict[str, Any]) -> Dict[str, Any]:
    temp_path = item.get("temp_path") or ""
    is_hls = (
        (isinstance(temp_path, str) and temp_path.lower().endswith(".hls"))
        or is_hls_url(item.get("download_url"))
    )
    return {**item, "download_url": "", "is_hls": is_hls}


class SubscriberQueue(asyncio.Queue[Dict[str, Any]]):
    def __init__(self) -> None:
        super().__init__(maxsize=500)
        self._needs_resync = False

    def publish(self, packet: Dict[str, Any]) -> None:
        if self.full():
            if packet["type"] in _TELEMETRY_EVENTS:
                return
            for index, queued in enumerate(self._queue):
                if queued["type"] in _TELEMETRY_EVENTS:
                    del self._queue[index]
                    break
            else:
                self.get_nowait()
                self._needs_resync = True
            self.task_done()
        self.put_nowait(packet)

    def enqueue_resync_if_needed(self) -> None:
        if self._needs_resync and not self.full():
            # Deliver after the remaining backlog so older state cannot overtake the refresh.
            self.put_nowait({"type": "resync_required", "data": {}})
            self._needs_resync = False


class EventBus:
    def __init__(self, max_logs: int = 500, redact: Callable[[str], str] = redact_diagnostic) -> None:
        self._subscribers: Set[SubscriberQueue] = set()
        self._logs: Deque[Dict[str, Any]] = deque(maxlen=max_logs)
        self._boot_id: str = secrets.token_hex(4)
        self._log_seq: int = 0
        self._redact = redact
        self._live_progress: Dict[int, Dict[str, Any]] = {}
        self._scheduler_status: Dict[int, Dict[str, Any]] = {}
        self._scan_status: Dict[int, Dict[str, Any]] = {}
        self._cancelling_jobs: Set[int] = set()

    def subscribe(self) -> SubscriberQueue:
        q = SubscriberQueue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: SubscriberQueue) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event_type: str, data: Any) -> None:
        if self._cancelling_jobs and isinstance(data, dict):
            if event_type == "job_updated" and data.get("id") in self._cancelling_jobs:
                if data.get("status") in {"queued", "scanning", "running"}:
                    return
            if event_type in {"item_updated", "job_items_changed"} and data.get("job_id") in self._cancelling_jobs:
                return
        if event_type == "item_updated":
            data = public_job_item(data)
        packet = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "data": data,
        }
        for q in self._subscribers:
            q.publish(packet)

    def emit_log(self, level: str, message: str, job_id: Optional[int] = None) -> None:
        lvl = (level or "INFO").upper()
        message = self._redact(message)
        self._log_seq += 1
        entry = {
            "id": self._log_seq,
            "boot_id": self._boot_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": lvl,
            "job_id": job_id,
            "message": message,
        }
        self._logs.append(entry)
        self.broadcast("log", entry)

        py_level = getattr(logging, lvl, logging.INFO)
        prefix = f"[Job #{job_id}] " if job_id is not None else ""
        logger.log(py_level, f"{prefix}{message}")

    def emit_item_progress(
        self,
        job_id: int,
        item_id: int,
        bytes_downloaded: int,
        bytes_total: int,
        speed_bps: float,
        phase: str = "downloading",
        *,
        estimated_total: bool = False,
    ) -> None:
        if job_id in self._cancelling_jobs:
            return
        payload: Dict[str, Any] = {
            "job_id": job_id,
            "item_id": item_id,
            "bytes_downloaded": int(bytes_downloaded),
            "bytes_total": int(bytes_total),
            "speed_bps": round(float(speed_bps), 1),
            "phase": (phase or "downloading").strip().lower(),
        }
        if estimated_total:
            payload["estimated_total"] = True
        self._live_progress[item_id] = payload
        self.broadcast("item_progress", payload)

    def clear_item_progress(self, item_id: int) -> None:
        self._live_progress.pop(item_id, None)

    def emit_scheduler_status(self, job_id: int, status: Dict[str, Any]) -> None:
        if job_id in self._cancelling_jobs:
            return
        payload = {"job_id": int(job_id), **status}
        self._scheduler_status[int(job_id)] = payload
        self.broadcast("job_scheduler", payload)

    def clear_scheduler_status(self, job_id: int) -> None:
        if self._scheduler_status.pop(int(job_id), None) is not None:
            self.broadcast("job_scheduler", {"job_id": int(job_id), "cleared": True})

    def emit_scan_status(self, job_id: int, status: Dict[str, Any]) -> None:
        if job_id in self._cancelling_jobs:
            return
        payload = {"job_id": int(job_id), **status}
        self._scan_status[int(job_id)] = payload
        self.broadcast("job_scan", payload)

    def clear_scan_status(self, job_id: int) -> None:
        if self._scan_status.pop(int(job_id), None) is not None:
            self.broadcast("job_scan", {"job_id": int(job_id), "cleared": True})

    def clear_job_progress(self, job_id: int) -> None:
        for k in [k for k, v in self._live_progress.items() if v.get("job_id") == job_id]:
            self._live_progress.pop(k, None)
        self.clear_scheduler_status(job_id)
        self.clear_scan_status(job_id)

    def set_job_cancelling(self, job_id: int) -> None:
        jid = int(job_id)
        if jid not in self._cancelling_jobs:
            self._cancelling_jobs.add(jid)
            self.broadcast("job_cancelling", {"job_id": jid, "cancelling": True})

    def clear_job_cancelling(self, job_id: int) -> None:
        jid = int(job_id)
        if jid in self._cancelling_jobs:
            self._cancelling_jobs.remove(jid)
            self.broadcast("job_cancelling", {"job_id": jid, "cancelling": False})

    def cancelling_jobs_snapshot(self) -> List[int]:
        return sorted(self._cancelling_jobs)

    def recent_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        return list(self._logs)[-limit:]

    def clear_logs(self) -> None:
        self._logs.clear()

    def live_progress_snapshot(self) -> Dict[int, Dict[str, Any]]:
        return dict(self._live_progress)

    def scheduler_status_snapshot(self) -> Dict[int, Dict[str, Any]]:
        return dict(self._scheduler_status)

    def scan_status_snapshot(self) -> Dict[int, Dict[str, Any]]:
        return dict(self._scan_status)
