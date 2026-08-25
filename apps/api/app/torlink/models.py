"""Typed models mirroring the torlink daemon's JSON payloads."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DaemonTorrentStatus(StrEnum):
    """Coarse view of the daemon's queue item status strings.

    The daemon reports whatever its queue uses internally; unknown values
    pass through so the adapter never breaks on upstream additions.
    """

    DOWNLOADING = "downloading"
    QUEUED = "queued"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DaemonDownload(BaseModel):
    """One item from the daemon's download queue."""

    id: str
    name: str | None = None
    status: str = "unknown"
    progress: float = 0.0
    peers: int = 0
    speed: float = 0.0

    model_config = {"extra": "allow"}  # forward compatibility with daemon


class DaemonSeed(BaseModel):
    """One seeding item from the daemon."""

    id: str
    name: str | None = None
    status: str = "unknown"
    peers: int = 0
    uploaded: float = 0.0

    model_config = {"extra": "allow"}


class DaemonStatus(BaseModel):
    """Full GET /downloads payload."""

    downloads: list[DaemonDownload] = Field(default_factory=list)
    seeds: list[DaemonSeed] = Field(default_factory=list)


class DaemonSearchResultItem(BaseModel):
    """One search hit from the daemon's GET /search."""

    info_hash: str = Field(alias="infoHash")
    name: str
    size_bytes: int = Field(default=0, alias="sizeBytes")
    seeders: int = 0
    leechers: int = 0
    num_files: int | None = Field(default=None, alias="numFiles")
    source: str
    magnet: str
    added: int | None = None

    model_config = {"extra": "allow", "populate_by_name": True}


class DaemonSearchResults(BaseModel):
    """Full GET /search payload."""

    ok: bool = True
    query: str
    elapsed_ms: int = Field(default=0, alias="elapsedMs")
    timed_out: bool = Field(default=False, alias="timedOut")
    results: list[DaemonSearchResultItem] = Field(default_factory=list)
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"extra": "allow", "populate_by_name": True}


class AddOutcome(BaseModel):
    """POST /add result. `outcome` is daemon-defined (added/duplicate/etc)."""

    ok: bool = True
    outcome: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ControlAction(StrEnum):
    """Actions the daemon's POST /control accepts."""

    PAUSE = "pause"
    RESUME = "resume"
    START_SEED = "start-seed"
    STOP_SEED = "stop-seed"
    REMOVE = "remove"
    DELETE = "delete"
