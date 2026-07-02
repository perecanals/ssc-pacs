import os
import sys
import glob
import json
import argparse
import logging
import traceback
from pathlib import Path
from datetime import datetime
import yaml
from image_integration_protocol import ImageIntegrationProtocol

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
WEB_APP_DIR = ROOT_DIR / "web-app"
if str(WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_APP_DIR))

from labelled_table_sync import sync_labelled_rows  # noqa: E402
from config import COLD_ARCHIVE_ROOT, DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402

DEFAULT_ENV_PATH = str(ROOT_DIR / ".env")


# Custom class to redirect stdout/stderr to logger
class StreamToLogger:
    """Redirect print statements to logger"""
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
    log_filename = f"execute_image_integration_protocol_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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


# Log markers parsed by determine_resume_start. These are strings this same
# script emits, so the resume contract stays stable as long as they match.
_SRC_DIR_MARKER = "Source directory: "
_PROCESSING_MARKER = "Processing case "
_COMPLETED_MARKER = "Successfully completed processing case "


def determine_resume_start(logs_dir, current_log_path, src_dir, nhc_list, logger=None):
    """Position-based resume: index in nhc_list at which to start processing.

    Parses the most recent prior run log whose 'Source directory:' header matches
    src_dir, finds the last case it reached, and returns where to resume:
      - interrupted mid-case  -> that case's index (re-analyze it)
      - case completed cleanly -> the next index (skip it too)
    Returns 0 (process everything) when there is no matching prior log, the
    marker can't be found, or the resume case no longer exists in nhc_list.
    """
    def _log(msg):
        if logger is not None:
            logger.info(msg)

    index_by_case = {nhc: i for i, nhc in enumerate(nhc_list)}
    current = os.path.abspath(current_log_path) if current_log_path else None

    pattern = os.path.join(logs_dir, "execute_image_integration_protocol_*.log")
    # Filename timestamp sorts chronologically, so reverse() is newest-first.
    candidates = sorted(glob.glob(pattern), reverse=True)

    for log_path in candidates:
        if current is not None and os.path.abspath(log_path) == current:
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
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
        if prior_src is None:
            continue  # not a real run log (e.g. crashed before header); keep looking
        if prior_src != src_dir:
            # Most recent matching-batch run wins; a different batch -> keep
            # scanning older logs rather than treating this as "no resume".
            continue

        # Last case the prior run reached.
        resume_case = None
        completed = False
        for line in lines:
            p = line.find(_PROCESSING_MARKER)
            if p != -1:
                resume_case = line[p + len(_PROCESSING_MARKER):].strip()
                completed = False  # reset; a later completion line re-sets it
            elif resume_case is not None:
                c = line.find(_COMPLETED_MARKER)
                if c != -1 and line[c + len(_COMPLETED_MARKER):].strip() == resume_case:
                    completed = True

        if resume_case is None:
            _log(f"Resume: prior log {os.path.basename(log_path)} matched src_dir "
                 "but reached no case; processing all cases")
            return 0
        if resume_case not in index_by_case:
            _log(f"Resume: prior log's last case {resume_case!r} is no longer in "
                 "the source directory; processing all cases")
            return 0

        start = index_by_case[resume_case] + (1 if completed else 0)
        start = min(start, len(nhc_list))  # completed last case -> nothing to do
        _log(f"Resume: matched prior log {os.path.basename(log_path)} "
             f"(last case {resume_case}, {'completed' if completed else 'interrupted'})")
        return start

    return 0


DEFAULT_CONFIG_PATH = Path(__file__).with_name("execute_image_integration_protocol.yaml")


def load_config(config_path):
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw_yaml = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw_yaml) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file must contain a top-level mapping: {config_path}")

    # Storage paths default from repo-root config.toml so the integration
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
        "cleanup_loose_after_indexing": False,
        "resume": True,
    }
    merged_config = {**defaults, **config}
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


def execute_image_integration_protocol(
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
):
    # Create an instance of the ImageIntegrationProtocol class
    protocol = ImageIntegrationProtocol(
        case_dir,
        postgres_engine,
        anonymize_files=anonymize_files,
        delete_originals_after_verification=delete_originals_after_verification,
        import_id=import_id,
        import_label=import_label,
        dataset=dataset,
        cold_archive_root=cold_archive_root,
    )
    # Execute the protocol
    return protocol.execute_image_integration_protocol(overwrite_if_exists=overwrite_if_exists)


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
    for series_uid, dicom_dir, archive in rows:
        if not archive:
            skipped.append(f"{series_uid}: no archive (compression failed?)")
            continue
        status, size, detail = clean_series_loose_dir(
            series_uid, Path(dicom_dir), Path(archive),
            series_in_orthanc=True, deep_verify=True, execute=True,
        )
        if status == "cleaned":
            cleaned += 1
            bytes_freed += size
        elif status != "already_clean":
            skipped.append(detail or f"{series_uid}: {status}")
    logger.info(f"Loose cleanup: removed {cleaned} DICOM dir(s) "
                f"({bytes_freed/1e6:.1f} MB freed)")
    for msg in skipped:
        logger.warning(f"Loose cleanup skipped — {msg}")


