import argparse
import glob
import json
import logging
import os
import queue
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml

from image_ingestion_protocol import ImageIngestionProtocol

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
WEB_APP_DIR = ROOT_DIR / "web-app"
if str(WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_APP_DIR))

from config import COLD_ARCHIVE_ROOT, DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402
from labelled_table_sync import sync_labelled_rows  # noqa: E402

DEFAULT_ENV_PATH = str(ROOT_DIR / ".env")


class StreamToLogger:
    """Redirect print statements to the logger.

    The protocol phases print(); this redirection is what lands them in the
    run log — the same file the resume markers ride on. Keep prints as
    prints; converting them to logging calls buys nothing.
    """
    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.linebuf = ''

    def write(self, buf):
        for line in buf.rstrip().split('\n'):
            if line:
                self.logger.log(self.log_level, line)

    def flush(self):
        pass


# Configure logging
def setup_logging():
    """Set up logging configuration"""
    log_filename = f"execute_image_ingestion_protocol_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', log_filename)
    os.makedirs(os.path.dirname(log_filepath), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filepath),
            logging.StreamHandler()  # Also log to console
        ]
    )
    return log_filepath


def configure_library_logging():
    # pydicom emits many VR length warnings through its logger rather than Python warnings.
    logging.getLogger("pydicom").setLevel(logging.ERROR)


# Log markers parsed by determine_resume_skip_set. These are strings this same
# script emits, so the resume contract stays stable as long as they match.
# NOTE: logs/ is load-bearing resume state — pruning old run logs discards
# resume progress for their src_dirs.
_SRC_DIR_MARKER = "Source directory: "
_COMPLETED_MARKER = "Successfully completed processing case "


def determine_resume_skip_set(logs_dir, current_log_path, src_dir, nhc_list, logger=None):
    """Success-based resume: the set of cases a prior run already completed.

    Scans every prior run log whose 'Source directory:' header matches src_dir
    and collects the cases with a 'Successfully completed processing case'
    marker, across all matching logs. Only proven successes are skipped —
    cases that failed (per-case errors don't stop a run) or were interrupted
    mid-case lack the marker and get re-processed.
    """
    def _log(msg):
        if logger is not None:
            logger.info(msg)

    known = set(nhc_list)
    completed = set()
    matched_logs = []
    current = os.path.abspath(current_log_path) if current_log_path else None

    # Match logs from before the integration→ingestion rename too, so resume
    # continuity survives it.
    patterns = ("execute_image_ingestion_protocol_*.log",
                "execute_image_integration_protocol_*.log")
    candidates = (p for pat in patterns
                  for p in glob.glob(os.path.join(logs_dir, pat)))

    for log_path in candidates:
        if current is not None and os.path.abspath(log_path) == current:
            continue
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        # Header match: the prior run's src_dir must equal the current one.
        prior_src = None
        for line in lines:
            idx = line.find(_SRC_DIR_MARKER)
            if idx != -1:
                prior_src = line[idx + len(_SRC_DIR_MARKER):].strip()
                break
        if prior_src != src_dir:
            continue

        matched_logs.append(os.path.basename(log_path))
        for line in lines:
            c = line.find(_COMPLETED_MARKER)
            if c != -1:
                case = line[c + len(_COMPLETED_MARKER):].strip()
                if case in known:
                    completed.add(case)

    if matched_logs:
        _log(f"Resume: {len(completed)} case(s) verified complete across "
             f"{len(matched_logs)} prior log(s) for this source directory")
    return completed


DEFAULT_CONFIG_PATH = Path(__file__).with_name("execute_image_ingestion_protocol.yaml")


