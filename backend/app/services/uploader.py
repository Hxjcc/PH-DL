"""Copy batches with rclone and verify remote contents before local cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..cancel import StopRequested
from ..config import RuntimeSettings
from .filename import is_valid_video_file
from .subprocesses import run_bounded_process

FileSnapshot = Tuple[int, int, int, int, int]
UploadProgressCallback = Callable[[str, int, int, float, str], None]  # (viewkey, bytes_sent, total, speed_bps, phase)


@dataclass
class UploadSessionCache:
    """Per-job cache avoiding repeated `rclone listremotes` and `rclone mkdir` calls."""

    remote_type: Optional[str] = None
    remote_type_checked: bool = False
    remote_types: Dict[str, Optional[str]] = field(default_factory=dict)
    created_remote_dirs: Set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def parse_rclone_json_line(line: str) -> Optional[Dict[str, Any]]:
    text = (line or "").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class UploadBatchResult:
    uploaded_viewkeys: List[str] = field(default_factory=list)
    failed_viewkeys: List[str] = field(default_factory=list)
    remote_dir: str = ""
    error: str = ""
    fatal_limit_error: str = ""
    verified_sources: Dict[str, FileSnapshot] = field(default_factory=dict)
    remote_paths: Dict[str, str] = field(default_factory=dict)


_GENERIC_RCLONE_EXACT = frozenset(
    {
        "fatal error received - not attempting retries",
        "not attempting retries",
    }
)
_GENERIC_RCLONE_PREFIXES = (
    "fatal error received",
    "cancelling sync due to fatal error",
)


def summarize_rclone_error(
    raw_lines: List[str],
    *,
    rc: int = 1,
    remote_type: Optional[str] = None,
    action: str = "copy",
) -> Tuple[str, str]:
    """Return ``(human_error, fatal_limit_error)`` from rclone stderr/JSON error lines.

    Strips generic trailing lines such as ``Fatal error received - not attempting retries`` so
    the underlying backend cause is surfaced, and flags remote-wide quota/upload-limit errors
    that should pause the remaining items in the current job.
    """
    cleaned = [str(line or "").strip() for line in raw_lines if str(line or "").strip()]
    joined_lower = "\n".join(cleaned).lower()
    rtype = (remote_type or "").strip().lower()

    if (
        "userratelimitexceeded" in joined_lower
        or "user rate limit exceeded" in joined_lower
        or "upload limit" in joined_lower
    ):
        msg = "已达到 Google Drive 每日 750GB 上传上限（请等待配额恢复或切换网盘后重试）"
        return msg, msg

    if (
        "storagequotaexceeded" in joined_lower
        or "storage quota has been exceeded" in joined_lower
        or "quota exceeded" in joined_lower
        or "insufficient storage" in joined_lower
        or "space is full" in joined_lower
        or "file_space_not_enough" in joined_lower
    ):
        label = "Google Drive" if rtype == "drive" else "远端网盘"
        msg = f"{label}存储空间已满，无法继续上传（本地文件已保留）"
        return msg, msg

    informative: List[str] = []
    for line in cleaned:
        low = line.lower()
        if low in _GENERIC_RCLONE_EXACT or any(low.startswith(p) for p in _GENERIC_RCLONE_PREFIXES):
            continue
        informative.append(line)

    if informative:
        chosen = informative[-1]
        if "last error was:" in chosen.lower():
            idx = chosen.lower().rfind("last error was:")
            tail = chosen[idx + len("last error was:") :].strip()
            if tail:
                chosen = tail
        if rtype == "drive" and rc == 7 and "rate limit" in chosen.lower():
            msg = "已达到 Google Drive 每日 750GB 上传上限（请等待配额恢复或切换网盘后重试）"
            return msg, msg
        return chosen, ""

    if rtype == "drive" and rc == 7:
        msg = "Google Drive 返回致命错误（通常为每日 750GB 上传上限或网盘空间已满），已保留本地文件"
        return msg, msg

    if cleaned:
        return cleaned[-1], ""
    return f"rclone {action} 返回 {rc}", ""


def classify_rclone_limit_kind(fatal_limit_error: str) -> str:
    """Classify a fatal limit message into ``'storage_full'``, ``'upload_limit'``, or ``''``."""
    text = (fatal_limit_error or "").strip().lower()
    if not text:
        return ""
    if (
        "存储空间已满" in text
        or "storagequotaexceeded" in text
        or "storage quota" in text
        or "insufficient storage" in text
        or "space is full" in text
        or "file_space_not_enough" in text
    ):
        return "storage_full"
    return "upload_limit"


def file_snapshot(path: Path) -> Optional[FileSnapshot]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        info = path.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def delete_verified_source(path: Path, snapshot: Optional[FileSnapshot]) -> bool:
    if snapshot is None or file_snapshot(path) != snapshot:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


async def _run_cmd(
    cmd: List[str],
    *,
    timeout: Optional[float] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_stderr_line: Optional[Callable[[str], None]] = None,
) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env.setdefault("GOGC", "50")
    env.setdefault("GOMEMLIMIT", "256MiB")

    def filter_line(text: str) -> Optional[str]:
        with contextlib.suppress(Exception):
            if on_stderr_line is not None:
                on_stderr_line(text)
        parsed = parse_rclone_json_line(text)
        if parsed is None:
            return text
        if isinstance(parsed.get("stats"), dict):
            return None
        if str(parsed.get("level") or "").lower() in {"error", "critical", "fatal", "warning"}:
            return str(parsed.get("msg") or "").strip()
        return None

    try:
        rc, out, err = await run_bounded_process(
            cmd, timeout=timeout, cancel_event=cancel_event, env=env,
            capture_stdout=len(cmd) < 2 or cmd[1] not in {"copy", "copyto", "mkdir"},
            filter_stderr=filter_line if on_stderr_line is not None else None,
        )
    except FileNotFoundError as err:
        return 127, "", f"找不到可执行文件: {err}"
    except Exception as err:
        return 1, "", f"{type(err).__name__}: {err}"

    if rc == 130:
        err = f"{err}\nrclone 执行已取消".strip()
    elif rc == 124:
        err = f"{err}\nrclone 执行超时（{timeout}s）".strip()
    return rc, out, err


def _parse_listremotes_long(output: str) -> Dict[str, str]:
    """Parse `rclone listremotes --long` output into {remote_name: backend_type}."""
    mapping: Dict[str, str] = {}
    for line in (output or "").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        name = parts[0].rstrip(":")
        if not name or name.lower() == "name" or set(name) == {"-"}:
            continue
        rtype = parts[1].strip().lower() if len(parts) >= 2 else ""
        mapping[name] = rtype
    return mapping


_BLOCKED_EXTRA_FLAGS = frozenset(
    {
        "--config",
        "--password-command",
        "--log-file",
        "--files-from",
        "--files-from-raw",
        "--include-from",
        "--exclude-from",
        "--filter-from",
        "--rc",
        "--rc-addr",
        "--rc-user",
        "--rc-pass",
        "--rc-no-auth",
        "--dry-run",
        "--backup-dir",
        "--delete-after",
        "--delete-before",
        "--delete-during",
        "--delete-excluded",
        "--max-delete",
        "--cache-dir",
        "--temp-dir",
    }
)


def parse_safe_rclone_extra_flags(raw: str, *, blocked: frozenset[str] = frozenset()) -> List[str]:
    """Parse user-provided extra rclone tuning flags while filtering unsafe/positional arguments."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        return []
    safe: List[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if not tok.startswith("--") or tok == "--":
            idx += 1
            continue
        flag_name = tok.split("=", 1)[0].strip().lower()
        if flag_name in _BLOCKED_EXTRA_FLAGS or flag_name in blocked or flag_name.startswith(("--rc-", "--dump")):
            # Skip blocked flag and its separate value token if present
            if "=" not in tok and idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"):
                idx += 2
            else:
                idx += 1
            continue
        safe.append(tok)
        if "=" not in tok and idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"):
            safe.append(tokens[idx + 1])
            idx += 2
        else:
            idx += 1
    return safe


async def list_rclone_remotes(
    settings: RuntimeSettings,
    cancel_event: Optional[asyncio.Event] = None,
) -> List[Dict[str, str]]:
    """List configured rclone remotes with their backend types."""
    rclone = (settings.rclone_bin or "rclone").strip()
    timeout = min(settings.rclone_timeout_seconds, 30)
    rc, out, _ = await _run_cmd(
        [rclone, "listremotes", "--long"],
        timeout=timeout,
        cancel_event=cancel_event,
    )
    if rc == 0 and out.strip():
        mapping = _parse_listremotes_long(out)
        return [{"name": name, "type": rtype} for name, rtype in mapping.items()]
    rc2, out2, _ = await _run_cmd(
        [rclone, "listremotes"],
        timeout=timeout,
        cancel_event=cancel_event,
    )
    if rc2 == 0 and out2.strip():
        return [
            {"name": line.strip().rstrip(":"), "type": ""}
            for line in out2.splitlines()
            if line.strip().rstrip(":")
        ]
    return []


async def detect_remote_type(
    settings: RuntimeSettings,
    cancel_event: Optional[asyncio.Event] = None,
) -> Optional[str]:
    rclone = (settings.rclone_bin or "rclone").strip()
    remote = (settings.rclone_remote or "").strip().rstrip(":")
    if not remote:
        return None
    rc, out, _ = await _run_cmd(
        [rclone, "listremotes", "--long"],
        timeout=min(settings.rclone_timeout_seconds, 60),
        cancel_event=cancel_event,
    )
    if rc == 0 and out.strip():
        remotes = _parse_listremotes_long(out)
        if remote in remotes and remotes[remote]:
            return remotes[remote]
    return "pikpak" if remote.lower() == "pikpak" else None


def build_remote_dir(settings: RuntimeSettings, uploader_folder: str, site: str = "pornhub") -> str:
    remote = (settings.rclone_remote or "pikpak").strip().rstrip(":")
    if not _valid_remote_name(remote):
        raise ValueError("rclone 远端名称无效，请使用 rclone.conf 中的名称")
    norm_site = (site or "pornhub").strip().lower()
    if norm_site in {"xhamster", "xh"}:
        base = (getattr(settings, "rclone_xh_remote_base_dir", "xHamster") or "xHamster").strip().strip("/")
    else:
        base = (settings.rclone_remote_base_dir or "Pornhub").strip().strip("/")
    sub = (uploader_folder or "Ungrouped").strip().strip("/")
    if not _valid_remote_path(base) or not _valid_remote_path(sub):
        raise ValueError("rclone 目标目录含不受支持的路径成分")
    return f"{remote}:{base}/{sub}" if base else f"{remote}:{sub}"


def _valid_remote_name(remote: str) -> bool:
    return bool(re.fullmatch(r"[\w.][\w. -]*", remote)) and remote not in {".", ".."}


def _valid_remote_path(path: str) -> bool:
    return not re.search(r"[\\:\x00-\x1f\x7f]", path) and all(part not in {".", ".."} for part in path.split("/"))


def build_rclone_copy_opts(
    settings: RuntimeSettings,
    *,
    remote_type: Optional[str] = None,
    stream_stats: bool = False,
    force: bool = False,
) -> List[str]:
    raw_level = str(settings.rclone_log_level or "ERROR").strip().upper()
    # Rclone reports completed transfers at INFO, including when progress is disabled.
    effective_log_level = raw_level if raw_level in {"DEBUG", "INFO"} else "INFO"
    opts = [
        "--no-traverse",
        f"--transfers={settings.rclone_transfers}",
        f"--checkers={settings.rclone_checkers}",
    ]
    if force:
        opts.append("--ignore-times")
    elif settings.rclone_ignore_size:
        opts.append("--ignore-size")
    else:
        # Skip re-uploading files already present on the remote with matching exact size
        # (common during compensation retries after a cancelled run), while still
        # overwriting partial or different-quality files whose byte size differs.
        opts.append("--size-only")
    if settings.rclone_ignore_checksum:
        opts.append("--ignore-checksum")

    effective_type = (
        remote_type.strip().lower()
        if isinstance(remote_type, str) and remote_type.strip()
        else ("pikpak" if (settings.rclone_remote or "").strip().rstrip(":").lower() == "pikpak" else "")
    )
    if effective_type == "pikpak":
        opts.extend(
            [
                f"--pikpak-upload-concurrency={settings.rclone_pikpak_upload_concurrency}",
                f"--pikpak-chunk-size={settings.rclone_pikpak_chunk_size}",
            ]
        )
    elif effective_type == "drive":
        drive_chunk = str(getattr(settings, "rclone_drive_chunk_size", "64Mi") or "").strip()
        if drive_chunk:
            opts.append(f"--drive-chunk-size={drive_chunk}")
        if getattr(settings, "rclone_drive_stop_on_upload_limit", True):
            opts.append("--drive-stop-on-upload-limit")

    if settings.rclone_tpslimit > 0:
        opts.append(f"--tpslimit={settings.rclone_tpslimit}")
        if settings.rclone_tpslimit_burst > 0:
            opts.append(f"--tpslimit-burst={settings.rclone_tpslimit_burst}")

    blocked = frozenset({"--log-level", "--use-json-log", "--log-format", "--stats", "--stats-log-level"})
    if force:
        blocked |= {
            "--size-only", "--ignore-size", "--ignore-existing", "--ignore-times",
            "--update", "--compare-dest", "--copy-dest",
        }
    extra_flags = parse_safe_rclone_extra_flags(getattr(settings, "rclone_extra_flags", ""), blocked=blocked)
    flag_names = {flag.split("=", 1)[0].lower() for flag in extra_flags if flag.startswith("--")}
    if "--buffer-size" not in flag_names:
        opts.append("--buffer-size=8Mi")
    if "--use-mmap" not in flag_names:
        opts.append("--use-mmap")
    opts.extend(extra_flags)
    opts.extend([f"--log-level={effective_log_level}", "--use-json-log"])
    opts.extend(["--stats=300ms", "--stats-log-level=NOTICE"] if stream_stats else ["--stats=0"])
    return opts


@dataclass(frozen=True)
class _RemoteVerification:
    ok: bool
    reason: str = ""
    size_only: bool = False
    hash_mismatch: bool = False


def _hash_file(path: Path, algorithm: str, stop: Optional[asyncio.Event]) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        while True:
            if stop is not None and stop.is_set():
                raise StopRequested
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


async def _verify_remote_file_detailed(
    rclone: str,
    remote_dir: str,
    download_dir: Path,
    local_path: Path,
    relative: str,
    snapshot: FileSnapshot,
    settings: RuntimeSettings,
    *,
    remote_relative: Optional[str] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> _RemoteVerification:
    """Verify remote file existence, exact size, and hash (optional download check)."""
    remote_rel = (remote_relative or relative).strip()
    rc, stdout, stderr = await _run_cmd(
        [
            rclone,
            "lsjson",
            "--stat",
            "--no-mimetype",
            "--no-modtime",
            f"{remote_dir}/{remote_rel}",
            "--hash",
            f"--log-level={settings.rclone_log_level}",
        ],
        timeout=min(settings.rclone_timeout_seconds, 60),
        cancel_event=cancel_event,
    )
    if rc != 0 or not stdout.strip():
        err_lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
        detail = err_lines[-1] if err_lines else f"lsjson exit={rc}"
        return _RemoteVerification(False, f"远端文件校验失败 ({detail})")
    try:
        meta = json.loads(stdout)
    except json.JSONDecodeError:
        return _RemoteVerification(False, "远端文件元数据解析失败")
    if not isinstance(meta, dict) or meta.get("IsDir") is not False:
        return _RemoteVerification(False, "远端目标不是有效文件")

    local_size = snapshot[2]
    remote_size = meta.get("Size")
    if type(remote_size) is not int or remote_size != local_size or local_size < settings.min_file_size_bytes:
        return _RemoteVerification(False, f"远端文件大小不匹配（远端 {remote_size} != 本地 {local_size}）")

    hashes = meta.get("Hashes")
    if isinstance(hashes, dict):
        hashes = {str(name).lower().replace("-", ""): value for name, value in hashes.items()}
        supported = {"sha256": 64, "sha1": 40, "md5": 32}
        for name, width in supported.items():
            digest = hashes.get(name)
            if not isinstance(digest, str) or len(digest) != width or not re.fullmatch(r"[0-9a-fA-F]+", digest):
                continue

            try:
                if await asyncio.to_thread(_hash_file, local_path, name, cancel_event) == digest.lower():
                    return _RemoteVerification(True)
                return _RemoteVerification(False, f"远端 {name.upper()} 校验和不匹配", hash_mismatch=True)
            except StopRequested:
                return _RemoteVerification(False, "已取消校验")
            except OSError:
                return _RemoteVerification(False, "读取本地文件计算校验和失败")
            except ValueError:
                # The local hashing provider may disable a specific algorithm.
                continue

    # Per rclone docs, some backends (notably PikPak) omit MD5/SHA hashes on uploaded
    # files because they use proprietary piece hashes (e.g. GCID) during upload.
    # Only fall back to `rclone check --download` (which re-downloads the entire file)
    # when explicitly opted in via `rclone_verify_download`.
    if not getattr(settings, "rclone_verify_download", False):
        return _RemoteVerification(True, size_only=True)

    if remote_rel == relative:
        with _upload_file_list([relative]) as check_list:
            rc, matching, stderr_chk = await _run_cmd(
                [rclone, "check", str(download_dir), remote_dir,
                 "--one-way", "--download", "--match", "-", "--files-from-raw", str(check_list),
                 f"--log-level={settings.rclone_log_level}"],
                timeout=settings.rclone_timeout_seconds,
                cancel_event=cancel_event,
            )
        if rc == 0 and relative in matching.splitlines():
            return _RemoteVerification(True)
        err_lines = [line.strip() for line in (stderr_chk or "").splitlines() if line.strip()]
        detail = err_lines[-1] if err_lines else f"check exit={rc}"
        return _RemoteVerification(False, f"远端回下校验未通过 ({detail})")

    with tempfile.TemporaryDirectory(prefix="phdl_verify_") as tmp_dir:
        verify_copy = Path(tmp_dir) / Path(remote_rel).name
        rc_dl, _, stderr_dl = await _run_cmd(
            [
                rclone,
                "copyto",
                f"{remote_dir}/{remote_rel}",
                str(verify_copy),
                f"--log-level={settings.rclone_log_level}",
            ],
            timeout=settings.rclone_timeout_seconds,
            cancel_event=cancel_event,
        )
        if rc_dl != 0 or not verify_copy.is_file():
            err_lines = [line.strip() for line in (stderr_dl or "").splitlines() if line.strip()]
            detail = err_lines[-1] if err_lines else f"copyto exit={rc_dl}"
            return _RemoteVerification(False, f"远端回下校验未通过 ({detail})")

        def _same_sha256() -> bool:
            return _hash_file(local_path, "sha256", cancel_event) == _hash_file(verify_copy, "sha256", cancel_event)

        try:
            if await asyncio.to_thread(_same_sha256):
                return _RemoteVerification(True)
        except StopRequested:
            return _RemoteVerification(False, "已取消校验")
        except OSError:
            return _RemoteVerification(False, "读取文件计算校验和失败")
        return _RemoteVerification(False, "远端回下校验内容不一致")


async def verify_remote_file(
    rclone: str,
    remote_dir: str,
    download_dir: Path,
    local_path: Path,
    relative: str,
    snapshot: FileSnapshot,
    settings: RuntimeSettings,
    *,
    cancel_event: Optional[asyncio.Event] = None,
) -> bool:
    """Require matching remote existence, exact size, and hash when available."""
    verification = await _verify_remote_file_detailed(
        rclone,
        remote_dir,
        download_dir,
        local_path,
        relative,
        snapshot,
        settings,
        cancel_event=cancel_event,
    )
    return verification.ok


@contextlib.contextmanager
def _upload_file_list(files: List[str]):
    path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", delete=False,
                                         prefix="phdl_upload_", suffix=".txt") as stream:
            path = Path(stream.name)
            for relative in files:
                stream.write(relative + "\n")
        yield path
    finally:
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)


