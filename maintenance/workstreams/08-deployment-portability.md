# Workstream 08 — Deployment portability

**Status:** `todo`
**Priority:** `P1`
**Size:** `S/M` (≈ 2–3 days)
**Owner:** _(unassigned)_
**Dependencies:** none

---

## 1. Context

The repo contains multiple hardcoded references to a specific machine,
user, and directory (`/home/perecanals/...`), plus a hardcoded server
IP in `scripts/connectivity/tunnel.sh`. A new maintainer cannot clone and run
without editing 4+ files first. This is a medium-severity bus-factor
risk and a constant friction during onboarding.

This workstream makes the stack clone-and-go portable:

- systemd unit parameterized via `EnvironmentFile` and relative paths.
- Shell scripts read paths from `.env` instead of hardcoding.
- `docker-compose.yml` uses a relative `env_file` path.
- `.env.example` documents every path-shaped variable.
- The patch and deploy steps in `installation_and_deployment.md` no
  longer require sed.

See `AUDIT_FINDINGS.md` §3.7, §3.9.

---

## 2. Scope

**In scope:**
- Parameterize `ssc-web-app.service`.
- Move hardcoded IP/user out of `scripts/connectivity/tunnel.sh` into `.env`.
- Make `init_orthanc_db.sh` relative-path-safe.
- Audit and eliminate all `/home/perecanals/` references outside
  `.env`/`config.toml`.
- Update `.env.example` to include the new variables.
- Update `installation_and_deployment.md` so the deploy flow is
  cut-paste-portable.

**Out of scope:**
- Containerizing the web app (replacing systemd with a container) —
  separate architectural change.
- Multi-host deployment (keeping a single-host design).
- Moving the conda env to a virtualenv or uv env — separate choice.

---

## 3. Findings

- **F-08.1** — `ssc-web-app.service` hardcodes `User=perecanals`,
  `WorkingDirectory=/home/perecanals/...`, and the conda env path in
  `ExecStart`.
- **F-08.2** — `stanford-stroke-pacs/scripts/connectivity/tunnel.sh:1` hardcodes
  server IP `10.110.128.149` and user `perecanals@`.
- **F-08.3** — `init_orthanc_db.sh:10` sources
  `"/home/perecanals/pacs/stanford-stroke-pacs/.env"` with an absolute
  path.
- **F-08.4** — `docker-compose.yml` uses a compose-relative `env_file`
  (good), but other docs reference it with absolute paths.
- **F-08.5** — No top-level `README.md` means a new maintainer has no
  high-level orientation before they hit the hardcoded paths.

---

## 4. Tasks

- [ ] **T1** — Grep the repo for every `/home/perecanals/` occurrence:
  ```bash
  grep -rn '/home/perecanals/' \
    --exclude-dir=.git --exclude-dir=node_modules \
    --exclude-dir=dist --exclude='*.log'
  ```
  Triage each occurrence: move to `.env`/`config.toml`, or leave as a
  locally-overridable default.
- [ ] **T2** — Parameterize `ssc-web-app.service`:
  ```ini
  [Service]
  User=${SSC_USER}
  WorkingDirectory=${SSC_REPO_ROOT}/stanford-stroke-pacs/web-app
  EnvironmentFile=${SSC_REPO_ROOT}/stanford-stroke-pacs/.env
  ExecStart=${SSC_CONDA_PREFIX}/envs/pacs/bin/uvicorn app:app --port 8043
  ```
  systemd doesn't support `${VAR}` expansion in `[Unit]` / `[Service]`
  stanzas the way shell does, so use one of these approaches:
  - (Recommended) **Generator script**:
    `stanford-stroke-pacs/scripts/install_systemd_unit.sh` (new) reads
    `.env` and renders the unit file into `/etc/systemd/system/` via
    `envsubst`.
  - Alternative: ship a hand-parameterized `ssc-web-app.service.in`
    and a short generator.
- [ ] **T3** — Add new `.env` variables: `SSC_USER`, `SSC_REPO_ROOT`,
  `SSC_CONDA_PREFIX`. Update `.env.example` with descriptions.
