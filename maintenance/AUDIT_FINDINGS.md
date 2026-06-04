# PACS maintainability audit — consolidated findings

**Date:** 2026-04-15
**Scope:** `/home/perecanals/pacs/` (stanford-stroke-pacs + orthanc-indexer-patched + ssc-sql-db)
**Method:** four parallel audit passes covering backend code, frontend code,
docs/deps/deployment, and data/security/ops.

This document preserves the raw audit for reference. Each workstream file in
`workstreams/` cites the relevant sections here. Read the workstream file first;
come back here for context if a finding seems ambiguous.

---

## Overall grade: **C+**

The system works and has unusually good documentation, but several fragile
spots will bite a future maintainer. The top three risks are catastrophic in
nature (data loss, unreproducible rebuilds, one SQL injection path) and
disproportionately cheap to fix.

---

## Top risks (the "fix first" list)

1. **No backup/DR for cold archives or PostgreSQL.** Single copy on `/DATA2/`.
   No WAL archiving, no snapshot schedule, no RTO/RPO. → WS 01.
2. **Unpinned Python deps + `orthancteam/orthanc:latest`.** A fresh rebuild is
   not reproducible. → WS 02.
3. **SQL injection vector in snapshot rebuild** (`web-app/app.py:1699–1722`):
   label names are only sanitized for spaces/dashes before being interpolated
   into SQL. → WS 03.
4. **No schema migration tool.** `INIT_SQL`/`MIGRATE_SQL` embedded in `app.py`
   (lines 70–277) run on every boot behind `DO $$` idempotent guards. → WS 04.
5. **Cold-storage failure modes leak state.** Crashes mid-warm leave
   `cache_state='warming'` with no timeout; failed `rmtree` still deletes the
   DB row → orphaned files. → WS 05.

---

## 1. Backend code quality

**Source report:** agent audit 2026-04-15.

### 1.1 Monolith vs modular split — LARGE KITCHEN SINK

- `web-app/app.py` is **1,865 lines** — all routes, DB helpers, filter SQL
  generators, annotation logic, snapshot management in one file.
- **29 routes** (18 GET, 8 POST, 1 PUT, 1 DELETE, 1 middleware) bundled
  directly on the `FastAPI` instance. No `APIRouter` abstraction.
- Helper modules exist (`config.py`, `cache_manager.py`,
  `labelled_table_sync.py`) but `app.py` repeats DB config and patterns
  internally rather than leveraging them.

### 1.2 Error handling — INCONSISTENT AND DANGEROUS

- Bare `except Exception` blocks in `cache_manager.py:242,288,295,308,313,370`
  and `app.py:312,1374`.
- Silent failures in background eviction loop (`app.py:312–313`): logs but
  doesn't propagate — masks failures.
- Nested bare excepts in cleanup blocks cascade silencing.
- HTTPException coverage is good (21+ explicit raises).
- No validation that required secrets are set — `JWT_SECRET` (`app.py:62`) and
  `ORTHANC_PASS` (`app.py:60`) accepted as `None`.

### 1.3 Logging and observability — MINIMAL, NO STRUCTURE

- Single logger: `_log = logging.getLogger("uvicorn.error")` (`app.py:68`).
- Only 2 log calls in app.py: eviction loop (line 311) and exception (line
  313).
- No structured logging, no metrics, no tracing.
- No health endpoint beyond `/api/me`.

### 1.4 Database layer — RAW SQL, NO ORM, WEAK MIGRATIONS

- Raw psycopg2 throughout. 56+ `cur.execute()` calls in `app.py` alone.
- `_label_filter_sql` / `_label_value_filter_sql` /
  `_label_select_values_filter_sql` / `_label_bool_filter_sql`
  (`app.py:522–719`) are ~95% duplicated.
- No connection pooling. `get_conn()` creates a fresh connection per request.
- No schema migration tool. `INIT_SQL` embedded in code (lines 70–277).
- Advisory locks (`cache_manager.py:86–90, 154–156`) used correctly for warm
  serialization.
- Transaction handling inconsistent — some endpoints commit multiple times in
  one try block.

