# Observability — logs, health, and metrics

The web app service emits JSON-structured logs, exposes a dependency
health probe at `/healthz`, and serves Prometheus metrics at `/metrics`.
This document is the operator-facing reference for all three.

> **Scope note.** §§1–3 (logging, `/healthz`, `/metrics`) describe what the
> **running web app** exposes. §4 is the Prometheus + Grafana stack that
> scrapes `/metrics` on the production host (Docker Compose under
> `~/monitoring/`, outside this repo). Only the web app is scraped — Orthanc,
> PostgreSQL and the host itself have no exporters, so what you can see is
> web-app traffic, errors, latency and the cold-storage cache.

---

## 1. Structured logging

### Format

Every log line is a single JSON object. The core fields:

| Field            | Source                                 |
|------------------|----------------------------------------|
| `timestamp`      | ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SS`)   |
| `level`          | `INFO` / `WARNING` / `ERROR` / …       |
| `logger`         | `__name__` of the emitting module      |
| `message`        | Log message (may carry `%s` formatting) |
| `request_id`     | UUID set by the middleware, empty off-request |
| `user`           | Authenticated username, empty when anonymous |

Additional fields are attached via `extra=` at the call site. The most
common ones:

| Field         | Where                               |
|---------------|-------------------------------------|
| `study_uid`   | `cache_manager.warm_study` / `evict_study`, `eviction_loop` |
| `series_uid`  | `cache_manager.warm_series` / `evict_series` (warm/evict are per-series) |
| `http_method`, `http_path`, `http_path_template`, `http_status`, `duration_seconds` | Per-request log line from the middleware |

`warm_study` runs in `app.state.warm_executor` (a bounded
`ThreadPoolExecutor`) rather than on the request thread. `request_id`
and `user` still appear on its log lines because Python copies the
caller's contextvars into the executor task when
`loop.run_in_executor()` is invoked — no per-call plumbing needed.

### Configuration

`web-app/logging_config.py` wires up the root logger with
`python-json-logger`. Two contextvars are used to thread request-scoped
context through every record:

- `request_id_ctx` — stamped by the request-ID middleware.
- `user_ctx`       — populated from the JWT cookie by the same middleware.

The middleware resets both tokens in a `finally` block so contextvars
don't leak between tasks.

### Log level

Set via the `LOG_LEVEL` environment variable (default `INFO`). `DEBUG`
enables verbose output from warm/evict and annotation CRUD. Bump it
temporarily when diagnosing a sticky warm failure:

```bash
# Linux (systemd):
sudo systemctl set-environment LOG_LEVEL=DEBUG && sudo systemctl restart ssc-web-app
# ...reproduce, then clear it again:
sudo systemctl unset-environment LOG_LEVEL     && sudo systemctl restart ssc-web-app

# macOS (launchd): add LOG_LEVEL to the daemon's EnvironmentVariables dict
# in /Library/LaunchDaemons/com.ssc.webapp.plist, then kickstart:
#   sudo /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:LOG_LEVEL string DEBUG" \
#     /Library/LaunchDaemons/com.ssc.webapp.plist
#   sudo launchctl kickstart -k system/com.ssc.webapp
#   ...reproduce, then delete the key and kickstart again:
#   sudo /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:LOG_LEVEL" \
#     /Library/LaunchDaemons/com.ssc.webapp.plist
#   sudo launchctl kickstart -k system/com.ssc.webapp
```

### Reading logs

On Linux (systemd) the records come from the journal. Each line is a JSON
object, so pipe `-o cat` through `jq`:

```bash
# Last 50 lines, pretty-printed
sudo journalctl -u ssc-web-app -n 50 -o cat | jq .

