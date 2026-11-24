"""FastAPI application container and route dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from ..auth import AuthManager
from ..config import EnvSettings
from ..db.database import Database
from ..db.repo import Repository
from ..engine.events import EventBus
from ..engine.loop_monitor import EventLoopMonitor
from ..engine.scheduler import UpdateScheduler
from ..engine.task_manager import TaskManager
from ..services.playback import PlaybackService


@dataclass
class AppContainer:
    env: EnvSettings
    db: Database
    repo: Repository
    auth: AuthManager
    events: EventBus
    loop_monitor: EventLoopMonitor
    tasks: TaskManager
    scheduler: UpdateScheduler
    playback: PlaybackService


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def require_auth(request: Request, container: AppContainer = Depends(get_container)) -> AppContainer:
    container.auth.require_auth(request)
    return container
