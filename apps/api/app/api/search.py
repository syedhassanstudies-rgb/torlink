"""Search API: proxy multi-source search through the torlink daemon.

- POST /api/search          -> run a search (any authenticated role)
- GET  /api/search/sources  -> registered source metadata
- GET  /api/search/history  -> caller's own recent searches
- DELETE /api/search/history-> clear caller's history

History is stored per user unless they opted out via
user_settings.settings['save_search_history'] = False.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.downloads import get_torlink
from app.core.auth_deps import get_current_user
from app.db.engine import get_db_session
from app.models.search_history import SearchHistory
from app.models.user import User
from app.torlink.client import (
    TorlinkClient,
    TorlinkClientError,
    TorlinkHTTPError,
    TorlinkUnavailableError,
)

router = APIRouter(prefix="/search", tags=["search"])

MAX_QUERY_LEN = 512


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)


class SearchHit(BaseModel):
    info_hash: str
    name: str
    size_bytes: int
    seeders: int
    leechers: int
    num_files: int | None
    source: str
    magnet: str
    added: int | None


class SearchResponse(BaseModel):
    query: str
    elapsed_ms: int
    timed_out: bool
    total: int
    results: list[SearchHit]
    sources: dict[str, dict]


class SourceInfo(BaseModel):
    id: str
    label: str
    groups: list[str]
    homepage: str
    reportsHealth: bool


class HistoryEntry(BaseModel):
    id: uuid.UUID
    query: str
    result_count: int
    created_at: object


async def _history_enabled(session: AsyncSession, user_id) -> bool:
    """Default True; users opt out via settings['save_search_history']=False."""

    from app.models.user_setting import UserSetting

    row = (
        await session.execute(select(UserSetting).where(UserSetting.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return True
    return bool(row.settings.get("save_search_history", True))


async def _daemon_search_or_error(client: TorlinkClient, query: str):
    try:
        return await client.search(query)
    except TorlinkUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="torlink daemon is unavailable",
        ) from exc
    except (TorlinkHTTPError, TorlinkClientError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("", response_model=SearchResponse)
async def run_search(
    payload: SearchRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> SearchResponse:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    outcome = await _daemon_search_or_error(client, query)

    # Best-effort history write; a failure must not kill the search result.
    if await _history_enabled(session, user.id):
        session.add(
            SearchHistory(user_id=user.id, query=query[:512], result_count=len(outcome.results))
        )
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - history is non-critical
            await session.rollback()

    return SearchResponse(
        query=outcome.query,
        elapsed_ms=outcome.elapsed_ms,
        timed_out=outcome.timed_out,
        total=len(outcome.results),
        results=[
            SearchHit(
                info_hash=r.info_hash,
                name=r.name,
                size_bytes=r.size_bytes,
                seeders=r.seeders,
                leechers=r.leechers,
                num_files=r.num_files,
                source=r.source,
                magnet=r.magnet,
                added=r.added,
            )
            for r in outcome.results
        ],
        sources=outcome.sources,
    )


@router.get("/sources", response_model=list[SourceInfo])
async def list_sources(
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Static registry metadata; no daemon round-trip needed."""

    # Mirrors src/sources/registry.ts. The TUI is the single writer of that
    # file; this list must be updated alongside it.
    return [
        {"id": "fitgirl", "label": "FitGirl", "groups": ["Games"],
         "homepage": "https://fitgirl-repacks.site", "reportsHealth": False},
        {"id": "yts", "label": "YTS", "groups": ["Movies"],
         "homepage": "https://yts.mx", "reportsHealth": True},
        {"id": "tpb-movies", "label": "PirateBay Movies", "groups": ["Movies"],
         "homepage": "https://apibay.org", "reportsHealth": True},
        {"id": "x1337-movies", "label": "1337x Movies", "groups": ["Movies"],
         "homepage": "https://1337x.to", "reportsHealth": True},
        {"id": "eztv", "label": "EZTV", "groups": ["TV"],
         "homepage": "https://eztv.re", "reportsHealth": True},
        {"id": "tpb-tv", "label": "PirateBay TV", "groups": ["TV"],
         "homepage": "https://apibay.org", "reportsHealth": True},
        {"id": "x1337-tv", "label": "1337x TV", "groups": ["TV"],
         "homepage": "https://1337x.to", "reportsHealth": True},
        {"id": "nyaa", "label": "Nyaa", "groups": ["Anime"],
         "homepage": "https://nyaa.si", "reportsHealth": True},
        {"id": "subsplease", "label": "SubsPlease", "groups": ["Anime"],
         "homepage": "https://subsplease.org", "reportsHealth": False},
        {"id": "bittorrented", "label": "BitTorrentED", "groups": [],
         "homepage": "https://bittorrented.com", "reportsHealth": True},
    ]


@router.get("/history", response_model=list[HistoryEntry])
async def get_history(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[SearchHistory]:
    stmt = (
        select(SearchHistory)
        .where(SearchHistory.user_id == user.id)
        .order_by(SearchHistory.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@router.delete("/history", status_code=204)
async def clear_history(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    await session.execute(delete(SearchHistory).where(SearchHistory.user_id == user.id))
    await session.commit()
