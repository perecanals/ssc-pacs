# SSC PACS Web App

FastAPI backend + React (Vite + Tailwind) frontend for multi-level annotation
workflows over the Stanford Stroke Center PACS, with an embedded OHIF preview
pane. In production a single uvicorn process on port **8043** serves both the
REST API and the pre-built frontend from `dist/`; Node.js is only needed at
build time.

This README is a build/dev quick-start only. For architecture, deployment, and
day-2 operations, start at
[`../documentation/context.md`](../documentation/context.md) — in particular
[`reference/web_app.md`](../documentation/reference/web_app.md) and
[`reference/web_app_frontend.md`](../documentation/reference/web_app_frontend.md)
(product + React detail), [`reference/architecture.md`](../documentation/reference/architecture.md)
(topology + auth), and the [`guides/`](../documentation/guides/) install/deploy docs.

## Prerequisites

- Python 3.11+ with the `ssc-pacs` conda environment (or equivalent virtualenv)
- Node.js 20+ and npm (build-time only)
- PostgreSQL with the `stanford-stroke` database
- `.env` at the stack root (`stanford-stroke-pacs/.env`)

## Build

```bash
cd web-app
npm ci            # install frontend deps (or `npm install`)
npm run build     # compile React into dist/  (also: ./build.sh)
```

## Development (hot-reload)

Run the FastAPI backend and the Vite dev server separately; Vite proxies `/api`
to the backend.

```bash
# Terminal 1 — FastAPI backend with auto-reload
conda activate ssc-pacs
cd web-app
uvicorn app:app --port 8043 --reload

# Terminal 2 — Vite dev server (hot-reload for React/CSS)
cd web-app
npm run dev
```

Open **http://localhost:5173**. In production only port 8043 is used.

Database schema is managed with Alembic; pending migrations apply automatically
at app startup (see
[`../documentation/operations/schema_migrations.md`](../documentation/operations/schema_migrations.md)).

## Deployment

The web app runs as a native-host service — macOS launchd (`com.ssc.webapp`) in
production, systemd on Linux — installed via `scripts/macos/install_launchd.sh`
or `scripts/linux/install_systemd.sh`. See
[`../documentation/guides/deployment_on_mac.md`](../documentation/guides/deployment_on_mac.md)
and [`../documentation/guides/installation_and_deployment.md`](../documentation/guides/installation_and_deployment.md)
for the full procedure and restart/log commands.
