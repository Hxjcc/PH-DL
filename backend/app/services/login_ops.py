"""Track UI-triggered background cookie refresh operations.

A PornHub login can spend several minutes waiting for reCAPTCHA, which is
longer than the default 60s read timeout of many reverse proxies (OpenResty,
Nginx, ...).  The HTTP endpoints start the login in the background and let the
WebUI poll for the result, so a proxy timeout can no longer turn a successful
login into a ``504 Gateway Time-out`` page.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

LoginRunner = Callable[[], Awaitable[Dict[str, Any]]]


@dataclass
class LoginOperation:
    site: str
    task: Optional["asyncio.Task[Dict[str, Any]]"] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Optional[Dict[str, Any]] = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return max(0.0, end - self.started_at)

    def snapshot(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "site": self.site,
            "running": self.running,
            "has_result": self.result is not None,
            "elapsed_seconds": round(self.elapsed(), 1),
        }
        if self.running:
            body["message"] = "登录仍在进行中..."
        elif self.result is not None:
            body.update(self.result)
        return body


class LoginOperationManager:
    """Keep one background login per site and expose its latest result."""

    def __init__(self) -> None:
        self._ops: Dict[str, LoginOperation] = {}

    def _op(self, site: str) -> LoginOperation:
        op = self._ops.get(site)
        if op is None:
            op = LoginOperation(site=site)
            self._ops[site] = op
        return op

    def snapshot(self, site: str) -> Dict[str, Any]:
        return self._op(site).snapshot()

    def is_running(self, site: str) -> bool:
        return self._op(site).running

    async def start(
        self,
        site: str,
        runner: LoginRunner,
        *,
        wait: bool = False,
    ) -> Dict[str, Any]:
        """Start a login unless one is already running for this site.

        With ``wait=True`` the caller blocks until the login finishes (used by
        tests and API clients that want the legacy synchronous behaviour).
        """

        op = self._op(site)
        started = False
        if not op.running:
            op.result = None
            op.started_at = time.monotonic()
            op.finished_at = 0.0
            op.task = asyncio.create_task(self._run(op, runner), name=f"login-refresh-{site}")
            started = True

        if wait:
            return await asyncio.shield(op.task)

        return {
            "site": site,
            "started": started,
            "running": True,
            "message": "已开始登录，请稍候..." if started else "登录已在进行中，请稍候...",
        }

    @staticmethod
    async def _run(op: LoginOperation, runner: LoginRunner) -> Dict[str, Any]:
        try:
            result = await runner()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive
            result = {"success": False, "message": f"登录任务异常: {err}"}
        if not isinstance(result, dict):
            result = {"success": False, "message": "登录任务返回了异常结果"}
        op.result = result
        op.finished_at = time.monotonic()
        return result

    async def shutdown(self) -> None:
        pending = [op.task for op in self._ops.values() if op.task is not None and not op.task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
