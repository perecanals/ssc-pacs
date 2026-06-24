# Observability — logs, health, and metrics

The web app service emits JSON-structured logs, exposes a dependency
health probe at `/healthz`, and serves Prometheus metrics at `/metrics`.
This document is the operator-facing reference for all three.

Prometheus and Grafana run as a Docker Compose stack at
`~/monitoring/`. See §6 for management commands and access URLs.

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
sudo systemctl set-environment LOG_LEVEL=DEBUG
sudo systemctl restart ssc-web-app
# ...reproduce, then...
sudo systemctl unset-environment LOG_LEVEL
sudo systemctl restart ssc-web-app
```

### Reading logs

Running under systemd, the web app writes to the journal. JSON is
harder to grep casually than plain text, so use `jq`:

```bash
# Last 50 lines, pretty-printed
sudo journalctl -u ssc-web-app -n 50 -o cat | jq .

# All lines for one request
RID=$(curl -sI http://localhost:8043/api/me | grep -i X-Request-ID | cut -d: -f2 | tr -d ' \r')
sudo journalctl -u ssc-web-app --since "5 minutes ago" | grep "\"request_id\": \"$RID\""

# All warm/evict activity for one study
sudo journalctl -u ssc-web-app -o cat | jq 'select(.study_uid == "<UID>")'

# Just the request rate for the last hour
sudo journalctl -u ssc-web-app --since "1 hour ago" -o cat \
  | jq -r 'select(.message == "request") | [.http_status, .http_path_template] | @tsv' \
  | sort | uniq -c | sort -rn
```

### Rotation

The systemd journal holds the logs; the web app does not write to a
file sink. Retention is controlled globally by `/etc/systemd/journald.conf`.
Recommended settings for the PACS host:

```ini
# /etc/systemd/journald.conf — merge these in the [Journal] block.
SystemMaxUse=2G
SystemKeepFree=5G
MaxRetentionSec=30day
MaxFileSec=1day
ForwardToSyslog=no
```

Apply:

```bash
sudo systemctl restart systemd-journald
```

This caps the journal at ~2 GiB and keeps at most 30 days of history,
which is enough for incident triage without eating the root volume.

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

### Prometheus scrape config

The live config is at `~/monitoring/prometheus.yml`. It scrapes
`host.docker.internal:8043` (the web app, as seen from inside the
Prometheus container) every 30 s. If you move Prometheus to a
different host, adjust the target:

```yaml
scrape_configs:
  - job_name: ssc-web-app
    metrics_path: /metrics
    static_configs:
      - targets: ['host.docker.internal:8043']
    scrape_interval: 30s
    scrape_timeout: 10s
```

### Recommended alert rules (pseudo-PromQL)

Not provisioned automatically — copy into your Alertmanager / Grafana
alert config once Prometheus is running.

```yaml
groups:
  - name: ssc-web-app
    rules:
      - alert: WebAppDown
        expr: up{job="ssc-web-app"} == 0
        for: 2m
      - alert: WebAppHighErrorRate
        expr: sum(rate(http_requests_total{status=~"5.."}[5m]))
              / sum(rate(http_requests_total[5m])) > 0.05
        for: 10m
      - alert: WebAppLatencyP95
        expr: histogram_quantile(
                0.95,
                sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
              ) > 2
        for: 10m
      - alert: ColdStorageWarmFailures
        expr: rate(cold_storage_warm_total{result!="success"}[15m]) > 0
        for: 15m
      - alert: ColdStorageWarmingStuck
        # More than 3 rows in 'warming' for sustained time suggests the
        # watchdog timeout is too high or something is crash-looping.
        expr: cold_storage_warming_rows > 3
        for: 10m
      - alert: LegacyDicomRootLowDisk
        expr: cold_storage_disk_free_bytes < 50e9   # 50 GiB
        for: 15m
```

---

## 4. Grafana dashboard

The **SSC Web App** dashboard is auto-provisioned when Grafana starts
— no manual import needed. It lives in two places:

| Copy | Purpose |
|------|---------|
| `documentation/operations/grafana_dashboard.json` | Portable template with `${DS_PROMETHEUS}` variable (for manual import into a different Grafana) |
| `~/monitoring/dashboards/ssc-companion.json` | Provisioned copy with the datasource UID hard-coded to `ssc-prometheus` (loaded automatically). The filename predates the Companion→Navigator/Web App rename and was kept to avoid disturbing the running Grafana provisioning. |

**Panels:** request rate, p50/p95 latency, warm/evict success rate,
cold-storage disk free, warming rows over time.

If you edit the dashboard in the Grafana UI and want to persist the
changes, export the JSON and overwrite both copies above.

---

## 5. Monitoring stack (`~/monitoring/`)

Prometheus and Grafana run as Docker containers managed by
`~/monitoring/docker-compose.yml`.

### Access

| Service    | URL                        | Credentials        |
|------------|----------------------------|---------------------|
| Grafana    | `http://localhost:3000`    | `admin` / `admin` (change on first login) |
| Prometheus | `http://localhost:9090`    | no auth             |
| Web App `/healthz` | `http://localhost:8043/healthz` | no auth |
| Web App `/metrics`  | `http://localhost:8043/metrics`  | no auth |

**Via SSH tunnel** (from a laptop):

```bash
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 -L 8043:localhost:8043 <host>
```

Then open `http://localhost:3000` in your browser, go to
_Dashboards_ > _SSC Web App_.

### Directory layout

```
~/monitoring/
├── docker-compose.yml          # Prometheus + Grafana services
├── prometheus.yml              # Scrape config (web app on :8043)
├── provisioning/
│   ├── datasources/
│   │   └── prometheus.yml      # Auto-wires Prometheus datasource in Grafana
│   └── dashboards/
│       └── default.yml         # Tells Grafana to load from /dashboards/
└── dashboards/
    └── ssc-companion.json    # Auto-provisioned dashboard (legacy filename)
```

### Common operations

```bash
cd ~/monitoring

# Start / stop
docker compose up -d
docker compose down

# View logs
docker compose logs -f prometheus
docker compose logs -f grafana

# Restart after editing prometheus.yml (hot-reload)
curl -X POST http://localhost:9090/-/reload

# Check Prometheus is scraping the web app
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool

# Check web app target health
curl -s http://localhost:9090/api/v1/query?query=up | python3 -m json.tool
```

### Data retention

Prometheus keeps 30 days of time-series data (set via
`--storage.tsdb.retention.time=30d` in docker-compose.yml). Data is
stored in a Docker volume (`monitoring_prometheus_data`). To wipe and
start fresh:

```bash
cd ~/monitoring && docker compose down -v && docker compose up -d
```

### If the web app restarts

Prometheus will show a brief gap in the time series (the scrape returns
an error until the web app is back). Counters reset to zero on
restart; Prometheus' `rate()` handles resets gracefully. No operator
action needed.

---

## 6. Frontend correlation

The request-ID middleware writes `X-Request-ID` on every response. The
frontend's `api/client.js` wrapper should surface this header in error
toasts so a user-visible error message can be grepped straight out of
the journal (`journalctl ... | grep <request_id>`). Wiring that up is a
follow-up task — not part of WS 06.
