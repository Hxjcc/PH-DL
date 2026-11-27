"""Release idle Python, SQLite, and glibc memory after large jobs."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import gc
import sys
from typing import Optional

from ..db.database import Database


def trim_process_memory() -> None:
    if sys.platform != "linux":
        return
    with contextlib.suppress(OSError, AttributeError):
        malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim(0)


async def reclaim_memory(db: Optional[Database] = None) -> None:
    await asyncio.to_thread(gc.collect)
    if db is not None:
        with contextlib.suppress(Exception):
            await db.execute("PRAGMA shrink_memory")
    await asyncio.to_thread(trim_process_memory)
