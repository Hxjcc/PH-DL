"""Copy batches with rclone and verify remote contents before local cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config import RuntimeSettings
from .filename import is_valid_video_file

FileSnapshot = Tuple[int, int, int, int, int]
UploadProgressCallback = Callable[[str, int, int, float, str], None]  # (viewkey, bytes_sent, total, speed_bps, phase)


@dataclass
class UploadSessionCache:
    """Per-job cache avoiding repeated `rclone listremotes` and `rclone mkdir` calls."""

    remote_type: Optional[str] = None
    remote_type_checked: bool = False
    created_remote_dirs: Set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def parse_rclone_json_line(line: str) -> Optional[Dict[str, Any]]:
    text = (line or "").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class UploadBatchResult:
    uploaded_viewkeys: List[str] = field(default_factory=list)
    failed_viewkeys: List[str] = field(default_factory=list)
    remote_dir: str = ""
    error: str = ""
    verified_sources: Dict[str, FileSnapshot] = field(default_factory=dict)
    remote_paths: Dict[str, str] = field(default_factory=dict)


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


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        return
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=3.0)


async def _run_cmd(
    cmd: List[str],
    *,
    timeout: Optional[float] = None,
    cancel_event: Optional[asyncio.Event] = None,
    on_stderr_line: Optional[Callable[[str], None]] = None,
) -> Tuple[int, str, str]:
    if cancel_event is not None and cancel_event.is_set():
        return 130, "", "rclone 执行已取消"

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as err:
        return 127, "", f"找不到可执行文件: {err}"
    except Exception as err:
        return 1, "", f"{type(err).__name__}: {err}"

    if on_stderr_line is not None:
        stderr_lines: List[str] = []

        async def _stream_communicate() -> Tuple[bytes, bytes]:
            async def _read_stdout() -> bytes:
                if proc.stdout is None:
                    return b""
                return await proc.stdout.read()

            async def _read_stderr() -> None:
                if proc.stderr is None:
                    return
                while True:
                    raw = await proc.stderr.readline()
                    if not raw:
                        break
                    text = raw.decode(errors="replace").rstrip("\r\n")
                    if text:
                        parsed = parse_rclone_json_line(text)
                        if parsed is not None:
                            if not isinstance(parsed.get("stats"), dict):
                                msg = str(parsed.get("msg") or "").strip()
                                lvl = str(parsed.get("level") or "").lower()
                                if msg and lvl in {"error", "critical", "fatal", "warning"}:
                                    stderr_lines.append(msg)
                                    if len(stderr_lines) > 50:
                                        stderr_lines.pop(0)
                        else:
                            stderr_lines.append(text)
                            if len(stderr_lines) > 50:
                                stderr_lines.pop(0)
                        with contextlib.suppress(Exception):
                            on_stderr_line(text)

            out_b, _ = await asyncio.gather(_read_stdout(), _read_stderr())
            await proc.wait()
            return out_b, ("\n".join(stderr_lines)).encode("utf-8", errors="replace")

        comm_coro = _stream_communicate()
    else:
        comm_coro = proc.communicate()

    if timeout is None and cancel_event is None:
        try:
            stdout_b, stderr_b = await comm_coro
        except asyncio.CancelledError:
            await _terminate_process(proc)
            raise
        return (
            int(proc.returncode or 0),
            (stdout_b or b"").decode(errors="replace"),
            (stderr_b or b"").decode(errors="replace"),
        )

    communicate_task = asyncio.create_task(comm_coro)
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    wait_tasks = {communicate_task}
    if cancel_task is not None:
        wait_tasks.add(cancel_task)

    try:
        done, _ = await asyncio.wait(
            wait_tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate_task in done:
            stdout_b, stderr_b = communicate_task.result()
            return (
                int(proc.returncode or 0),
                (stdout_b or b"").decode(errors="replace"),
                (stderr_b or b"").decode(errors="replace"),
            )

        cancelled = cancel_task is not None and cancel_task in done
        reason = "rclone 执行已取消" if cancelled else f"rclone 执行超时（{timeout}s）"
        await _terminate_process(proc)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(communicate_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            communicate_task.cancel()
            with contextlib.suppress(BaseException):
                await communicate_task
            stdout_b, stderr_b = b"", b""

        stderr_text = (stderr_b or b"").decode(errors="replace").strip()
        stderr_text = f"{stderr_text}\n{reason}".strip()
        return (
            130 if cancelled else 124,
            (stdout_b or b"").decode(errors="replace"),
            stderr_text,
        )
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        for task in wait_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*wait_tasks, return_exceptions=True)


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


def parse_safe_rclone_extra_flags(raw: str) -> List[str]:
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
        if flag_name in _BLOCKED_EXTRA_FLAGS or flag_name.startswith("--rc-"):
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


def build_remote_dir(settings: RuntimeSettings, uploader_folder: str) -> str:
    remote = (settings.rclone_remote or "pikpak").strip().rstrip(":")
    base = (settings.rclone_remote_base_dir or "Pornhub").strip().strip("/")
    sub = (uploader_folder or "Ungrouped").strip().strip("/")
    return f"{remote}:{base}/{sub}" if base else f"{remote}:{sub}"


def build_rclone_copy_opts(
    settings: RuntimeSettings,
    *,
    remote_type: Optional[str] = None,
    stream_stats: bool = False,
) -> List[str]:
    raw_level = str(settings.rclone_log_level or "ERROR").strip().upper()
    # rclone suppresses --stats-log-level=NOTICE when --log-level=ERROR, so streaming
    # progress must run at least at NOTICE (which still omits per-file INFO chatter).
    effective_log_level = (
        raw_level if (not stream_stats or raw_level in {"DEBUG", "INFO", "NOTICE"}) else "NOTICE"
    )
    opts = [
        "--no-traverse",
        f"--transfers={settings.rclone_transfers}",
        f"--checkers={settings.rclone_checkers}",
        f"--log-level={effective_log_level}",
    ]
    if stream_stats:
        opts.extend(["--use-json-log", "--stats=300ms", "--stats-log-level=NOTICE"])
    else:
        opts.append("--stats=0")
    if settings.rclone_ignore_size:
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

    extra_flags = parse_safe_rclone_extra_flags(getattr(settings, "rclone_extra_flags", ""))
    if extra_flags:
        opts.extend(extra_flags)
    return opts


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
) -> Tuple[bool, str]:
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
        return False, f"远端文件校验失败 ({detail})"
    try:
        meta = json.loads(stdout)
    except json.JSONDecodeError:
        return False, "远端文件元数据解析失败"
    if not isinstance(meta, dict) or meta.get("IsDir") is not False:
        return False, "远端目标不是有效文件"

    local_size = snapshot[2]
    remote_size = meta.get("Size")
    if type(remote_size) is not int or remote_size != local_size or local_size < settings.min_file_size_bytes:
        return False, f"远端文件大小不匹配（远端 {remote_size} != 本地 {local_size}）"

    hashes = meta.get("Hashes")
    if isinstance(hashes, dict):
        hashes = {str(name).lower().replace("-", ""): value for name, value in hashes.items()}
        supported = {"sha256": 64, "sha1": 40, "md5": 32}
        for name, width in supported.items():
            digest = hashes.get(name)
            if not isinstance(digest, str) or len(digest) != width:
                continue

            def hash_local() -> str:
                with local_path.open("rb") as stream:
                    return hashlib.file_digest(stream, name).hexdigest()

            try:
                if (await asyncio.to_thread(hash_local)).lower() == digest.lower():
                    return True, ""
                return False, f"远端 {name.upper()} 校验和不匹配"
            except OSError:
                return False, "读取本地文件计算校验和失败"
            except ValueError:
                # The local hashing provider may disable a specific algorithm.
                continue

    # Per rclone docs, some backends (notably PikPak) omit MD5/SHA hashes on uploaded
    # files because they use proprietary piece hashes (e.g. GCID) during upload.
    # Only fall back to `rclone check --download` (which re-downloads the entire file)
    # when explicitly opted in via `rclone_verify_download`.
    if not getattr(settings, "rclone_verify_download", False):
        return True, ""

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
            return True, ""
        err_lines = [line.strip() for line in (stderr_chk or "").splitlines() if line.strip()]
        detail = err_lines[-1] if err_lines else f"check exit={rc}"
        return False, f"远端回下校验未通过 ({detail})"

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
            return False, f"远端回下校验未通过 ({detail})"

        def _same_sha256() -> bool:
            with local_path.open("rb") as a, verify_copy.open("rb") as b:
                return hashlib.file_digest(a, "sha256").hexdigest() == hashlib.file_digest(b, "sha256").hexdigest()

        try:
            if await asyncio.to_thread(_same_sha256):
                return True, ""
        except OSError:
            return False, "读取文件计算校验和失败"
        return False, "远端回下校验内容不一致"


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
    ok, _ = await _verify_remote_file_detailed(
        rclone,
        remote_dir,
        download_dir,
        local_path,
        relative,
        snapshot,
        settings,
        cancel_event=cancel_event,
    )
    return ok


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


async def test_rclone_connection(settings: RuntimeSettings) -> Tuple[bool, str]:
    rclone = (settings.rclone_bin or "rclone").strip()
    short_timeout = min(settings.rclone_timeout_seconds, 60)
    rc, out, err = await _run_cmd([rclone, "version"], timeout=short_timeout)
    if rc != 0:
        return False, f"rclone 不可用: {err.strip() or f'exit={rc}'}"

    remote = (settings.rclone_remote or "").strip().rstrip(":")
    if not remote:
        return False, "未配置 rclone remote 名称"

    rc2, out2, err2 = await _run_cmd([rclone, "listremotes", "--long"], timeout=short_timeout)
    remotes_map: Dict[str, str] = {}
    if rc2 == 0:
        remotes_map = _parse_listremotes_long(out2)
    else:
        rc2_fb, out2_fb, err2_fb = await _run_cmd([rclone, "listremotes"], timeout=short_timeout)
        if rc2_fb != 0:
            return False, f"读取 rclone remotes 失败: {(err2_fb or err2).strip()}"
        remotes_map = {line.strip().rstrip(":"): "" for line in out2_fb.splitlines() if line.strip()}

    if remote not in remotes_map:
        available = ", ".join(remotes_map.keys()) or "无"
        return False, f"未在 rclone.conf 找到远端 '{remote}'（当前可用: {available}）"

    base = (settings.rclone_remote_base_dir or "").strip().strip("/")
    target = f"{remote}:{base}" if base else f"{remote}:"
    rc3, _, err3 = await _run_cmd(
        [rclone, "lsd", target, "--max-depth", "1"],
        timeout=short_timeout,
    )
    if rc3 != 0:
        return False, f"远端目录 '{target}' 访问失败: {err3.strip() or f'exit={rc3}'}"

    ver_line = out.splitlines()[0].strip() if out.strip() else "rclone ok"
    rtype = remotes_map.get(remote)
    type_tag = f", type={rtype}" if rtype else ""
    return True, f"连接成功 ({ver_line}{type_tag} -> {target})"


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
) -> UploadBatchResult:
    """Copy and verify the batch. The caller records success before deleting any source."""
    remote_dir = build_remote_dir(settings, uploader_folder)
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
    if session_cache is not None:
        async with session_cache._lock:
            if not session_cache.remote_type_checked:
                session_cache.remote_type = await detect_remote_type(settings, cancel_event=cancel_event)
                if cancel_event is None or not cancel_event.is_set():
                    session_cache.remote_type_checked = True
            remote_type = session_cache.remote_type
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
                    result.error = err_mk.strip() or f"rclone mkdir 失败 (code={rc_mk})"
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
            result.error = err_mk.strip() or f"rclone mkdir 失败 (code={rc_mk})"
            return result

    def _make_stderr_handler(
        subset: Dict[str, Tuple[Path, str, str, FileSnapshot]],
        error_sink: List[str],
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
                return
            lvl = str(parsed.get("level") or "").lower()
            if lvl in {"error", "critical", "fatal"}:
                msg = str(parsed.get("msg") or "").strip()
                if msg:
                    error_sink.append(msg)
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

    same_name_entries = {vk: val for vk, val in entries.items() if val[1] == val[2]}
    renamed_entries = {vk: val for vk, val in entries.items() if val[1] != val[2]}
    copied_entries: Dict[str, Tuple[Path, str, str, FileSnapshot]] = {}
    copy_opts = build_rclone_copy_opts(settings, remote_type=remote_type, stream_stats=on_progress is not None)

    if same_name_entries:
        reported_error_lines: List[str] = []
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
            run_kwargs: Dict[str, Any] = {
                "timeout": settings.rclone_timeout_seconds,
                "cancel_event": cancel_event,
            }
            if on_progress is not None:
                run_kwargs["on_stderr_line"] = _make_stderr_handler(same_name_entries, reported_error_lines)
            rc, _, stderr = await _run_cmd(cmd, **run_kwargs)
        if rc != 0:
            if reported_error_lines:
                result.error = result.error or reported_error_lines[-1]
            else:
                lines = [line for line in stderr.strip().splitlines() if line.strip()]
                result.error = result.error or (lines[-1] if lines else f"rclone copy 返回 {rc}")
            result.failed_viewkeys.extend(same_name_entries)
        else:
            copied_entries.update(same_name_entries)

    if renamed_entries:
        transfer_sem = asyncio.Semaphore(max(1, int(settings.rclone_transfers)))

        async def _copyto_one(vk_item: str, entry_tuple: Tuple[Path, str, str, FileSnapshot]) -> Tuple[str, bool, str]:
            local_p, _, rem_rel, _ = entry_tuple
            async with transfer_sem:
                if cancel_event is not None and cancel_event.is_set():
                    return vk_item, False, "rclone 执行已取消"
                err_lines_one: List[str] = []
                cmd = [
                    rclone,
                    "copyto",
                    str(local_p),
                    f"{remote_dir}/{rem_rel}",
                    *copy_opts,
                ]
                run_kwargs: Dict[str, Any] = {
                    "timeout": settings.rclone_timeout_seconds,
                    "cancel_event": cancel_event,
                }
                if on_progress is not None:
                    run_kwargs["on_stderr_line"] = _make_stderr_handler({vk_item: entry_tuple}, err_lines_one)
                rc_one, _, stderr_one = await _run_cmd(cmd, **run_kwargs)
                if rc_one == 0:
                    return vk_item, True, ""
                if err_lines_one:
                    return vk_item, False, err_lines_one[-1]
                lines = [line for line in stderr_one.strip().splitlines() if line.strip()]
                return vk_item, False, lines[-1] if lines else f"rclone copyto 返回 {rc_one}"

        copyto_results = await asyncio.gather(
            *(_copyto_one(vk_item, entry_tuple) for vk_item, entry_tuple in renamed_entries.items())
        )
        for vk_item, ok_one, err_one in copyto_results:
            if ok_one:
                copied_entries[vk_item] = renamed_entries[vk_item]
            else:
                result.failed_viewkeys.append(vk_item)
                if err_one and not result.error:
                    result.error = err_one

    if not copied_entries:
        return result

    verify_error = ""
    for vk, (path, relative, remote_rel, snapshot) in copied_entries.items():
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
        verified, reason = await _verify_remote_file_detailed(
            rclone,
            remote_dir,
            root,
            path,
            relative,
            snapshot,
            settings,
            remote_relative=remote_rel,
            cancel_event=cancel_event,
        )
        if verified and file_snapshot(path) == snapshot and not (cancel_event and cancel_event.is_set()):
            result.uploaded_viewkeys.append(vk)
            result.verified_sources[vk] = snapshot
            result.remote_paths[vk] = f"{remote_dir}/{remote_rel}"
        else:
            result.failed_viewkeys.append(vk)
            if reason and not verify_error:
                verify_error = reason
    if result.failed_viewkeys and not result.error:
        result.error = verify_error or "无法确认远端文件与本地内容一致，已保留本地文件"

    return result
