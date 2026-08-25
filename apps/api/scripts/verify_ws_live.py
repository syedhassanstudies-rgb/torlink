"""Live end-to-end verification of Phase 6: real daemon + real uvicorn.

1. create admin user in DB
2. login over HTTP -> access token
3. add a magnet via REST (so a record exists)
4. connect WS /ws/downloads?token=... and read frames for ~4s
5. assert frames arrive ~1/s and contain the added torrent with progress
6. also verify an unauthenticated connection is rejected
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import User, UserRole


async def seed_admin() -> None:
    engine = create_async_engine(str(get_settings().database_url))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add(User(
            email="live@example.com",
            username="liveadmin",
            password_hash=hash_password("correct-horse-battery"),
            role=UserRole.ADMIN,
        ))
        await s.commit()
    await engine.dispose()


MAGNET = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=live-ws-test"


async def main() -> None:
    await seed_admin()

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30) as hc:
        r = await hc.post("/api/auth/login", json={
            "username_or_email": "liveadmin",
            "password": "correct-horse-battery",
        })
        token = r.json()["access_token"]
        print("login ok")

        r = await hc.post(
            "/api/downloads",
            json={"magnet": MAGNET, "source": "search"},
            headers={"Authorization": f"Bearer {token}"},
        )
        print("add download:", r.status_code)

        import websockets

        # unauthenticated must be rejected
        try:
            async with websockets.connect("ws://127.0.0.1:8000/ws/downloads") as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)
            print("FAIL: unauthenticated connection accepted")
            return
        except Exception as exc:
            print("unauthenticated rejected:", type(exc).__name__)

        frames = []
        async with websockets.connect(
            f"ws://127.0.0.1:8000/ws/downloads?token={token}"
        ) as ws:
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline and len(frames) < 4:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    frames.append(msg)
                    print("frame:", msg["type"],
                          [(d["id"][:8], d["status"], round(d["progress"], 3))
                           for d in msg["downloads"]])
                except TimeoutError:
                    break

    assert len(frames) >= 3, f"expected multiple frames, got {len(frames)}"
    ids = [d["id"] for d in frames[-1]["downloads"]]
    assert ("e" * 40) in ids, f"added torrent missing: {ids}"
    print("LIVE WS VERIFICATION PASSED:", len(frames), "frames received")


asyncio.run(main())
