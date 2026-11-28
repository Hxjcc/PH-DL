"""In-memory event bus for WebSocket real-time progress and structured logs."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from ..redaction import redact_diagnostic
from ..services.hls_downloader import is_hls_url
from ..services.subprocesses import has_active_real_upload_processes

logger = logging.getLogger("phdl")
_TELEMETRY_EVENTS = frozenset({"item_progress", "job_scheduler", "job_scan", "log"})
_NET_TX_WINDOW_SECONDS = 2.0
_NET_TX_MIN_SPAN_SECONDS = 0.5
_NET_TX_SAMPLE_INTERVAL_SECONDS = 0.2
_NET_TX_ACK_RATIO = 0.02


def read_linux_net_bytes(proc_net_dev_path: str = "/proc/net/dev") -> Optional[Tuple[int, int]]:
    """Sum non-loopback (rx_bytes, tx_bytes) from Linux /proc/net/dev."""
    try:
        with open(proc_net_dev_path, "r", encoding="ascii", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    rx_total = 0
    tx_total = 0
    found = False
    for line in lines[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        if not iface or iface == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
            found = True
        except ValueError:
            continue
    return (rx_total, tx_total) if found else None


class NetTxRateMeter:
    """Estimate actual container outbound upload bandwidth from Linux NIC counters."""

    def __init__(
        self,
        *,
        reader: Callable[[], Optional[Tuple[int, int]]] = read_linux_net_bytes,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = _NET_TX_WINDOW_SECONDS,
        min_span_seconds: float = _NET_TX_MIN_SPAN_SECONDS,
        sample_interval: float = _NET_TX_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._reader = reader
        self._clock = clock
        self._window_seconds = window_seconds
        self._min_span_seconds = min_span_seconds
        self._sample_interval = sample_interval
        self._samples: Deque[Tuple[float, int, int]] = deque()
        self._disabled = False

    def sample(self) -> None:
        if self._disabled:
            return
        now = self._clock()
        if self._samples and (now - self._samples[-1][0]) < self._sample_interval:
            return
        reading = self._reader()
        if reading is None:
            if not self._samples:
                self._disabled = True
            return
        rx_bytes, tx_bytes = reading
        if self._samples and (rx_bytes < self._samples[-1][1] or tx_bytes < self._samples[-1][2]):
            self._samples.clear()
        self._samples.append((now, rx_bytes, tx_bytes))
        cutoff = now - self._window_seconds
        while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()

    def upload_ceiling_bps(self) -> Optional[float]:
        self.sample()
        if len(self._samples) < 2:
            return None
        old_ts, old_rx, old_tx = self._samples[0]
        now_ts, now_rx, now_tx = self._samples[-1]
        span = now_ts - old_ts
        if span < self._min_span_seconds:
            return None
        nic_tx_bps = max(0.0, (now_tx - old_tx) / span)
        nic_rx_bps = max(0.0, (now_rx - old_rx) / span)
        return max(0.0, nic_tx_bps - nic_rx_bps * _NET_TX_ACK_RATIO)


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
    def __init__(
        self,
        max_logs: int = 500,
        redact: Callable[[str], str] = redact_diagnostic,
        *,
        net_tx_meter: Optional[NetTxRateMeter] = None,
        net_ceiling_active: Callable[[], bool] = has_active_real_upload_processes,
    ) -> None:
        self._subscribers: Set[SubscriberQueue] = set()
        self._logs: Deque[Dict[str, Any]] = deque(maxlen=max_logs)
        self._boot_id: str = secrets.token_hex(4)
        self._log_seq: int = 0
        self._redact = redact
        self._live_progress: Dict[int, Dict[str, Any]] = {}
        self._raw_upload_speeds: Dict[int, float] = {}
        self._net_tx_meter = net_tx_meter or NetTxRateMeter()
        self._net_ceiling_active = net_ceiling_active
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
        norm_phase = (phase or "downloading").strip().lower()
        # Keep the Linux NIC TX baseline warm so uploads are capped immediately from tick 1.
        self._net_tx_meter.sample()
        raw_speed = max(0.0, float(speed_bps))
        if norm_phase != "uploading":
            self._raw_upload_speeds.pop(item_id, None)
            payload: Dict[str, Any] = {
                "job_id": job_id,
                "item_id": item_id,
                "bytes_downloaded": int(bytes_downloaded),
                "bytes_total": int(bytes_total),
                "speed_bps": round(raw_speed, 1),
                "phase": norm_phase,
            }
            if estimated_total:
                payload["estimated_total"] = True
            self._live_progress[item_id] = payload
            self.broadcast("item_progress", payload)
            return

        self._raw_upload_speeds[item_id] = raw_speed
        payload = {
            "job_id": job_id,
            "item_id": item_id,
            "bytes_downloaded": int(bytes_downloaded),
            "bytes_total": int(bytes_total),
            "speed_bps": round(raw_speed, 1),
            "phase": norm_phase,
        }
        self._live_progress[item_id] = payload

        ceiling = self._net_tx_meter.upload_ceiling_bps() if self._net_ceiling_active() else None
        if ceiling is not None:
            uploading_ids = [
                iid
                for iid, entry in self._live_progress.items()
                if entry.get("job_id") == job_id and entry.get("phase") == "uploading"
            ]
            raw_total = sum(self._raw_upload_speeds.get(iid, 0.0) for iid in uploading_ids)
            scale = (ceiling / raw_total) if (raw_total > ceiling and raw_total > 0) else 1.0
            payload["speed_bps"] = round(raw_speed * scale, 1)
            self.broadcast("item_progress", payload)
            for other_id in uploading_ids:
                if other_id == item_id:
                    continue
                other_entry = self._live_progress.get(other_id)
                if other_entry is None:
                    continue
                target = round(self._raw_upload_speeds.get(other_id, 0.0) * scale, 1)
                prev = float(other_entry.get("speed_bps") or 0.0)
                if abs(target - prev) >= max(131072.0, prev * 0.1) or (scale == 1.0 and abs(target - prev) > 0.5):
                    updated = {**other_entry, "speed_bps": target}
                    self._live_progress[other_id] = updated
                    self.broadcast("item_progress", updated)
            return

        self.broadcast("item_progress", payload)

    def clear_item_progress(self, item_id: int) -> None:
        self._live_progress.pop(item_id, None)
        self._raw_upload_speeds.pop(item_id, None)

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
            self._raw_upload_speeds.pop(k, None)
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
