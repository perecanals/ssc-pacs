# PACS maintenance — remediation workstreams

This directory is the durable home for the long-term maintainability plan of the
Stanford Stroke PACS stack. It was created on **2026-04-15** following a
comprehensive audit across backend, frontend, docs/deps/deployment, and
data/security/ops dimensions.

The audit surfaced ~18 discrete findings, grouped into 12 self-contained
workstreams. Each workstream is designed to be picked up by a single agent
instance (or a human) **without prior conversation context** — the workstream
file alone is enough of a briefing.

---

## Directory layout

```
maintenance/
├── README.md                 ← you are here
├── AUDIT_FINDINGS.md         ← raw audit report, preserved verbatim
├── WORKSTREAM_TEMPLATE.md    ← template new workstreams should follow
├── PROGRESS.md               ← single-source-of-truth checklist
└── workstreams/
    ├── 01-backup-and-dr.md
    ├── 02-dependency-pinning.md
    ├── 03-security-hardening.md
    ├── 04-schema-migrations.md
    ├── 05-cold-storage-robustness.md
    ├── 06-observability.md
    ├── 07-ci-and-testing.md
    ├── 08-deployment-portability.md
    ├── 09-backend-refactor.md
    ├── 10-frontend-refactor.md
    ├── 11-data-integrity-two-db.md
    ├── 12-annotation-audit-trail.md
    └── 13-scripts-reorganization.md
```

---

## Priority matrix

| Priority | Workstream | Risk reduced | Size |
|---|---|---|---|
| **P0** | 01 — Backup and DR | Catastrophic data loss | S/M |
| **P0** | 02 — Dependency pinning | Non-reproducible rebuilds, silent upgrades | S |
| **P0** | 03 — Security hardening | SQL injection, session hijack | M |
| **P1** | 04 — Schema migrations | Schema drift, partial-apply corruption | M |
| **P1** | 05 — Cold-storage robustness | Stuck `warming` state, orphaned files | M |
| **P1** | 06 — Observability | Silent production failures | M |
| **P1** | 07 — CI and testing | Regression risk, no safety net | L |
| **P1** | 08 — Deployment portability | Bus factor, re-deploy friction | S/M |
| **P2** | 09 — Backend refactor | 1,865-line monolith | L |
| **P2** | 10 — Frontend refactor | 1,272-line god component | L |
| **P2** | 11 — Data integrity (two-DB) | Stale UI, broken OHIF links | M |
| **P2** | 12 — Annotation audit trail | Lost edit history | M |
| **P2** | 13 — Scripts reorganization | Poor discoverability, dead files | S |

Size legend: S (≤1 day), M (2–5 days), L (1–2 weeks), XL (>2 weeks).

---

## Recommended sequencing

```
Phase A — Risk reduction (weeks 1–3, sequential):
    01 → 02 → 03

Phase B — Operability (weeks 4–8, mostly parallel):
    04, 05, 06, 08 in parallel
    then 07

Phase C — Structural debt (weeks 9+, parallel):
    09 (after 07)
    10 (after 07)
    11 (after 06)
    12 (after 04)
```

---

## How to run a workstream (agent-facing)

1. Read `AUDIT_FINDINGS.md` once if you want the overall context (optional — each workstream embeds what it needs).
2. Open the workstream file you've been assigned.
3. Check `Dependencies` — if any upstream workstream is not marked `done` in `PROGRESS.md`, stop and flag it.
4. Mark the workstream `in_progress` in `PROGRESS.md` with your name/date.
5. Execute the **Tasks** in order. Respect the **Files touched** allowlist — do not modify anything outside it.
6. Verify using the **Verification** section.
7. Mark the workstream `done` in `PROGRESS.md` with the commit SHA(s).
8. If you can't finish, mark `blocked` with a one-line reason.

---

## How to add a new workstream (human-facing)

1. Copy `WORKSTREAM_TEMPLATE.md` to `workstreams/NN-short-name.md` (use the next free number).
2. Fill in all 10 sections. A workstream file is not ready until every section is populated.
3. Add a row to `PROGRESS.md` and to this README's priority matrix.

---

## Conventions

- **Numbering is stable.** Never renumber a workstream; split instead (e.g., `05a`, `05b`).
- **Files touched is a hard allowlist.** If a workstream needs to touch a file outside its list, pause and update the file list in this repo before proceeding.
- **File references use `path:line`** format so they're clickable in most editors.
- **All line numbers are as of 2026-04-15.** If a referenced line has moved, find the symbol by name instead.
- **No workstream modifies another workstream's files** unless the dependency graph says so.
