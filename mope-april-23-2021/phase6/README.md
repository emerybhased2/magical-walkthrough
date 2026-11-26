# Phase 6 — Integration, final verification, startup and maintenance

Phase 6 ties the earlier phases together:

- **Task 35:** one script, `start-all.sh`, installs, configures, starts and maintains the stack.
- **Task 33:** one verifier, `phase6_verify.py`, tests the whole stack, including every region
  and the real archived client.
- **Task 34:** a generated report, `phase6_compat.py` → [`COMPATIBILITY.md`](COMPATIBILITY.md),
  measures what matches the April 2021 archive and what doesn't.

**Verification: 12/12 integration checks pass** ([`data/verify_report.json`](data/verify_report.json),
112.5 s, 20 regions). These include the Phase 3, 4 and 5 verifiers (34 + 8 + 8 checks) and every unit
test suite (Python, Bun, Ruby).

| Check | What | Result |
|---|---|---|
| C1 | configuration validates | PASS |
| S1 | every service up (master, account, gateway, all regions online in the master) | PASS |
| R1 | region selection: /servers/get and the served $config.gameServers list every enabled region | PASS |
| G1 | every region: join, Play, spawn (17, 107, 18, 6 + own animal) | PASS |
| G2 | multiplayer in every region: two players see each other; a leaver is removed | PASS |
| A1 | account registration, login, @me and a refused wrong password (through the gateway) | PASS |
| W1 | assets: snapshot + archived-image fallback served, season-1 sprite present, traversal refused | PASS |
| B1 | real client session: only known gaps fail, no unhandled client messages, no page errors | PASS |
| V3 | phase3_verify.py passes in full | PASS |
| V4 | phase4_verify.py passes in full | PASS |
| V5 | phase5_verify.py passes in full | PASS |
| U1 | unit tests: Python, Bun, Ruby | PASS |

## Installation

Requirements: Linux or macOS with Bash, and Python 3.11 or newer. Optional: Bun (`decode_images.ts`),
Ruby 3 (`fetch_live_assets.rb`), and Playwright's Chromium (browser checks in the verifiers).

```bash
cd mope-april-23-2021
./start-all.sh setup        # creates .venv, installs requirements.txt, creates .env, validates config,
                            # checks the archived client, the hosts entry and the optional tools
```

The archived client is fetched once from Arquivo.pt, as Phase 2 does. `setup` prints these two
commands if the snapshot is missing:

```bash
.venv/bin/python fetch_mope_archive.py --date 2025-12-10 --output snapshots/2025-12-10 --provider arquivo --all
.venv/bin/python rebuild_client_js.py --output snapshots/2025-12-10 --timestamp 20251210042757 --provider arquivo
```

Optional data:

- `bun install && bun decode_images.ts` fetches the 82 archived images. The gateway serves them after
  the snapshot.
- `ruby fetch_live_assets.rb` saves the live 2026 site's images, for reference only.

Map the public host once, in `/etc/hosts`, or on Windows in
`C:\Windows\System32\drivers\etc\hosts`:

```
127.0.0.1 local.mope.io
```

## Startup and daily use (`start-all.sh`)

| Command | What it does |
|---|---|
| `./start-all.sh` / `start` | setup checks, start every service in the background, wait until all are healthy (`MOPE_START_TIMEOUT`, default 60 s), print the status table |
| `./start-all.sh up` | same, in the foreground; Ctrl-C shuts everything down gracefully |
| `./start-all.sh stop [svc…]` | graceful shutdown: each region sends its players S→C 11 (disconnect) and deregisters from the master; anything still running after 8 s is killed |
| `./start-all.sh restart [svc…]` | restart everything, or only `master`, `account`, `web`, `regions` or one region id |
| `./start-all.sh status` | process, port, master view and player count per service |
| `./start-all.sh logs [svc] [-n N] [-f] [--debug]` | central logs: the last N lines of every service, merged by time (INFO and above unless `--debug`), or followed live |
| `./start-all.sh doctor` | read-only environment check |
| `./start-all.sh verify [--quick]` | Python/Bun/Ruby unit tests + `phase6_verify.py` |

