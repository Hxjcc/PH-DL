"""Cooperative cancellation for network reads and retry delays."""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")


class StopRequested(Exception):
    """The job's stop event fired while waiting for an operation."""


async def until_stopped(awaitable: Awaitable[T], stop: asyncio.Event) -> T:
    operation = asyncio.ensure_future(awaitable)
    signal = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({operation, signal}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            raise StopRequested
        return operation.result()
    finally:
        for task in (operation, signal):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, signal, return_exceptions=True)
