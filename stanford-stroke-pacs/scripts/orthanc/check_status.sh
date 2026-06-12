#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# _lib.sh resolves STACK_DIR from its own location so the stack .env is found
# without hardcoding an absolute path.
# shellcheck source=../_lib.sh
source "$SCRIPT_DIR/../_lib.sh"
CONTAINER="ssc-orthanc"   # pinned by container_name in docker-compose.yml
# Env override > stack .env > fallback (strip surrounding quotes, keep =/$ in passwords)
ENV_FILE="$STACK_DIR/.env"
env_get() { grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | sed "s/^['\"]//;s/['\"]$//"; }
ORTHANC_URL="${ORTHANC_URL:-$(env_get ORTHANC_URL)}"
ORTHANC_URL="${ORTHANC_URL:-http://localhost:8042}"
ORTHANC_USER="${ORTHANC_ADMIN_USER:-$(env_get ORTHANC_ADMIN_USER)}"
ORTHANC_PASSWORD="${ORTHANC_ADMIN_PASSWORD:-$(env_get ORTHANC_ADMIN_PASSWORD)}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✔${NC} $1"; }
fail() { echo -e "  ${RED}✘${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
info() { echo -e "  ${CYAN}ℹ${NC} $1"; }

ERRORS=0

echo -e "\n${BOLD}═══════════════════════════════════════${NC}"
echo -e "${BOLD}  Stanford Stroke Center PACS – Status Check${NC}"
echo -e "${BOLD}═══════════════════════════════════════${NC}"
echo -e "  $(date '+%Y-%m-%d %H:%M:%S')\n"

# ── 1. Docker daemon ────────────────────────────────────────────────
echo -e "${BOLD}[1/5] Docker Daemon${NC}"
if docker info &>/dev/null; then
    pass "Docker daemon is reachable"
else
    fail "Cannot connect to Docker daemon"
    echo -e "\n${RED}Cannot proceed without Docker. Exiting.${NC}"
    exit 1
fi

# ── 2. Container state ──────────────────────────────────────────────
echo -e "\n${BOLD}[2/5] Container${NC}"
STATE=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")

if [[ "$STATE" == "running" ]]; then
    UPTIME=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER")
    UPTIME_HUMAN=$(docker ps --filter "name=$CONTAINER" --format '{{.Status}}')
    pass "Container ${CYAN}${CONTAINER}${NC} is running  (${UPTIME_HUMAN})"
else
    fail "Container ${CONTAINER} is ${STATE}"
    ERRORS=$((ERRORS + 1))
    if [[ "$STATE" == "not_found" ]]; then
        info "Try: $SCRIPT_DIR/dc.sh up -d"
    else
        info "Try: $SCRIPT_DIR/dc.sh restart"
    fi
fi

# ── 3. Resource usage ───────────────────────────────────────────────
echo -e "\n${BOLD}[3/5] Resource Usage${NC}"
if [[ "$STATE" == "running" ]]; then
    STATS=$(docker stats "$CONTAINER" --no-stream --format '{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}')
    CPU=$(echo "$STATS" | cut -f1)
    MEM=$(echo "$STATS" | cut -f2)
    MEM_PCT=$(echo "$STATS" | cut -f3)
    NET=$(echo "$STATS" | cut -f4)
    BLOCK=$(echo "$STATS" | cut -f5)
    info "CPU:    ${CPU}"
    info "Memory: ${MEM}  (${MEM_PCT})"
    info "Net IO: ${NET}"
    info "Disk:   ${BLOCK}"
else
    warn "Skipped – container not running"
fi

# ── 4. Orthanc API ──────────────────────────────────────────────────
echo -e "\n${BOLD}[4/5] Orthanc API${NC}"
if [[ "$STATE" == "running" ]]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -u "${ORTHANC_USER}:${ORTHANC_PASSWORD}" \
        --max-time 5 "${ORTHANC_URL}/system" 2>/dev/null || echo "000")

    if [[ "$HTTP_CODE" == "200" ]]; then
        pass "REST API responding (HTTP ${HTTP_CODE})"

        SYS=$(curl -s -u "${ORTHANC_USER}:${ORTHANC_PASSWORD}" --max-time 5 "${ORTHANC_URL}/system")
        VERSION=$(echo "$SYS" | python3 -c "import sys,json; print(json.load(sys.stdin)['Version'])" 2>/dev/null || echo "?")
        DB_PLUGIN=$(echo "$SYS" | python3 -c "import sys,json; d=json.load(sys.stdin); print('PostgreSQL' if 'PostgreSQL' in d.get('DatabaseBackendPlugin','') else 'SQLite')" 2>/dev/null || echo "?")
        STORAGE=$(echo "$SYS" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Indexer' if 'Indexer' in d.get('StorageAreaPlugin','') else 'Filesystem')" 2>/dev/null || echo "?")

        info "Version:  ${VERSION}"
        info "DB Index: ${DB_PLUGIN}"
        info "Storage:  ${STORAGE}"

        STATS_JSON=$(curl -s -u "${ORTHANC_USER}:${ORTHANC_PASSWORD}" --max-time 5 "${ORTHANC_URL}/statistics" 2>/dev/null)
        if [[ -n "$STATS_JSON" ]]; then
            PATIENTS=$(echo "$STATS_JSON"  | python3 -c "import sys,json; print(json.load(sys.stdin)['CountPatients'])" 2>/dev/null || echo "?")
            STUDIES=$(echo "$STATS_JSON"   | python3 -c "import sys,json; print(json.load(sys.stdin)['CountStudies'])" 2>/dev/null || echo "?")
            SERIES=$(echo "$STATS_JSON"    | python3 -c "import sys,json; print(json.load(sys.stdin)['CountSeries'])" 2>/dev/null || echo "?")
            INSTANCES=$(echo "$STATS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['CountInstances'])" 2>/dev/null || echo "?")
            DISK_MB=$(echo "$STATS_JSON"   | python3 -c "import sys,json; print(json.load(sys.stdin)['TotalDiskSizeMB'])" 2>/dev/null || echo "?")

            if [[ "$DISK_MB" =~ ^[0-9]+$ ]]; then
                DISK_HUMAN=$(python3 -c "mb=$DISK_MB; print(f'{mb/1024:.1f} GB') if mb>=1024 else print(f'{mb} MB')")
            else
                DISK_HUMAN="?"
            fi

            echo ""
            info "Patients:  ${PATIENTS}"
            info "Studies:   ${STUDIES}"
            info "Series:    ${SERIES}"
            info "Instances: ${INSTANCES}"
            info "Disk used: ${DISK_HUMAN}"
        fi
    elif [[ "$HTTP_CODE" == "401" ]]; then
        fail "API returned 401 – bad credentials"
        ERRORS=$((ERRORS + 1))
    elif [[ "$HTTP_CODE" == "000" ]]; then
        fail "API unreachable (connection refused / timeout)"
        ERRORS=$((ERRORS + 1))
    else
        fail "API returned unexpected HTTP ${HTTP_CODE}"
        ERRORS=$((ERRORS + 1))
    fi
else
    warn "Skipped – container not running"
fi

# ── 5. Plugins / endpoints ──────────────────────────────────────────
echo -e "\n${BOLD}[5/5] Plugin Endpoints${NC}"
if [[ "$STATE" == "running" && "$HTTP_CODE" == "200" ]]; then
    for ENDPOINT in "/ui/app/" "/ohif/" "/dicom-web/studies?limit=1" "/app/explorer.html"; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -u "${ORTHANC_USER}:${ORTHANC_PASSWORD}" \
            --max-time 10 "${ORTHANC_URL}${ENDPOINT}" 2>/dev/null || true)
        if [[ -z "$CODE" ]]; then
            CODE="000"
        fi
        LABEL=$(echo "$ENDPOINT" | sed 's|^/||;s|/$||')
        if [[ "$CODE" =~ ^(200|301|302)$ ]]; then
            pass "${LABEL}  (HTTP ${CODE})"
        else
            fail "${LABEL}  (HTTP ${CODE})"
            ERRORS=$((ERRORS + 1))
        fi
    done
else
    warn "Skipped – API not available"
fi

# ── Summary ─────────────────────────────────────────────────────────
echo -e "\n${BOLD}───────────────────────────────────────${NC}"
if [[ $ERRORS -eq 0 ]]; then
    echo -e "  ${GREEN}${BOLD}All checks passed.${NC}"
else
    echo -e "  ${RED}${BOLD}${ERRORS} check(s) failed.${NC}"
fi
echo ""

exit $ERRORS