### 1.5 Type hints and docstrings

- Type hints generally present. 4 Pydantic models (`LoginRequest`, `PrefsBody`,
  `AnnotationCreate`, `LabelDefinitionCreate`).
- API endpoints have no docstrings.
- No mypy config (no `pyproject.toml`, no `setup.cfg`, no `.mypy.ini`).

### 1.6 Code duplication

- `DB_CONFIG` redeclared in `app.py` (line 50), `cache_manager.py` (line 26),
  and most scripts under `scripts/` and the top level.
- SQL filter generation (above) is 4× duplicated.
- Annotation UPSERT queries centralized in `_UPSERT_SQL` dict
  (`app.py:1427–1461`) — good.

### 1.7 Module coupling

- No circular imports.
- Scripts import `config.py` via `sys.path` hack
  (`scripts/cold_storage/cleanup_loose_dicoms.py:49`, `scripts/cold_storage/archive_all_series.py:31`) — breaks IDE
  tooling.
- Global state via independently-defined `DB_CONFIG` in each module.

### 1.8 Secrets and config

- `.env` via `load_dotenv` used consistently.
- No validation that required secrets are present at startup.
- `manage_users.py:100–106` syncs admin password into `.env` via regex —
  brittle.

### 1.9 Concurrency

- Only the eviction loop is async; all endpoints are sync.
- `_eviction_loop` uses `asyncio.sleep(900)` (15 min polling) — coarse but OK.
- Advisory locks in `warm_study()` are correct.
- No explicit transaction isolation level set.
- Temp dir cleanup (`cache_manager.py:291–296`) ignores failures — orphans
  possible.

### 1.10 Scripts

- `manage_users.py` is the cleanest (argparse subcommands, coherent CLI).
- Most scripts are idempotent.
- No shared utility module — `DB_CONFIG` duplicated 4+ times.

---

## 2. Frontend code quality

**Source report:** agent audit 2026-04-15.

### 2.1 Component sizing

- **`DataTable.jsx`: 1,272 lines** — god-component. Handles API fetching,
  filtering, sorting, pagination, inline editing, expandable rows, and column
  management.
- `Navigator.jsx`: ~180 lines (page container, reasonable).
- `InlineEdit.jsx`: 324 lines.
- `LabelDefModal.jsx`: 207 lines.
- `Sidebar.jsx`: 169 lines.
- `TopBar.jsx`: 87 lines.
- `PreviewPane.jsx`: 74 lines.

### 2.2 State management

- React Context for auth only (`AuthContext`).
- `Navigator.jsx` holds 9+ state vars, prop-drills through 5+ props.
- No Redux or Zustand.

### 2.3 API layer

- Centralized in `src/api/client.js` (`apiFetch`, `apiGet`, `apiPost`,
  `apiDelete`).
- 401 handling dispatches a custom event. Other errors are inconsistent.

### 2.4 Type safety

- Plain JavaScript.
- **Zero PropTypes.**
- Component APIs implicit.

### 2.5 Testing

- **No tests.** No test framework in `package.json`.

### 2.6 Styling

- Tailwind 4.0 with custom design tokens in `app.css` (11 color vars).
- 11 component-scoped CSS files using `@apply`.
- Minimal inline styles (4 instances).

### 2.7 Build

- `package.json`: React 19.0.0, React Router 7.1.0 — current.
- Vite 6.0.0, Tailwind 4.0.0 — clean.
- `vite.config.js` is simple (plugins + proxy).

### 2.8 A11y and i18n

- Only 2 `aria-label`/`role` attrs total.
- No alt text on icons.
- No i18n framework; all strings English.

### 2.9 Code duplication

- `hashStr()` and `valueColor()` + `NOTION_COLORS` duplicated between
  `InlineEdit.jsx:19–26` and `LabelDefModal.jsx:19–26`.
- Utilities in `DataTable` (`formatDatetime`, `normalizeSelectFilterValues`,
  `buildBuiltinColumnCatalog`) not extracted.
- Form patterns repeated (login in TopBar, filter selects in Sidebar).

---

## 3. Docs, dependencies, deployment