The order of the start-up steps follows the plan:

1. install dependencies (venv + `requirements.txt`; skipped while the requirements hash is unchanged);
2. initialise components (`.env` from documented defaults; configuration validated by
   `localstack.py check-config`);
3. load `.env` (every service reads it through `services/settings.py`);
4. start the master, the account server, every enabled region and the web gateway
   (`localstack.py start`);
5. wait for health checks and the master's view of each region;
6. display the status table.

**Logs** live in one place, `logs/`:

- `logs/services/<service>.log`, with rotation at 5 MB, keeping 5 files;
- `logs/services/<service>.out` for raw output;
- `logs/start-all.log` for every `start-all.sh` action.

Each process's PID is kept in `run/<service>.pid`.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| `[fail] configuration invalid` | `.venv/bin/python localstack.py check-config` lists every setting with its source and the failing rule |
| A service shows `stopped` or `offline` | `./start-all.sh logs <service> -n 80`, then `./start-all.sh restart <service>` |
| `port … is used by another program` | another process holds the port: stop it, or change the port in `config/local_stack.json` / `.env` |
| The browser shows a blank page | open `http://local.mope.io:8080/`, not `127.0.0.1`: the archived client only runs on an allowed host name, and the hosts entry above provides it |
| "Connecting…" never ends | `./start-all.sh status`: the master must list at least one region as `online` |
| Sign-in buttons fail | social sign-in cannot work offline; use **Login with a local account** |
| Missing sprites or sounds | expected for files no archive holds; the client draws fallback shapes, and `phase6/COMPATIBILITY.md` lists them |
| Verifier browser checks fail | `./start-all.sh doctor` must show Playwright; Chromium comes with the project's Playwright install |

## Maintenance

- **Update dependencies:** edit `requirements.txt`, then `./start-all.sh setup` (reinstalls when the hash changes).
- **Change settings:** edit `.env` (see `.env.example`), then `./start-all.sh restart`.
- **Add or disable a region:** edit `config/local_stack.json` → `regions` (`enabled: false`
  disables one), then `./start-all.sh restart`.
- **After any change:** `./start-all.sh verify` (or `--quick` for the integration checks alone),
  then `.venv/bin/python phase6_compat.py` to refresh the compatibility report.
- **Back up accounts:** copy `data/accounts.sqlite3` while the account server is stopped, or use
  `services/account_admin.py`.
- **Rotate logs:** automatic for the service logs; `logs/start-all.log` and `logs/services/*.out`
  can be deleted at any time.

## Task 33 — repairs made during integration

Recording a full real-client session (page load, Play, movement) found two incompatibilities,
which are now repaired:

1. **Wrong asset season.** The server sent season 0, both in the server-info message (S→C 2) and
   in each object's `mopeSeasonID` byte. The client then requested
   `./assets/s/0/skins/land/mouse/0/mouse.png`, which doesn't exist. The client's own default is
   1 (L2166), and every archived season asset is under `assets/s/1/`, so the server now sends 1.
   The animal reader fields are skin theme, season and VIP (L10577); the non-animal season byte is
   read at L8287. The mouse now loads its archived sprite.
2. **Unhandled window resize.** The client sends C→S 17 (`_0x48481b`, L28874: width, height, inner
   width) whenever the window changes size; this was logged as unhandled 223 times. The server now
   updates that player's view rectangle.

Integration also added the archived-image fallback to the web gateway. The gateway looks in the
snapshot first, then in `archive/images/files/mope.io`. Every response says which folder served
it in an `X-Local-Source` header. URLs with `..` segments are refused.

Known failed requests that no archive can satisfy:

- `audio/*.mp3`;
- `img/howToPlay1.png`;
- `/none`, which the client itself requests as the logged-out avatar `src`.

Third-party scripts (ads, analytics, social SDKs) are blocked by design. `phase6_verify.py` B1
accepts exactly these and fails on anything else.
