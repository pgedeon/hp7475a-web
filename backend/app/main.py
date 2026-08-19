"""FastAPI application factory.

Lifespan wires: Database, JobStore (+WS bridge), DeviceManager, FileRegistry,
HardwareWorker. Static frontend serving is optional (dev mode uses Vite
proxy); when frontend/dist exists it is served at /.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.ws import WSHub
from app.config import Settings
from app.db import Database
from app.jobs.store import JobStore
from app.jobs.worker import HardwareWorker
from app.registry import AppState
from app.services.device_manager import DeviceManager
from app.services.files import FileRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        jobs = JobStore(db, history_keep=settings.job_history_keep)
        hub = WSHub()
        devices = DeviceManager()
        files = FileRegistry(settings.data_dir / "files", settings.max_upload_bytes)
        worker = HardwareWorker(jobs, devices, settings, publish=hub.publish)
        jobs.subscribe(lambda job: hub.publish({
            "type": "job", "job_id": job.id, "status": job.status.value,
            "bytes_sent": job.bytes_sent, "bytes_total": job.bytes_total,
            "error": job.error,
        }))
        worker.start()
        app.state.container = AppState(
            settings=settings, db=db, jobs=jobs, devices=devices,
            files=files, worker=worker, ws_hub=hub,
        )
        logger.info("hp7475a-web ready on %s:%s (data: %s)", settings.host, settings.port, settings.data_dir)
        try:
            yield
        finally:
            worker.shutdown()
            devices.disconnect()
            db.close()

    app = FastAPI(title="HP 7475A Web Controller", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    dist = Path(__file__).resolve().parent / "frontend_dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
        logger.info("serving frontend from %s", dist)

    return app


app = create_app()
