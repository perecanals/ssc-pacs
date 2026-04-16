Read @stanford-stroke-pacs/documentation/context.md for context. Conda env is "pacs". Never write passwords to the terminal, source .env files when necessary.

Execute the workstream at /home/perecanals/ssc-pacs/maintenance/12-annotation-audit-trail.md.

  Before starting:
  1. Read the workstream file end-to-end.
  2. Read /home/perecanals/ssc-pacs/maintenance/AUDIT_FINDINGS.md only if you need
     more context than the workstream embeds.
  3. Check /home/perecanals/ssc-pacs/maintenance/PROGRESS.md — confirm every
     dependency listed in the workstream's header is marked `done`. If any
     is not, stop and report.
  4. Set this workstream's status to `in_progress` in PROGRESS.md with your
     name/date.

  While executing:
  - Work through Tasks in order.
  - Only modify files listed in the workstream's "Files touched" allowlist.
    If you need to touch something outside, stop, update the allowlist, and
    ask for confirmation.
  - Verify each Acceptance criterion before moving on.
  - Do NOT commit or push unless explicitly told to — create the changes
    and summarize them for review.

  When done:
  - Run the Verification section end-to-end; paste results.
  - Mark the workstream `done` in PROGRESS.md with the commit SHA(s) (or
    "uncommitted" if we haven't committed yet).
  - Summarize: what changed, what was skipped and why, any follow-ups.

  If blocked: mark `blocked` with a one-line reason and hand back.

  Per-workstream prompts

  #### Done workstreams

  WS 01 — Backup and DR (P0, no deps)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/01-backup-and-dr.md.
  Follow the generic workstream protocol. This is the highest-severity
  workstream (cold archives are single-copy). Focus: pg backups, cold-archive
  mirror, restore runbook, dry-run restore is the acceptance gate.

  WS 02 — Dependency pinning (P0, no deps)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/02-dependency-pinning.md.
  Resolve current versions from the live `pacs` conda env via `pip freeze`
  and `conda env export`. Pin both requirements.txt files, commit
  environment.yml, pin the Orthanc image by digest in both
  docker-compose.yml and the patched-indexer Dockerfile.

  WS 03 — Security hardening (P0, no deps)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/03-security-hardening.md.
  Priority: fix the SQL-injection path at companion/app.py:1699-1722 first
  (use psycopg2.sql.Identifier). Then secret validation, cookie Secure flag,
  absolute session timeout, and slowapi rate limiting.

  WS 04 — Schema migrations (Alembic) (P1, no deps)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/04-schema-migrations.md.
  Introduce Alembic for the `stanford-stroke` DB only (NOT orthanc_db).
  Baseline revision must exactly match current production schema — the
  pg_dump schema-diff in Task T7 is the gate. Do not merge until diff is clean.

  WS 05 — Cold-storage robustness (P1, no deps; pairs with WS 06)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/05-cold-storage-robustness.md.
  Focus: warming-state watchdog, disk-space precheck, transactional
  eviction, per-study-UID log context. If WS 04 has landed, add the
  warming_started_at column via an Alembic revision; otherwise follow the
  inline DDL pattern already in app.py.

  WS 06 — Observability (P1, no deps)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/06-observability.md.
  Add JSON structured logging, /healthz, Prometheus /metrics, request-ID
  middleware, and a starter Grafana dashboard JSON. Do not deploy
  Prometheus/Grafana — document how but don't provision infra.

  WS 07 — CI and testing (P1, blocks WS 09 and WS 10)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/07-ci-and-testing.md.
  This unblocks the P2 refactors. Target 20-30% backend coverage focused on
  auth, annotations, cache_manager. Frontend: vitest smoke tests.
  Pre-commit + GitHub Actions CI that blocks merges on failures.

  WS 08 — Deployment portability (P1, no deps)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/08-deployment-portability.md.
  Acceptance gate: `grep -rn '/home/perecanals/' --exclude .env --exclude-dir=.git`
  must return empty. Parameterize ssc-companion.service via a generator
  script; move hardcoded IP/user out of scripts/connectivity/tunnel.sh into .env;
  add top-level READMEs.

  WS 09 — Backend refactor (P2, BLOCKED until WS 07 is done)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/09-backend-refactor.md.
  VERIFY WS 07 is marked `done` in PROGRESS.md before starting — this
  refactor requires the pytest safety net. Work in Phases A→B→C→D; each
  phase is independently shippable. Goal: app.py ≤ 300 lines, zero
  behavior change, identical URL route table.

  WS 10 — Frontend refactor (P2, BLOCKED until WS 07 is done)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/10-frontend-refactor.md.
  VERIFY WS 07 is marked `done`. Split DataTable.jsx (1,272 lines) into the
  proposed directory structure. At Task T11 record your PropTypes vs
  TypeScript decision in PROGRESS notes before proceeding.
  Acceptance: no component >500 lines, zero visual diff.

  WS 11 — Two-DB reconciliation (P2, pairs with WS 06)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/11-data-integrity-two-db.md.
  Read-only observer; do NOT auto-repair mismatches. If WS 06 is not yet
  done, skip the Prometheus metrics tasks and keep the JSON report +
  admin endpoint. Retire verify_indexing.py by re-exporting from the new
  reconciliation module.

  WS 12 — Annotation audit trail (P2, BLOCKED until WS 04 is done)
  Execute /home/perecanals/ssc-pacs/maintenance/workstreams/12-annotation-audit-trail.md.
  VERIFY WS 04 is marked `done` — the new table + trigger ship as an
  Alembic revision. Prefer trigger-based capture over app-level. Wire the
  middleware to set app.current_user per transaction. Backfill one synthetic
  "I" row per existing annotation.