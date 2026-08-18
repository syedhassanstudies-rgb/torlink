# torlink FastAPI app roadmap

This note summarizes the current terminal-first architecture and a practical path to turn it into a full app from a Python/FastAPI stack.

## What the repo is today

- **Runtime:** Node 22+, TypeScript, React 19, Ink 7 terminal UI, WebTorrent 2. The published command is `torlnk`.
- **Main entrypoint:** `src/index.tsx` parses CLI commands, starts the Ink TUI by default, or switches into headless modes such as `watch`, `serve`, `files`, and `attach`.
- **Core domain:** torrent source search, magnet/torrent parsing, queue persistence, WebTorrent download/seeding, and config paths are already separated from terminal rendering.
- **Existing API:** `torlnk serve` starts a small local HTTP server with endpoints for health, status/downloads, add, and control actions.
- **File server:** `torlnk files` can expose completed downloads over HTTP.

## Existing seams we can reuse

### Search

The search path is currently UI-driven: `src/ui/hooks/useConcurrentSearch.ts` fans a query out to every source in `SOURCES`, streams per-source status, deduplicates by info hash, and orders by seeders/date. This is a good candidate to extract into a shared service, because a web API will need the same behavior without React hooks.

Recommended extraction:

- Move source fan-out, dedupe, and default sorting into `src/search/service.ts`.
- Keep the React hook as a thin wrapper over that service.
- Add a headless `/search?q=...&category=...` endpoint that returns results and source diagnostics.

### Download queue

The download engine is already headless-friendly. `src/daemon/runtime.ts` restores config, queue, history, seeds, trackers, and safe-mode boot state without Ink. `src/daemon/serve.ts` already uses that runtime to add magnets and control queue items.

Recommended extraction:

- Treat `startRuntime()` and `addInput()` as the backend boundary.
- Expand the JSON status payload to include history, seed progress, bytes downloaded, upload speed, ETA, and download directory.
- Add stable endpoint contracts before building a Python client/UI.

### Control surface

The current HTTP control actions are enough for a first web UI:

- `pause`
- `resume`
- `start-seed`
- `stop-seed`
- `remove`
- `delete`

A FastAPI application can call these endpoints immediately if it runs `torlnk serve` as a sidecar process.

## FastAPI integration options

### Option A: FastAPI as a sidecar/orchestrator

Run torlink's Node daemon locally and let FastAPI proxy and enrich it.

```text
Browser / mobile UI
        |
        v
FastAPI app (auth, users, preferences, WebSocket/SSE)
        |
        v
local torlnk serve + torlnk files processes
        |
        v
WebTorrent + persisted torlink state
```

Pros:

- Fastest path from terminal app to proper app.
- Minimal risk to proven torrent/download logic.
- Python can own auth, database models, jobs, UI API shape, deployment, and notifications.

Cons:

- Two runtimes to supervise.
- FastAPI needs a process manager for the Node daemon.
- Search still needs a Node endpoint or a Python reimplementation unless extracted first.

This is the recommended first version.

### Option B: FastAPI frontend over a new Node JSON API

Keep all torrent/source logic in TypeScript, but build a richer official HTTP API in this repo. FastAPI consumes that API and focuses on app concerns.

Pros:

- Clean responsibility split.
- Easier to test the TypeScript core and Python app separately.
- Avoids duplicating source scrapers in Python.

Cons:

- Requires API design work in torlink before the FastAPI layer feels complete.

This is the recommended production architecture after Option A proves the product shape.

### Option C: Port core logic to Python

Rewrite source scrapers, queueing, persistence, and torrent engine integration in Python.

Pros:

- Single language/runtime for the backend.

Cons:

- Highest risk and slowest path.
- WebTorrent behavior, source parsing, seeding state, and crash recovery would need to be rebuilt.

This should be avoided unless there is a hard requirement to remove Node.

## Suggested MVP scope

1. **Node daemon sidecar**
   - FastAPI starts or connects to `torlnk serve`.
   - Health check verifies `/health`.
   - API token is generated and stored by FastAPI.

2. **Search API**
   - Extract headless search service from the Ink hook.
   - Add `GET /search` to torlink's daemon.
   - FastAPI proxies search results to the frontend.

3. **Downloads API**
   - Use existing `/add`, `/status`, and `/control` endpoints.
   - Add richer fields as the UI needs them.

4. **Realtime updates**
   - FastAPI polls the Node daemon initially.
   - Later, add Server-Sent Events or WebSockets from FastAPI to the browser.

5. **Files and playback**
   - Use `torlnk files` for finished content.
   - FastAPI can issue signed links, enforce user access, and provide metadata.

6. **App shell**
   - A React/Next.js, plain React, or server-rendered frontend can sit on FastAPI.
   - Main screens: search, detail/result list, active downloads, completed library, seeding/settings.

## First engineering tasks

- Add a pure `searchTorrents(query, options)` service in TypeScript.
- Add tests proving the service dedupes and sorts the same way as the current UI hook.
- Add `GET /search` to `src/daemon/serve.ts`.
- Add a small FastAPI proof of concept that proxies `/health`, `/search`, `/downloads`, `/add`, and `/control`.
- Document the local dev command sequence for running both processes.

## Open decisions

- Should the full app be single-user local-first, or multi-user/server-hosted?
- Should FastAPI own persistence for user/library metadata, or should torlink remain the source of truth?
- Should downloads be per-user isolated by directory, or shared across users?
- What frontend stack should pair with FastAPI?
- How much moderation/legal guardrail UX should be built around source categories and file types?
