# Direct read-only database access from Python

How a collaborator queries the `stanford-stroke` research database directly
(pandas / SQLAlchemy / psql) without going through the web app.

Three steps: an admin creates you a **read-only database role**, you open an
**SSH tunnel** to the server, and your script connects through the tunnel
using credentials kept in **your own `.env` file**.

No sensitive values appear in this document — usernames, hosts, and passwords
below are placeholders.

## 1. Prerequisite: a read-only role (admin task)

Database roles are separate from web-app logins (those live in the `users`
table and grant no database access). An admin creates the role on the server:

```bash
python scripts/admin/manage_readonly_db_users.py add <username>
```

and hands you the username and password out of band. The role can `SELECT`
every research table (current and future) in `stanford-stroke` plus the
`orthanc_db` index, its sessions are forced read-only (accidental writes are
rejected by the server), and the web-app auth tables are excluded. Details:
`docs/operations/commands.md` §User management.

## 2. SSH tunnel

PostgreSQL listens on `localhost` only — it is never exposed to the network.
Reach it by forwarding a local port over SSH (same pattern as the web-app
tunnels in `scripts/connectivity/`, just for port 5432):

```bash
ssh -N -L 5432:localhost:5432 \
    -o ServerAliveInterval=60 -o ServerAliveCountMax=3 \
    <ssh-user>@<server-ip>
```

Leave that window open while you work; close it to disconnect. If your own
machine already runs PostgreSQL on 5432, forward a different local port
(e.g. `-L 55432:localhost:5432`) and put that port in your `.env` below.

## 3. Your own `.env` file

Keep credentials out of code and shell history: put them in a `.env` file
next to your analysis scripts. Create it once:

```bash
# ~/my-analysis/.env  — never commit this file (add `.env` to .gitignore)
DB_USER=<username>            # the read-only role from step 1
DB_PASSWORD=<password>
DB_HOST=localhost             # the tunnel's local end
DB_PORT=5432                  # or 55432 etc. if you forwarded another port
DB_NAME=stanford-stroke
```

and restrict it to yourself: `chmod 600 .env`.

## 4. Connecting from Python

Dependencies: `pip install pandas sqlalchemy psycopg2-binary python-dotenv`.

```python
import os
from urllib.parse import quote_plus

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine

# Same env keys + defaults as web-app/db.py
load_dotenv()  # reads .env from the current directory; or load_dotenv("/path/to/.env")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "stanford-stroke")

# quote_plus: passwords with @ : / % survive URL parsing
engine = create_engine(
    f"postgresql://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

patient_df = pd.read_sql("SELECT * FROM patient_labelled", engine)
image_series_labelled_df = pd.read_sql("SELECT * FROM image_series_labelled", engine)
image_study_labelled_df = pd.read_sql("SELECT * FROM image_study_labelled", engine)
```

The `*_labelled` mirrors are usually what you want: the upstream imaging
tables joined with their annotation labels as columns (see
`docs/reference/data_stores.md` for every table and its schema).

## Notes

- **Read-only is server-enforced.** `INSERT`/`UPDATE`/`CREATE` fail with
  "cannot execute ... in a read-only transaction" — this protects the
  database, not just you.
- **`orthanc_db` is also readable** (set `DB_NAME=orthanc_db`), but its
  schema is Orthanc-internal and may change across Orthanc upgrades — prefer
  the `stanford-stroke` tables for anything durable.
- **Password changes**: an admin runs
  `manage_readonly_db_users.py passwd <username>`; update your `.env` to
  match.
