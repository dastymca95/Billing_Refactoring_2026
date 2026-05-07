# Billing Refactoring 2026 — Docker for the web app

This repo ships a minimal `docker compose` setup so the web app can run on any machine that has Docker without juggling Python venvs, Tesseract installs, or Poppler binaries.

> **Files added in this build**
>
> - [Dockerfile.backend](Dockerfile.backend) — Python 3.11 slim + Tesseract + Poppler + the `requirements.txt` deps.
> - [webapp/frontend/Dockerfile](webapp/frontend/Dockerfile) — Node 20 alpine + the Vite dev server.
> - [docker-compose.yml](docker-compose.yml) — two services (`backend`, `frontend`) wired up with persistent `webapp_data/`.
> - [.dockerignore](.dockerignore) — keeps `.venv`, `node_modules`, `webapp_data`, `.env`, generated `Processed_Output`, etc. out of the build context.
> - [requirements.txt](requirements.txt) — pinned Python deps used by both the CLI and the backend.

Note: this guide lives under `docs/`. If a Markdown viewer resolves the file
links above relative to this document, use the repo-root paths instead:
`../Dockerfile.backend`, `../webapp/frontend/Dockerfile`,
`../docker-compose.yml`, `../.dockerignore`, and `../requirements.txt`.

## Quick start

```bash
# 1. Make sure your .env is in place at the project root (NEVER commit it).
#    See .env.example for the keys (Dropbox refresh-token flow recommended).
copy .env.example .env       # PowerShell / cmd
cp   .env.example .env       # bash

# 2. Build images and boot both services.
docker compose up --build
```

When the build finishes you should see:

```
billing_refactoring_2026_backend   | INFO:     Uvicorn running on http://0.0.0.0:8000
billing_refactoring_2026_frontend  | ➜  Local:   http://localhost:5173/
```

Open http://localhost:5173 in your browser. The frontend uses the Vite proxy to talk to the backend on the docker network (`http://backend:8000`); your browser only needs to reach `localhost:5173` and `localhost:8000`.

The frontend source currently calls relative `/api/...` URLs. In Docker dev,
Vite proxies those requests to `http://backend:8000` because
`VITE_API_BASE_URL` is set for the dev server. That variable does not rewrite
browser fetch URLs in a static production bundle; a static build needs a
reverse proxy or a runtime API-base implementation in `webapp/frontend/src/api.ts`.
Restart the frontend container after changing `VITE_API_BASE_URL`.

## Verifying the services

| Check | Command / URL | What you should see |
| --- | --- | --- |
| Backend health | <http://localhost:8000/api/health> | `{"ok":true,"service":"billing_refactoring_2026_webapp"}` |
| Backend API docs | <http://localhost:8000/docs> | FastAPI Swagger UI listing every `/api/...` endpoint |
| Frontend | <http://localhost:5173> | Three-column workspace: sidebar, doc preview, ResMan template |
| Backend container is healthy | `docker compose ps` | `backend` shows `healthy` (compose only starts the frontend after the backend healthcheck passes) |
| Backend logs | `docker compose logs -f backend` | Streamed `INFO` / `WARNING` lines |
| Frontend logs | `docker compose logs -f frontend` | Vite HMR + per-request log lines |

## Stopping the stack

```bash
docker compose down            # stop and remove containers (data persists on host)
docker compose down -v         # also delete named volumes (we don't use any)
docker compose down --rmi all  # nuclear: remove the built images too
```

The bind-mounted folders on the host (`./webapp_data`, the project tree itself) are **never** touched by `docker compose down`.

## Persistence

There are three bind mounts in `docker-compose.yml`:

| Host path | Container path | Mode | Purpose |
| --- | --- | --- | --- |
| `.` (project root) | `/app` | read-only | Code hot-reloads — `uvicorn --reload` picks up edits to `.py` files instantly. Read-only so the container can never overwrite your tree. |
| `./webapp_data` | `/app/webapp_data` | read-write | Generated batches: uploads, OCR'd preview JSON, manual-review xlsx, exported xlsx, split PDFs. **Survives container restarts.** |
| `./webapp/frontend` | `/app` (frontend container) | read-write | Source for the Vite dev server. Anonymous volume on `node_modules` so the container's installed deps aren't masked by an empty host folder. |

So generated work is at:

```
webapp_data/
└── batches/
    └── batch_20260502_094337_193/
        ├── input/             # uploaded CSVs / PDFs
        ├── processed/         # per-vendor outputs + per-bill split PDFs
        ├── export/            # final ResMan xlsx ready to download
        ├── logs/
        └── manual_review/
```

Restarting the stack (`docker compose down && docker compose up`) keeps every batch intact. Combined with the localStorage rehydration in the frontend (Phase 1E), refreshing the browser also restores the active batch.

## Environment & secrets

`.env` is loaded by `docker-compose.yml` via `env_file: - .env` and forwarded to the backend container only. Required keys:

