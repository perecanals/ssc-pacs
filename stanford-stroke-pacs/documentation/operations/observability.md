# Observability — logs, health, and metrics

The companion service emits JSON-structured logs, exposes a dependency
health probe at `/healthz`, and serves Prometheus metrics at `/metrics`.
This document is the operator-facing reference for all three.

Everything here is _in-process_. Provisioning a Prometheus server and a
Grafana instance is out of scope for WS 06 — §3 below documents how to
wire the exporters up when the infra team is ready.

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
| `http_method`, `http_path`, `http_path_template`, `http_status`, `duration_seconds` | Per-request log line from the middleware |

### Configuration

`companion/logging_config.py` wires up the root logger with
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
sudo systemctl restart ssc-companion
# ...reproduce, then...
sudo systemctl unset-environment LOG_LEVEL
sudo systemctl restart ssc-companion
```

### Reading logs

Running under systemd, the companion writes to the journal. JSON is
harder to grep casually than plain text, so use `jq`:

```bash
# Last 50 lines, pretty-printed
sudo journalctl -u ssc-companion -n 50 -o cat | jq .

# All lines for one request
RID=$(curl -sI http://localhost:8043/api/me | grep -i X-Request-ID | cut -d: -f2 | tr -d ' \r')
sudo journalctl -u ssc-companion --since "5 minutes ago" | grep "\"request_id\": \"$RID\""

# All warm/evict activity for one study
sudo journalctl -u ssc-companion -o cat | jq 'select(.study_uid == "<UID>")'

# Just the request rate for the last hour
sudo journalctl -u ssc-companion --since "1 hour ago" -o cat \
  | jq -r 'select(.message == "request") | [.http_status, .http_path_template] | @tsv' \
  | sort | uniq -c | sort -rn
```

### Rotation

The systemd journal holds the logs; the companion does not write to a
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
  "disk_free_percent_legacy_dicom_root": 46.6,
  "disk_free_bytes_legacy_dicom_root": 1833597009920
}
```

### HTTP codes

- `200` — all **critical** checks pass.
- `503` — at least one critical check failed (`"status": "degraded"`).

**Critical checks:**
- `db_stanford_stroke` must be `ok` (companion cannot serve its own
  data without it).
- When `STORAGE_MODE == "cold_path_cache"`, `orthanc_api` must also be
  `ok` (OHIF can't render otherwise).

**Non-critical but reported:**
- `db_orthanc` — connected via `PG_ORTHANC_USER`/`PG_ORTHANC_PASSWORD`.
  Reported as `"unconfigured"` if those env vars are absent.
- `disk_free_*_legacy_dicom_root` — observability only; a `null` here
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
| `cold_storage_warming_rows`                                  | gauge     | —                                      | `cache_state` rows currently in `status='warming'`, refreshed on scrape. |
| `cold_storage_disk_free_bytes`                               | gauge     | —                                      | Free bytes on the filesystem holding `legacy_dicom_root`, refreshed on scrape. |

`path_template` is the matched FastAPI route template (e.g.
`/api/studies/{studyinstanceuid}/warm`) — **never the concrete path**,
so UIDs don't explode cardinality.

The `/metrics` handler refreshes the two gauges on every scrape
(`metrics.refresh_cold_storage_gauges`). Warm/evict counters are
incremented in the API layer so they stay accurate even when the
gauge refresh is skipped due to a transient DB error.

### Prometheus scrape config

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: ssc-companion
    metrics_path: /metrics
    static_configs:
      - targets: ['localhost:8043']
    scrape_interval: 30s
    scrape_timeout: 10s
```

If the companion is only reachable via SSH tunnel from the observability
host, use Prometheus' `blackbox_exporter` with a `socks5` proxy, or
terminate the tunnel on the Prometheus host and point at
`127.0.0.1:8043`.

### Recommended alert rules (pseudo-PromQL)

Not provisioned automatically — copy into your Alertmanager / Grafana
alert config once Prometheus is running.

```yaml
groups:
  - name: ssc-companion
    rules:
      - alert: CompanionDown
        expr: up{job="ssc-companion"} == 0
        for: 2m
      - alert: CompanionHighErrorRate
        expr: sum(rate(http_requests_total{status=~"5.."}[5m]))
              / sum(rate(http_requests_total[5m])) > 0.05
        for: 10m
      - alert: CompanionLatencyP95
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

A starter dashboard lives at
[`operations/grafana_dashboard.json`](grafana_dashboard.json). It has
five panels — request rate, p50/p95 latency, warm/evict success rate,
cold-storage disk free, and `warming_rows` over time.

Import:

1. Grafana → _Dashboards_ → _New_ → _Import_.
2. Upload `grafana_dashboard.json` (or paste its contents).
3. Pick the Prometheus datasource that scrapes the companion.
4. Save.

The dashboard declares the datasource as a variable
(`${DS_PROMETHEUS}`), so you can rename or rebind it without editing
panel queries.

---

## 5. Frontend correlation

The request-ID middleware writes `X-Request-ID` on every response. The
frontend's `api/client.js` wrapper should surface this header in error
toasts so a user-visible error message can be grepped straight out of
the journal (`journalctl ... | grep <request_id>`). Wiring that up is a
follow-up task — not part of WS 06.
