"""Looking up — and, at a person's confirmation, creating — datasets.

Shared by ingestion's preflight and scripts/admin/manage_datasets.py, so an
unknown dataset name gets the same treatment everywhere: a new dataset or a
typo (which would file every patient under a new, unlinked identity) is only
created when someone at a terminal confirms it, with similar existing names
shown. Without a terminal it stops with the command to register it.
"""

from __future__ import annotations

import difflib
import re
import sys

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def dataset_slug_for(name: str) -> str:
    """The slug the 0026 migration derives from a dataset name (crisp2-lvo)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def resolve_or_offer_dataset(conn, name: str, *, stdin=None, stdout=None,
                             dry_run: bool = False) -> tuple[str, bool]:
    """(slug, created) for dataset ``name``; offers to create an unknown one.

    The INSERT runs in the caller's transaction — the caller commits (or rolls
    back a dry run). With ``dry_run`` an unknown name is reported, not
    prompted for. Raises SystemExit when the name is unknown and not created.
    """
    stdin = stdin or sys.__stdin__
    stdout = stdout or sys.__stdout__
    with conn.cursor() as cur:
        cur.execute("SELECT name, slug FROM dataset")
        registered = {r[0]: r[1] for r in cur.fetchall()}
    if name in registered:
        return registered[name], False

    slug = dataset_slug_for(name)
    by_lower = {n.lower(): n for n in registered}
    similar = difflib.get_close_matches(name.lower(), list(by_lower), n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(repr(by_lower[s]) for s in similar)}?" if similar else ""
    if not SLUG_RE.fullmatch(slug) or slug in registered.values():
        raise SystemExit(
            f"Dataset {name!r} is not registered and its derived slug {slug!r} is "
            f"unusable or taken.{hint} Register it with an explicit slug: "
            f"scripts/admin/manage_datasets.py add --slug <slug> --name {name!r}"
        )
    if dry_run:
        stdout.write(f"Dataset {name!r} is not registered.{hint} "
                     f"Would offer to create it (slug {slug!r}).\n")
        return slug, True
    if not (stdin and stdin.isatty()):
        raise SystemExit(
            f"Dataset {name!r} is not registered.{hint} If it is new, register it "
            f"first (scripts/admin/manage_datasets.py add --slug {slug} --name "
            f"{name!r}) or run from a terminal to confirm it."
        )
    stdout.write(
        f"\nDataset {name!r} is not registered.{hint}\n"
        f"Create it now as a new dataset (slug {slug!r}, permanent)? [y/N] "
    )
    stdout.flush()
    if stdin.readline().strip().lower() not in ("y", "yes"):
        raise SystemExit(f"Aborted: dataset {name!r} not created.")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dataset (slug, name) VALUES (%s, %s)", (slug, name))
    return slug, True
