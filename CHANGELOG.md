# Changelog

## v1.6 — 2026-07-13

- **Feature**: the classifier's verdicts are now visible in the web app, as
  read-only **Auto Series Type** (series) and **Auto Timepoint** (study, and on
  the series table too) columns. Both are filterable and sortable. No migration —
  the columns landed in `0015_series_classification`; this only surfaces them.
- They render as *muted, outlined, non-clickable* pills, deliberately unlike the
  filled editable pills of the human `series_type` / `timepoint` annotation
  labels sitting in adjacent columns. The two remain independent axes; the web
  app selects and filters the machine columns but never writes them.
- **Auto Series Type** carries the per-patient preference rank as a superscript
  badge (rank 1 bolded = the series of that type to use). The column filter
  matches `series_label`, so `NCCT` finds all 3,714 NCCTs and `NCCT_1` isolates
  the 1,450 preferred ones — one per patient. Sorting keys on `series_label`
  (type, then rank).
- An **excluded** series (NULL `series_type` + a `series_type_rule` — most of the
  corpus) shows a faint `—` whose tooltip names the exclusion that fired, rather
  than an empty cell. A blank cell now means genuinely unclassified. Hovering any
  Auto pill gives its provenance (rule + version, or anchor + signed hours).
- An **estimated** timepoint (anchor `receiving_arrival_time` / `time_recognized`
  rather than a recorded puncture) is drawn with a dashed border and a `~`, so
  the estimate is visible without hovering.
- Both columns are on **by default, including for users who already have saved
  column preferences** — `utils/table.js` gains `COLUMN_DEFAULTS_VERSION` and
  per-column `introducedIn`; `useColumnPrefs` merges newly-introduced columns into
  saved prefs exactly once and stamps `prefs.defaultsVersion`, so hiding one
  sticks. Reuse this for any future default-on built-in column.
- Auto Timepoint is the exception: hidden by default on series **sub-rows** (the
  parent study row already shows it), on by default in the flat series table,
  where no parent row carries it. A column's `defaultVisible` may now be a
  predicate on the active level, not just a boolean.
- Internal: the built-in cell renderer, previously duplicated across `index.jsx`
  and both tables in `ChildRows.jsx`, is extracted to `DataTable/BuiltinCell.jsx`.

## v1.5 — 2026-07-13

- **Fix**: series ingested directly into `cold_path_cache` could become
  permanently unopenable in OHIF (endless loading spinner). Orthanc's DICOMweb
  plugin builds each series' WADO-RS metadata cache by *reading the DICOM
  files*, in a background worker that fires when the series goes stable —
  but ingestion deleted the loose files as soon as the indexer had registered
  them, with no grace period. Series that lost that race got an empty metadata
  cache, which never expires (the index still points at the absent files by
  design, `RemoveMissingFiles: false`), so every later metadata request
  returned HTTP 400. It hit ~55% of the July CRISP2/LVO batch (19,658 series /
  342 patients); the legacy→cold migration cohort was unaffected because its
  files sat on disk while the cache was built.