if __name__ == "__main__":
    from sqlalchemy import create_engine
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Execute the Stanford image integration protocol.")
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

    logger.info("Starting image integration protocol execution")
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
    run_import_id = ImageIntegrationProtocol.get_next_import_id(postgres_engine)

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
    logger.info(f"Storage mode (config.toml): {config['_storage_mode']}")
    logger.info(f"DICOM data root (config.toml): {config['_dicom_data_root']}")
    logger.info(f"Cold archive root (resolved): {config.get('cold_archive_root')}")
    logger.info(f"Resume from prior run: {resume_enabled}")

    error_log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', f"error_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    if os.path.exists(error_log_file_path):
        with open(error_log_file_path, 'r') as f:
            error_log = json.load(f)
    else:
        error_log = {}

    total_cases = 0
    processed_cases = 0
    skipped_cases = 0
    resumed_skipped = 0
    failed_cases = 0
    indexing_failed_cases = 0
    synced_study_ids = set()
    synced_series_ids = set()

    try:
        nhc_list = sorted(
            nhc for nhc in os.listdir(src_dir)
            if not nhc.startswith(".") and os.path.isdir(os.path.join(src_dir, nhc))
        )
        total_cases = len(nhc_list)
        logger.info(f"Found {total_cases} cases to process")

        logs_dir = os.path.dirname(log_filepath)
        start_index = 0
        if resume_enabled:
            start_index = determine_resume_start(
                logs_dir, log_filepath, src_dir, nhc_list, logger
            )
            if start_index >= len(nhc_list):
                logger.info(
                    "Resume: prior run already completed all cases for this "
                    "source directory; nothing to do (use --no-resume to re-run)"
                )
            elif start_index > 0:
                logger.info(
                    f"Resume: skipping {start_index} case(s) already processed in "
                    f"the prior run; resuming at {nhc_list[start_index]}"
                )
            else:
                logger.info("Resume: no matching prior run found; processing all cases")
        else:
            logger.info("Resume disabled (--no-resume); processing all cases")

        for idx, nhc in enumerate(nhc_list):
            if idx < start_index:
                logger.info(f"Skipping case {nhc} - already processed in prior run (resume)")
                resumed_skipped += 1
                continue
            case_dir = os.path.join(src_dir, nhc)
            if len(os.listdir(case_dir)) > 0:
                logger.info(f"Processing case {nhc}")
                try:
                    result = execute_image_integration_protocol(
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
                        and start_index > 0
                        and idx == start_index
                        and not to_index
                        and skipped_existing
                    ):
                        # Resume boundary: the prior run was interrupted at this
                        # case AFTER its DB commit but possibly before/mid
                        # indexing. Its series are already in image_series, so
                        # re-index them (idempotent; already-registered files
                        # are fast to skip).
                        logger.info(
                            f"Resume boundary: re-indexing "
                            f"{len(skipped_existing)} already-ingested series "
                            f"for case {nhc}"
                        )
                        to_index = skipped_existing
                        synced_series_ids.update(skipped_existing)
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
                        error_log[f"{nhc}#indexing"] = {
                            "error": str(e),
                            "type": "indexing_error",
                            "traceback": traceback.format_exc().splitlines(),
                        }
                        with open(error_log_file_path, 'w') as f:
                            json.dump(error_log, f, indent=4)
                        indexing_failed_cases += 1

                    if config["cleanup_loose_after_indexing"] and indexed_uids:
                        try:
                            cleanup_case_loose_dirs(
                                postgres_engine, logger, indexed_uids)
                        except Exception as e:
                            logger.error(
                                f"Loose cleanup failed for case {nhc} "
                                f"(non-fatal; loose files remain on disk): {e}"
                            )

                    logger.info(f"Successfully completed processing case {nhc}")
                    processed_cases += 1
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

    except Exception as e:
        logger.error(f"Failed to list cases in source directory {src_dir}: {e}")
        raise

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
    logger.info("IMAGE INTEGRATION PROTOCOL SUMMARY")
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
    logger.info("Image integration protocol execution completed")