def load_config(config_path):
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw_yaml = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw_yaml) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain a top-level mapping: {config_path}")

    # Storage paths default from repo-root config.toml so the ingestion
    # protocol can never drift from the running stack. The YAML can still
    # override on a per-run basis but a warning is emitted.
    config_toml_archive_root = (
        str(COLD_ARCHIVE_ROOT) if STORAGE_MODE == "cold_path_cache" else None
    )

    defaults = {
        "env_path": DEFAULT_ENV_PATH,
        "database": "stanford-stroke",
        "src_dir": "/home/perecanals/pacs/imaging_database_test",
        "overwrite_if_exists": False,
        "anonymize_files": False,
        "delete_originals_after_verification": False,
        "import_label": None,
        "dataset": None,
        "cold_archive_root": config_toml_archive_root,
        "cleanup_loose_after_indexing": True,
        "resume": True,
        "compress_workers": 4,
        "pipeline_indexing": True,
    }
    merged_config = {**defaults, **config}
    merged_config["compress_workers"] = max(
        1, int(merged_config["compress_workers"] or 1))
    if merged_config["overwrite_if_exists"] and merged_config["pipeline_indexing"]:
        print(
            "WARNING: pipeline_indexing disabled because overwrite_if_exists "
            "is set — overwriting deletes existing studies' files, which "
            "could race an in-flight background indexing scan reading them."
        )
        merged_config["pipeline_indexing"] = False
    merged_config["config_path"] = str(config_path)
    merged_config["_storage_mode"] = STORAGE_MODE
    merged_config["_dicom_data_root"] = str(DICOM_DATA_ROOT)
    merged_config["_config_toml_cold_archive_root"] = config_toml_archive_root

    # Validate consistency with config.toml.
    yaml_archive_root = config.get("cold_archive_root")
    if STORAGE_MODE == "cold_path_cache":
        if not merged_config["cold_archive_root"]:
            raise RuntimeError(
                "config.toml has mode='cold_path_cache' but no cold_archive_root "
                "could be resolved (neither config.toml nor YAML provides one). "
                "Set [storage].cold_archive_root in config.toml."
            )
        if yaml_archive_root and yaml_archive_root != config_toml_archive_root:
            print(
                f"WARNING: YAML overrides cold_archive_root "
                f"({yaml_archive_root!r}) — config.toml has "
                f"{config_toml_archive_root!r}. Make sure this is intentional."
            )
    elif STORAGE_MODE == "legacy":
        if merged_config["cold_archive_root"]:
            print(
                f"WARNING: cold_archive_root is set ({merged_config['cold_archive_root']!r}) "
                f"but config.toml mode is 'legacy'. The protocol will create archives "
                f"that the running stack will not use."
            )
        if merged_config["cleanup_loose_after_indexing"]:
            print(
                "WARNING: cleanup_loose_after_indexing is set but config.toml mode "
                "is 'legacy' — loose files are the canonical store in legacy mode. "
                "Ignoring the flag."
            )
            merged_config["cleanup_loose_after_indexing"] = False

    return merged_config, raw_yaml


def execute_image_ingestion_protocol(
    case_dir,
    postgres_engine,
    logger,
    overwrite_if_exists=False,
    anonymize_files=False,
    delete_originals_after_verification=False,
    import_id=None,
    import_label=None,
    dataset=None,
    cold_archive_root=None,
    compress_workers=None,
):
    # Create an instance of the ImageIngestionProtocol class
    protocol = ImageIngestionProtocol(
        case_dir,
        postgres_engine,
        anonymize_files=anonymize_files,
        delete_originals_after_verification=delete_originals_after_verification,
        import_id=import_id,
        import_label=import_label,
        dataset=dataset,
        cold_archive_root=cold_archive_root,
        compress_workers=compress_workers or 1,
    )
    # Execute the protocol
    return protocol.execute_image_ingestion_protocol(overwrite_if_exists=overwrite_if_exists)


def sync_batch_labelled_tables(postgres_engine, logger, study_ids, series_ids):
    if not study_ids and not series_ids:
        logger.info("No labelled-table sync needed for this batch")
        return

    raw_conn = postgres_engine.raw_connection()
    try:
        if study_ids:
            synced = sync_labelled_rows(raw_conn, "study", study_ids)
            logger.info(f"Synced {synced} study labelled row(s)")
        if series_ids:
            synced = sync_labelled_rows(raw_conn, "series", series_ids)
            logger.info(f"Synced {synced} series labelled row(s)")
        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()


COLD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "cold_storage"
if str(COLD_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(COLD_SCRIPTS_DIR))


def _orthanc_session():
    import requests  # noqa: E402
    from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)
    return session, ORTHANC_URL


