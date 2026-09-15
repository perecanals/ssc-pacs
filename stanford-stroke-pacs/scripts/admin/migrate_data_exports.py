#!/usr/bin/env python3
"""Rename host configuration and export storage; dry-run unless --execute.

Stop the web app before execution. Existing credentials and export files are
preserved. Alembic applies the table rename when the updated app starts.
Historical names appear here only to support the one-time upgrade.
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile
import tomllib
from contextlib import ExitStack
from pathlib import Path

from dotenv import dotenv_values
from psycopg2 import sql

STACK = Path(__file__).resolve().parents[2]
OLD_MARKER = ".ssc-explorer-spool"
NEW_MARKER = ".ssc-data-exports-spool"
OLD_ROLE_MARKER = "ssc-data-explorer dedicated reader"
NEW_ROLE_MARKER = "ssc-data-exports dedicated reader"


def plan(config_path, env_path):
    if config_path.is_symlink() or env_path.is_symlink():
        raise ValueError("Configuration and credential files must not be symlinks")
    config_text = config_path.read_text()
    env_text = env_path.read_text()
    config = tomllib.loads(config_text)
    if "data-explorer" in config and "data-exports" in config:
        raise ValueError("Both module sections exist; resolve the configuration conflict first")
    section = config.get("data-explorer", config.get("data-exports", {}))
    raw_path = section.get("spool_dir")
    source = Path(raw_path) if raw_path else None
    if source and not source.is_absolute():
        raise ValueError("The spool path must be absolute")
    target = source.with_name("data-exports") if source and source.name == "data-explorer" else source
    if source != target and source.exists() and target.exists():
        raise ValueError("Both spool directories exist; refusing to overwrite export files")
    existing = source if source and source.exists() else target
    if existing and existing.exists():
        if existing.is_symlink() or not existing.is_dir():
            raise ValueError("The spool must be a real directory")
        if existing.stat().st_uid != os.getuid() or existing.stat().st_mode & 0o077:
            raise ValueError("The spool must be owned by the app user with mode 0700")
        if (existing / OLD_MARKER).exists() and (existing / NEW_MARKER).exists():
            raise ValueError("Both spool markers exist; resolve the conflict first")
        if not any((existing / marker).is_file() for marker in (OLD_MARKER, NEW_MARKER)):
            raise ValueError("The existing spool has no module ownership marker")
        if any((existing / marker).is_symlink() for marker in (OLD_MARKER, NEW_MARKER)):
            raise ValueError("Spool ownership markers must not be symlinks")

    def rename_section(match):
        body = match[1]
        if source != target:
            body, count = re.subn(r"(?m)^(\s*spool_dir\s*=\s*).*$", lambda m: m[1] + json.dumps(str(target)), body)
            if count != 1:
                raise ValueError("Expected one spool_dir setting in the module section")
        return "[data-exports]\n" + body

    updated_config = re.sub(
        r"(?ms)^\[data-(?:explorer|exports)\][^\n]*\n(.*?)(?=^\[|\Z)",
        rename_section,
        config_text,
    )
    if "data-explorer" in tomllib.loads(updated_config):
        raise ValueError("Use an unquoted [data-explorer] section header before upgrading")
    env = dotenv_values(stream=io.StringIO(env_text))
    updated_env = env_text
    for suffix in ("USER", "PASSWORD"):
        old, new = f"EXPLORER_DB_{suffix}", f"DATA_EXPORTS_DB_{suffix}"
        if old in env and new in env:
            raise ValueError("Both old and new credential keys exist; resolve the conflict first")
        updated_env = re.sub(rf"(?m)^(\s*(?:export\s+)?)\b{old}(\s*=)", rf"\g<1>{new}\g<2>", updated_env)
    role = env.get("DATA_EXPORTS_DB_USER") or env.get("EXPLORER_DB_USER")
    files = [(config_path, updated_config), (env_path, updated_env)]
    return files, source, target, role


def write_private(path, text):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".data-exports-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def execute(config_path, env_path, conn):
    """Hold the worker lock and restore filesystem changes if any step fails."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(782341, 21)")
            if not cur.fetchone()[0]:
                raise ValueError("Stop the web app before migrating its export storage")
            files, source, target, role = plan(config_path, env_path)
            if role:
                cur.execute("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s", (role,))
                row = cur.fetchone()
                if row is None or row[0] not in (OLD_ROLE_MARKER, NEW_ROLE_MARKER):
                    raise ValueError("The configured reader lacks the module ownership marker")
            with ExitStack() as undo:
                if source != target and source.exists():
                    source.rename(target)
                    undo.callback(target.rename, source)
                if target and target.exists() and (target / OLD_MARKER).exists():
                    old = target / OLD_MARKER
                    new = target / NEW_MARKER
                    previous = old.read_text()
                    write_private(new, "SSC Data Exports temporary artifacts\n")
                    undo.callback(new.unlink, missing_ok=True)
                    old.unlink()
                    undo.callback(write_private, old, previous)
                for path, text in files:
                    previous = path.read_text()
                    if previous != text:
                        write_private(path, text)
                        undo.callback(write_private, path, previous)
                if role:
                    cur.execute(sql.SQL("COMMENT ON ROLE {} IS %s").format(sql.Identifier(role)), (NEW_ROLE_MARKER,))
                conn.commit()
                undo.pop_all()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Apply the rename after stopping the web app")
    args = parser.parse_args()
    config_path, env_path = STACK / "config.toml", STACK / ".env"
    try:
        _, source, target, _ = plan(config_path, env_path)
        print(
            "Rename module configuration, credential keys and spool ownership marker; preserve credentials and files."
        )
        if source != target:
            print(f"Spool: {source} -> {target}")
        if not args.execute:
            print("Dry run. Stop the web app, then run again with --execute.")
            return 0
        sys.path.insert(0, str(STACK / "web-app"))
        import psycopg2
        from db import DB_CONFIG

        execute(config_path, env_path, psycopg2.connect(**DB_CONFIG))
        print("Host naming upgraded. Build the frontend and start the app to apply Alembic migrations.")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Never expose credentials through a raw connection exception.
        print(
            "Upgrade failed; check file permissions and database connectivity. Files were restored where possible.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