**Source report:** agent audit 2026-04-15.

### 3.1 Documentation — GOOD

- 18 modular markdown files under `documentation/`, indexed by
  `documentation/context.md`.
- Changelog current (last entry 2026-04-09).
- No `TODO` markers, no stale dates, no broken links found.
- `installation_and_deployment.md` is 469 lines and thorough.

### 3.2 Missing docs

- **No top-level `README.md`** in `/home/perecanals/pacs/` or
  `stanford-stroke-pacs/`.
- **No backup/DR docs** despite being critical for a medical imaging system.
- `history/` retains 3 old implementation plans marked outdated.

### 3.3 Python deps — CRITICAL

- `/home/perecanals/pacs/requirements.txt` — 7 packages, **no versions pinned**
  (pandas, python-dotenv, SQLAlchemy, psycopg2-binary, numpy, pydicom,
  SimpleITK).
- `stanford-stroke-pacs/web-app/requirements.txt` — 8 packages, **no
  versions pinned** (fastapi, uvicorn, psycopg2-binary, requests, PyJWT,
  bcrypt, python-dotenv, zipstream-ng, zstandard).
- No `pyproject.toml`, no `setup.py`, no constraints file.

### 3.4 Node deps — GOOD

- `package.json` uses `^` semver ranges.
- `package-lock.json` present (2,456 lines).
- Modern stack, no deprecated packages.
- `installation_and_deployment.md:228` uses `npm install` rather than
  `npm ci` — doesn't enforce lock file.

### 3.5 Docker — MIXED

- `docker-compose.yml:5` and `orthanc-indexer-patched/Dockerfile:74`:
  `orthancteam/orthanc:latest` — **not pinned**.
- Patched indexer Dockerfile uses `ubuntu:25.10` (OS pinned) and
  `ORTHANC_FRAMEWORK_DEFAULT_VERSION=1.12.3` (framework pinned). Reproducible.
- Risk: Orthanc upstream changes could break the patched plugin ABI silently.

### 3.6 Environment reproducibility — PARTIAL

- `.env.example` documents 9 required secrets. Good.
- `config.toml` documents non-secret tuning. Good.
- `pacs` conda env: **no `environment.yml` exported** anywhere. If the host
  is replaced, the env must be reverse-engineered.

### 3.7 systemd — HARDCODED PATHS

- `ssc-web-app.service` is in repo but contains hardcoded paths:
  `User=perecanals`, `WorkingDirectory=/home/perecanals/pacs/...`, conda env
  path in `ExecStart`.
- No templating. Each deployment must manually edit.

### 3.8 CI/CD — ABSENT

- No `.github/workflows`, no `.gitlab-ci.yml`, no pre-commit hooks.
- No automated tests, linting, or build gates.

### 3.9 Hardcoded paths and personal fingerprints

Files with hardcoded `/home/perecanals/` or personal identifiers:

- `init_orthanc_db.sh:10` — `source "/home/perecanals/pacs/.../.env"`
- `scripts/connectivity/tunnel.sh:1` — hardcoded server IP `10.110.128.149` + user
  `perecanals@`
- `ssc-web-app.service` — all paths under `/home/perecanals/`
- `docker-compose.yml` — absolute env_file path

No `TODO: Pere` markers in code proper.

---

## 4. Data model, security, operations

**Source report:** agent audit 2026-04-15.

### 4.1 Schema management — HIGH RISK

- `INIT_SQL` + `MIGRATE_SQL` embedded in `web-app/app.py:70–277`.
- `init_db()` (`app.py:293–302`) runs on every startup, guarded by PL/pgSQL
  `DO $$` blocks for idempotency.
- Failure mid-migration leaves DB in partial state; no version table.
- `ssc-sql-db/` contains read-only DDL for `image_study` and `image_series` —
  not managed by the web app.
- Upstream schema changes could break web app assumptions silently.

### 4.2 Two-DB integrity — MEDIUM/HIGH

- No FK from `stanford-stroke.image_series` → `orthanc_db`.
- Cold-storage eviction (`cache_manager.py:322–346`) removes files but can
  crash before `cache_state` update; Orthanc index still points to missing
  paths.