def _series_targets_from_db(postgres_engine, series_ids):
    """image_series rows for `series_ids` as scoped_index.SeriesTarget objects."""
    import scoped_index as si  # noqa: E402

    raw_conn = postgres_engine.raw_connection()
    try:
        cur = raw_conn.cursor()
        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path, COALESCE(number_of_slices,0) "
            "FROM image_series WHERE seriesinstanceuid = ANY(%s) "
            "AND dicom_dir_path IS NOT NULL",
            (list(series_ids),),
        )
        return [si.SeriesTarget(s, d, int(n)) for s, d, n in cur.fetchall()]
    finally:
        raw_conn.close()


def index_case_into_orthanc(postgres_engine, logger, series_ids):
    """Register one just-ingested case into Orthanc via POST /indexer/scan.

    cold_path_cache only. Called per case, right after the case's DB commit, so
    each scan is naturally bounded (a case is one patient — OOM-safe) and the
    case is viewable in OHIF while the batch is still running. The endpoint scans
    exactly the case's study subtrees (see orthanc-indexer-patched/PATCHES.md and
    scripts/cold_storage/scoped_index.py) — no config edits, no restarts. An
    oversized case is routed through bounded passes. Returns the verified
    SeriesInstanceUIDs.
    """
    if STORAGE_MODE != "cold_path_cache" or not series_ids:
        return []

    import scoped_index as si  # noqa: E402

    targets = _series_targets_from_db(postgres_engine, series_ids)
    if not targets:
        return []

    session, orthanc_url = _orthanc_session()
    kwargs = dict(host_root=str(DICOM_DATA_ROOT), orthanc_url=orthanc_url,
                  session=session, force=False, granularity="study",
                  poll_s=3, log=logger.info)
    total_instances = sum(t.n_instances for t in targets)
    logger.info(f"Indexing case into Orthanc ({len(targets)} series, "
                f"~{total_instances:,} instances)…")
    if total_instances > si.MAX_INSTANCES_PER_PASS:
        summary = si.register_in_bounded_passes(targets, **kwargs)
    else:
        summary = si.scoped_register_series(targets, **kwargs)
    if summary["registered"] < summary["targets"]:
        logger.warning(f"Orthanc indexing incomplete for this case: "
                       f"{summary['registered']}/{summary['targets']} series verified "
                       f"(end-of-run sanity pass will retry)")
    return summary["registered_suids"]


def sanity_reindex_run(postgres_engine, logger, series_ids):
    """End-of-run sanity pass: prove every series ingested this run is indexed.

    Verifies all of the run's series against Orthanc (/tools/lookup); any that
    are missing (per-case indexing failed or a scan was truncated) are
    re-registered in bounded passes with Force=true (drops possible orphaned
    rows first), then re-verified. Returns the list of still-missing UIDs.
    """
    if STORAGE_MODE != "cold_path_cache" or not series_ids:
        return []

    import scoped_index as si  # noqa: E402

    session, orthanc_url = _orthanc_session()
    series_ids = sorted(series_ids)
    logger.info(f"Sanity pass: verifying {len(series_ids)} series against "
                f"Orthanc's index…")
    verified = set(si.verify_registered(session, orthanc_url, series_ids))
    missing = [s for s in series_ids if s not in verified]
    if not missing:
        logger.info(f"Sanity pass: Orthanc index clean — "
                    f"{len(series_ids)}/{len(series_ids)} series verified")
        return []

    logger.warning(f"Sanity pass: {len(missing)} series missing from Orthanc's "
                   f"index; re-registering in bounded passes (Force=true)…")
    targets = _series_targets_from_db(postgres_engine, missing)
    summary = si.register_in_bounded_passes(
        targets, host_root=str(DICOM_DATA_ROOT), orthanc_url=orthanc_url,
        session=session, force=True, granularity="series", poll_s=5,
        log=logger.info,
    )
    still_missing = sorted(set(missing) - set(summary["registered_suids"]))
    if not still_missing:
        logger.info(f"Sanity pass: Orthanc index clean after re-registration — "
                    f"{len(series_ids)}/{len(series_ids)} series verified")
    else:
        logger.error(f"Sanity pass: {len(still_missing)} series STILL missing "
                     f"from Orthanc after re-registration (data is safe on "
                     f"disk/DB; run scripts/cold_storage/reindex_missing_series.py "
                     f"to backfill). First few: {still_missing[:5]}")
    return still_missing


