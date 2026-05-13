import os
import sys
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
COMPANION_DIR = ROOT_DIR / "companion"
if str(COMPANION_DIR) not in sys.path:
    sys.path.insert(0, str(COMPANION_DIR))

from labelled_table_sync import sync_labelled_rows
from config import COLD_ARCHIVE_ROOT, LEGACY_DICOM_ROOT, STORAGE_MODE  # noqa: E402

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
        "cold_archive_root": config_toml_archive_root,
    }
    merged_config = {**defaults, **config}
    merged_config["config_path"] = str(config_path)
    merged_config["_storage_mode"] = STORAGE_MODE
    merged_config["_legacy_dicom_root"] = str(LEGACY_DICOM_ROOT)
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


if __name__ == "__main__":
    from sqlalchemy import create_engine
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Execute the Stanford image integration protocol.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the YAML config file.",
    )
    args = parser.parse_args()
    config, raw_yaml = load_config(args.config)

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
    logger.info(f"Anonymize files: {config['anonymize_files']}")
    logger.info(
        "Delete originals after verification: "
        f"{config['delete_originals_after_verification']}"
    )
    logger.info(f"Overwrite existing studies: {config['overwrite_if_exists']}")
    logger.info(f"Storage mode (config.toml): {config['_storage_mode']}")
    logger.info(f"Legacy DICOM root (config.toml): {config['_legacy_dicom_root']}")
    logger.info(f"Cold archive root (resolved): {config.get('cold_archive_root')}")

    error_log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', f"error_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    if os.path.exists(error_log_file_path):
        with open(error_log_file_path, 'r') as f:
            error_log = json.load(f)
    else:
        error_log = {}

    total_cases = 0
    processed_cases = 0
    skipped_cases = 0
    failed_cases = 0
    synced_study_ids = set()
    synced_series_ids = set()

    try:
        nhc_list = sorted(
            nhc for nhc in os.listdir(src_dir)
            if not nhc.startswith(".") and os.path.isdir(os.path.join(src_dir, nhc))
        )
        total_cases = len(nhc_list)
        logger.info(f"Found {total_cases} cases to process")

        for nhc in nhc_list:
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
                        cold_archive_root=config.get("cold_archive_root"),
                    )
                    synced_study_ids.update(result["studyinstanceuids"])
                    synced_series_ids.update(result["seriesinstanceuids"])
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

    # Log final summary
    logger.info("=" * 60)
    logger.info("IMAGE INTEGRATION PROTOCOL SUMMARY")
    logger.info(f"Total cases found: {total_cases}")
    logger.info(f"Successfully processed: {processed_cases}")
    logger.info(f"Skipped (empty directories): {skipped_cases}")
    logger.info(f"Failed: {failed_cases}")
    logger.info("=" * 60)
    logger.info("Image integration protocol execution completed")