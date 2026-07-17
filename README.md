# SSC-PACS

A self-hosted research PACS for stroke imaging: DICOM storage compatible with compressed cold storage (decompressed on demand and kept as a warm cache) and viewing (Orthanc + OHIF) paired with a web application for browsing studies against research metadata and annotating them at the patient, study, and series level.

## What's in the repo

| Path | What it is |
|---|---|
| `stanford-stroke-pacs/web-app/` | The web application: FastAPI backend + React (Vite/Tailwind) frontend in one uvicorn process — API, auth, per-user dataset access, multi-level annotations, cold-storage cache manager, and the authenticated reverse proxy to Orthanc |
| `stanford-stroke-pacs/alembic/` | **Single source of truth for the DB schema** — migrations run automatically at web-app startup; never edit a shipped revision |
| `stanford-stroke-pacs/scripts/` | Operational tooling: user/credential admin, nightly backups, cold-storage lifecycle, data-integrity audits, and the Linux/macOS installers (`scripts/README.md` is the index) |
| `stanford-stroke-pacs/deploy/` | systemd + launchd unit **templates** (`*.in`) — rendered by the installers, never installed by hand |
| `stanford-stroke-pacs/image_ingestion_protocols/` | Ingestion pipeline for new imaging data (metadata extraction, series/study typing, archiving, indexing).  |
| `orthanc-indexer-patched/` | Source for the custom `ssc-orthanc:patched-indexer` image the stack requires (not on any registry — built on the host) |
| `docs/` | All documentation — [`docs/context.md`](docs/context.md) is the index |

## Particularities worth knowing up front

- **Compressed cold storage is the canonical store.** In production
  (`cold_path_cache` mode) the authoritative copy of every series is a
  `*.tar.zst` archive; the uncompressed DICOM tree Orthanc indexes is only a
  **warm cache** — series are extracted on demand and evicted after a TTL.
  The original DICOM bytes inside the archives are never rewritten.
- **The patched Orthanc image makes that work**: its Folder Indexer keeps index
  entries for files that are (deliberately) absent (`RemoveMissingFiles: false`)
  and adds a scoped `POST /indexer/scan` — there is no continuous tree scan;
  indexing is explicit and per-case.
- **Two databases, one PostgreSQL server**: `stanford-stroke` (research
  metadata + everything the web app owns) and `orthanc_db` (Orthanc's private
  index — hands off, except sanctioned read-only reconciliation). The cluster
  runs as a dedicated system user under `ssc-postgres.service`, provisioned by
  the repo.
- **End users never touch Orthanc credentials**: they log into the web app
  (PostgreSQL `users`, JWT cookie), and it proxies all viewer/DICOMweb traffic
  with a service account. Non-admin data visibility is **deny-by-default**,
  gated per user by `patient.dataset` grants.
- **Three config files, nothing else hand-edited**: `.env` (secrets),
  `config.toml` (non-secret ops — storage mode/paths, backups, ports),
  `deploy.env` (per-host identity). Compose files and service units are always
  generated. Map: [`docs/reference/configuration_sources.md`](docs/reference/configuration_sources.md).

## Getting started

| I want to… | Read |
|---|---|
| **Install on a fresh server** | [`docs/guides/installation_and_deployment.md`](docs/guides/installation_and_deployment.md) |
| Port an existing deployment to a new host | [`docs/operations/cluster_migration.md`](docs/operations/cluster_migration.md) |
| Understand the whole system first | [`docs/reference/system_overview.md`](docs/reference/system_overview.md) |
| Find any other topic | [`docs/context.md`](docs/context.md) — the full documentation index |
| Operate it day to day | [`docs/operations/commands.md`](docs/operations/commands.md) |

## Development

```bash
conda activate ssc-pacs
make install-dev   # one-time: Python + Node dev deps + pre-commit hooks
make test          # backend (pytest) + frontend (vitest) + ingestion (pytest)
make lint          # ruff + eslint
```

Developer setup detail: [`docs/guides/installation_and_deployment.md` §8](docs/guides/installation_and_deployment.md).
Releases are tagged `vX.Y` and logged in [`CHANGELOG.md`](CHANGELOG.md).
