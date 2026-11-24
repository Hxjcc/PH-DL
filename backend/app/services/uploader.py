"""Copy batches with rclone and verify remote contents before local cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import RuntimeSettings
from .filename import is_valid_video_file

FileSnapshot = Tuple[int, int, int, int, int]


@dataclass
class UploadBatchResult:
    uploaded_viewkeys: List[str] = field(default_factory=list)
    failed_viewkeys: List[str] = field(default_factory=list)
    remote_dir: str = ""
    error: str = ""
    verified_sources: Dict[str, FileSnapshot] = field(default_factory=dict)


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

    if timeout is None and cancel_event is None:
        try:
            stdout_b, stderr_b = await proc.communicate()
        except asyncio.CancelledError:
            await _terminate_process(proc)
            raise
        return (
            int(proc.returncode or 0),
            (stdout_b or b"").decode(errors="replace"),
            (stderr_b or b"").decode(errors="replace"),
        )

    communicate_task = asyncio.create_task(proc.communicate())
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
) -> List[str]:
    opts = [
        "--no-traverse",
        "--ignore-times",
        f"--transfers={settings.rclone_transfers}",
        f"--checkers={settings.rclone_checkers}",
        f"--log-level={settings.rclone_log_level}",
        "--stats=0",
    ]
    if settings.rclone_ignore_checksum:
        opts.append("--ignore-checksum")
    if settings.rclone_ignore_size:
        opts.append("--ignore-size")

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

    if settings.rclone_tpslimit > 0:
        opts.append(f"--tpslimit={settings.rclone_tpslimit}")
        if settings.rclone_tpslimit_burst > 0:
            opts.append(f"--tpslimit-burst={settings.rclone_tpslimit_burst}")
    return opts


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
    """Require matching content from a remote hash or a streamed comparison."""
    rc, stdout, _ = await _run_cmd(
        [
            rclone,
            "lsjson",
            "--stat",
            "--no-mimetype",
            "--no-modtime",
            f"{remote_dir}/{relative}",
            "--hash",
            f"--log-level={settings.rclone_log_level}",
        ],
        timeout=min(settings.rclone_timeout_seconds, 60),
        cancel_event=cancel_event,
    )
    if rc != 0 or not stdout.strip():
        return False
    try:
        meta = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(meta, dict) or meta.get("IsDir") is not False:
        return False

    local_size = snapshot[2]
    if type(meta.get("Size")) is not int or meta["Size"] != local_size or local_size < settings.min_file_size_bytes:
        return False

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
                return (await asyncio.to_thread(hash_local)).lower() == digest.lower()
            except OSError:
                return False
            except ValueError:
                # The local hashing provider may disable a specific algorithm.
                continue

    # Some providers report a length but no hash. A matching name and size
    # still do not establish that it is our file.
    with _upload_file_list([relative]) as check_list:
        rc, matching, _ = await _run_cmd(
            [rclone, "check", str(download_dir), remote_dir,
             "--one-way", "--download", "--match", "-", "--files-from-raw", str(check_list),
             f"--log-level={settings.rclone_log_level}"],
            timeout=settings.rclone_timeout_seconds,
            cancel_event=cancel_event,
        )
    return rc == 0 and relative in matching.splitlines()


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
) -> UploadBatchResult:
    """Copy and verify the batch. The caller records success before deleting any source."""
    remote_dir = build_remote_dir(settings, uploader_folder)
    result = UploadBatchResult(remote_dir=remote_dir)
    if not files_by_viewkey:
        return result

    root = download_dir.resolve()
    entries: Dict[str, Tuple[Path, str, FileSnapshot]] = {}
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
        entries[vk] = (path, relative, snapshot)
    if not entries:
        result.error = "没有可上传且完整的本地文件"
        return result

    rclone = (settings.rclone_bin or "rclone").strip()
    remote_type = await detect_remote_type(settings, cancel_event=cancel_event)

    rc_mk, _, err_mk = await _run_cmd(
        [rclone, "mkdir", remote_dir, f"--log-level={settings.rclone_log_level}"],
        timeout=min(settings.rclone_timeout_seconds, 60),
        cancel_event=cancel_event,
    )
    if rc_mk != 0:
        result.failed_viewkeys.extend(entries)
        result.error = err_mk.strip() or f"rclone mkdir 失败 (code={rc_mk})"
        return result

    with _upload_file_list([entry[1] for entry in entries.values()]) as list_file:
        cmd = [
            rclone,
            "copy",
            str(root),
            remote_dir,
            *build_rclone_copy_opts(settings, remote_type=remote_type),
            "--files-from-raw",
            str(list_file),
        ]
        rc, _, stderr = await _run_cmd(
            cmd,
            timeout=settings.rclone_timeout_seconds,
            cancel_event=cancel_event,
        )
    if rc != 0:
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        result.error = lines[-1] if lines else f"rclone copy 返回 {rc}"
        result.failed_viewkeys.extend(entries)
        return result

    for vk, (path, relative, snapshot) in entries.items():
        if (
            not (cancel_event and cancel_event.is_set())
            and file_snapshot(path) == snapshot
            and await verify_remote_file(rclone, remote_dir, root, path, relative, snapshot, settings,
                                         cancel_event=cancel_event)
            and file_snapshot(path) == snapshot
            and not (cancel_event and cancel_event.is_set())
        ):
            result.uploaded_viewkeys.append(vk)
            result.verified_sources[vk] = snapshot
        else:
            result.failed_viewkeys.append(vk)
    if result.failed_viewkeys:
        result.error = "无法确认远端文件与本地内容一致，已保留本地文件"

    return result