async def _probe_single_remote(
    rclone: str,
    remote: str,
    base: str,
    remotes_map: Dict[str, str],
    timeout: float,
) -> Tuple[bool, str, str]:
    """Check one remote's existence and directory accessibility.

    Returns ``(ok, detail, detected_type)``.
    """
    if not _valid_remote_name(remote) or not _valid_remote_path(base):
        return False, "rclone 远端名称或目标目录无效", ""
    if remote not in remotes_map:
        available = ", ".join(remotes_map.keys()) or "无"
        return False, f"未在 rclone.conf 找到远端 '{remote}'（当前可用: {available}）", ""

    rtype = remotes_map.get(remote) or ""
    target = f"{remote}:{base}" if base else f"{remote}:"
    rc3, _, err3 = await _run_cmd(
        [rclone, "lsd", target, "--max-depth", "1"],
        timeout=timeout,
    )
    if rc3 == 0:
        return True, target, rtype

    err3_text = (err3 or "").strip()
    if base and ("directory not found" in err3_text.lower() or "not found" in err3_text.lower()):
        rc_root, _, err_root = await _run_cmd(
            [rclone, "lsd", f"{remote}:", "--max-depth", "1"],
            timeout=timeout,
        )
        if rc_root == 0:
            return True, f"{remote}:（{base} 目录将在首次上传时自动创建）", rtype
        err3_text = (err_root or err3_text).strip()

    return False, f"远端目录 '{target}' 访问失败: {err3_text or f'exit={rc3}'}", rtype


