# Workstream 09 — Backend refactor (split `app.py`, dedupe SQL helpers, add connection pool)

**Status:** `todo`
**Priority:** `P2`
**Size:** `L` (≈ 1–2 weeks)
**Owner:** _(unassigned)_
**Dependencies:** **WS 07 (tests) must land first** — do not refactor without
a safety net.

---

## 1. Context

`web-app/app.py` is 1,865 lines and contains 29 route handlers plus filter
SQL generators, snapshot management, annotation logic, and DB helpers. Any
feature change today requires scrolling through unrelated code. Four
near-duplicate filter-SQL functions (`_label_filter_sql`,
`_label_value_filter_sql`, `_label_select_values_filter_sql`,
`_label_bool_filter_sql`) repeat ~95% of the same nine-branch logic across
lines 522–719.

Separately, `DB_CONFIG` is redefined in `app.py` (line 50),
`cache_manager.py` (line 26), and most scripts. Each psycopg2 connection is
created per-request (`get_conn()`), with no pooling — fine at current load
but pathological under concurrent use.

This workstream is a pure refactor: no behavior changes, no new endpoints,
no schema changes. Its goal is to make the next ten workstreams cheaper.

See `AUDIT_FINDINGS.md` §1.1, §1.4, §1.6.

---

## 2. Scope

**In scope:**
- Split `app.py` into `APIRouter` submodules under
  `web-app/routes/`.
- Extract shared DB helpers (`DB_CONFIG`, `get_conn()`, Orthanc client)
  into `web-app/db.py` and `web-app/orthanc_client.py`.
- Remove duplicate `DB_CONFIG` definitions from `cache_manager.py` and all
  scripts.
- Collapse the four `_label_*_filter_sql` helpers into a single
  parameterized function.
- Add a psycopg2 connection pool (`ThreadedConnectionPool`).

**Out of scope:**
- Migrating to SQLAlchemy (ORM) — keep raw SQL.
- Adding new endpoints or changing response shapes.
- Changing authentication behavior (WS 03 owns that).
- Changing schema (WS 04 owns that).

---

## 3. Findings

- **F-09.1** — `web-app/app.py` is 1,865 lines; 29 routes, no routers.
- **F-09.2** — `_label_filter_sql` (lines 522–575), `_label_value_filter_sql`
  (576–625), `_label_select_values_filter_sql` (626–680),
  `_label_bool_filter_sql` (681–719) are 95% duplicated.
- **F-09.3** — `DB_CONFIG` redefined in `app.py:50`, `cache_manager.py:26`,
  `label_studies.py:25`, `enrich_orthanc.py:28`, etc.
- **F-09.4** — `get_conn()` opens a fresh psycopg2 connection per request
  with no pooling.
- **F-09.5** — Scripts import `config.py` via `sys.path` hacks (e.g.
  `scripts/cold_storage/cleanup_loose_dicoms.py:49`, `scripts/cold_storage/archive_all_series.py:31`).

---

## 4. Tasks

### Phase A — extract shared infrastructure

- [ ] **T1** — Create `web-app/db.py` with:
  - `DB_CONFIG` loaded from `config.py` (single source of truth),
  - `get_pool()` returning a `ThreadedConnectionPool`,
  - `get_conn()` context manager that acquires from the pool,
  - unit tests (requires WS 07) covering pool exhaustion and connection
    recycling.
- [ ] **T2** — Create `web-app/orthanc_client.py` wrapping all existing
  Orthanc REST calls (collect from `app.py` grep). Functions take a
  session object so tests can mock it.
- [ ] **T3** — Replace `DB_CONFIG` + `get_conn()` in `app.py`,
  `cache_manager.py`, and every script under `scripts/` and the top
  level (`label_studies.py`, `enrich_orthanc.py`, `verify_indexing.py`,
  `manage_users.py`) with imports from `web-app/db.py`.
- [ ] **T4** — Turn `web-app/` into a proper importable package:
  ensure `__init__.py` exists; fix the `sys.path` hack in scripts by
  adding `web app` to `PYTHONPATH` via a small wrapper or by
  converting scripts to `python -m web app.scripts.cleanup_loose_dicoms`
  style. Document the new invocation in `documentation/operations/commands.md`.

### Phase B — collapse duplicated SQL helpers

- [ ] **T5** — Design a single `build_label_filter_sql(level, label_id,
  mode, value)` function that encompasses the four existing variants.
  `mode` is an enum (`EQ`, `ANY`, `BOOL`, etc.). Same input surface,
  one implementation, one set of tests.
- [ ] **T6** — Replace call sites in `app.py` with the new function.
  Diff the emitted SQL against the old versions on representative
  inputs; paste the side-by-side diff into the PR description.
- [ ] **T7** — Delete the four old helpers.

### Phase C — split app.py into routers

- [ ] **T8** — Create `web-app/routes/__init__.py` and empty module
  files for each logical grouping:
  - `auth.py` — `/api/login`, `/api/logout`, `/api/me`, session refresh
  - `annotations.py` — `/api/annotations/*`
  - `labels.py` — `/api/label-definitions/*`
  - `studies.py` — `/api/studies/*`, `/api/patients/*`, `/api/series/*`,
    `/api/ohif-link/*`
  - `preferences.py` — `/api/preferences/*`
  - `cold_storage.py` — `/api/studies/{uid}/warm`, `/evict`,
    `/cache-status`
  - `admin.py` — any admin-only endpoints (health, reconciliation; WS 11
    may add more)
  - `static.py` — SPA fallback / static file serving
