"""FastAPI application factory and entrypoint."""

from __future__ import annotations

import logging
import logging.handlers
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import auth as auth_router
from .api import jobs as jobs_router
from .api import overview as overview_router
from .api import settings as settings_router
from .api import targets as targets_router
from .api import videos as videos_router
from .api import ws as ws_router
from .api.deps import AppContainer
from .auth import AuthManager
from .config import EnvSettings
from .db.database import Database
from .db.repo import Repository
from .engine.events import EventBus
from .engine.loop_monitor import EventLoopMonitor
from .engine.scheduler import UpdateScheduler
from .engine.task_manager import TaskManager
from .services.login_ops import LoginOperationManager
from .services.playback import PlaybackService


def _configure_logging(env: EnvSettings) -> None:
    logger = logging.getLogger("phdl")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        env.ensure_dirs()
        fh = logging.handlers.RotatingFileHandler(
            env.logs_dir / "phdl.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass


def create_app(env_settings: Optional[EnvSettings] = None) -> FastAPI:
    env = env_settings or EnvSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        env.ensure_dirs()
        _configure_logging(env)
        if env.admin_password in {"admin", "change-me-strong-password"}:
            logging.getLogger("phdl").warning(
                "检测到默认管理员密码，请立即在环境变量 PHDL_ADMIN_PASSWORD 中设置强密码。"
            )

        db = Database(env.db_path)
        await db.connect()
        repo = Repository(db)
        recovered = await repo.recover_interrupted_jobs()

        secret_key = env.get_or_create_secret_key()
        auth = AuthManager(
            admin_password=env.admin_password,
            secret_key=secret_key,
            ttl_hours=env.session_ttl_hours,
            cookie_secure=env.cookie_secure,
            generation=await repo.get_auth_generation(),
        )
        await repo.get_settings()
        events = EventBus(max_logs=500, redact=repo.redact_diagnostic)
        loop_monitor = EventLoopMonitor(events)
        tasks = TaskManager(env=env, repo=repo, events=events)

        scheduler = UpdateScheduler(repo=repo, events=events, trigger_auto_update=tasks.enqueue_scheduled_job)

        app.state.container = AppContainer(
            env=env,
            db=db,
            repo=repo,
            auth=auth,
            events=events,
            loop_monitor=loop_monitor,
            tasks=tasks,
            scheduler=scheduler,
            playback=PlaybackService(),
            logins=LoginOperationManager(),
        )

        queued = await tasks.restore_queued_jobs()
        tasks.start()
        scheduler.start()
        loop_monitor.start()
        events.emit_log("INFO", "PH-DL 后端已启动")
        if recovered:
            events.emit_log("WARNING", f"启动恢复：已将 {recovered} 个上次意外中断的任务标记为 cancelled，可在任务页重试/续传。")
        if queued:
            events.emit_log("INFO", f"启动恢复：已恢复 {queued} 个未开始的排队任务。")

        try:
            yield
        finally:
            await app.state.container.logins.shutdown()
            await app.state.container.playback.aclose()
            await loop_monitor.stop()
            await scheduler.stop()
            await tasks.stop()
            await db.close()

    app = FastAPI(
        title="PH-DL API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if env.enable_docs else None,
        redoc_url="/redoc" if env.enable_docs else None,
        openapi_url="/openapi.json" if env.enable_docs else None,
    )

    allowed_origins = env.parsed_allowed_origins()
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        api_request = request.url.path.startswith("/api/")
        if api_request and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not AuthManager.verify_origin(
            request.headers, allowed_origins,
        ):
            response = JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if api_request:
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        detail = [{key: entry[key] for key in ("loc", "msg", "type")} for entry in error.errors()]
        return JSONResponse({"detail": detail}, status_code=422)

    @app.get("/api/health", tags=["health"])
    async def health():
        return {"status": "ok", "version": "1.0.0"}

    app.include_router(auth_router.router)
    app.include_router(overview_router.router)
    app.include_router(targets_router.router)
    app.include_router(jobs_router.router)
    app.include_router(videos_router.router)
    app.include_router(settings_router.router)
    app.include_router(ws_router.router)

    return app


app = create_app()