def cleanup_case_loose_dirs(postgres_engine, logger, indexed_series_ids):
    """Delete a case's loose DICOM dirs once archive + Orthanc index are proven.

    Only called with series UIDs that just verified in Orthanc's index. Applies
    the same per-series safety checks as scripts/cold_storage/
    cleanup_loose_dicoms.py (archive present + non-empty + file-count match;
    NIFTI siblings preserved). Series without an archive (compression failure)
    are skipped and logged.

    Returns {"cleaned": [uid, ...], "kept": [(uid, dicom_dir_path), ...]} —
    which loose dirs are gone from disk vs still present — so the caller can
    stamp series_cache_state to match. Note: every "kept" series here failed
    an archive safety check, so it must NOT be stamped hot (evict_series
    rmtrees without checking the archive — a hot row would expose the only
    copy to TTL eviction). Row-less = cold and eviction-proof.
    """
    from cleanup_loose_dicoms import clean_series_loose_dir  # noqa: E402

    raw_conn = postgres_engine.raw_connection()
    try:
        cur = raw_conn.cursor()
        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path, dicom_archive_path "
            "FROM image_series WHERE seriesinstanceuid = ANY(%s) "
            "AND dicom_dir_path IS NOT NULL",
            (list(indexed_series_ids),),
        )
        rows = cur.fetchall()
    finally:
        raw_conn.close()

    cleaned = 0
    bytes_freed = 0
    skipped = []
    cleaned_uids = []
    kept = []
    for series_uid, dicom_dir, archive in rows:
        if not archive:
            skipped.append(f"{series_uid}: no archive (compression failed?)")
            kept.append((series_uid, dicom_dir))
            continue
        status, size, detail = clean_series_loose_dir(
            series_uid, Path(dicom_dir), Path(archive),
            series_in_orthanc=True, deep_verify=True, execute=True,
        )
        if status == "cleaned":
            cleaned += 1
            bytes_freed += size
            cleaned_uids.append(series_uid)
        elif status == "already_clean":
            cleaned_uids.append(series_uid)
        else:
            skipped.append(detail or f"{series_uid}: {status}")
            kept.append((series_uid, dicom_dir))
    logger.info(f"Loose cleanup: removed {cleaned} DICOM dir(s) "
                f"({bytes_freed/1e6:.1f} MB freed)")
    for msg in skipped:
        logger.warning(f"Loose cleanup skipped — {msg}")
    return {"cleaned": cleaned_uids, "kept": kept}


def _series_dirs_from_db(postgres_engine, series_ids):
    """(seriesinstanceuid, dicom_dir_path) rows for the given series.

    Archive-less series are excluded: a 'hot' cache row makes the loose dir
    TTL-evictable, and evict_series deletes it without checking the archive —
    never hand eviction the only copy of a series.
    """
    raw_conn = postgres_engine.raw_connection()
    try:
        cur = raw_conn.cursor()
        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path "
            "FROM image_series WHERE seriesinstanceuid = ANY(%s) "
            "AND dicom_dir_path IS NOT NULL "
            "AND dicom_archive_path IS NOT NULL AND dicom_archive_path <> ''",
            (list(series_ids),),
        )
        return cur.fetchall()
    finally:
        raw_conn.close()


