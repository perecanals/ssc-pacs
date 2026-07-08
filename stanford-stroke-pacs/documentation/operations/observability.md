# Observability — logs, health, and metrics

The web app service emits JSON-structured logs, exposes a dependency
health probe at `/healthz`, and serves Prometheus metrics at `/metrics`.
This document is the operator-facing reference for all three.

> **Scope note.** §§1–3 (logging, `/healthz`, `/metrics`) describe what the
> **running web app** exposes today — all live. §§4–5 (Prometheus + Grafana
> scraping stack) are a **future-install template, not currently deployed**
> (decision 2026-07-07: not reinstalling the `~/monitoring/` stack). The web
> app emits metrics regardless; nothing is scraping them. `grafana_dashboard.json`
> is kept as the importable template for when a stack is stood up again.

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
# macOS (production): add LOG_LEVEL to the daemon's EnvironmentVariables dict
# in /Library/LaunchDaemons/com.ssc.webapp.plist, then kickstart:
sudo /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:LOG_LEVEL string DEBUG" \
  /Library/LaunchDaemons/com.ssc.webapp.plist
sudo launchctl kickstart -k system/com.ssc.webapp
# ...reproduce, then delete the key and kickstart again:
sudo /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:LOG_LEVEL" \
  /Library/LaunchDaemons/com.ssc.webapp.plist
sudo launchctl kickstart -k system/com.ssc.webapp

# Linux (systemd):
#   sudo systemctl set-environment LOG_LEVEL=DEBUG && sudo systemctl restart ssc-web-app
#   sudo systemctl unset-environment LOG_LEVEL     && sudo systemctl restart ssc-web-app
```

### Reading logs

On the macOS production host the daemon writes flat JSON files at
`~/Library/Logs/ssc-web-app.log` (stdout) and `ssc-web-app.err` (stderr).
Each line is a JSON object, so pipe through `jq`:

```bash
# Last 50 lines, pretty-printed
tail -n 50 ~/Library/Logs/ssc-web-app.log | jq .

# All lines for one request
RID=$(curl -sI http://localhost:8043/api/me | grep -i X-Request-ID | cut -d: -f2 | tr -d ' \r')
grep "\"request_id\": \"$RID\"" ~/Library/Logs/ssc-web-app.log

# All warm/evict activity for one study
jq 'select(.study_uid == "<UID>")' ~/Library/Logs/ssc-web-app.log

# Just the request rate over the whole file
jq -r 'select(.message == "request") | [.http_status, .http_path_template] | @tsv' \
  ~/Library/Logs/ssc-web-app.log | sort | uniq -c | sort -rn
```

On Linux the same records come from the journal
(`sudo journalctl -u ssc-web-app -o cat | jq .`).

### Rotation

macOS: the flat log files are rotated by the OS `newsyslog`/`ASL` machinery
(or add a `newsyslog.d` entry to cap size). The web app itself does not
manage rotation.

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
| `cold_storage_evict_total`                                   | counter   | `result` ∈ {`success`, `failure`}      | `POST /api/studies/{uid}/evict` outcomes. |
| `cold_storage_warming_rows`                                  | gauge     | —                                      | `series_cache_state` rows currently in `status='warming'`, refreshed on scrape (no new label). |
| `cold_storage_disk_free_bytes`                               | gauge     | —                                      | Free bytes on the filesystem holding `dicom_data_root`, refreshed on scrape. |

`path_template` is the matched FastAPI route template (e.g.
`/api/studies/{studyinstanceuid}/warm`) — **never the concrete path**,
so UIDs don't explode cardinality.

The `/metrics` handler refreshes the two gauges on every scrape
(`metrics.refresh_cold_storage_gauges`). The evict counter is
incremented in the route handler; `cold_storage_warm_total` is
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

Nothing scrapes `/metrics` today (see the scope note at the top). When a stack
is stood up again, a Prometheus scrape target and a starter set of alert rules
(web app down, 5xx error rate, p95 latency, cold-storage warm failures,
warming stuck, low disk) are part of that reinstall — see §4.

---

## 4. Future: Prometheus + Grafana stack (NOT DEPLOYED)

> **Not currently deployed.** The `~/monitoring/` Docker Compose stack
> (Prometheus + Grafana) was retired (decision 2026-07-07: not reinstalling).
> Nothing scrapes the web app's `/metrics` today. This section is the
> reinstall template for standing it back up.

The importable Grafana dashboard is kept in the repo:

| File | Purpose |
|------|---------|
| `documentation/operations/grafana_dashboard.json` | Portable **SSC Web App** dashboard with a `${DS_PROMETHEUS}` datasource variable — import into any Grafana. Panels: request rate, p50/p95 latency, warm/evict success rate, cold-storage disk free, warming rows over time. |

A minimal reinstall would recreate a `~/monitoring/` (or equivalent) with:

```
docker-compose.yml   # Prometheus + Grafana services
prometheus.yml       # scrape target host.docker.internal:8043 (see §3)
provisioning/         # Grafana datasource (Prometheus) + dashboard loader
dashboards/           # a copy of grafana_dashboard.json, datasource UID filled in
```

The `prometheus.yml` scrape target is `host.docker.internal:8043` (the web app
as seen from inside the container), scraped every 30 s. Provision a starter
alert set alongside it: web app down (`up == 0`), 5xx error rate
(`http_requests_total{status=~"5.."}` share > 5%), p95 latency
(`http_request_duration_seconds` > 2 s), cold-storage warm failures
(`cold_storage_warm_total{result!="success"}`), warming stuck
(`cold_storage_warming_rows > 3`), and low disk (`cold_storage_disk_free_bytes`).

Bring it up with `docker compose up -d`, reach Grafana at
`http://localhost:3000` (default `admin`/`admin`) and Prometheus at
`http://localhost:9090`, tunnelling those ports over SSH as needed.
Prometheus `rate()` handles the counter resets from a web app restart, so no
operator action is needed across restarts.

---

## 5. Frontend correlation

The request-ID middleware writes `X-Request-ID` on every response. The
frontend's `api/client.js` wrapper should surface this header in error
toasts so a user-visible error message can be grepped straight out of the
logs (`grep <request_id> ~/Library/Logs/ssc-web-app.log`). Wiring that up is
a follow-up task.