async def test_rclone_remotes(
    settings: RuntimeSettings,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Test primary remote and any configured backup remotes, returning per-remote statuses."""
    rclone = (settings.rclone_bin or "rclone").strip()
    short_timeout = min(settings.rclone_timeout_seconds, 60)
    rc, out, err = await _run_cmd([rclone, "version"], timeout=short_timeout)
    if rc != 0:
        return False, f"rclone 不可用: {err.strip() or f'exit={rc}'}", []

    chain = settings.rclone_remote_chain()
    if not chain:
        return False, "未配置 rclone remote 名称", []

    rc2, out2, err2 = await _run_cmd([rclone, "listremotes", "--long"], timeout=short_timeout)
    remotes_map: Dict[str, str] = {}
    if rc2 == 0:
        remotes_map = _parse_listremotes_long(out2)
    else:
        rc2_fb, out2_fb, err2_fb = await _run_cmd([rclone, "listremotes"], timeout=short_timeout)
        if rc2_fb != 0:
            return False, f"读取 rclone remotes 失败: {(err2_fb or err2).strip()}", []
        remotes_map = {line.strip().rstrip(":"): "" for line in out2_fb.splitlines() if line.strip()}

    base = (settings.rclone_remote_base_dir or "").strip().strip("/")
    ver_line = out.splitlines()[0].strip() if out.strip() else "rclone ok"

    probes = await asyncio.gather(
        *[_probe_single_remote(rclone, r, base, remotes_map, short_timeout) for r in chain]
    )
    results: List[Dict[str, Any]] = []
    for remote_name, (ok_i, detail_i, rtype_i) in zip(chain, probes):
        results.append(
            {
                "remote": remote_name,
                "type": rtype_i,
                "ok": ok_i,
                "message": detail_i,
            }
        )

    if len(chain) == 1:
        r0 = results[0]
        if not r0["ok"]:
            return False, str(r0["message"]), results
        type_tag = f", type={r0['type']}" if r0["type"] else ""
        return True, f"连接成功 ({ver_line}{type_tag} -> {r0['message']})", results

    ok_items = [r for r in results if r["ok"]]
    fail_items = [r for r in results if not r["ok"]]
    if not fail_items:
        labels = [
            f"{r['remote']}({r['type']})" if r["type"] else r["remote"]
            for r in ok_items
        ]
        return True, f"连接成功 ({ver_line}) · 全部 {len(chain)} 个远端可用: {' → '.join(labels)}", results

    fail_desc = "；".join(f"{r['remote']}: {r['message']}" for r in fail_items)
    return (
        False,
        f"部分远端连接失败（可用 {len(ok_items)}/{len(chain)}）：{fail_desc}",
        results,
    )


async def test_rclone_connection(settings: RuntimeSettings) -> Tuple[bool, str]:
    ok, msg, _ = await test_rclone_remotes(settings)
    return ok, msg


async def upload_file_batch(
    download_dir: Path,
    uploader_folder: str,
    files_by_viewkey: Dict[str, Path],
    settings: RuntimeSettings,
    cancel_event: Optional[asyncio.Event] = None,
    *,
    session_cache: Optional[UploadSessionCache] = None,
    on_progress: Optional[UploadProgressCallback] = None,
    remote_names_by_viewkey: Optional[Dict[str, str]] = None,
    site: str = "pornhub",
) -> UploadBatchResult:
    """Copy and verify the batch. The caller records success before deleting any source."""
    remote_dir = build_remote_dir(settings, uploader_folder, site=site)
    result = UploadBatchResult(remote_dir=remote_dir)
    if not files_by_viewkey:
        return result

    root = download_dir.resolve()
    entries: Dict[str, Tuple[Path, str, str, FileSnapshot]] = {}
    for vk, path in files_by_viewkey.items():
        try:
            relative = path.resolve(strict=True).relative_to(root).as_posix()
        except (OSError, ValueError):
            result.failed_viewkeys.append(vk)
            continue
        snapshot = file_snapshot(path)
        if (
            snapshot is None or "\n" in relative or "\r" in relative
            or not await asyncio.to_thread(is_valid_video_file, path, settings.min_file_size_bytes, snapshot[2])
        ):
            result.failed_viewkeys.append(vk)
            continue
        custom_name = str((remote_names_by_viewkey or {}).get(vk) or "").strip()
        if custom_name and "/" not in custom_name and "\\" not in custom_name and "\n" not in custom_name and "\r" not in custom_name:
            parent_rel = Path(relative).parent.as_posix()
            remote_rel = f"{parent_rel}/{custom_name}" if parent_rel not in {"", "."} else custom_name
        else:
            remote_rel = relative
        entries[vk] = (path, relative, remote_rel, snapshot)
    if not entries:
        result.error = "没有可上传且完整的本地文件"
        return result

    rclone = (settings.rclone_bin or "rclone").strip()
    remote_key = (settings.rclone_remote or "").strip().rstrip(":").lower()
    if session_cache is not None:
        async with session_cache._lock:
            if remote_key in session_cache.remote_types:
                remote_type = session_cache.remote_types[remote_key]
            else:
                remote_type = await detect_remote_type(settings, cancel_event=cancel_event)
                if cancel_event is None or not cancel_event.is_set():
                    session_cache.remote_types[remote_key] = remote_type
                    session_cache.remote_type = remote_type
                    session_cache.remote_type_checked = True
    else:
        remote_type = await detect_remote_type(settings, cancel_event=cancel_event)

    need_mkdir = True
    if session_cache is not None:
        async with session_cache._lock:
            if remote_dir in session_cache.created_remote_dirs:
                need_mkdir = False
            else:
                rc_mk, _, err_mk = await _run_cmd(
                    [rclone, "mkdir", remote_dir, f"--log-level={settings.rclone_log_level}"],
                    timeout=min(settings.rclone_timeout_seconds, 60),
                    cancel_event=cancel_event,
                )
                if rc_mk != 0:
                    result.failed_viewkeys.extend(entries)
                    err_msg, fatal_msg = summarize_rclone_error(
                        err_mk.splitlines(),
                        rc=rc_mk,
                        remote_type=remote_type,
                        action="mkdir",
                    )
                    result.error = err_msg or f"rclone mkdir 失败 (code={rc_mk})"
                    if fatal_msg:
                        result.fatal_limit_error = fatal_msg
                    return result
                session_cache.created_remote_dirs.add(remote_dir)
                need_mkdir = False

    if need_mkdir:
        rc_mk, _, err_mk = await _run_cmd(
            [rclone, "mkdir", remote_dir, f"--log-level={settings.rclone_log_level}"],
            timeout=min(settings.rclone_timeout_seconds, 60),
            cancel_event=cancel_event,
        )
        if rc_mk != 0:
            result.failed_viewkeys.extend(entries)
            err_msg, fatal_msg = summarize_rclone_error(
                err_mk.splitlines(),
                rc=rc_mk,
                remote_type=remote_type,
                action="mkdir",
            )
            result.error = err_msg or f"rclone mkdir 失败 (code={rc_mk})"
            if fatal_msg:
                result.fatal_limit_error = fatal_msg
            return result

    def _make_stderr_handler(
        subset: Dict[str, Tuple[Path, str, str, FileSnapshot]],
        error_sink: deque[str],
        transferred: Set[str],
    ) -> Callable[[str], None]:
        by_relative: Dict[str, Tuple[str, int]] = {}
        by_name: Dict[str, Tuple[str, int]] = {}
        for vk_item, (_, loc_rel, rem_rel, snap) in subset.items():
            by_relative[loc_rel] = (vk_item, snap[2])
            by_relative[rem_rel] = (vk_item, snap[2])
            by_name[Path(loc_rel).name] = (vk_item, snap[2])
            by_name[Path(rem_rel).name] = (vk_item, snap[2])
        sole_entry: Optional[Tuple[str, int]] = (
            next(((vk_item, snap[2]) for vk_item, (_, _, _, snap) in subset.items()), None)
            if len(subset) == 1
            else None
        )

        def _on_stderr_line(line: str) -> None:
            parsed = parse_rclone_json_line(line)
            if parsed is None:
                text = (line or "").strip()
                if text:
                    error_sink.append(text[:4096])
                return
            lvl = str(parsed.get("level") or "").lower()
            message = str(parsed.get("msg") or "").strip()
            if lvl == "info" and message.lower().startswith(("copied (", "multi-thread copied (")):
                name = str(parsed.get("object") or "")
                matched = by_relative.get(name) or by_relative.get(name.replace("\\", "/"))
                if matched is None and sole_entry is not None:
                    matched = by_name.get(name) or by_name.get(Path(name).name)
                if matched is not None and parsed.get("size", matched[1]) == matched[1]:
                    transferred.add(matched[0])
            if lvl in {"error", "critical", "fatal", "warning"}:
                err_field = str(parsed.get("error") or "").strip()
                if message:
                    error_sink.append(message[:4096])
                if err_field and err_field not in message:
                    error_sink.append(err_field[:4096])
            if on_progress is None:
                return
            stats = parsed.get("stats")
            if not isinstance(stats, dict):
                return
            transferring = stats.get("transferring")
            if isinstance(transferring, list) and transferring:
                fallback_speed = float(stats.get("speed") or 0.0)
                for item in transferring:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    matched = by_relative.get(name) or by_name.get(Path(name).name) or sole_entry
                    if matched is None:
                        continue
                    vk_m, local_total = matched
                    raw_total = item.get("size")
                    total = int(raw_total) if isinstance(raw_total, (int, float)) and raw_total > 0 else local_total
                    raw_bytes = item.get("bytes")
                    sent = int(raw_bytes) if isinstance(raw_bytes, (int, float)) and raw_bytes >= 0 else 0
                    if total > 0:
                        sent = min(sent, total)
                    raw_speed = item.get("speed")
                    if isinstance(raw_speed, (int, float)) and raw_speed > 0:
                        speed = float(raw_speed)
                    else:
                        avg_speed = item.get("speedAvg")
                        speed = (
                            float(avg_speed)
                            if isinstance(avg_speed, (int, float)) and avg_speed > 0
                            else max(0.0, fallback_speed)
                        )
                    on_progress(vk_m, sent, total, speed, "uploading")
            elif sole_entry is not None:
                vk_m, local_total = sole_entry
                raw_bytes = stats.get("bytes")
                if isinstance(raw_bytes, (int, float)) and raw_bytes > 0:
                    sent = min(int(raw_bytes), local_total) if local_total > 0 else int(raw_bytes)
                    speed = float(stats.get("speed") or 0.0)
                    on_progress(vk_m, sent, local_total, max(0.0, speed), "uploading")

        return _on_stderr_line

    async def _run_copy(
        cmd: List[str], subset: Dict[str, Tuple[Path, str, str, FileSnapshot]],
    ) -> Tuple[int, str, str, Set[str]]:
        errors: deque[str] = deque(maxlen=32)
        transferred: Set[str] = set()
        handler = _make_stderr_handler(subset, errors, transferred)
        rc, _, stderr = await _run_cmd(
            cmd, timeout=settings.rclone_timeout_seconds, cancel_event=cancel_event, on_stderr_line=handler,
        )
        for line in stderr.splitlines():
            if parse_rclone_json_line(line) is not None:
                handler(line)
        if rc == 0:
            return rc, "", "", transferred
        error, fatal = summarize_rclone_error(
            [*errors, *stderr.splitlines()], rc=rc, remote_type=remote_type, action=cmd[1],
        )
        return rc, error, fatal, transferred

    same_name_entries = {vk: val for vk, val in entries.items() if val[1] == val[2]}
    renamed_entries = {vk: val for vk, val in entries.items() if val[1] != val[2]}
    verification_entries: Dict[str, Tuple[Path, str, str, FileSnapshot]] = {}
    confirmed_transfers: Set[str] = set()
    partial_batch = False
    copy_opts = build_rclone_copy_opts(settings, remote_type=remote_type, stream_stats=on_progress is not None)

    async def _copyto(
        vk: str, entry: Tuple[Path, str, str, FileSnapshot], *, force: bool = False,
    ) -> Tuple[int, str, str, Set[str]]:
        if cancel_event is not None and cancel_event.is_set():
            return 130, "rclone 执行已取消", "", set()
        local_path, _, remote_rel, _ = entry
        opts = build_rclone_copy_opts(
            settings, remote_type=remote_type, stream_stats=on_progress is not None, force=True,
        ) if force else copy_opts
        return await _run_copy(
            [rclone, "copyto", str(local_path), f"{remote_dir}/{remote_rel}", *opts], {vk: entry},
        )

    if same_name_entries:
        with _upload_file_list([entry[1] for entry in same_name_entries.values()]) as list_file:
            cmd = [
                rclone,
                "copy",
                str(root),
                remote_dir,
                *copy_opts,
                "--files-from-raw",
                str(list_file),
            ]
            rc, error, fatal, transferred = await _run_copy(cmd, same_name_entries)
        if rc != 0:
            result.error = error
            result.fatal_limit_error = fatal
        partial_batch = rc not in {0, 130} and len(same_name_entries) > 1 and not (cancel_event and cancel_event.is_set())
        if rc == 0 or partial_batch:
            verification_entries.update(same_name_entries)
            confirmed_transfers.update(transferred)
        else:
            result.failed_viewkeys.extend(same_name_entries)

    if renamed_entries:
        transfer_sem = asyncio.Semaphore(max(1, int(settings.rclone_transfers)))

        async def _copyto_one(vk: str, entry: Tuple[Path, str, str, FileSnapshot]):
            async with transfer_sem:
                return await _copyto(vk, entry)

        copyto_results = await asyncio.gather(
            *(_copyto_one(vk, entry) for vk, entry in renamed_entries.items())
        )
        for (vk, entry), (rc, error, fatal, transferred) in zip(renamed_entries.items(), copyto_results, strict=True):
            if rc == 0:
                verification_entries[vk] = entry
                confirmed_transfers.update(transferred)
            else:
                result.failed_viewkeys.append(vk)
                result.error = result.error or error
                result.fatal_limit_error = result.fatal_limit_error or fatal

    verify_error = ""
    for vk, entry in verification_entries.items():
        path, relative, remote_rel, snapshot = entry
        if cancel_event and cancel_event.is_set():
            result.failed_viewkeys.append(vk)
            continue
        if on_progress is not None:
            with contextlib.suppress(Exception):
                on_progress(vk, snapshot[2], snapshot[2], 0.0, "verifying")
        if file_snapshot(path) != snapshot:
            result.failed_viewkeys.append(vk)
            verify_error = verify_error or "本地文件在上传期间发生变化，已保留本地文件"
            continue
        async def _verify() -> _RemoteVerification:
            return await _verify_remote_file_detailed(
                rclone, remote_dir, root, path, relative, snapshot, settings,
                remote_relative=remote_rel, cancel_event=cancel_event,
            )

        verification = await _verify()
        transferred = vk in confirmed_transfers
        unconfirmed_skip = verification.size_only and not transferred and not (partial_batch and vk in same_name_entries)
        if (verification.hash_mismatch or unconfirmed_skip) and file_snapshot(path) == snapshot:
            rc, error, fatal, recopy_transfers = await _copyto(vk, entry, force=True)
            result.fatal_limit_error = result.fatal_limit_error or fatal
            if rc == 0:
                transferred = vk in recopy_transfers
                verification = await _verify()
            else:
                verification = _RemoteVerification(False, error or verification.reason)
        verified = verification.ok and (not verification.size_only or transferred)
        if verified and file_snapshot(path) == snapshot and not (cancel_event and cancel_event.is_set()):
            result.uploaded_viewkeys.append(vk)
            result.verified_sources[vk] = snapshot
            result.remote_paths[vk] = f"{remote_dir}/{remote_rel}"
        else:
            result.failed_viewkeys.append(vk)
            verify_error = verify_error or verification.reason or (
                "远端仅通过大小校验，未确认本次传输" if verification.size_only and not transferred else ""
            )
    if result.failed_viewkeys and not result.error:
        result.error = verify_error or "无法确认远端文件与本地内容一致，已保留本地文件"

    return result
