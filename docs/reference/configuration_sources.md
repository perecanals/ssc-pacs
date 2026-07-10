# Configuration sources of truth

**Purpose:** the single map of *where every configurable value lives* and *what
must stay in sync* when deploying or operating the stack. This is an **index**,
not a procedure — each row links to the doc that explains how to use it. For the
step-by-step fresh install see
[`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md);
for macOS deltas see [`../guides/deployment_on_mac.md`](../guides/deployment_on_mac.md).

The design goal is **one authoritative home per value**. A fresh deployment edits
exactly three files — `.env` (secrets), `config.toml` (non-secret ops), and
optionally `deploy.env` (per-host identity) — and runs the two installers.
Nothing else (compose, service units, `orthanc.json`) should need hand-editing.

---

## The three tiers

| Tier | Authoritative file | Holds | Consumed by |
|---|---|---|---|
| **Secrets** | `.env` | DB credentials, Orthanc service-account credential, JWT secret, optional Orthanc ports | `web-app/db.py`, docker-compose `${VAR}` interpolation, `init_orthanc_db.sh`, host-local scripts |
| **Non-secret ops** | `config.toml` | storage mode + paths, cold-cache tuning, backup settings, session/auth tuning | `web-app/config.py`, `scripts/orthanc/dc.sh`, `scripts/backup/*` (via `config_get`) |
| **Per-host identity** | `deploy.env` (optional; auto-derived) | OS user/group, repo path, python/uvicorn bin, Homebrew prefix, conda env, PGDATA | `scripts/linux/install_systemd.sh`, `scripts/macos/install_launchd.sh`, `scripts/_lib.sh` (`resolve_python` reads `PYTHON_BIN`/`CONDA_ENV_BIN` at runtime) |

`.env` and `deploy.env` are gitignored. `config.toml` is **version-controlled and
required** — `web-app/config.py` fails fast if it is missing.

---

## Every configuration source

| File | Holds | Secret? | In git? | Authoritative or derived | Per-host edit on fresh deploy? |
|---|---|---|---|---|---|
| `.env` | DB + Orthanc-service-account creds, `JWT_SECRET`, `ORTHANC_URL`, optional `ORTHANC_HTTP_PORT`/`ORTHANC_DICOM_PORT` | **Yes** | No (`.env.example` is) | **Authoritative** for all secrets | **Yes** — copy from `.env.example`, fill in |
| `config.toml` | `[storage]` mode + paths + cold-cache tuning, `[backup]`, `[web-app]` session/auth | No | Yes (required) | **Authoritative** for non-secret ops | **Yes** — set mode + paths for this host |
| `deploy.env` | per-host service-unit identity (user, paths, conda) | No | No (`deploy.env.example` is) | **Authoritative** override; else auto-derived | Only if auto-derivation is wrong |
| `orthanc.json` | Orthanc structural config: ports (in-image default), Folder Indexer (`ScanRoots: ["/dicom-data"]`, `Folders: []`, `RemoveMissingFiles:false`), plugins | No | Yes | **Authoritative** for Orthanc structure; ports are overridable from `.env` | No |
| `orthanc_users.json` | Orthanc service-account + admin plaintext creds | **Yes** | No | **Derived** from `.env` + DB via `manage_users.py` | No — never hand-edit |
| `docker-compose.yml` | Orthanc service (Linux base, host networking); DB env from `.env`; DICOM mount from `${DICOM_MOUNT_SOURCE}` | No | Yes | **Derived** — interpolates `.env` + `dc.sh` | No |
| `docker-compose.override.macos.yml` | macOS deltas (ports, `host.docker.internal`, drop host net) | No | Yes | Static (platform delta) | No |
| `deploy/systemd/*.in`, `deploy/launchd/*.plist.in` | service-unit **templates** with `__TOKENS__` | No | Yes | **Derived** at install time from `deploy.env` | No — never install by hand |
| `init_orthanc_db.sh` | creates Orthanc role/DB | No | Yes | Reads `.env` (portable; resolves its own path) | No |

> The deprecated auto-merged `docker-compose.override.yml` is **gitignored**.
> macOS deltas now live in the tracked `docker-compose.override.macos.yml`, which
> `scripts/orthanc/dc.sh` selects explicitly on Darwin. Delete any local
> `docker-compose.override.yml` — it would wrongly drop host networking on Linux.

---

## Sync-point matrix — values that must match across files

| Logical value | Authoritative home | Also appears in | Kept in sync by |
|---|---|---|---|
| Orthanc service-account password | `.env` `ORTHANC_ADMIN_PASSWORD` | `orthanc_users.json` | `manage_users.py rotate-service-account`; verify with `manage_users.py check-service-account` |
| DB host/port/name/creds | `.env` | `docker-compose.yml` (`${DB_*}`/`${PG_ORTHANC_*}`), `web-app/db.py` | docker-compose `${VAR}` interpolation + `env_file: .env` (automatic) |
| Storage mode + DICOM mount path | `config.toml` `[storage]` | the Orthanc `/dicom-data` bind mount | `scripts/orthanc/dc.sh` exports `DICOM_MOUNT_SOURCE` (automatic) |
| Orthanc HTTP/DICOM ports | `.env` (optional) → `orthanc.json` default | `docker-compose*` | compose interpolation, default `8042`/`4242` |
| Per-host user / repo path / conda bin | `deploy.env` (or auto-derived) | every rendered service unit | the two installers substitute `__TOKENS__` |
| Effective non-secret config | `config.toml` | — | `web-app/config.py` fails fast if absent, WARNs on missing keys, and logs the effective values at startup (`startup: effective config`) |

The first row is the only pair with no fully-automatic enforcement at runtime —
run `python scripts/admin/manage_users.py check-service-account` after any manual
edit (it exits non-zero on mismatch, so it also works from a healthcheck).

---

## Fresh-deploy: what you actually edit

1. **`.env`** — `cp .env.example .env`, fill DB creds, `JWT_SECRET`, service-account password.
2. **`config.toml`** — set `[storage].mode` and the storage paths for this host.
3. **`deploy.env`** *(optional)* — `cp deploy.env.example deploy.env` only if the
   installers' auto-derived user/paths are wrong for this host.

Then run the bring-up (`scripts/orthanc/dc.sh up -d`) and the unit installer
(`scripts/linux/install_systemd.sh` or `scripts/macos/install_launchd.sh`). The
full ordered sequence is in
[`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md) §5.

---

## Precedence rules

- **Secrets** → always `.env`. Never commit them; never duplicate a secret into a
  non-secret file by hand (the one mirror, `orthanc_users.json`, is tool-managed).
- **Non-secret ops** → always `config.toml`. `web-app/config.py` and the shell
  scripts (`config_get`) read it; built-in defaults are last-resort only and
  trigger a WARNING when used.
- **Per-host identity** → auto-derived by the installers and by `scripts/_lib.sh:resolve_python`; `deploy.env` overrides (no interpreter paths are hardcoded in tracked files).
- **Bring the stack up via `scripts/orthanc/dc.sh`**, not bare `docker compose` —
  it is what resolves the DICOM mount from `config.toml` and selects the macOS
  override. Bare `docker compose up` errors that `DICOM_MOUNT_SOURCE` is unset.

---

## Related procedures (don't duplicate them here)

- Secret rotation: [`../operations/secret_rotation.md`](../operations/secret_rotation.md)
- Storage mode / cold cache: [`../cold_storage/runbook.md`](../cold_storage/runbook.md)
- Backups (`[backup]` settings + unit files): [`../operations/backup_strategy.md`](../operations/backup_strategy.md)
- Porting to a new host: [`../operations/cluster_migration.md`](../operations/cluster_migration.md)
- Runtime/packaging facts: [`runtime_and_config.md`](runtime_and_config.md)
