# Workstream 03 — Security hardening

**Status:** `todo`
**Priority:** `P0`
**Size:** `M` (≈ 3–5 days)
**Owner:** _(unassigned)_
**Dependencies:** none (but benefits from WS 07 for regression safety)

---

## 1. Context

Four security concerns identified by the audit, in descending order of
severity:

1. **SQL injection in snapshot rebuild.** `companion/app.py:1699–1722` builds
   DDL from user-supplied label names with only space/dash sanitization. A
   label named `value; DROP TABLE annotations;--` would execute.
2. **Missing secret validation at startup.** `JWT_SECRET` and
   `ORTHANC_ADMIN_PASSWORD` are read without verification; a blank or
   missing value silently produces a broken but running service.
3. **Cookie hardening.** JWT cookies lack `Secure=True`, session is a
   sliding window with no absolute timeout.
4. **No login rate limiting.** The login endpoint (`app.py:414–417`) accepts
   unlimited attempts — trivial credential stuffing surface.

None of these is a full compromise today — the stack lives behind an SSH
tunnel and users are trusted — but each shifts the system toward accepted
defense-in-depth minimums for a medical research tool.

See `AUDIT_FINDINGS.md` §4.3.

---

## 2. Scope

**In scope:**
- Fix the snapshot-rebuild SQL-injection path.
- Validate required secrets at startup; fail fast with a clear error.
- Add `Secure` cookie flag (config-driven so dev HTTP still works).
- Add an absolute session timeout alongside the existing sliding refresh.
- Per-IP login rate limiting.
- Document a JWT_SECRET rotation procedure.

**Out of scope:**
- Replacing plaintext Orthanc users with a hashed store (upstream
  limitation; needs separate design).
- Migrating from JWT to server-side sessions.
- Two-factor auth.
- Full audit log of admin actions (separate workstream could own this).
- CORS configuration (no cross-origin use case today).

---

## 3. Findings

- **F-03.1** — `companion/app.py:1699–1722` builds SQL via string
  interpolation using label names:
  ```python
  safe_name = ld["name"].replace(" ", "_").replace("-", "_").lower()
  pivot_cols += f", {alias}.value AS label_{safe_name}"
  ```
  Sanitizer is incomplete; e.g. `value; DROP TABLE annotations;--` passes.
- **F-03.2** — `JWT_SECRET` (`app.py:62`) and `ORTHANC_ADMIN_PASSWORD`
  (`app.py:60`) are accepted as `None`/empty with no startup guard.
- **F-03.3** — Cookies set at `app.py:353–358` and `426–432` have
  `httponly=True`, `samesite="lax"`, but no `secure=True`.
- **F-03.4** — `app.py:340–359` slides session expiry on every request, so
  active users never log out.
- **F-03.5** — No rate limiting on `/api/login` (`app.py:414–417`). A
  scripted brute-force on the SSH tunnel endpoint is unbounded.

---

## 4. Tasks

- [ ] **T1** — Fix the snapshot-rebuild SQL construction
  (`companion/app.py:1699–1722`). Use
  `psycopg2.sql.Identifier(safe_name)` instead of string interpolation;
  validate `ld["name"]` against a regex allowlist (e.g.
  `^[a-zA-Z][a-zA-Z0-9_]{0,62}$`) and reject otherwise. Add a unit test that
  feeds a malicious label name and asserts the query is safely parameterized
  (add to pytest once WS 07 lands; for now add as an inline doctest or a
  standalone `companion/tests/test_snapshot_sql.py`).
- [ ] **T2** — Add a `_require_env()` helper at the top of `app.py` that
  raises `RuntimeError("{VAR} must be set")` when a required secret is
  absent or empty. Apply to `JWT_SECRET`, `ORTHANC_ADMIN_USER`,
  `ORTHANC_ADMIN_PASSWORD`, and DB creds.
- [ ] **T3** — Add `COOKIE_SECURE` setting to `config.toml` (default
  `true`; override to `false` in dev). Read it in `config.py`. Apply to
  cookie-setting calls in `app.py:353–358` and `426–432`.
- [ ] **T4** — Add `SESSION_ABSOLUTE_TIMEOUT_HOURS` (default 24) alongside
  the existing `SESSION_TIMEOUT_HOURS`. Put the issuance time (`iat`) in
  the JWT; on every request, reject if `now - iat >
  SESSION_ABSOLUTE_TIMEOUT_HOURS * 3600`. Sliding refresh continues to
  govern the `exp` claim.
- [ ] **T5** — Add `slowapi` to `companion/requirements.txt` (pin the
  version). Wire a per-IP limiter: 10 login attempts per 5 minutes,
  returning 429 with `Retry-After`.
