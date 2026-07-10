# Changelog

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
