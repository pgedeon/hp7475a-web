"""Application configuration (12-factor via environment)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HP7475A_", env_file=".env", extra="ignore")

    # Bind: default loopback-only. LAN opt-in is explicit and documented
    # (spec: auth disabled by default → must not expose beyond localhost).
    host: str = "127.0.0.1"
    port: int = 8750
    data_dir: Path = Path("data")
    max_upload_bytes: int = 20 * 1024 * 1024  # spec: cap uploads
    max_hpgl_bytes: int = 5 * 1024 * 1024

    # Serial defaults mirrored from the plotter's documented default config;
    # editable at runtime via settings API (persisted in DB, not env).
    default_baudrate: int = 9600
    default_bytesize: int = 8
    default_parity: str = "N"
    default_stopbits: int = 1
    default_timeout_s: float = 2.0
    write_timeout_s: float = 5.0

    # Buffer-safe streaming (docs/hardware-notes.md §6)
    stream_safety_margin: int = 64
    stream_default_chunk: int = 256
    stream_query_timeout_s: float = 2.0
    stream_max_retries: int = 3
    completion_timeout_s: float = 600.0
    status_poll_interval_s: float = 1.0

    job_history_keep: int = 100

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hp7475a.sqlite3"
