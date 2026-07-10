# Secret rotation

Procedures for rotating the secrets this stack depends on. Each section is
self-contained; run them independently.

Secrets live in `stanford-stroke-pacs/.env` (loaded by Web App via
`python-dotenv` and by `docker-compose.yml` via `env_file`). Changing a
secret there is the source of truth; restart the consumer(s) to pick it up.

---

## 1. `JWT_SECRET`

Used by Web App (`app.py`) to sign and verify the `auth_token` cookie.
Rotating invalidates every live session — users will need to log in again.

### When to rotate

- Suspected leak (developer laptop compromise, accidental commit, log-spill).
- Quarterly hygiene rotation for medical-research-grade compliance posture.
- Off-boarding of anyone who had shell access to the host.

### Procedure

```bash
# 1. Generate a new 256-bit secret (hex is fine; slashes/equals are also fine).
python -c 'import secrets; print(secrets.token_hex(32))'

# 2. Edit .env on the host.
sudo -e /opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs/.env
#    Replace the JWT_SECRET=... line with the new value.

# 3. Reload Web App to pick it up. All existing cookies are now invalid.
#    Linux (systemd):  sudo systemctl restart ssc-web-app
#    macOS (launchd):  sudo launchctl kickstart -k system/com.ssc.webapp
sudo systemctl restart ssc-web-app

# 4. Spot-check that the service came back up and auth still works for a
#    fresh login:
sudo journalctl -u ssc-web-app -n 50   # macOS: tail -n 50 ~/Library/Logs/ssc-web-app.err
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8043/
```

### Staggered roll-out (optional)

The current implementation signs with a single secret; there is no
dual-accept window. If you need to avoid a hard re-login wave, coordinate
the restart for a low-traffic window (e.g. off-hours). A future improvement
is to accept `JWT_SECRET` **and** `JWT_SECRET_PREVIOUS` during verification,
sign with `JWT_SECRET` only, and drop `JWT_SECRET_PREVIOUS` after the
longest `session_absolute_timeout_hours` interval.

### What can go wrong

- **Empty secret after edit.** The service now **fails fast** at startup
  with `RuntimeError: JWT_SECRET must be set ...`. Check
  `journalctl -u ssc-web-app -n 50` (macOS: `~/Library/Logs/ssc-web-app.err`)
  and restore the value.
- **Cookies still show old value in the browser.** Expected — the browser
  keeps the old cookie until it expires or the user hits a protected page
  and gets a 401. Clearing cookies or logging in again is the workaround.

---

## 2. Orthanc service-account password (`ORTHANC_ADMIN_PASSWORD`)

Used by:

1. Web App's reverse proxy and other host-local scripts (Basic auth against
   Orthanc on `:8042`), via `ORTHANC_ADMIN_USER` / `ORTHANC_ADMIN_PASSWORD`
   in `.env`.
2. The matching entry in `orthanc_users.json` (Orthanc's own auth, plaintext).

Both must stay in sync. The `rotate-service-account` subcommand updates both
atomically.

### Procedure

```bash
cd /opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs

# 1. Rotate the password in both .env and orthanc_users.json in one go:
python scripts/admin/manage_users.py rotate-service-account

# 2. Restart both consumers:
#    Linux (systemd):  sudo systemctl restart ssc-web-app
#    macOS (launchd):  sudo launchctl kickstart -k system/com.ssc.webapp
docker restart ssc-orthanc
sudo systemctl restart ssc-web-app

# 3. Verify .env and orthanc_users.json agree, the service account works
#    against Orthanc, and Web App can still proxy:
python scripts/admin/manage_users.py check-service-account   # exits non-zero on drift
curl -u admin:<newpass> http://localhost:8042/system | head -5
sudo journalctl -u ssc-web-app -n 50 | grep -iE 'orthanc|401' || echo 'no auth errors'
#    macOS: grep -iE 'orthanc|401' ~/Library/Logs/ssc-web-app.err
```

### What can go wrong

- **`.env` and `orthanc_users.json` drift out of sync.** Shouldn't happen — one
  command writes both. If you suspect drift (e.g., after manual edits),
  re-run `rotate-service-account` to bring them back into sync.
- **Editing `orthanc_users.json` manually.** Don't. `scripts/admin/manage_users.py`
  is the single source of truth.

---

## 3. Database password (`DB_PASSWORD`)

Used by Web App to connect to the `stanford-stroke` PostgreSQL database.
The database is owned by the host (not managed by this repo); rotation is
coordinated with whoever owns the Postgres role.

### Procedure

```bash
# 1. Change the role password in Postgres (example; exact command is
#    site-specific).
#    Linux with a system PostgreSQL:
sudo -u postgres psql -c "ALTER USER stanford_app WITH PASSWORD '<newpass>';"
#    macOS with a user-level Homebrew PostgreSQL, connect as the instance owner
#    rather than via `sudo -u postgres`:
#      psql -d postgres -c "ALTER USER stanford_app WITH PASSWORD '<newpass>';"

# 2. Update .env.
sudo -e /opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs/.env

# 3. Restart Web App. Startup will now fail-fast if DB_USER/DB_PASSWORD
#    are missing, but an *incorrect* password surfaces as connection errors
#    on the first request — watch the logs.
#    Linux (systemd):  sudo systemctl restart ssc-web-app
#    macOS (launchd):  sudo launchctl kickstart -k system/com.ssc.webapp
sudo systemctl restart ssc-web-app
sudo journalctl -u ssc-web-app -f   # macOS: tail -f ~/Library/Logs/ssc-web-app.err
```

---

## Appendix — relevant config

- `stanford-stroke-pacs/.env` — all secrets listed above.
- `stanford-stroke-pacs/config.toml` — `[web-app]` section controls
  session durations (`session_timeout_hours`,
  `session_absolute_timeout_hours`) and the `cookie_secure` flag that
  accompanies rotated JWTs.
- `stanford-stroke-pacs/web-app/db.py` — startup helper `require_env()`
  enforces that required secrets (`DB_USER`, `DB_PASSWORD`,
  `ORTHANC_ADMIN_USER`, `ORTHANC_ADMIN_PASSWORD`, `JWT_SECRET`) are non-empty.
