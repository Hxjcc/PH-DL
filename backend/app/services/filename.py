"""Filename sanitization and video binary integrity validation."""

from __future__ import annotations

from datetime import date as _date
import os
import re
from pathlib import Path
from typing import Optional

_MP4_LIKE_EXTS = {".mp4", ".m4v", ".mov"}
_TS_LIKE_EXTS = {".ts", ".mts"}
_TEMP_WRAPPER_EXTS = {".tmp", ".part"}
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$")
_US_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
_CDN_VIDEO_DATE_RE = re.compile(r"/videos/(\d{4})(\d{2})/(\d{2})/")
_DATE_PREFIX_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[_\s-]|\[\d{4}-\d{2}-\d{2}\])")


def _effective_video_suffix(path: Path) -> str:
    suffixes = [s.lower() for s in path.suffixes]
    while suffixes and suffixes[-1] in _TEMP_WRAPPER_EXTS:
        suffixes.pop()
    return suffixes[-1] if suffixes else ""


def sanitize_filename(name: str, max_bytes: int = 200) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", name or "")
    name_part, ext_part = os.path.splitext(cleaned)
    ext_bytes = len(ext_part.encode("utf-8"))

    out_chars = []
    used = 0
    for ch in name_part:
        ch_len = len(ch.encode("utf-8"))
        if used + ch_len + ext_bytes > max_bytes:
            break
        out_chars.append(ch)
        used += ch_len

    final_base = "".join(out_chars).strip().rstrip(" .")
    if not final_base:
        return f"downloaded_video{ext_part or '.mp4'}"
    return final_base + ext_part


def video_filename_base(title: str, viewkey: str) -> str:
    safe_title = sanitize_filename(title or viewkey, max_bytes=180)
    return f"{safe_title} [{viewkey}]"


def _valid_date_str(year: int, month: int, day: int) -> str:
    if not (1990 <= year <= 2100):
        return ""
    try:
        return _date(year, month, day).isoformat()
    except ValueError:
        return ""


def normalize_published_date(raw: Optional[str]) -> str:
    """Normalize ISO dates/timestamps or MM/DD/YYYY strings into ``YYYY-MM-DD``."""
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _ISO_DATE_RE.match(text)
    if m:
        return _valid_date_str(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m_us = _US_DATE_RE.match(text)
    if m_us:
        return _valid_date_str(int(m_us.group(3)), int(m_us.group(1)), int(m_us.group(2)))
    return ""


def extract_date_from_cdn_url(text: Optional[str]) -> str:
    """Extract upload calendar date from PornHub CDN paths like ``/videos/202610/05/``."""
    raw = str(text or "")
    if "/videos/" not in raw:
        return ""
    m = _CDN_VIDEO_DATE_RE.search(raw)
    if not m:
        return ""
    return _valid_date_str(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _split_remote_path(remote_path: str) -> tuple[str, str]:
    raw = str(remote_path or "").strip()
    if "/" in raw:
        prefix, name = raw.rsplit("/", 1)
        return f"{prefix}/", name
    if ":" in raw:
        prefix, name = raw.rsplit(":", 1)
        return f"{prefix}:", name
    return "", raw


def has_date_prefix(filename_or_path: Optional[str]) -> bool:
    _, name = _split_remote_path(str(filename_or_path or ""))
    return bool(name and _DATE_PREFIX_RE.match(name))


def format_remote_video_filename(local_filename: str, published_at: Optional[str], max_bytes: int = 240) -> str:
    """Prefix a remote filename with ``YYYY-MM-DD_`` idempotently while keeping byte length safe."""
    name = str(local_filename or "").strip()
    date_str = normalize_published_date(published_at)
    if not name or not date_str or has_date_prefix(name):
        return name

    prefix = f"{date_str}_"
    candidate = prefix + name
    if len(candidate.encode("utf-8")) <= max_bytes:
        return candidate

    stem, ext = os.path.splitext(name)
    suffix_tag = ""
    if stem.endswith("]") and " [" in stem:
        title_part, bracket_part = stem.rsplit(" [", 1)
        suffix_tag = f" [{bracket_part}"
    else:
        title_part = stem

    reserved = len((prefix + suffix_tag + ext).encode("utf-8"))
    budget = max(16, max_bytes - reserved)
    kept: list[str] = []
    used = 0
    for ch in title_part:
        ch_len = len(ch.encode("utf-8"))
        if used + ch_len > budget:
            break
        kept.append(ch)
        used += ch_len
    trimmed_title = "".join(kept).strip().rstrip(" .")
    if trimmed_title:
        return f"{prefix}{trimmed_title}{suffix_tag}{ext}"
    return f"{prefix}{suffix_tag.strip() or 'video'}{ext}"


def normalized_quality(quality: str) -> str:
    match = re.fullmatch(r"(\d+)p?", (quality or "").strip(), re.IGNORECASE)
    return match.group(1) if match and int(match.group(1)) > 0 else ""


def _looks_like_mp4(path: Path) -> bool:
    """Check top-level box boundaries, rather than accepting a partial ftyp header."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            offset = 0
            required = {b"ftyp", b"moov", b"mdat"}
            while offset < size:
                f.seek(offset)
                header = f.read(8)
                if len(header) != 8:
                    return False
                kind = header[4:]
                box_size = int.from_bytes(header[:4], "big")
                header_size = 8
                if box_size == 1:
                    extended = f.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if kind == b"uuid":
                    header_size += 16
                if box_size < header_size or offset + box_size > size:
                    return False
                payload_size = box_size - header_size
                if kind == b"ftyp" and payload_size >= 8:
                    required.discard(kind)
                elif kind in {b"moov", b"mdat"} and payload_size > 0:
                    required.discard(kind)
                offset += box_size
            return not required
    except OSError:
        return False


def _looks_like_ts(path: Path) -> bool:
    packet_size = 188
    try:
        with open(path, "rb") as f:
            probe = f.read(packet_size * 6)
    except OSError:
        return False
    if len(probe) < packet_size * 3:
        return False
    max_packets = min(5, len(probe) // packet_size)
    for offset in range(min(packet_size, len(probe))):
        sync_count = 0
        for idx in range(max_packets):
            pos = offset + idx * packet_size
            if pos >= len(probe) or probe[pos] != 0x47:
                break
            sync_count += 1
        if sync_count >= 3:
            return True
    return False


def is_valid_video_file(
    path: Path, min_size_bytes: int = 10240, expected_size: Optional[int] = None,
) -> bool:
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size < max(1, int(min_size_bytes)) or (expected_size is not None and size != expected_size):
            return False
    except OSError:
        return False

    suffix = _effective_video_suffix(path)
    if suffix in _MP4_LIKE_EXTS:
        return _looks_like_mp4(path)
    if suffix in _TS_LIKE_EXTS:
        return _looks_like_ts(path)
    return True