- `scripts/cold_storage/cleanup_loose_dicoms.py` gains safety check 5: loose
  DICOMs are not deleted until Orthanc has built a non-empty metadata cache for
  the series (`--metadata-cache-timeout`, default 120s). Guards both the
  ingestion pipeline and the manual CLI. `orthanc.json` sets `"StableAge": 10`
  (was Orthanc's 60s default) so that wait is short — **requires an Orthanc
  restart**.
- `cache_manager.warm_series` now rebuilds a poisoned metadata cache whenever a
  series is warmed, so any affected series self-heals the first time it is
  opened.
- **New**: `scripts/data_integrity/repair_dicomweb_metadata_cache.py` — repairs
  the existing backlog (warm → rebuild → evict, batched; dry-run by default,
  `--hot-only` repairs already-warm series at no extraction cost).

## v1.4 — 2026-07-13

- **Schema** (Alembic `0015_series_classification`, one revision): new `series_dicom_tags` table — one row per
  series holding the full DICOM tag set of a representative instance as
  GIN-indexed `jsonb`, plus the cross-instance aggregates no single header carries
  (`same_position_count`, `distinct_kernels`, …) and 21 `GENERATED` columns
  projected out of the blob (modality, convolution_kernel, slice_thickness, …), so
  it is queryable as a table without a 794-column schema. PHI tags are deliberately
  not promoted. Adds classification provenance (`series_type_rule`,
  `series_type_version`), the study `timepoint` axis, and `series_type_rank` +
  `series_label`. **The GENERATED columns rewrite `series_dicom_tags` (~6.5 min on 131k rows) —
  run `alembic upgrade head` before restarting the web app on a populated DB.**
  Backfill: `maintenance/scripts/backfill_series_dicom_tags.py` (~48 min, idempotent).
- **Feature**: rule-based series/study classification
  (`image_ingestion_protocols/series_classification.py`), applied at ingest and
  re-runnable via `scripts/admin/reclassify_series_types.py` (dry-run by default).
  Because it reads `series_dicom_tags` rather than the archives, recomputing the
  whole corpus is a ~30s table scan, not a 48-minute disk sweep.
  - **Emitted types** are the reference implementation's five (`NCCT`, `CTA`,
    `CTP`, `PWI`, `DWI`) plus `ADC`, `MRA_TOF`, `MRA_CE`. Everything else in his
    taxonomy — bone, dual-energy, topogram, test bolus, RAPID output, projections,
    CT reformats, DSA — stays an **exclusion**, not a type: `series_type` is NULL
    and `series_type_rule` records which exclusion fired. His criteria are used
    verbatim, including the ≥80-instance CTA and ≥10-instance NCCT minimums and
    the 14-frame CTP floor.
  - **Rank + display label**: `series_type_rank` and the combined `series_label`
    (`NCCT_1`, `CTA_2`) reproduce his per-patient preference ordering — CTA
    thinnest-slice first, NCCT thickest first, the rest chronological. `series_label`
    is the column to display.
  - **MR angiography**: 2,140 `MRA_TOF` + 183 `MRA_CE`. Contrast state comes from
    the description (`+C`, `Gad`), not `ContrastBolusAgent` — that tag is empty on
    the gado carotid studies.
- **Data fix**: the existing `series_type` values were substantially wrong, not
  merely sparse. The old geometry-only classifier could not distinguish a
  perfusion/diffusion *map* from an *acquisition*: of the 5,315 rows labelled
  `DWI`, ~3,800 were RAPID post-processing summaries or MRA MIPs; 829 `CTP` rows
  likewise. Also fixed: ~2,900 coronal/sagittal CT reformats typed as acquisitions
  (the plane now comes from `ImageOrientationPatient`, because `ImageType` and the
  series name both lie), and 1,221 MRA projections typed as `LOCALIZER`. All
  130,921 rows recomputed under `rules-v2`. Validated against the ~5,500 independent
  human `series_type` annotations: PWI 100%, MRA 99.5%, CTP 99.2%, DWI 96%, ADC 89%,
  CTA 80%, NCCT 77%.
- **Behavior change**: `image_study.study_type` is now populated (from
  `StudyDescription`) instead of `''`, activating the `study_type` filter and sort
  on `/api/studies` and `/api/series`. Adds `image_study.timepoint`
  (`BL` / `THROMBECTOMY` / `FU`), anchored on the **femoral-sheath puncture** from
  `lvo_clinical_data` — not stroke onset. Only 59% of clinical rows carry a recorded
  puncture, so `timepoint_anchor_source` records whether the anchor was real or a
  `+5h`/`+10h` estimate; filter on it before trusting a timepoint. This deliberately
  re-opens `lvo_clinical_data`, previously retired as a roster.
- All machine columns stay strictly independent of the human annotation labels of
  the same names (`label_series_type_*`, `label_study_type_*`, `label_timepoint_*`).
- **Removed**: the dead Spanish study classifier. Auto-NIfTI's dormancy is now
  explicit (`utils.NIFTI_SERIES_TYPES`, empty) rather than an accident.

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
