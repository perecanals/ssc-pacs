# Workstream 05 — Cold-storage robustness

**Status:** `todo`
**Priority:** `P1`
**Size:** `M` (≈ 3–5 days)
**Owner:** _(unassigned)_
**Dependencies:** benefits from WS 06 (observability) — structured logs
make the fixes observable — but not blocking.

---

## 1. Context

The cold-storage warm/evict subsystem
(`stanford-stroke-pacs/web-app/cache_manager.py`) is clever but has
several failure modes that can corrupt operational state:

- A process crash between "atomic rename of `.warming` → final" and
  "mark `cache_state.status='hot'`" leaves the study stuck in `warming`
  forever. No timeout exists to recover.
- Eviction is **not** transactional: if `shutil.rmtree()` fails
  (permissions, EBUSY, disk errors), the `cache_state` row is still
  deleted — orphaning the files on disk with no record.
- No pre-extraction disk-space check. If disk fills during extraction,
  `untar_zst` fails and partial files may linger in `.warming` tmp dirs.
- Background eviction loop catches all exceptions generically and logs
  without a study UID context — bugs are invisible in production.

All four are latent — they don't cause problems today — but any of them
will eventually trigger a Friday-night pager for a future maintainer.

See `AUDIT_FINDINGS.md` §4.4 and §1.2.

---

## 2. Scope

**In scope:**
- Add a `warming` timeout so crashed warms self-recover.
- Pre-extraction disk-space check.
- Make eviction transactional: only delete `cache_state` row after `rmtree`
  succeeds; surface failures.
- Add structured log context (study UID) to all warm/evict log lines.
- Add a simple `scripts/cold_storage/cold_storage_health.py` that reports stuck
  `warming` rows, orphaned tmp dirs, and disk pressure.

**Out of scope:**
- Redesigning the storage mode (`cold_path_cache` is the production
  mode — keep it).
- Adding replication of cold archives (that is WS 01).
- Replacing advisory locks with a different concurrency primitive (they
  work).

---

## 3. Findings

- **F-05.1** — `warm_study()` in `web-app/cache_manager.py:134–320`
  sets `status='warming'` at start; if the process dies between line 240
  (atomic rename) and the `finish()` call at 147–150, the row stays
  `warming` forever. Subsequent warm attempts hit the hot-check at 163–182
  and short-circuit, masking the problem.
- **F-05.2** — Eviction at `cache_manager.py:322–346` calls
  `shutil.rmtree()` then always deletes the `cache_state` row (lines
  340–343). If `rmtree` raises, the row is still deleted.
- **F-05.3** — No disk-space check before extraction
  (`cache_manager.py:215–246`). A full disk results in partial extraction
  and an exception, then a bare cleanup catch.
- **F-05.4** — Eviction loop at `app.py:305–313` catches `Exception`
  generically and logs without per-study context; the log is a single
  line without UID prefix.
- **F-05.5** — Temp dir cleanup at `cache_manager.py:291–296` swallows
  errors; orphaned `.warming` dirs can accumulate silently.

---

## 4. Tasks

- [ ] **T1** — Add `warming_started_at TIMESTAMPTZ` column to `cache_state`
  (Alembic revision if WS 04 has landed; otherwise inline DDL matching the
  existing pattern). Populate it whenever a row transitions to `warming`.
- [ ] **T2** — Add a `WARMING_TIMEOUT_MINUTES` config setting (default 30)
  to `config.toml` and `config.py`.
- [ ] **T3** — In `warm_study()`, before taking the hot-check short-circuit
  at `cache_manager.py:163–182`, check whether the row is `warming` with
  `warming_started_at` older than the timeout. If so, log a warning with
  the study UID and treat it as cold (proceed to re-warm).
- [ ] **T4** — In `warm_study()`, before untar, check available disk space
  on the target filesystem vs. the archive's uncompressed size (peek the
  tar header for total content length, or estimate as 3× the
  compressed size for `.zst`). If insufficient, raise a specific
  `InsufficientDiskSpaceError` with required/available bytes, mark the
  row as `cold` (not `warming`), and return a clear error to the caller.
- [ ] **T5** — Rewrite `evict_study()` at `cache_manager.py:322–346` so
  the `cache_state` deletion only happens **after** `rmtree` succeeds.
  Wrap in a single transaction and add a try/except that logs the study
  UID on failure, leaves the row intact, and re-raises.
- [ ] **T6** — Replace the bare logger usage in
  `cache_manager.py` with a module-level logger (`logger =
  logging.getLogger(__name__)`). Every warm/evict log line must include
  the study UID as a structured field (via `extra={'study_uid': uid}`).
  Pairs with WS 06.