def _stamp_series_cache_state(postgres_engine, logger, hot_rows, cleaned_uids):
    """Make series_cache_state reflect a case's actual on-disk state.

    `hot_rows` [(uid, dicom_dir_path)]: loose files remain on disk → upsert
    'hot' with last_accessed_at set (run_eviction skips NULL, so this is what
    makes the loose files TTL-evictable). `cleaned_uids`: loose dir deleted →
    drop any row (absence reads as 'cold', matching evict_series semantics).
    Rows in status 'warming' are left alone in both directions — an in-flight
    warm (possible under overwrite_if_exists) lands its own truthful row.
    """
    if not hot_rows and not cleaned_uids:
        return
    raw_conn = postgres_engine.raw_connection()
    try:
        cur = raw_conn.cursor()
        if hot_rows:
            cur.executemany(
                "INSERT INTO series_cache_state "
                "    (seriesinstanceuid, status, warmed_at, last_accessed_at, "
                "     cache_path, error_message, warming_started_at) "
                "VALUES (%s, 'hot', now(), now(), %s, NULL, NULL) "
                "ON CONFLICT (seriesinstanceuid) DO UPDATE "
                "SET status = 'hot', warmed_at = now(), last_accessed_at = now(), "
                "    cache_path = EXCLUDED.cache_path, "
                "    error_message = NULL, warming_started_at = NULL "
                "WHERE series_cache_state.status <> 'warming'",
                [(uid, dicom_dir) for uid, dicom_dir in hot_rows],
            )
        if cleaned_uids:
            cur.execute(
                "DELETE FROM series_cache_state "
                "WHERE seriesinstanceuid = ANY(%s) AND status <> 'warming'",
                (list(cleaned_uids),),
            )
        raw_conn.commit()
        logger.info(f"Cache-state stamp: {len(hot_rows)} hot, "
                    f"{len(cleaned_uids)} cleared")
    finally:
        raw_conn.close()


def process_index_job(postgres_engine, logger, nhc, to_index, cleanup_enabled):
    """Index + clean up + stamp one already-committed case. Returns a result dict.

    Runs on the background indexing worker thread (or synchronously when
    pipeline_indexing is off). The success marker parsed by
    determine_resume_skip_set is logged HERE — only after index + cleanup +
    stamp have run — so an interrupt can never mark a half-finished case
    complete. Semantics match the previous inline code exactly: indexing,
    cleanup, and stamping failures are each non-fatal (the marker is still
    logged; the sanity pass / reindex_missing_series.py backfill). Only an
    unexpected error outside those guards withholds the marker and reports
    status="worker_error".

    error_log and the run counters are deliberately NOT touched here — the
    main thread owns them (single writer) and applies this result dict.
    """
    result = {"nhc": nhc, "status": "ok",
              "indexing_error": None, "indexing_traceback": None,
              "error": None, "traceback": None}
    try:
        indexed_uids = []
        try:
            indexed_uids = index_case_into_orthanc(
                postgres_engine, logger, sorted(to_index))
        except Exception as e:
            logger.error(
                f"Orthanc indexing failed for case {nhc} (data is "
                f"safe on disk/DB; the end-of-run sanity pass will "
                f"retry): {e}"
            )
            result["indexing_error"] = str(e)
            result["indexing_traceback"] = traceback.format_exc().splitlines()

        if cleanup_enabled and indexed_uids:
            try:
                outcome = cleanup_case_loose_dirs(
                    postgres_engine, logger, indexed_uids)
                # "kept" series failed an archive check — leave them
                # row-less (cold + eviction-proof), never hot.
                _stamp_series_cache_state(
                    postgres_engine, logger,
                    hot_rows=[],
                    cleaned_uids=outcome["cleaned"])
            except Exception as e:
                logger.error(
                    f"Loose cleanup failed for case {nhc} "
                    f"(non-fatal; loose files remain on disk; run "
                    f"scripts/cold_storage/rebuild_cache_state.py "
                    f"to reconcile cache state): {e}"
                )
        elif indexed_uids:
            # Cleanup disabled: loose files stay on disk. Stamp them 'hot'
            # so the UI reflects reality and the TTL sweep can reclaim the
            # space later. Series whose indexing failed are deliberately
            # left row-less (reads cold; TTL sweep never touches them, so
            # their loose files survive for the reindex retry).
            try:
                _stamp_series_cache_state(
                    postgres_engine, logger,
                    hot_rows=_series_dirs_from_db(
                        postgres_engine, indexed_uids),
                    cleaned_uids=[])
            except Exception as e:
                logger.error(
                    f"Cache-state stamping failed for case {nhc} "
                    f"(non-fatal; run scripts/cold_storage/"
                    f"rebuild_cache_state.py to reconcile): {e}"
                )

        logger.info(f"Successfully completed processing case {nhc}")
    except Exception as e:
        result["status"] = "worker_error"
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc().splitlines()
        logger.error(f"Indexing worker failed for case {nhc}: {e}")
    return result


