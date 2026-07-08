#!/usr/bin/env bash
#
# Ingest every LVO_SIR-CRISP2 batch in sequence, driving
# execute_image_ingestion_protocol.py once per batch.
#
# For each batch it derives a per-batch config from the base YAML
# (execute_image_ingestion_protocol.yaml), overriding only src_dir and
# import_label, then runs the protocol. All other settings (database, dataset,
# anonymize, overwrite, cold_archive_root resolution, resume) come from the base
# YAML / config.toml unchanged.
#
# Resume is per-src_dir (the Python script keys resume on the "Source directory:"
# log header), so this whole script is idempotent: re-run it after an
# interruption and each batch picks up where it left off; already-finished
# batches report "nothing to do" and cost nothing.
#
# Usage:
#   ./run_all_batches.sh                 # batch1..batch7 (default)
#   ./run_all_batches.sh batch3 batch4   # only the named batches, in order
#
# SRC_ROOT / LABEL_PREFIX / CONDA_ENV are env-overridable for the next
# campaign, e.g.:
#   SRC_ROOT=/Volumes/Disk/NEW_COHORT LABEL_PREFIX=new ./run_all_batches.sh b1 b2
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="${SRC_ROOT:-/Volumes/ThunderBay_RAID1/LVO_SIR-CRISP2}"
BASE_CONFIG="$SCRIPT_DIR/execute_image_ingestion_protocol.yaml"
CONFIG_DIR="$SCRIPT_DIR/batch_configs"
LABEL_PREFIX="${LABEL_PREFIX:-sir}"
CONDA_ENV="${CONDA_ENV:-ssc-pacs}"

# Batches to ingest, in order. Note: rapid_processed is intentionally excluded
# (it is not a patient batch and reuses batch IDs). Override via CLI args.
BATCHES=(batch1 batch2 batch3 batch4 batch5 batch6 batch7)
if [ "$#" -gt 0 ]; then
    BATCHES=("$@")
fi

if [ ! -f "$BASE_CONFIG" ]; then
    echo "ERROR: base config not found: $BASE_CONFIG" >&2
    exit 1
fi

# Activate the conda env so the run is self-contained (safe for unattended use).
if command -v conda >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate "$CONDA_ENV" || {
        echo "ERROR: could not activate conda env '$CONDA_ENV'" >&2
        exit 1
    }
else
    echo "WARNING: conda not found; assuming the '$CONDA_ENV' env is already active." >&2
fi

mkdir -p "$CONFIG_DIR"
cd "$SCRIPT_DIR"

RUNNER_LOG="$SCRIPT_DIR/logs/run_all_batches_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$SCRIPT_DIR/logs"

echo "Batches to ingest: ${BATCHES[*]}" | tee -a "$RUNNER_LOG"
echo "Runner log: $RUNNER_LOG"

declare -a FAILED=()

for batch in "${BATCHES[@]}"; do
    src="$SRC_ROOT/$batch"
    label="${LABEL_PREFIX}_${batch}"
    cfg="$CONFIG_DIR/${batch}.yaml"

    if [ ! -d "$src" ]; then
        echo "[$(date '+%F %T')] SKIP $batch — missing source dir: $src" | tee -a "$RUNNER_LOG"
        FAILED+=("$batch(missing)")
        continue
    fi

    # Derive the per-batch config: copy the base, override src_dir + import_label.
    sed -E \
        -e "s|^src_dir:.*|src_dir: $src|" \
        -e "s|^import_label:.*|import_label: $label|" \
        "$BASE_CONFIG" > "$cfg"

    echo "===================================================================" | tee -a "$RUNNER_LOG"
    echo "[$(date '+%F %T')] START $batch  (label=$label, src=$src)" | tee -a "$RUNNER_LOG"
    echo "===================================================================" | tee -a "$RUNNER_LOG"

    python execute_image_ingestion_protocol.py --config "$cfg"
    rc=$?

    if [ "$rc" -eq 0 ]; then
        echo "[$(date '+%F %T')] DONE  $batch (exit 0)" | tee -a "$RUNNER_LOG"
    else
        echo "[$(date '+%F %T')] FAIL  $batch (exit $rc) — continuing to next batch" | tee -a "$RUNNER_LOG"
        FAILED+=("$batch(exit $rc)")
    fi
done

echo "===================================================================" | tee -a "$RUNNER_LOG"
if [ "${#FAILED[@]}" -eq 0 ]; then
    echo "[$(date '+%F %T')] All batches finished cleanly." | tee -a "$RUNNER_LOG"
else
    echo "[$(date '+%F %T')] Completed with issues in: ${FAILED[*]}" | tee -a "$RUNNER_LOG"
    echo "Re-run ./run_all_batches.sh to resume (idempotent per batch)." | tee -a "$RUNNER_LOG"
fi
