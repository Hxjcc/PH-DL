"""Drain subprocess pipes with bounded memory and reap the child on every exit path."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from typing import TypeVar

_PIPE_CHUNK = 16 * 1024
_STDERR_LIMIT = 64 * 1024
_STDOUT_LIMIT = 8 * 1024 * 1024
_CANCEL_TERMINATE_GRACE = 0.4
_CANCEL_KILL_GRACE = 1.0
_TIMEOUT_GRACE = 2.0
_T = TypeVar("_T")


async def _wait_for_cleanup(task: asyncio.Task[_T]) -> _T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _read_stdout(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    output = bytearray()
    overflow = False
    while chunk := await stream.read(_PIPE_CHUNK):
        remaining = _STDOUT_LIMIT - len(output)
        overflow |= len(chunk) > remaining
        output.extend(chunk[:remaining])
    return bytes(output), overflow


async def _read_stderr(
    stream: asyncio.StreamReader, filter_line: Callable[[str], str | None] | None,
) -> str:
    output = bytearray()
    line = bytearray()

    def retain_line() -> None:
        if not line:
            return
        msg = line.decode(errors="replace").rstrip("\r")
        if filter_line is not None:
            msg = filter_line(msg)
        line.clear()
        if msg:
            output.extend(msg.encode("utf-8", errors="replace")[:_STDERR_LIMIT] + b"\n")
            del output[:-_STDERR_LIMIT]

    while chunk := await stream.read(_PIPE_CHUNK):
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            line.extend(part[:_PIPE_CHUNK - len(line)])
            retain_line()
        line.extend(parts[-1][:_PIPE_CHUNK - len(line)])
    retain_line()
    return output.decode(errors="replace").strip()


async def run_bounded_process(
    cmd: Sequence[str],
    *,
    timeout: float | None,
    cancel_event: asyncio.Event | None,
    env: dict[str, str] | None = None,
    capture_stdout: bool = True,
    filter_stderr: Callable[[str], str | None] | None = None,
) -> tuple[int, str, str]:
    if cancel_event is not None and cancel_event.is_set():
        return 130, "", ""

    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    ))
    spawn_cancelled = False
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Creation can finish after the caller has been cancelled.
        spawn_cancelled = True
        try:
            proc = await _wait_for_cleanup(spawn)
        except OSError:
            raise asyncio.CancelledError from None

    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_read_stderr(proc.stderr, filter_stderr))
    stdout_task = asyncio.create_task(_read_stdout(proc.stdout)) if proc.stdout is not None else None
    tasks = [asyncio.create_task(proc.wait()), stderr_task]
    if stdout_task is not None:
        tasks.append(stdout_task)
    wait_task = asyncio.gather(*tasks)
    signal = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None

    async def stop_and_drain(*, cancelled: bool) -> None:
        term_grace = _CANCEL_TERMINATE_GRACE if cancelled else _TIMEOUT_GRACE
        kill_grace = _CANCEL_KILL_GRACE if cancelled else _TIMEOUT_GRACE
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        _, pending = await asyncio.wait(tasks, timeout=term_grace)
        if pending:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            _, pending = await asyncio.wait(pending, timeout=kill_grace)
        if pending:
            # A descendant may still hold a pipe even after the direct child exited.
            proc._transport.close()
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        if spawn_cancelled:
            raise asyncio.CancelledError
        waiting = {wait_task, signal} if signal is not None else {wait_task}
        done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if wait_task in done:
            await wait_task
            stdout, overflow = stdout_task.result() if stdout_task is not None else (b"", False)
            stderr = stderr_task.result()
            if overflow:
                return proc.returncode or 1, "", f"{stderr}\n子进程标准输出超过 {_STDOUT_LIMIT // 1024**2} MiB".strip()
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr
        cancelled = signal in done
        await stop_and_drain(cancelled=cancelled)
        stderr = stderr_task.result() if stderr_task.done() and not stderr_task.cancelled() else ""
        return (130 if cancelled else 124), "", stderr
    except BaseException:
        await _wait_for_cleanup(asyncio.create_task(stop_and_drain(cancelled=True)))
        raise
    finally:
        pending_cleanup = [wait_task, *tasks]
        if signal is not None:
            pending_cleanup.append(signal)
        for task in pending_cleanup:
            task.cancel()
        await asyncio.gather(*pending_cleanup, return_exceptions=True)