def _indexing_worker(job_q, result_q, postgres_engine, logger):
    """Background thread: consume index jobs until the None sentinel."""
    while True:
        job = job_q.get()
        if job is None:
            return
        result_q.put(process_index_job(postgres_engine, logger, **job))


if __name__ == "__main__":
    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    parser = argparse.ArgumentParser(description="Execute the Stanford image ingestion protocol.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the YAML config file.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Process every case from the top, ignoring prior-run progress "
        "(overrides resume in the YAML).",
    )
    args = parser.parse_args()
    config, raw_yaml = load_config(args.config)
    resume_enabled = config.get("resume", True) and not args.no_resume

    # Initialize logging
    log_filepath = setup_logging()
    configure_library_logging()
    logger = logging.getLogger(__name__)

    # Redirect stdout and stderr to logger
    sys.stdout = StreamToLogger(logger, logging.INFO)
    sys.stderr = StreamToLogger(logger, logging.ERROR)

    logger.info("Starting image ingestion protocol execution")
    logger.info(f"Log file: {log_filepath}")
    logger.info(f"Config file: {config['config_path']}")
    logger.info("Loaded YAML configuration:\n%s", raw_yaml.rstrip())

    env_path = config["env_path"]
    load_dotenv(dotenv_path=env_path)
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")

    postgres_engine = create_engine(
        f"postgresql://{DB_USER}:{DB_PASSWORD}@localhost:5432/{config['database']}"
    )
    run_import_id = ImageIngestionProtocol.get_next_import_id(postgres_engine)

    src_dir = config["src_dir"]
    logger.info(f"Source directory: {src_dir}")
    logger.info(f"Database: {config['database']}")
    logger.info(f"Env path: {env_path}")
    logger.info(f"Run import_id: {run_import_id}")
    logger.info(f"Import label: {config['import_label']}")
    logger.info(f"Dataset: {config['dataset']}")
    logger.info(f"Anonymize files: {config['anonymize_files']}")
    logger.info(
        "Delete originals after verification: "
        f"{config['delete_originals_after_verification']}"
    )
    logger.info(f"Overwrite existing studies: {config['overwrite_if_exists']}")
    logger.info(f"Cleanup loose after indexing: {config['cleanup_loose_after_indexing']}")
    logger.info(f"Compression workers: {config['compress_workers']}")
    logger.info(f"Pipelined Orthanc indexing: {config['pipeline_indexing']}")
    logger.info(f"Storage mode (config.toml): {config['_storage_mode']}")
    logger.info(f"DICOM data root (config.toml): {config['_dicom_data_root']}")
    logger.info(f"Cold archive root (resolved): {config.get('cold_archive_root')}")
    logger.info(f"Resume from prior run: {resume_enabled}")

    error_log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', f"error_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    error_log = {}

    total_cases = 0
    processed_cases = 0
    skipped_cases = 0
    resumed_skipped = 0
    failed_cases = 0
    indexing_failed_cases = 0
    synced_study_ids = set()
    synced_series_ids = set()

    # Indexing pipeline: one background worker indexes/cleans/stamps case N
    # while the main thread ingests case N+1 (queue depth 1 → at most ~2
    # cases of un-cleaned loose data outstanding). The patched indexer only
    # runs one scan at a time (409-busy guard), so one worker matches the
    # server. With pipeline_indexing off the same code path runs, the main
    # thread just blocks on each result — no forked logic.
    pipeline_indexing = config["pipeline_indexing"]
    index_job_q = queue.Queue(maxsize=1)
    index_result_q = queue.Queue()
    pending_index_cases = deque()
    indexing_worker = threading.Thread(
        target=_indexing_worker,
        args=(index_job_q, index_result_q, postgres_engine, logger),
        name="orthanc-indexer", daemon=True,
    )
    indexing_worker.start()

    def _handle_index_result(res):
        """Apply a worker result. Main thread only — sole owner of error_log
        (and its JSON file) and of the run counters."""
        global processed_cases, failed_cases, indexing_failed_cases
        try:
            pending_index_cases.remove(res["nhc"])
        except ValueError:
            pass
        if res["status"] == "worker_error":
            logger.error(f"Failed to process case {res['nhc']}: {res['error']}")
            error_log[str(res["nhc"])] = {
                "error": res["error"],
                "traceback": res["traceback"],
            }
            with open(error_log_file_path, 'w') as f:
                json.dump(error_log, f, indent=4)
            failed_cases += 1
            return
        processed_cases += 1
        if res["indexing_error"]:
            error_log[f"{res['nhc']}#indexing"] = {
                "error": res["indexing_error"],
                "type": "indexing_error",
                "traceback": res["indexing_traceback"],
            }
            with open(error_log_file_path, 'w') as f:
                json.dump(error_log, f, indent=4)
            indexing_failed_cases += 1

    def _drain_index_results():
        while True:
            try:
                res = index_result_q.get_nowait()
            except queue.Empty:
                return
            _handle_index_result(res)

    try:
        nhc_list = sorted(
            nhc for nhc in os.listdir(src_dir)
            if not nhc.startswith(".") and os.path.isdir(os.path.join(src_dir, nhc))
        )
        total_cases = len(nhc_list)
        logger.info(f"Found {total_cases} cases to process")

        logs_dir = os.path.dirname(log_filepath)
        skip_cases = set()
        if resume_enabled:
            skip_cases = determine_resume_skip_set(
                logs_dir, log_filepath, src_dir, nhc_list, logger
            )
            if len(skip_cases) >= len(nhc_list):
                logger.info(
                    "Resume: prior run(s) already completed all cases for this "
                    "source directory; nothing to do (use --no-resume to re-run)"
                )
            elif skip_cases:
                first = next(n for n in nhc_list if n not in skip_cases)
                logger.info(
                    f"Resume: skipping {len(skip_cases)} case(s) completed in "
                    f"prior run(s); {len(nhc_list) - len(skip_cases)} to process, "
                    f"starting with {first}"
                )
            else:
                logger.info("Resume: no completed cases found in prior runs; "
                            "processing all cases")
        else:
            logger.info("Resume disabled (--no-resume); processing all cases")

        for nhc in nhc_list:
            if nhc in skip_cases:
                logger.info(f"Skipping case {nhc} - completed in a prior run (resume)")
                resumed_skipped += 1
                continue
            case_dir = os.path.join(src_dir, nhc)
            if len(os.listdir(case_dir)) > 0:
                logger.info(f"Processing case {nhc}")
                try:
                    result = execute_image_ingestion_protocol(
                        case_dir,
                        postgres_engine,
                        logger,
                        overwrite_if_exists=config["overwrite_if_exists"],
                        anonymize_files=config["anonymize_files"],
                        delete_originals_after_verification=config["delete_originals_after_verification"],
                        import_id=run_import_id,
                        import_label=config["import_label"],
                        dataset=config["dataset"],
                        cold_archive_root=config.get("cold_archive_root"),
                        compress_workers=config["compress_workers"],
                    )
                    synced_study_ids.update(result["studyinstanceuids"])
                    synced_series_ids.update(result["seriesinstanceuids"])

                    # Per-case Orthanc indexing: register the case right after
                    # its DB commit so each scan is case-sized (OOM-safe) and an
                    # interruption loses at most the in-flight case.
                    to_index = list(result["seriesinstanceuids"])
                    skipped_existing = result.get(
                        "skipped_existing_seriesinstanceuids", [])
                    if (
                        resume_enabled
                        and not to_index
                        and skipped_existing
                    ):
                        # Resume boundary: this case is fully in the DB but a
                        # prior run never logged it complete — it was
                        # interrupted (or failed) AFTER its DB commit but
                        # possibly before/mid indexing. Re-index its series
                        # (idempotent; already-registered files are fast to
                        # skip).
                        logger.info(
                            f"Resume boundary: re-indexing "
                            f"{len(skipped_existing)} already-ingested series "
                            f"for case {nhc}"
                        )
                        to_index = skipped_existing
                        synced_series_ids.update(skipped_existing)

                    # Hand the committed case to the indexing worker. Every
                    # successful ingest enqueues — even with an empty
                    # to_index — because the worker owns the success-marker
                    # log line (the resume contract). put() blocks while a
                    # job is already waiting (pipeline depth 1).
                    pending_index_cases.append(nhc)
                    index_job_q.put({
                        "nhc": nhc,
                        "to_index": sorted(to_index),
                        "cleanup_enabled": config["cleanup_loose_after_indexing"],
                    })
                    if pipeline_indexing:
                        _drain_index_results()
                    else:
                        _handle_index_result(index_result_q.get())
                except Exception as e:
                    logger.error(f"Failed to process case {nhc}: {e}")
                    error_log[str(nhc)] = {
                        "error": str(e),
                        "traceback": traceback.format_exc().splitlines()
                    }
                    with open(error_log_file_path, 'w') as f:
                        json.dump(error_log, f, indent=4)
                    failed_cases += 1
            else:
                logger.info(f"Skipping case {nhc} - empty directory")
                skipped_cases += 1

        # Drain the indexing pipeline before the batch-level sync and sanity
        # pass: they need the complete run's series set and no scan in flight.
        index_job_q.put(None)
        if pending_index_cases:
            logger.info(
                f"Waiting for background indexing of "
                f"{len(pending_index_cases)} case(s) to finish…"
            )
        indexing_worker.join()
        _drain_index_results()

    except KeyboardInterrupt:
        if pending_index_cases:
            logger.warning(
                f"Interrupted with indexing pending for case(s) "
                f"{list(pending_index_cases)} — no completion marker was "
                f"logged for them, so the next run re-processes them "
                f"(resume-boundary re-index; an orphaned in-flight Orthanc "
                f"scan is handled by the next run's busy-wait)."
            )
        raise
    except Exception as e:
        logger.error(f"Batch run aborted: {e}")
        raise
    finally:
        # Flush any finished-but-unapplied worker results so the counters and
        # error log are truthful on every exit path.
        _drain_index_results()

    try:
        sync_batch_labelled_tables(
            postgres_engine,
            logger,
            sorted(synced_study_ids),
            sorted(synced_series_ids),
        )
    except Exception as e:
        logger.error(f"Failed to sync labelled tables after batch: {e}")
        raise

    # End-of-run sanity pass (cold_path_cache): verify every series ingested
    # this run against Orthanc's index and re-register any that are missing
    # (cases whose per-case indexing failed or was truncated). Non-fatal: a
    # failure leaves the data on disk + in the DB; run
    # `scripts/cold_storage/reindex_missing_series.py` to backfill.
    sanity_still_missing = []
    try:
        sanity_still_missing = sanity_reindex_run(
            postgres_engine, logger, synced_series_ids)
    except Exception as e:
        logger.error(f"End-of-run sanity pass failed (data is safe on disk/DB; "
                     f"run reindex_missing_series.py to backfill): {e}")
        sanity_still_missing = None  # unknown state

    # Log final summary
    logger.info("=" * 60)
    logger.info("IMAGE INGESTION PROTOCOL SUMMARY")
    logger.info(f"Total cases found: {total_cases}")
    logger.info(f"Successfully processed: {processed_cases}")
    logger.info(f"Skipped (empty directories): {skipped_cases}")
    logger.info(f"Skipped (resume): {resumed_skipped}")
    logger.info(f"Failed: {failed_cases}")
    if STORAGE_MODE == "cold_path_cache":
        logger.info(f"Cases with Orthanc indexing failures: {indexing_failed_cases}")
        if sanity_still_missing is None:
            logger.warning(
                "Orthanc index state UNKNOWN (sanity pass failed) — run "
                "scripts/cold_storage/reindex_missing_series.py to verify/backfill"
            )
        elif sanity_still_missing:
            logger.warning(
                f"Orthanc index NOT clean: {len(sanity_still_missing)} series "
                f"missing — run scripts/cold_storage/reindex_missing_series.py"
            )
        elif synced_series_ids:
            logger.info(
                f"Orthanc index clean: {len(synced_series_ids)}/"
                f"{len(synced_series_ids)} series verified"
            )
    logger.info("=" * 60)
    logger.info("Image ingestion protocol execution completed")