- [ ] **T9** — Move route handlers one router at a time, running tests
  after each move. Each router file should own its Pydantic models and
  helpers; cross-router helpers go to `web-app/common.py`.
- [ ] **T10** — Rewire `app.py` to include each router:
  ```python
  app.include_router(auth.router)
  app.include_router(annotations.router)
  ...
  ```
  Target `app.py` ≤ 300 lines: app instantiation, middleware, lifespan,
  router registration, static file mounts.
- [ ] **T11** — Preserve every existing URL path exactly. Add a pytest
  fixture that snapshots the route table (`app.routes`) before and
  after and asserts equality.

### Phase D — verify

- [ ] **T12** — Run full pytest + vitest + `npm run build` + `docker
  compose up -d` smoke. All green.
- [ ] **T13** — Run a 5-minute soak with concurrent requests (ab,
  `curl`-in-a-loop, or a simple asyncio script) and verify the
  connection pool doesn't exhaust under realistic load.

---

## 5. Acceptance criteria

- [ ] `app.py` ≤ 300 lines.
- [ ] No file imports `DB_CONFIG` or builds one inline except
  `web-app/db.py`.
- [ ] The four `_label_*_filter_sql` helpers are gone; a single
  replacement exists.
- [ ] Connection pool is initialized at app startup and released at
  shutdown via FastAPI lifespan.
- [ ] All pytest tests remain green; no endpoint behavior change.
- [ ] URL table is identical before and after (snapshot test passes).

---

## 6. Verification

```bash
# Line counts
wc -l stanford-stroke-pacs/web-app/app.py
wc -l stanford-stroke-pacs/web-app/routes/*.py

# No stray DB_CONFIG
grep -rn 'DB_CONFIG = ' --include='*.py' stanford-stroke-pacs/ \
  | grep -v 'web-app/db.py'   # should be empty

# Tests
cd stanford-stroke-pacs/web-app && pytest -x --cov

# Route-table snapshot
pytest -k test_route_table

# Smoke
curl http://localhost:8043/api/me
curl http://localhost:8043/api/label-definitions
# ... etc; every old path still 200s
```

---

## 7. Rollback

Refactor lives on a feature branch. If any post-merge regression
surfaces, `git revert` the merge commit. Because schema is unchanged,
rollback is pure code.

---

## 8. Files touched

- `stanford-stroke-pacs/web-app/app.py` (heavy edit — shrink to
  ≤300 lines)
- `stanford-stroke-pacs/web-app/db.py` (new)
- `stanford-stroke-pacs/web-app/orthanc_client.py` (new)
- `stanford-stroke-pacs/web-app/common.py` (new — optional)
- `stanford-stroke-pacs/web-app/routes/__init__.py` (new)
- `stanford-stroke-pacs/web-app/routes/auth.py` (new)
- `stanford-stroke-pacs/web-app/routes/annotations.py` (new)
- `stanford-stroke-pacs/web-app/routes/labels.py` (new)
- `stanford-stroke-pacs/web-app/routes/studies.py` (new)
- `stanford-stroke-pacs/web-app/routes/preferences.py` (new)
- `stanford-stroke-pacs/web-app/routes/cold_storage.py` (new)
- `stanford-stroke-pacs/web-app/routes/admin.py` (new)
- `stanford-stroke-pacs/web-app/routes/static.py` (new)
- `stanford-stroke-pacs/web-app/cache_manager.py` (edit — use
  `db.get_conn`)
- `stanford-stroke-pacs/scripts/*.py` (edit — use `db.get_conn`, fix
  sys.path)
- `label_studies.py`, `enrich_orthanc.py`, `verify_indexing.py`,
  `manage_users.py` (edit)
- `stanford-stroke-pacs/documentation/operations/commands.md` (edit — new
  invocation patterns)
- `stanford-stroke-pacs/web-app/tests/test_route_table.py` (new)
- `stanford-stroke-pacs/web-app/tests/test_label_filter_sql.py` (new)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Moving a route accidentally changes its behavior | med | high | WS 07 tests + route-table snapshot test (T11) |
| SQL consolidation (T5) produces slightly different SQL for one input | med | med | Parametric diff test; manual review of emitted SQL |
| Pool sizing wrong (too small → deadlock, too large → pg too many) | med | med | Start with `minconn=2, maxconn=10`; tune under soak |
| `sys.path` hack replacement breaks some script invocation | high | low | Document the new invocation; keep a compatibility shim for a release |
| Refactor takes longer than estimate due to incidental discovery | high | low | Timebox each phase; land phase A before moving to B |

---

## 10. Notes

- **Sequence Phases A → B → C → D.** Each is independently shippable,
  reducing risk.
- Some modules may grow during the split (labels and annotations are
  closely coupled via `label_definitions`). Prefer a "feature"
  boundary over strict "resource" boundary when they conflict.
- Avoid the temptation to fix unrelated issues during this refactor
  ("while I'm in here..."). Those belong in their own workstreams.
- The connection pool can trip up `psycopg2` autocommit behavior.
  Wrap every acquisition in an explicit transaction block.
