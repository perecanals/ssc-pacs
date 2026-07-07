# Shared helpers for the stack scripts. Source this file; do not execute it.
#
# Provides a single source of truth for repo-relative paths and operational
# settings, so the scripts are deployable on any host without editing absolute
# paths:
#   STACK_DIR    — stanford-stroke-pacs/ (resolved from this file's location)
#   CONFIG_TOML  — $STACK_DIR/config.toml
#   config_get <section> <key> <fallback>
#                — echo config.toml [section].key, or <fallback> if the file,
#                  section, key, or python3 is unavailable.
#   deploy_env_get <key>
#                — echo a KEY=value from the gitignored per-host deploy.env
#                  (stack root), or nothing. Read, not sourced, so callers'
#                  environments stay clean.
#   resolve_python
#                — echo the project Python interpreter: $SSC_PYTHON override,
#                  then deploy.env (PYTHON_BIN, CONDA_ENV_BIN — the same keys
#                  the unit installers use), then derived conventional conda
#                  locations, then PATH; non-zero if none. No literal host
#                  paths live here — per-host identity belongs in deploy.env
#                  (see documentation/reference/configuration_sources.md).
#
# Resolution precedence the callers should use: explicit env override (if set)
# > deploy.env / config.toml value > derived fallback.

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$_LIB_DIR/.." && pwd)"
CONFIG_TOML="$STACK_DIR/config.toml"
DEPLOY_ENV="$STACK_DIR/deploy.env"

config_get() {
    local section="$1" key="$2" fallback="$3" val=""
    if [[ -r "$CONFIG_TOML" ]] && command -v python3 >/dev/null 2>&1; then
        val="$(python3 - "$CONFIG_TOML" "$section" "$key" <<'PY' 2>/dev/null || true
import sys, tomllib
path, section, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    v = data.get(section, {}).get(key)
    if v is not None:
        print(v)
except Exception:
    pass
PY
)"
    fi
    printf '%s' "${val:-$fallback}"
}

deploy_env_get() {
    local key="$1" val=""
    if [[ -r "$DEPLOY_ENV" ]]; then
        val="$(sed -n "s/^[[:space:]]*${key}=//p" "$DEPLOY_ENV" | tail -1)"
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
    fi
    printf '%s' "$val"
}

resolve_python() {
    local candidates=("${SSC_PYTHON:-}")

    # Per-host identity from deploy.env — same keys the unit installers use.
    candidates+=("$(deploy_env_get PYTHON_BIN)")
    local conda_env_bin
    conda_env_bin="$(deploy_env_get CONDA_ENV_BIN)"
    [[ -n "$conda_env_bin" ]] && candidates+=("$conda_env_bin/python")

    # Derived conventional locations (no literal host paths).
    if command -v brew >/dev/null 2>&1; then
        local brew_prefix
        brew_prefix="$(brew --prefix 2>/dev/null || true)"
        [[ -n "$brew_prefix" ]] && candidates+=(
            "$brew_prefix/Caskroom/miniconda/base/envs/ssc-pacs/bin/python")
    fi
    local root env
    for root in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3"; do
        for env in ssc-pacs pacs; do
            candidates+=("$root/envs/$env/bin/python")
        done
    done

    local candidate
    for candidate in "${candidates[@]}"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    candidate="$(command -v python3 || true)"
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        printf '%s' "$candidate"
        return 0
    fi
    return 1
}