- No reconciliation mechanism.

### 4.3 Security — HIGH RISK

**Authentication:**
- JWT: `app.py:366–380`. Secret is a static env var loaded at startup.
- Session sliding window (`app.py:340–359`) refreshes cookie on every request
  except `/api/me` — indefinite sessions for active users.
- bcrypt cost factor is library default (12).
- No rate limiting on login endpoint (`app.py:414–417`).

**SQL injection:**
- Parameterized queries everywhere via `%s` placeholders. **Except**
  `app.py:1699–1722` (snapshot rebuild), which builds DDL from user-supplied
  label names with only space/dash sanitization. A label named
  `value; DROP TABLE annotations;--` would execute.

**Cookie security:**
- `app.py:353–358, 426–432`: `httponly=True`, `samesite="lax"`.
- **Missing `Secure=True` flag.**

**Orthanc:**
- Admin creds flow through web app env vars.
- Orthanc users stored as plaintext per `orthanc_users.json` (known
  limitation).

**CORS/rate limiting:** none.

**Path traversal:** low risk — archive paths validated via `.resolve()` and
relative-to checks (`cache_manager.py:41–60`). `dicom_dir_path` from DB is used
directly — requires DB compromise first.

### 4.4 Cold-storage operational risks — HIGH

**Warming (`cache_manager.py:134–320`):**
- Advisory locks serialize per-study. Good.
- Extraction to `.warming` temp dir with atomic rename. Good.
- If process dies after rename but before `finish()` (lines 147–150), study
  stays `'warming'` forever — no timeout to recover.
- Hot-check (lines 163–182) masks the problem on subsequent attempts.

**Eviction (`cache_manager.py:322–346`):**
- 15-minute polling (line 307) via async `_eviction_loop`.
- TTL: `last_accessed_at < (now() - EVICTION_TTL_HOURS)` (lines 361–362).
- **Not transactional.** If `shutil.rmtree` fails, the `cache_state` row is
  still deleted (lines 340–343). Orphaned files, no alert.

**Disk space:**
- No precheck. If disk fills during extraction, `untar_zst` fails, study stays
  `warming`, partial files may remain.

### 4.5 Observability — CRITICAL GAP

- One logger, two call sites in `app.py`.
- No context in log lines (no study UID prefix).
- No `/healthz` endpoint.
- No metrics, no alerts.
- Failures detected only by user reports.

### 4.6 Data loss risks

- **Annotations hard-deleted** (`app.py:1521`). No audit trail.
- **User preferences CASCADE DELETE** on username. Lost when user removed.
- **Cold archives single copy.** No replication, no backups. **CRITICAL.**
- **Orthanc index**: no documented backup frequency.

### 4.7 `image_integration_protocols/` — MEDIUM RISK

- Documented as "legacy" and "site-specific" but still actively used (logs
  dated March–April 2026).
- Hardcoded DICOM layouts specific to SSC.
- No schema validation on upserts — would fail silently if web app adds NOT
  NULL columns.

---

## Workstream mapping

Each finding above maps to one or more workstreams:

| Finding cluster | Workstream(s) |
|---|---|
| Backup/DR (§4.6, §3.2) | 01 |
| Unpinned deps, conda env, Docker (§3.3, §3.5, §3.6) | 02 |
| SQL injection, cookie flags, secret validation, rate limiting (§4.3) | 03 |
| Schema migrations (§4.1, §1.4) | 04 |
| Cold-storage robustness (§4.4, §1.2, §1.9) | 05 |
| Structured logging, metrics, /healthz (§4.5, §1.3) | 06 |
| CI, pytest, vitest, pre-commit (§3.8, §2.5) | 07 |
| Hardcoded paths, systemd templating (§3.7, §3.9) | 08 |
| `app.py` split, SQL helper dedup, connection pool (§1.1, §1.4, §1.6) | 09 |
| `DataTable.jsx` split, PropTypes, util extraction (§2.1, §2.4, §2.9) | 10 |
| Two-DB reconciliation (§4.2) | 11 |
| Annotation audit trail (§4.6) | 12 |
