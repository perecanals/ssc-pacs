# Workstream 13 — Scripts reorganization

**Status:** `done`
**Priority:** `P2`
**Size:** `S`
**Owner:** claude (opus 4.6)
**Dependencies:** none

---

## 1. Context

The `scripts/` directory grew organically to 25 files in a flat layout, mixing
cold-storage tools, backup scripts, admin utilities, one-off migration helpers,
a deprecated script, a misplaced regression test, and a stray JSON output
artifact. Discoverability was poor — operators had to scan the full listing to
find the right tool.

This workstream reorganizes scripts into category-based subdirectories, removes
dead files, moves the test to the proper test directory, and fixes a latent
`NameError` bug.

---

## 2. Scope

**In scope:**
- Reorganize `scripts/` into subdirectories by function.
- Delete deprecated `verify_indexing.py` (superseded by `reconcile.py`).
- Move `test_snapshot_sql.py` to `web-app/tests/`.
- Delete stray `verify_and_repair_archives_results.json`.
- Fix `label_studies.py` `ENV_PATH` `NameError` bug.
- Update all documentation, systemd units, and CLAUDE.md references.
- Add `scripts/README.md` index.

**Out of scope:**
- Refactoring script internals (DB config patterns, error handling).
- Consolidating duplicate SQL query patterns across scripts.

---

## 3. Findings

- **F-13.1** — Flat directory with 25 files mixing unrelated concerns.
  - Evidence: `ls scripts/` — cold storage, backup, admin, DICOM, deprecated, test artifacts all at same level.
  - Impact: Poor discoverability; operators must scan full listing.

- **F-13.2** — `verify_indexing.py` deprecated but still present.
  - Evidence: `scripts/verify_indexing.py:4-7` — explicit deprecation warning.
  - Impact: Operators may run the wrong script.

- **F-13.3** — `test_snapshot_sql.py` is a regression test in `scripts/`.
  - Evidence: `scripts/test_snapshot_sql.py:1` — docstring says "Regression test for WS 03 T1".
  - Impact: Confuses script inventory; not discovered by `pytest`.

- **F-13.4** — `verify_and_repair_archives_results.json` is a stray output artifact.
  - Impact: Should not be in version control.

- **F-13.5** — `label_studies.py:83` references undefined `ENV_PATH`.
  - Impact: `NameError` at runtime.

---

## 4. Tasks

- [x] **T1** — Create subdirectories and move scripts via `git mv`.
- [x] **T2** — Delete `verify_indexing.py` via `git rm`.
- [x] **T3** — Move `test_snapshot_sql.py` to `web-app/tests/`; fix path references.
- [x] **T4** — Delete `verify_and_repair_archives_results.json` via `git rm`.
- [x] **T5** — Fix `label_studies.py` `ENV_PATH` bug.
- [x] **T6** — Update all `REPO_ROOT` path computations in moved scripts (extra `.parent`).
- [x] **T7** — Update documentation, CLAUDE.md, systemd units, and maintenance files.
- [x] **T8** — Add `scripts/README.md` index.

---

## 5. Acceptance criteria

- [x] No script references undefined `REPO_ROOT` (all paths resolve correctly).
- [x] `verify_indexing.py` no longer exists.
- [x] `test_snapshot_sql.py` lives in `web-app/tests/`.
- [x] All systemd unit `ExecStart` paths point to new locations.
- [x] `grep -r 'scripts/reconcile.py' CLAUDE.md documentation/` returns only new path.
- [x] `scripts/README.md` exists with directory index.

---

## 6. Verification

```bash
# Spot-check that REPO_ROOT resolves correctly
python -c "
from pathlib import Path
import sys
sys.path.insert(0, 'scripts/data_integrity')
# just check the path computation
p = Path('scripts/data_integrity/reconcile.py').resolve().parent.parent.parent
print(f'REPO_ROOT = {p}')
assert p.name == 'stanford-stroke-pacs', f'unexpected: {p}'
"

# No stale references
grep -rn 'scripts/reconcile\.py' CLAUDE.md documentation/ | grep -v data_integrity && echo FAIL || echo OK
grep -rn 'scripts/verify_indexing' CLAUDE.md documentation/ && echo FAIL || echo OK

# Tests still pass
make test
```

---

## 7. Rollback

Pure file-move + documentation edit. Revert the commit:
```bash
git revert <sha>
```

---

## 8. Files touched

- `stanford-stroke-pacs/scripts/*` (reorganized into subdirectories)
- `stanford-stroke-pacs/scripts/verify_indexing.py` (delete)
- `stanford-stroke-pacs/scripts/verify_and_repair_archives_results.json` (delete)
- `stanford-stroke-pacs/scripts/test_snapshot_sql.py` → `web-app/tests/test_snapshot_sql.py` (move)
- `stanford-stroke-pacs/scripts/README.md` (new)
- `stanford-stroke-pacs/systemd/*.service` (path updates)
- `CLAUDE.md` (path updates)
- `stanford-stroke-pacs/documentation/**/*.md` (path updates)
- `maintenance/README.md` (add WS 13 row)
- `maintenance/PROGRESS.md` (add WS 13 row)
- `maintenance/workstreams/13-scripts-reorganization.md` (new)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Broken systemd paths after reinstall | med | med | Updated all unit files; operator must re-copy to `/etc/systemd/system/` |
| Missed documentation reference | low | low | Comprehensive grep-based update; verification step catches stragglers |

---

## 10. Notes

- Systemd units on the host still reference old paths if they were installed
  before this change. The operator must re-copy the updated `.service` files
  and `systemctl daemon-reload`.
- Historical entries in `PROGRESS.md` changelog and `documentation/history/`
  were intentionally left unchanged — they record what was true at the time.