```
# Dropbox (for support-document upload)
DROPBOX_REFRESH_TOKEN=...
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
# Optional fallback if the refresh-token flow isn't set up:
DROPBOX_ACCESS_TOKEN=...
DROPBOX_BASE_FOLDER=/Billing_Refactoring_2026

# Phase 1H — AI fallback (optional; disabled by default)
AI_FALLBACK_ENABLED=false
AI_PROVIDER=disabled
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# GOOGLE_API_KEY=AIza...
# DEEPSEEK_API_KEY=...
```

Things to know:

- `.env` is in `.gitignore` and `.dockerignore` — it never gets baked into an image and never gets committed.
- Without Dropbox creds the app still runs; the `Document Url` column is left blank and rows are flagged `dropbox_credentials_missing`.
- AI fallback is **disabled by default** (`AI_FALLBACK_ENABLED=false`). When unset, the topbar shows `AI: off`, no provider call is made, and the app behaves identically to Phase 1G. To enable, set both `AI_FALLBACK_ENABLED=true` AND `AI_PROVIDER=<name>` plus the matching `<PROVIDER>_API_KEY`. `/api/ai/status` never returns the API key — only `enabled / provider / configured / reason / policy`.
- `docker compose config` will print the resolved env values (including secrets) — don't share that output. Use `docker compose config --no-interpolate` if you need to inspect the file structure without resolving `${VARS}`.

## Ports

| Service | Container port | Host port | Override |
| --- | ---: | ---: | --- |
| backend | 8000 | 8000 | edit the `ports:` block in `docker-compose.yml` if 8000 is already taken on the host |
| frontend | 5173 | 5173 | same |

Troubleshooting port collisions:

```bash
# Find what's listening on 8000 (Windows PowerShell)
netstat -ano | findstr :8000

# Free the port (replace <PID> with the value from above)
taskkill /F /PID <PID>
```

If you can't free the port, edit the `ports:` lines in `docker-compose.yml` to remap (e.g. `"8001:8000"`) and rebuild.

Stale-backend reset checklist:

```powershell
netstat -ano | findstr :8000
netstat -ano | findstr :5173
taskkill /F /PID <PID>
docker compose down
docker compose up --build
Invoke-RestMethod http://localhost:8000/api/health
python scripts/verify_backend_routes.py
$o = Invoke-RestMethod http://localhost:8000/openapi.json
$o.paths.PSObject.Properties.Name | Sort-Object
```

## Common operations

```bash
# Reload backend after a Python edit (hot-reload should already do it,
# but if you change requirements.txt you need a rebuild):
docker compose up --build backend

# Reload frontend deps after editing package.json:
docker compose up --build frontend

# One-off shell into the backend container:
docker compose exec backend bash

# Run the Richmond Utilities CLI inside the container:
docker compose exec backend python "Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py"

# Tail just one service's logs:
docker compose logs -f backend
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `bind source path does not exist: ...\\webapp_data` | First-time boot before the folder exists. | Create it: `mkdir webapp_data` (or just let `docker compose` create it on the next `up`). |
| Backend container repeatedly restarts | Probably missing system deps (Tesseract / Poppler) — but the bundled image already includes them. Check `docker compose logs backend` for the real traceback. | If you see `pytesseract` / `pdf2image` import errors, do `docker compose build --no-cache backend`. |
| Frontend boots but the API returns CORS errors | The browser is bypassing the Vite `/api` proxy or a stale frontend/backend pair is still running. | Load the page from `http://localhost:5173`, restart the frontend after changing `VITE_API_BASE_URL`, and confirm `/api/health` reaches the current backend. |
| `EADDRINUSE: 5173` / `EADDRINUSE: 8000` | Another local process already owns the port. | Stop the local process or remap the host port in `docker-compose.yml`. |
| Browser refresh forgets the batch | The frontend now stores the active `batch_id` in `localStorage`. If you cleared site data, this is expected. | Clear the cached entry under `billing_refactoring_active_batch_id` in DevTools → Application → Local Storage. |
| Want to nuke a batch from disk | The `Clear Batch` button calls the API which deletes the batch folder. | If the API is down, just remove the folder by hand: `rm -rf webapp_data/batches/batch_<id>`. |

## Production-style serving (optional)

The Dockerfile.backend already supports being baked without a bind mount:

```bash
docker build -f Dockerfile.backend -t billing_refactoring_2026/backend:prod .
docker run --rm -p 8000:8000 \
  -v "$(pwd)/webapp_data:/app/webapp_data" \
  --env-file .env \
  billing_refactoring_2026/backend:prod
```

For the frontend a `npm run build` output served by nginx is the usual path; the dev `Dockerfile` is intentionally for the local "drop bills, process, export" flow.

## What this Docker setup does NOT do

- ❌ No managed database (everything is JSON / xlsx on disk under `webapp_data/`).
- ❌ No reverse proxy / TLS — both services bind to localhost.
- ❌ No multi-instance scaling — the processor uses on-disk caches keyed by batch_id.
- ❌ No Anthropic / OpenAI / external AI calls — the Richmond processor is rule-based.
- ❌ No automatic backup of `webapp_data/`. Snapshot the folder yourself if you need to.
