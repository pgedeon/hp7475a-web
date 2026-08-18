"""App-wide singleton container (wired once in create_app)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.db import Database
from app.jobs.store import JobStore
from app.services.device_manager import DeviceManager
from app.services.files import FileRegistry


@dataclass
class AppState:
    settings: Settings
    db: Database
    jobs: JobStore
    devices: DeviceManager
    files: FileRegistry
    worker: Any = None  # HardwareWorker (set in lifespan; avoids cycle)
    ws_hub: Any = None
