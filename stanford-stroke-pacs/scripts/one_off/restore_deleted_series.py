#!/usr/bin/env python3
"""
SAFE recovery of the 6 series deleted on 2026-06-24 (see
maintenance/deleted_series_2026-06-24.md). Copies each series' DICOM directory
and cold archive from the OLD ThunderBay store back to the current Expansion
store at the identical relative path.

SAFETY (this script is COPY-ONLY by construction):
  * It never imports or calls anything that deletes/moves: no os.remove, no
    shutil.rmtree, no os.rename, no os.rmdir. Only os.makedirs + shutil.copytree
    + shutil.copy2.
  * shutil.copytree REFUSES to write if the destination already exists, and the
    archive copy is guarded by an explicit "dest must not exist" check — so an
    existing file can never be overwritten.
  * Dry-run by default; --execute required to copy.
  * After copying, it verifies the destination file count matches the source.

Usage:
  python scripts/one_off/restore_deleted_series.py            # dry-run plan
  python scripts/one_off/restore_deleted_series.py --execute  # copy + verify
"""

from __future__ import annotations

import argparse
import os
import shutil

OLD_ROOT = "/Volumes/ThunderBay_RAID1/ssc-pacs-data"
NEW_ROOT = "/Volumes/Expansion/ssc-pacs-data"

# Relative paths (patient/studyUID/seriesDesc/seriesUID) of the 6 deleted series.
RELS = [
    "2-541/1.2.826.0.1.3680043.8.498.11792295114794255814153145814225100983/3MM AXIAL HEAD WO CON/1.2.826.0.1.3680043.8.498.32359208132823927409418195545814563921",
    "2-516/1.2.826.0.1.3680043.8.498.96085580139132690074402381819523315133/3MM AXIAL HEAD WO CON/1.2.826.0.1.3680043.8.498.28137512477282027280224440373726023114",
    "2-506/1.2.826.0.1.3680043.8.498.11226078086560322455613186396840410412/Brain Ax 3D MRA_TOF/1.2.826.0.1.3680043.8.498.11171006158311228630573310150248357044",
    "2-502/1.2.826.0.1.3680043.8.498.46060407245085072707754228370668795549/Brain Ax 3D MRA_TOF/1.2.826.0.1.3680043.8.498.58978382558123661507943719563274479924",
    "2-528/1.2.826.0.1.3680043.8.498.62862303101418907629154982288605418782/b0-Brain Ax Neuromix/1.2.826.0.1.3680043.8.498.12074783882910074762814149478633723182",
    "2-488/1.2.826.0.1.3680043.8.498.13129829169035076002667194531599232250/b0-Brain Ax Neuromix/1.2.826.0.1.3680043.8.498.13975191647426128411022112978420722890",
]


def _count(d: str) -> int:
    return len([f for f in os.listdir(d)
                if not f.startswith(".") and os.path.isfile(os.path.join(d, f))]) if os.path.isdir(d) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="Perform the copy (default: dry-run)")
    args = ap.parse_args()

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"FROM {OLD_ROOT}  ->  TO {NEW_ROOT}\n")

    done = skipped = errors = 0
    for rel in RELS:
        src_dir = os.path.join(OLD_ROOT, "imaging_data", rel, "DICOM")
        dst_dir = os.path.join(NEW_ROOT, "imaging_data", rel, "DICOM")
        src_arch = os.path.join(OLD_ROOT, "compressed", rel, "DICOM.tar.zst")
        dst_arch = os.path.join(NEW_ROOT, "compressed", rel, "DICOM.tar.zst")
        pat = rel.split("/")[0]

        if not os.path.isdir(src_dir) or not os.path.isfile(src_arch):
            print(f"  ERROR {pat}: source missing on ThunderBay (dir={os.path.isdir(src_dir)} "
                  f"arch={os.path.isfile(src_arch)}) — skipping")
            errors += 1
            continue
        # Never overwrite: if either destination already exists, skip entirely.
        if os.path.exists(dst_dir) or os.path.exists(dst_arch):
            print(f"  SKIP  {pat}: destination already exists "
                  f"(dir={os.path.exists(dst_dir)} arch={os.path.exists(dst_arch)}) — not overwriting")
            skipped += 1
            continue

        n_src = _count(src_dir)
        print(f"  {'COPY' if args.execute else 'WOULD COPY'} {pat}: {n_src} files + archive")
        print(f"      dir : {dst_dir}")
        print(f"      arch: {dst_arch}")
        if not args.execute:
            continue

        os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
        shutil.copytree(src_dir, dst_dir)  # refuses if dst_dir exists
        os.makedirs(os.path.dirname(dst_arch), exist_ok=True)
        shutil.copy2(src_arch, dst_arch)
        n_dst = _count(dst_dir)
        ok = (n_dst == n_src and os.path.isfile(dst_arch))
        print(f"      verify: dst files={n_dst} (src={n_src}) archive={os.path.isfile(dst_arch)} "
              f"-> {'OK' if ok else 'MISMATCH!'}")
        if ok:
            done += 1
        else:
            errors += 1

    print(f"\n{'=' * 60}")
    print(f"{'Copied' if args.execute else 'Would copy'}: "
          f"{done if args.execute else len(RELS) - skipped - errors}   "
          f"Skipped(existing): {skipped}   Errors: {errors}")
    if not args.execute:
        print("DRY RUN — nothing written. Re-run with --execute to copy.")
    return 0 if errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