- [ ] **T6** — Write `documentation/operations/secret_rotation.md` (new
  file) covering JWT_SECRET rotation (how to generate a new one, how to
  coordinate rollout so active users aren't all forced to re-login
  simultaneously) and Orthanc admin password rotation (`manage_users.py
  passwd admin`).
- [ ] **T7** — Add a small pre-commit-style grep check to CI (when WS 07
  lands) that flags `f"... {x} ..."` inside strings passed to
  `cur.execute(`, to prevent regression of the SQL-injection pattern.

---

## 5. Acceptance criteria

- [ ] Feeding a malicious label name (e.g. `DROP TABLE annotations;--`) to
  the label creation endpoint either fails validation or produces a
  parameterized query that does not execute as DDL. Verified by a script
  or pytest case.
- [ ] Starting the service with `JWT_SECRET=` (empty) crashes immediately
  with a clear error message, **not** a silent running process.
- [ ] Cookies set under `COOKIE_SECURE=true` include the `Secure` flag (visible
  in browser devtools / curl `-v`).
- [ ] With `SESSION_ABSOLUTE_TIMEOUT_HOURS=24`, a token minted >24 h ago is
  rejected with 401 even if it was recently sliding-refreshed.
- [ ] 11 failed login attempts in 5 minutes from the same IP return 429 on
  the 11th.
- [ ] `secret_rotation.md` exists and is linked from
  `documentation/context.md`.

---

## 6. Verification

```bash
# SQL-injection regression
python stanford-stroke-pacs/companion/tests/test_snapshot_sql.py  # T1

# Startup secret validation
( cd stanford-stroke-pacs && JWT_SECRET= uvicorn companion.app:app --port 18043 ) \
  2>&1 | grep -q 'JWT_SECRET must be set'

# Cookie flag (curl -v shows Set-Cookie header)
curl -v -X POST -d '{"username":"admin","password":"..."}' \
  -H 'Content-Type: application/json' https://pacs.local/api/login 2>&1 | grep 'Secure'

# Absolute timeout (requires a crafted token with old iat — use PyJWT in a REPL)

# Rate limit
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST -d '{"username":"x","password":"y"}' \
    -H 'Content-Type: application/json' \
    http://localhost:8043/api/login
done | tail -3  # should see 429 for the last two
```

---

## 7. Rollback

Pure code changes under version control. `git revert <sha>`. Remove the
`slowapi` dependency from requirements if reverting T5. Set
`COOKIE_SECURE=false` in `config.toml` if the Secure flag breaks a
specific dev flow, but prefer keeping it on and fixing the dev flow.

---

## 8. Files touched

- `stanford-stroke-pacs/companion/app.py` (edit — T1, T2, T4, T5)
- `stanford-stroke-pacs/companion/config.py` (edit — T3, T4)
- `stanford-stroke-pacs/config.toml` (edit — add
  `cookie_secure`, `session_absolute_timeout_hours`)
- `stanford-stroke-pacs/companion/requirements.txt` (edit — add pinned
  `slowapi==X.Y.Z`)
- `stanford-stroke-pacs/companion/tests/test_snapshot_sql.py` (new, or move to
  `companion/tests/` once WS 07 creates the tree)
- `stanford-stroke-pacs/documentation/operations/secret_rotation.md` (new)
- `stanford-stroke-pacs/documentation/context.md` (edit — add link)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Label-name validation rejects legitimate names in existing DB | med | med | Audit existing `label_definitions.name` before landing; grandfather old names via a one-time migration rename |
| `Secure` flag breaks dev over plain HTTP | high (in dev) | low | `COOKIE_SECURE=false` in dev config.toml |
| Absolute timeout forces users to re-login mid-session | high | low | Document behavior; 24 h default is forgiving |
| Rate limit traps legitimate users behind a NAT | low | med | Make threshold configurable; use `X-Forwarded-For` if behind a reverse proxy |
| `slowapi` introduces transitive deps | low | low | Pin version; review its transitive footprint |

---

## 10. Notes

- Once WS 07 (CI/testing) lands, migrate the inline test scripts into the
  pytest suite and add a CI check for `cur.execute(f"...")` anti-patterns.
- The sliding-session + absolute-timeout pattern is the right default but be
  aware it breaks "keep me logged in forever" UX. Align with the user if
  there's a product preference.
- Plaintext Orthanc users is a known limitation of the Orthanc image's
  built-in auth. A longer-term fix is to put Orthanc behind the Companion's
  auth via a reverse proxy, which is a separate workstream not scoped here.
