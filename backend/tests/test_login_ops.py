"""Background login operation tracking tests."""

import asyncio

import pytest

from app.services.login_ops import LoginOperationManager


@pytest.mark.asyncio
async def test_login_operation_reports_progress_and_deduplicates():
    mgr = LoginOperationManager()
    release = asyncio.Event()
    calls = 0

    async def runner():
        nonlocal calls
        calls += 1
        await release.wait()
        return {"success": True, "message": "登录成功"}

    first = await mgr.start("pornhub", runner)
    assert first["started"] is True
    assert first["running"] is True

    second = await mgr.start("pornhub", runner)
    assert second["started"] is False
    assert second["running"] is True
    assert mgr.snapshot("pornhub")["running"] is True

    release.set()
    result = await mgr.start("pornhub", runner, wait=True)
    assert result["success"] is True
    assert calls == 1

    status = mgr.snapshot("pornhub")
    assert status["running"] is False
    assert status["has_result"] is True
    assert status["message"] == "登录成功"
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_login_operation_converts_runner_exception_to_failure():
    mgr = LoginOperationManager()

    async def runner():
        raise RuntimeError("boom")

    result = await mgr.start("xhamster", runner, wait=True)
    assert result["success"] is False
    assert "boom" in result["message"]
    await mgr.shutdown()
