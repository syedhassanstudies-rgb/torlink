# torlink FastAPI app

This app is the production-facing API layer for torlink. The first milestone keeps torlink as the torrent engine and exposes a clean FastAPI surface around it.

## Local development

```sh
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

## Configuration

Settings use the `TORLINK_API_` environment prefix:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TORLINK_API_APP_NAME` | `torlink API` | FastAPI application name |
| `TORLINK_API_APP_VERSION` | `0.1.0` | FastAPI application version |
| `TORLINK_API_ENVIRONMENT` | `development` | Runtime environment label |
| `TORLINK_API_TORLINK_BASE_URL` | `http://127.0.0.1:9161` | Local torlink daemon URL |
| `TORLINK_API_TORLINK_TOKEN` | unset | Bearer token for protected torlink daemon endpoints |
| `TORLINK_API_TORLINK_TIMEOUT_SECONDS` | `5.0` | Timeout for torlink daemon calls |

## Current endpoints

- `GET /api/health` returns FastAPI app health and metadata.
- `GET /api/torlink/health` checks the underlying torlink daemon.
