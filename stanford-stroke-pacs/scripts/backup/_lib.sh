# Shared helpers for the backup scripts. Source this file; do not execute it.
#
# Provides a single source of truth for repo-relative paths and operational
# settings, so the scripts are deployable on any host without editing absolute
# paths:
#   STACK_DIR    — stanford-stroke-pacs/ (resolved from this file's location)
#   CONFIG_TOML  — $STACK_DIR/config.toml
#   config_get <section> <key> <fallback>
#                — echo config.toml [section].key, or <fallback> if the file,
#                  section, key, or python3 is unavailable.
#
# Resolution precedence the callers should use: explicit env override (if set)
# > config.toml value > built-in fallback passed to config_get.

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$_LIB_DIR/../.." && pwd)"
CONFIG_TOML="$STACK_DIR/config.toml"

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