- [ ] **T4** — Rewrite `scripts/connectivity/tunnel.sh` to:
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  source "$(dirname "$0")/../.env"
  : "${SSH_TUNNEL_HOST:?set in .env}"
  : "${SSH_TUNNEL_USER:?set in .env}"
  ssh -N -L 8042:localhost:8042 -L 8043:localhost:8043 \
    "${SSH_TUNNEL_USER}@${SSH_TUNNEL_HOST}"
  ```
  Add `SSH_TUNNEL_HOST`, `SSH_TUNNEL_USER` to `.env.example`.
- [ ] **T5** — Rewrite `init_orthanc_db.sh:10` to source `.env`
  relative to the script:
  ```bash
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  source "${SCRIPT_DIR}/.env"
  ```
- [ ] **T6** — Add a top-level `README.md` at
  `/home/perecanals/pacs/README.md` (new) with:
  - one-paragraph stack description,
  - pointer to `stanford-stroke-pacs/documentation/context.md` as the
    doc map,
  - minimal "how to deploy" (3 bullets pointing to the install guide),
  - license / ownership / contact info.
- [ ] **T7** — Add a top-level `README.md` at
  `/home/perecanals/pacs/stanford-stroke-pacs/README.md` (new) with
  a short "what this directory is" header plus a link back to the
  main docs — so browsing GitHub/GitLab lands on something useful.
- [ ] **T8** — Walk through `installation_and_deployment.md` and remove
  every manual `sed` / path-edit step, replacing with "set X in
  `.env`."
- [ ] **T9** — Rehearse: on a scratch user (or a VM), clone the repo,
  copy `.env.example` → `.env`, edit per the new guide, run the install
  steps. Document any residual manual edits still required.

---

## 5. Acceptance criteria

- [ ] `grep -rn '/home/perecanals/' --exclude-dir=.git --exclude=.env
  --exclude='*.log' | wc -l` returns 0 (outside `.env` and user-local
  files).
- [ ] `scripts/connectivity/tunnel.sh` has no hardcoded IP or user.
- [ ] `init_orthanc_db.sh` can be run from anywhere (cwd-independent).
- [ ] A fresh clone + `.env` edits + install guide walkthrough produces
  a working stack without any sed/patching.
- [ ] Top-level `README.md` files exist.
- [ ] `.env.example` documents all path-shaped variables.

---

## 6. Verification

```bash
# No hardcoded personal paths
grep -rn '/home/perecanals/' \
  --exclude-dir=.git --exclude-dir=node_modules \
  --exclude-dir=dist --exclude=.env --exclude='*.log' \
  /home/perecanals/pacs

# No hardcoded IPs
grep -rn '10\.110\.128\.149' \
  --exclude-dir=.git /home/perecanals/pacs

# Systemd unit renders correctly
stanford-stroke-pacs/scripts/install_systemd_unit.sh --dry-run

# Scripts cwd-independent
cd /tmp && bash /home/perecanals/pacs/stanford-stroke-pacs/init_orthanc_db.sh --dry-run
```

---

## 7. Rollback

All changes are text edits under version control. `git revert` restores
prior state. The deployed systemd unit is a copy in
`/etc/systemd/system/` — it must be re-copied from the pre-change
service file if reverting.

---

## 8. Files touched

- `stanford-stroke-pacs/ssc-web-app.service` (edit — or rename to
  `.service.in` and add a generator)
- `stanford-stroke-pacs/scripts/install_systemd_unit.sh` (new)
- `stanford-stroke-pacs/scripts/connectivity/tunnel.sh` (edit)
- `stanford-stroke-pacs/init_orthanc_db.sh` (edit — line 10 region)
- `stanford-stroke-pacs/.env.example` (edit — add new vars)
- `README.md` (new, at repo root)
- `stanford-stroke-pacs/README.md` (new)
- `stanford-stroke-pacs/documentation/guides/installation_and_deployment.md`
  (edit — rewrite manual-edit sections)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Generator script missed on production — unit still uses old paths | med | med | Add an `install_systemd_unit.sh` idempotency check; document in deploy guide |
| `.env` drift — variables added but deployed `.env` not updated | high | med | `.env.example` diff check in CI (WS 07); startup validation of required vars (WS 03 T2) |
| systemd `EnvironmentFile` ordering — vars not available in `[Service]` stanza substitution | med | med | Use a generator + templated `.service` file instead of relying on systemd substitution |
| README files go stale | med | low | Keep them minimal; link out to the authoritative docs |

---

## 10. Notes

- Some maintainers prefer a `Jinja`-based template render (via a simple
  Python script). `envsubst` is fine if the substitutions are flat.
- The broader portability question — "should this whole stack be a
  container?" — is out of scope. Once this workstream lands, the manual
  friction of redeploying on a new host should be down to editing
  `.env` once.
- Keep `config.toml` for non-secret tuning and `.env` for secrets +
  host-specific paths. Don't mix them.
