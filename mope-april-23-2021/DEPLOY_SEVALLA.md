# Hosting the backend on Sevalla

The hosted part is the **backend only**: account server, master server and the region
servers, behind one public port. The archived game client is **not** deployed. Every
player runs it on their own machine (hosts-file entry, `localstack.py up`) and points
it at the shared backend.

```
 player's machine                                            Sevalla (one container)
 ───────────────                                             ───────────────────────
 browser ── http://local.mope.io:8080 ──► web gateway ─┐      ┌─ edge :$PORT ──┬─► master   (loopback)
    │                                     (client)     └─https┤                ├─► account  (loopback, SQLite on disk)
    └── ws://local.mope.io:7000/r/<id> ──► relay ──────wss────┘   /r/<id>      └─► region × 20 (loopback)
```

* The browser loads the client over plain `http`, so it opens `ws://` sockets. The local
  relay (`services/edge.py relay`) turns them into `wss://` connections to the backend,
  so no local HTTPS certificate is needed.
* The gateway forwards the account and master API calls to the backend server-side, so
  no CORS setup is needed either.
* Only the public API and `/r/<id>` sockets are exposed. The master's internal
  register/heartbeat endpoints stay on loopback.

## Files

| File | Role |
|---|---|
| `../Dockerfile` (repo root) | `python:3.12-slim`, installs `requirements-cloud.txt`, runs `cloud.py` |
| `cloud.py` | Container entrypoint: starts master, account and regions on loopback, then the edge on `$PORT`. If any process dies, it exits so Sevalla restarts the container. |
| `services/edge.py` | `cloud` mode (public edge) and `relay` mode (player side) |

## Sevalla app settings

| Setting | Value |
|---|---|
| Source | public Git: this repository and branch |
| Build | Dockerfile, path `Dockerfile`, context `.` |
| Web process | port 8080, size `s1` (1 GB) — all 20 regions use about 750 MB |
| Disk | 10 GB at `/var/lib/data` (the account database) |

Environment variables:

| Variable | Value |
|---|---|
| `ACCOUNT_DB_PATH` | `/var/lib/data/accounts.sqlite3` |
| `PUBLIC_HOST` | the app's host name, e.g. `mope-backend-xxxxx.sevalla.app` |
| `BACKEND_ACCESS_KEY` | a random string; share it only with your players (secret) |
| `CLOUD_REGIONS` | optional: comma-separated region ids, to run fewer regions on a smaller pod |

Any `ACCOUNT_*`, `CLIENT_*` or `DEV_*` setting from `.env.example` can be set the same
way. Leave `DEV_*` off on a public backend.

## Players: connect to the backend

### One command: `play.py`

Put the `mope.env` file you were given next to `play.py` (or in Downloads), then run:

```bash
python play.py                 # or double-click play.bat (Windows) / play.command (macOS)
python play.py --check         # only report what is ready
```

It checks Python 3.10+, copies `mope.env` to `.env`, installs `requirements-play.txt`,
checks the `127.0.0.1 local.mope.io` hosts entry (`--add-hosts` writes it when run as
administrator / sudo), downloads the 2025-12-10 snapshot once, checks the backend accepts
your key, then runs `localstack.py up` and opens `http://local.mope.io:8080/`. Ctrl-C stops it.

### By hand

Each player needs the Phase 2 snapshot (`snapshots/2025-12-10/`) and the hosts-file
entry from `phase3/README.md`. Then, in `mope-april-23-2021/.env`:

```
REMOTE_BACKEND_URL=https://mope-backend-xxxxx.sevalla.app
REMOTE_BACKEND_KEY=<the BACKEND_ACCESS_KEY value>
```

```bash
pip install -r requirements.txt        # now includes aiohttp for the relay
python localstack.py up                # starts only: relay (:7000) and web (:8080)
# open http://local.mope.io:8080/
```

Remove `REMOTE_BACKEND_URL` to go back to the fully local stack.

## Checks

```bash
curl https://<app>.sevalla.app/__edge/health                                # no key needed
curl -H "X-Backend-Key: <key>" https://<app>.sevalla.app/servers/get        # region player counts
python -m unittest tests.test_cloud -v                                      # offline tests
```

## Render

`render.yaml` at the repository root is a Render Blueprint for the same container: Docker build from the root
`Dockerfile`, health check `/__edge/health`, a 1 GB disk at `/var/lib/data` for the account database, and a
generated `BACKEND_ACCESS_KEY`. Render sets `PORT`, and `cloud.py` uses `RENDER_EXTERNAL_HOSTNAME` as `PUBLIC_HOST`.
In the Render dashboard choose **New > Blueprint**, pick this repository, then copy the generated key for players.
