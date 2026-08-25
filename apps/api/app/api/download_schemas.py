"""Pydantic schemas for the public downloads API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.download_record import DownloadStatus


class DownloadCreate(BaseModel):
    """POST /api/downloads body."""

    magnet: str = Field(min_length=8, max_length=2048)
    label: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    # Where this came from ("search", "manual", api key name, ...) for audit.
    source: str = Field(default="manual", max_length=64)


class DownloadOut(BaseModel):
    """Public shape of an application download record."""

    id: uuid.UUID
    info_hash: str
    name: str | None
    status: DownloadStatus
    label: str | None
    category: str | None
    source: str
    is_deletion_allowed: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class DownloadDetail(DownloadOut):
    """Record plus live progress from the daemon when available."""

    live_status: str | None = None
    live_progress: float | None = None
    live_peers: int | None = None
    live_speed: float | None = None
    seeding: bool = False


class ControlBody(BaseModel):
    """Optional body for pause/resume/seeding endpoints (kept minimal)."""

    pass


class ActionOk(BaseModel):
    ok: bool = True
    id: uuid.UUID
    action: str
