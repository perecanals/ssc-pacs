# Changelog

## v1.3 — 2026-07-10

- **Fix**: `rotate_db_password.py` now handles the case where the `DB_USER`
  role is also Orthanc's PostgreSQL index user (`PG_ORTHANC_USER` — the default
  here). A Postgres role has one password, so `ALTER ROLE` changes it for both;
  `rotate` now rewrites `PG_ORTHANC_PASSWORD` alongside `DB_PASSWORD` and prints
  a `docker restart ssc-orthanc` reminder, and `check` verifies the `orthanc_db`
  connection too. Without this, rotating would have silently broken Orthanc's
  index connection. No schema change.

## v1.2 — 2026-07-10

- **Scripts**: split credential rotation out of `manage_users.py` into two
  dedicated, same-shaped tools — `scripts/admin/rotate_service_account.py`
  (Orthanc service account: `.env` + `orthanc_users.json`) and the new
  `scripts/admin/rotate_db_password.py` (`DB_PASSWORD`: `ALTER ROLE` on the live
  DB + `.env`). Each exposes `rotate [--generate]` / `check`; the secret is only
  ever prompted (hidden) or generated, never placed on the command line.
  Shared secret mechanics moved to `scripts/admin/_secret_helpers.py` (also fixes
  a latent `.env`-rewrite bug where a `\` in a secret was mis-interpreted as a
  regex backreference). The `rotate-service-account` / `check-service-account`
  subcommands were removed from `manage_users.py`. No schema change.
- **Docs**: rewrote `operations/secret_rotation.md` §2–3 and updated the command
  and reference docs to the new script invocations.

## v1.1 — 2026-07-10

- **Ops**: added non-destructive whole-stack stop/start helpers for both
  platforms — `scripts/{linux,macos}/stop_stack.sh` and `start_stack.sh`. They
  pause/resume every service in dependency order (macOS handles the
  watchdog-before-`colima stop` ordering; Linux leaves shared dockerd + host
  Postgres running), support `--dry-run`, and a `--retire`/`--enable` pair to
  toggle boot autostart. Distinct from the destructive `admin/teardown.sh`.
- **Docs**: documented "Stopping / retiring the stack" in the macOS and Linux
  deploy guides and the day-2 commands cheat sheet. No schema change.

## v1.0 — 2026-07-08

First tagged release, cutting over from the pre-tag history. Consolidates a
full repo audit across code, database, ingestion, scripts, setup, and docs.

- **Performance**: patient/study list pages are ~100× faster — added the
  missing relational indexes on `image_series`/`image_study` (Alembic 0011).
- **Fixes**: `/healthz` now reports a real version; the admin "latest
  reconciliation" endpoint works; warm buttons render correctly under React 19;
  the cold-storage health probe no longer hangs; ingestion has path-safety
  guards on its delete paths.
- **Database** (Alembic 0011–0014, applied at app startup): relational indexes;
  size-column bootstrap for fresh installs; dropped the retired snapshot tables;
  annotation index cleanup.
- **Cleanup**: retired the snapshot feature (tables + endpoint + UI button) in
  favor of the labelled mirror tables; triaged the scripts inventory (broken
  tools archived, one-offs relocated under `maintenance/`, dry-run-by-default on
  mutating scripts); deleted the vestigial `environment.yml`.
- **Quality**: test suites grew to 182 backend + 110 frontend + 69 ingestion
  (incl. a gated end-to-end); `make lint` now covers backend, scripts, and
  ingestion plus ESLint, with CI wired to match.
- **Docs**: the fresh-deploy guide works end to end; `CLAUDE.md` trimmed to a
  180-line index; every doc re-verified against the live system and routed via
  `docs/context.md`.