- [ ] **T7** — Refactor temp-dir cleanup at `cache_manager.py:291–296` to
  log (not swallow) orphaned-dir errors. Consider moving `.warming` dirs
  into a single well-known parent (e.g.
  `/DATA2/pacs_imaging_data/.warming/<uid>/`) so orphans are easy to
  sweep.
- [ ] **T8** — Write `stanford-stroke-pacs/scripts/cold_storage/cold_storage_health.py`
  (new) that prints:
  - count of `warming` rows with `warming_started_at < now() - timeout`;
  - count of orphaned `.warming` dirs on disk;
  - disk free on `legacy_dicom_root` mount;
  - distribution of `last_accessed_at` in `cache_state` (hint of eviction
    pressure).
  Exit nonzero if any critical condition is met.
- [ ] **T9** — Wire `cold_storage_health.py` into a systemd timer that runs
  every 15 minutes; on failure, log to journal (integrates with WS 06
  later).
- [ ] **T10** — Update `documentation/cold_storage/runbook.md` with a new
  section on the watchdog, disk-space failure mode, and how to manually
  clear a stuck `warming` row.

---

## 5. Acceptance criteria

- [ ] Simulating `SIGKILL` between rename and `finish()` (e.g., by
  monkey-patching `finish` to `raise`) leaves a recoverable state: the
  next warm attempt, after `WARMING_TIMEOUT_MINUTES`, succeeds.
- [ ] Filling the destination disk to within 100 MB of the archive's
  uncompressed size produces a clear `InsufficientDiskSpaceError`, leaves
  no `.warming` dir behind, and the `cache_state` row returns to `cold`.
- [ ] Killing `rmtree` (e.g., `chmod 000` on a file in the target dir)
  produces a logged failure; the `cache_state` row is **not** deleted;
  retrying succeeds after fixing perms.
- [ ] All log lines in `cache_manager.py` include a study UID.
- [ ] `cold_storage_health.py --json` produces machine-parseable output.

---

## 6. Verification

```bash
# Warming timeout
python3 -c "
from web app.cache_manager import warm_study
# inject a fault via monkeypatch — see scripts/test_warming_timeout.py
"

# Disk-space precheck (use a small loopback filesystem)
truncate -s 100M /tmp/cold_test.img
mkfs.ext4 /tmp/cold_test.img
sudo mount -o loop /tmp/cold_test.img /mnt/cold_test
# Point legacy_dicom_root at /mnt/cold_test via config override, then warm
# a study whose archive is > 100 MB uncompressed. Expect a clean error.

# Health report
python3 stanford-stroke-pacs/scripts/cold_storage/cold_storage_health.py
echo $?

# Structured logs (journalctl)
sudo journalctl -u ssc-web-app -n 100 | grep study_uid
```

---

## 7. Rollback

Pure code changes. `git revert`. The new `warming_started_at` column can
stay (harmless if unused) or be dropped via an Alembic downgrade.

---

## 8. Files touched

- `stanford-stroke-pacs/web-app/cache_manager.py` (edit — T3, T4, T5, T6,
  T7)
- `stanford-stroke-pacs/web-app/app.py` (edit — T6 — update eviction
  loop logger)
- `stanford-stroke-pacs/web-app/config.py` (edit — add
  `warming_timeout_minutes`)
- `stanford-stroke-pacs/config.toml` (edit — add the same)
- `stanford-stroke-pacs/web-app/alembic/versions/000N_warming_started_at.py`
  (new, if WS 04 landed) OR inline in `INIT_SQL`/`MIGRATE_SQL`
- `stanford-stroke-pacs/scripts/cold_storage/cold_storage_health.py` (new)
- `stanford-stroke-pacs/systemd/cold-storage-health.service` (new)
- `stanford-stroke-pacs/systemd/cold-storage-health.timer` (new)
- `stanford-stroke-pacs/documentation/cold_storage/runbook.md` (edit)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Watchdog timeout fires during a legitimately slow warm | low | med | Default 30 min is > 10× typical warm; make configurable |
| Disk-space estimate (3× compressed) is wrong | med | low | Read uncompressed size from tar header if cheap; otherwise be conservative |
| Non-transactional eviction becomes too strict — blocks a retry | low | med | On `rmtree` failure, log and leave the row; operator can manually force-evict |
| Log volume increases with per-study context | high | low | Use JSON logs (pairs with WS 06); log aggregator can filter |

---

## 10. Notes

- The advisory-lock concurrency (`cache_manager.py:86–90, 154–156`) is
  correct and should not be touched by this workstream.
- Consider recording `warming_started_at` in a separate operational table
  if you don't want to touch `cache_state` schema — but a single column
  is simpler.
- Pair the work with WS 06 closely: structured logging is much more
  useful than bare logger calls for debugging cold-storage incidents.