# All lines for one request
RID=$(curl -sI http://localhost:8043/api/me | grep -i X-Request-ID | cut -d: -f2 | tr -d ' \r')
sudo journalctl -u ssc-web-app -o cat | grep "\"request_id\": \"$RID\""

# All warm/evict activity for one study
sudo journalctl -u ssc-web-app -o cat | jq 'select(.study_uid == "<UID>")'

# Just the request rate over the whole journal
sudo journalctl -u ssc-web-app -o cat \
  | jq -r 'select(.message == "request") | [.http_status, .http_path_template] | @tsv' \
  | sort | uniq -c | sort -rn
```

On macOS (launchd) the same records land in flat JSON files at
`~/Library/Logs/ssc-web-app.log` (stdout) and `ssc-web-app.err` (stderr) — run
the same `jq` filters against those files (e.g. `tail -n 50
~/Library/Logs/ssc-web-app.log | jq .`).

### Rotation

Linux (systemd journal): retention is controlled globally by
`/etc/systemd/journald.conf` — recommended for the PACS host:

```ini
# /etc/systemd/journald.conf — merge these in the [Journal] block.
SystemMaxUse=2G
SystemKeepFree=5G
MaxRetentionSec=30day
MaxFileSec=1day
ForwardToSyslog=no
```

Apply with `sudo systemctl restart systemd-journald`. This caps the journal
at ~2 GiB and keeps ~30 days of history — enough for incident triage without
eating the root volume.

macOS (launchd): the flat log files are rotated by the OS `newsyslog`/`ASL`
machinery (or add a `newsyslog.d` entry to cap size). The web app itself does
not manage rotation.

---

## 2. `/healthz`

Unauthenticated liveness probe returning JSON:

```json
{
  "status": "ok",
  "version": "42ddf25de81f",
  "db_stanford_stroke": "ok",
  "db_orthanc": "ok",
  "orthanc_api": "ok",
  "disk_free_percent_dicom_data_root": 46.6,
  "disk_free_bytes_dicom_data_root": 1833597009920
}
```

### HTTP codes

- `200` — all **critical** checks pass.
- `503` — at least one critical check failed (`"status": "degraded"`).

**Critical checks:**
- `db_stanford_stroke` must be `ok` (web app cannot serve its own
  data without it).
- When `STORAGE_MODE == "cold_path_cache"`, `orthanc_api` must also be
  `ok` (OHIF can't render otherwise).

**Non-critical but reported:**
- `db_orthanc` — connected via `PG_ORTHANC_USER`/`PG_ORTHANC_PASSWORD`.
  Reported as `"unconfigured"` if those env vars are absent.
- `disk_free_*_dicom_data_root` — observability only; a `null` here
  does **not** degrade `/healthz`.

### Version field

`version` is the 12-character git SHA of `HEAD`, resolved from
`.git/HEAD` on startup. `"unknown"` if the directory is not a git
checkout (e.g. tarball deploy).

### Wiring into a probe

For a reverse proxy / load balancer:

```
health_check_path = /healthz
interval = 30s
timeout = 5s
unhealthy_threshold = 2
```

The endpoint runs two DB connects and one HTTP call to Orthanc — each
bounded at 3 s — so total worst-case latency is ~9 s on a fully dead
dependency stack. Size your probe timeout accordingly.

---

## 3. `/metrics` (Prometheus)

Unauthenticated, same as `/healthz`.

### Starter metric catalogue

| Name                                                         | Type      | Labels                                 | Meaning |
|--------------------------------------------------------------|-----------|----------------------------------------|---------|
| `http_requests_total`                                        | counter   | `method`, `path_template`, `status`    | Total HTTP requests by matched route template and status. |
| `http_request_duration_seconds`                              | histogram | `method`, `path_template`              | Request latency. Buckets tuned for fast JSON + long warm/evict. |
| `cold_storage_warm_total`                                    | counter   | `result` ∈ {`success`, `failure`, `insufficient_disk_space`} | `POST /api/studies/{uid}/warm` outcomes. |
| `cold_storage_evict_total`                                   | counter   | `trigger` ∈ {`manual`, `ttl`}, `result` ∈ {`success`, `failure`} | Per-series evictions. `manual` = the study/series `POST …/evict` routes; `ttl` = the background eviction loop (`eviction_ttl_hours`), which is where almost all evictions happen. |
| `cold_storage_warming_rows`                                  | gauge     | —                                      | `series_cache_state` rows currently in `status='warming'`, refreshed on scrape (no new label). |
| `cold_storage_disk_free_bytes`                               | gauge     | —                                      | Free bytes on the filesystem holding `dicom_data_root`, refreshed on scrape. |

`path_template` is the matched FastAPI route template (e.g.
`/api/studies/{studyinstanceuid}/warm`) — **never the concrete path**,
so UIDs don't explode cardinality.

The `/metrics` handler refreshes the two gauges on every scrape
(`metrics.refresh_cold_storage_gauges`). The evict counter is
incremented in the route handlers (`manual`) and per series inside
`cache_manager.run_eviction` (`ttl`); `cold_storage_warm_total` is
incremented from the **worker thread** that runs the extraction
(`routes/cold_storage._run_warm_with_metrics`) so the `success` /
`failure` label reflects the actual extraction outcome rather than
the synchronous 202 response. The `insufficient_disk_space` variant
is still emitted from the route handler at precheck time.

### Reconciliation gauges (CLI-process-local — not on the web app `/metrics`)

`scripts/data_integrity/reconcile.py` also defines gauges:

| Name                                 | Type  | Labels | Meaning |
|--------------------------------------|-------|--------|---------|
| `reconciliation_mismatches_total`    | gauge | `category` ∈ {`in_db_not_in_orthanc`, `in_orthanc_not_in_db`, `dicom_archive_missing`, `orphaned_annotations`} | Count per mismatch category from the last run. |
| `reconciliation_last_run_timestamp`  | gauge | —      | Unix epoch of the last reconciliation run. |
| `reconciliation_duration_seconds`    | gauge | —      | Duration of the last run. |

**These do not appear on the web app `/metrics` endpoint.** `reconcile.py`
updates them in its **own** process registry and then exits — the web app is
a separate process and never calls that code, so scraping `:8043/metrics`
shows them as 0 / absent. See
[`reconciliation.md`](reconciliation.md#prometheus-metrics).

### Scraping and alerting

The stack in §4 scrapes `/metrics` every 30 s. **No alert rules or
Alertmanager are configured** (deliberately, as of 2026-10-08): the data is
there to look at, nothing pages anyone. The queries in §4 are the ones to
run by hand.

---

## 4. Prometheus + Grafana stack

Runs on the production host as a Docker Compose project outside this repo:

| | |
|---|---|
| Compose dir | `~/monitoring/` of the deploying user (`docker-compose.yml`, `prometheus.yml`, `provisioning/`, `dashboards/`) |
| Containers | `ssc-prometheus` (`prom/prometheus`), `ssc-grafana` (`grafana/grafana`) |
| Ports | Grafana **3000**, Prometheus **9090** — both bound on all interfaces, so reach them directly or tunnel: `ssh -L 3000:localhost:3000 -L 9090:localhost:9090 <host>` |
| Scrape | one job, `ssc-companion` → `host.docker.internal:8043/metrics`, every 30 s |
| Retention | 30 days (`docker exec ssc-prometheus prometheus --version`; check with `curl -s localhost:9090/api/v1/status/runtimeinfo`) |
| Dashboard | provisioned from `~/monitoring/dashboards/ssc-companion.json`; `docs/operations/grafana_dashboard.json` is the portable copy with a `${DS_PROMETHEUS}` variable — note its queries say `job="ssc-web-app"`, so rename the job when importing elsewhere |

Status at a glance:

```bash
docker ps --filter name=ssc-grafana --filter name=ssc-prometheus
curl -s localhost:3000/api/health
curl -s 'localhost:9090/api/v1/targets' | jq '.data.activeTargets[] | {job: .labels.job, health, lastError}'
```

Useful ad-hoc queries (paste into Prometheus at `:9090/graph` or a Grafana
Explore panel):

```promql
# what is being hit, last 24 h
topk(10, sum by (path_template, method) (increase(http_requests_total[24h])))
# server errors by route
sum by (path_template, status) (increase(http_requests_total{status=~"5.."}[24h])) > 0
# p95 latency by route
histogram_quantile(0.95, sum by (le, path_template) (rate(http_request_duration_seconds_bucket[1h])))
# cold-storage cache churn: warms vs TTL evictions
sum by (result) (increase(cold_storage_warm_total[24h]))
sum by (trigger, result) (increase(cold_storage_evict_total[24h]))
# free space on the DICOM root, GB
cold_storage_disk_free_bytes / 1e9
```

What the data cannot tell you: anything about Orthanc, PostgreSQL or the host
(no exporters), and the reconciliation gauges (see §3 — they only exist in the
`reconcile.py` process). Prometheus `rate()`/`increase()` handle the counter
resets from a web-app restart, so no operator action is needed across restarts.

If the stack needs rebuilding, the Compose dir needs: `docker-compose.yml`
(the two services, data on named volumes), `prometheus.yml` (the scrape
job above), `provisioning/datasources/` (Prometheus datasource) and
`provisioning/dashboards/` pointing at a `dashboards/` copy of
`grafana_dashboard.json`. Grafana's first login is `admin`/`admin`.

---

## 5. Frontend correlation

The request-ID middleware writes `X-Request-ID` on every response. The
frontend's `api/client.js` wrapper should surface this header in error
toasts so a user-visible error message can be grepped straight out of the
logs (`journalctl -u ssc-web-app -o cat | grep <request_id>`; on macOS
`grep <request_id> ~/Library/Logs/ssc-web-app.log`). Wiring that up is
a follow-up task.
