# Phase 3 — Local server infrastructure

Phase 3 runs the archived **2025-12-10** client (the Phase 2 baseline) against local
servers only. It covers the master server, one gameplay server per region (9 archived +
11 local additions), and a web gateway. **Verification: 34/34 checks pass** ([`data/verify_report.json`](data/verify_report.json)).
Gameplay itself is Phase 5. For now Play reaches the region server and gets a notice back.

## Quick start

```bash
pip install -r requirements.txt
python localstack.py up            # or: start / stop / restart / status [targets]
# open http://localhost:8080/  (since 2026-11-26 the gateway's domain-lock shim lets the client run on
# localhost; the tables below were written when the default host was local.mope.io, which still works
# with a hosts entry "127.0.0.1 local.mope.io" and public_host set back in config/local_stack.json)
python phase3_verify.py            # task 20 checks against the running stack
```

The snapshot must exist first (`snapshots/2025-12-10/`, see `phase2/README.md`).

## Task 14 — Architecture

```
 browser ── http://local.mope.io:8080 ──► web gateway ──┬─► static client (snapshot working copy)
    │                                    (web_gateway)  ├─► /servers/get, /master/* ──► master server :8090
    │                                                   └─► /auth/*, /shop/*, ... ──► account server :8091 (Phase 4)
    └── ws://local.mope.io:7001..7020 ──► region servers (one process per region)
                                              └── register + heartbeat every 2 s ──► master
```

| Service | Process | Port | Role |
|---|---|---|---|
| `web` | `services/web_gateway.py` | 8080 | Serves the client and acts as the client's account-server origin. Applies the serve-time rewrites below and routes the API. |
| `master` | `services/master_server.py` | 8090 (loopback) | Region registry and status, `/servers/get`, client config overlay, registration and heartbeats |
| `region-<id>` ×20 | `services/region_server.py --region <id>` | 7001–7020 | Gameplay server per original region: ping, handshake, sessions, Play notice, graceful shutdown |
| `account` | Phase 4 | 8091 | Not enabled yet; account routes answer `503 {"success": false}` |

**Conventions:**
* **Configuration:** one file, [`config/local_stack.json`](../config/local_stack.json):
  public host, bind address (default `127.0.0.1`; use `0.0.0.0` to serve the LAN), ports,
  timings and the region table.
* **Logs:** one per service, `logs/services/<service>.log`. Raw stdout/stderr goes to `.out`.
* **PIDs:** `run/<service>.pid`. The master's internal endpoints need the generated token
  in `run/local_stack_token`.
* **Startup order:** master → regions → web. Shutdown runs in reverse.
* **Graceful stop:** every service handles SIGTERM. A region tells its players why it
  is going away (see task 20).

## Task 15 — Every original region, locally

Generated from `phase2/data/regions.json`. Ids, names, region labels and `gm` are the
archived values; only the address changes.

| Region id | Name | Region | Local address | Archived address |
|---|---|---|---|---|
| prod-lnd-us-ny-1 | New York 1 | New York, USA | `local.mope.io:7001` | prod-lnd-us-ny-1.mathstudyguides.com |
| prod-lnd-us-da-1 | Dallas 1 | Dallas, USA | `local.mope.io:7002` | prod-lnd-us-da-1.mathstudyguides.com |
| prod-lnd-us-la-1 | Los Angeles 1 | Los Angeles, USA | `local.mope.io:7003` | prod-lnd-us-la-1.mathstudyguides.com |
| prod-lnd-br-sp-1 | Sao Paulo 1 | Sao Paulo, Brazil | `local.mope.io:7004` | prod-lnd-br-sp-1.mathstudyguides.com |
| prod-ovh-de-ff-1 | Frankfurt 1 | Frankfurt, Germany | `local.mope.io:7005` | prod-ovh-de-ff-1.mathstudyguides.com |
| prod-ovh-fr-pr-1 | Paris 1 | Paris, France | `local.mope.io:7006` | prod-ovh-fr-pr-1.mathstudyguides.com |
| prod-lnd-sw-sh-1 | Stockholm 1 | Stockholm, Sweden | `local.mope.io:7007` | prod-lnd-sw-sh-1.mathstudyguides.com |
| prod-lnd-sg-sg-1 | Singapore 1 | Singapore, Singapore | `local.mope.io:7008` | prod-lnd-sg-sg-1.mathstudyguides.com |
| prod-lnd-au-sd-1 | Sydney 1 | Sydney, Australia | `local.mope.io:7009` | prod-lnd-au-sd-1.mathstudyguides.com |

**Local additions.** These 11 regions were added for this project. They do **not**
exist in any archive, so they use a `local-` id prefix and `"origin": "local-addition"`
in the config. The 9 archived regions carry `"origin": "archived-2025-12-10"`. Both
appear in `/master/regions`.

| Region id | Name | Region | Local address |
|---|---|---|---|
| local-us-chi-1 | Chicago 1 | Chicago, USA | `local.mope.io:7010` |
| local-us-sea-1 | Seattle 1 | Seattle, USA | `local.mope.io:7011` |
| local-us-mia-1 | Miami 1 | Miami, USA | `local.mope.io:7012` |
| local-nl-ams-1 | Amsterdam 1 | Amsterdam, Netherlands | `local.mope.io:7013` |
| local-uk-lon-1 | London 1 | London, United Kingdom | `local.mope.io:7014` |
| local-jp-tyo-1 | Tokyo 1 | Tokyo, Japan | `local.mope.io:7015` |
| local-ru-mow-1 | Moscow 1 | Moscow, Russia | `local.mope.io:7016` |
| local-us-sfo-1 | San Francisco 1 | San Francisco, USA | `local.mope.io:7017` |
| local-us-san-1 | San Diego 1 | San Diego, USA | `local.mope.io:7018` |
| local-us-nh-1 | New Hampshire 1 | New Hampshire, USA | `local.mope.io:7019` |
| local-us-bos-1 | Boston 1 | Boston, USA | `local.mope.io:7020` |

To add another region, append an entry with a free port to `config/local_stack.json` and
restart the master and web (`python localstack.py restart master web`). Then start the
new region (`python localstack.py start <id>`).

Each region is its own process and can be stopped or started alone:

```bash
python localstack.py stop prod-lnd-au-sd-1
python localstack.py start prod-lnd-au-sd-1
```

Set `"enabled": false` in the config to leave a region out entirely.

**Region server protocol** (as the client expects it; see `phase2/README.md` task 12):

| Path | Exchange |
|---|---|
| `/ping` | echo `[255]` |
| `/` | C→S `1` join → S→C `2` server info (session id, game version 296, the region's `gm`, season, server id = region id, not alive, 0 static objects) → S→C `3` connected |

* **Play:** C→S `113` → S→C `16` "Connected to local region … Gameplay is not rebuilt yet (Phase 5)."
* **Leave:** C→S `11` is a clean leave.
* **Unknown opcodes:** logged with their bytes, as input for Phase 5.
* **Session resume:** a join carrying a session id seen in the last 60 s gets the same
  session back. This is what the client does after a connection loss.

## Tasks 16, 17, 24 — Master server and region discovery

In the 2025 client the region list lives in `$config.gameServers` and live counts come
from `{ACCOUNT_SERVER}/servers/get` (Phase 2, task 11). The master owns both:

| Endpoint | Purpose |
|---|---|
| `GET /servers/get` | `[{"id", "totalPlayerCount"}]` for **online** regions: the exact shape the client parses. Offline regions are left out; the client shows them as `...`. |
| `GET /master/regions` | Registry: status (`online` / `offline` / `unknown` / `disabled`), players, pid, url, heartbeat age, registration count |
| `GET /master/client-config` | The `$config` overlay: local `gameServers`, plus `useDevAccountServer: true` and `devAccountServerUrl` = gateway origin |
| `POST /master/register`, `/master/heartbeat`, `/master/deregister` | Internal only: loopback plus `X-Local-Stack-Token`. Heartbeats insert-or-update, so regions re-register by themselves after a master restart. |

A region is **online** while heartbeats (every 2 s) arrive within 6 s. It is **offline**
after a deregister (clean stop) or a missed timeout (crash).

## Tasks 18–19 — Networking and connecting the archived client

`client.js` and `index.html` stay byte-for-byte as archived on disk. The gateway applies
two documented rewrites while serving; they are listed live at
`http://local.mope.io:8080/__local/rewrites` and can be switched off in the config:

1. **`client.js` `$config`.** The `$config = {…}` literal (the plain config object before
   the obfuscated code) is replaced by the archived values merged with the master's
   overlay:
   * `gameServers[].url` → `local.mope.io:7001…7009`
   * `useDevAccountServer: true`, `devAccountServerUrl: "http://local.mope.io:8080"`. This
     uses the client's own switch to point accounts and server stats at the gateway.

   Everything after the literal is the archived bytes. If the master is down, the
   gateway serves the last known list, or else the static config (verified, P7).
2. **`index.html` Turnstile loader.** It becomes `/__local/turnstile_local.js?onload=run`
   (`shims/turnstile_local.js`), because the page only starts `client.js` from Turnstile's
   `onload` callback.

**Other networking settings:**
* **Hostnames:** `local.mope.io` (page and regions). It satisfies the client's domain
  lock, which only allows `*.mope.io` and a few sister domains.
* **Origins:** the API is same-origin with the page. Extra origins can be allowed with
  `web.allowed_origins` (CORS, credentials, preflight).
* **Production dependence:** no gameplay or server connection goes to production.
  `index.html` still references ads, analytics and social-login SDKs; they load only if
  your browser can reach them and are not needed to connect or play.

## Task 20 — Verification ([`../phase3_verify.py`](../phase3_verify.py))

| Check | Result |
|---|---|
| P1 every region: ping echo, join → 2 + 3 (correct id, version, mode), play → notice | 20/20 pass |
| P2 3 clients on one region; master count 3, then 0 after they leave | pass |
| P3 session resume with the id from message 2; unknown ids get a new session | pass |
| P4 abrupt disconnect detected; reconnect works | pass |
| P5 stop a region: clients get type 11 "Local region 'Boston 1' is shutting down", master marks it offline, `/servers/get` omits it, new connections are refused; start it: back online | pass |
| P6 restart the master: all 20 regions re-register via heartbeat, discovery restored | pass |
| P7 master down: gateway still serves `client.js` with the local region list | pass |
| B1 archived client runs with the local `$config` (20 local servers, local account origin) | pass |
| B2 server list shows every region with player counts from the master | pass |
| B3 ping test reaches every region | pass |
| B4 handshake completes ("Connecting…" hidden); master counts the player | pass |
| B5 switching through the client's own server list connects to each of the 20 regions | 20/20 pass |
| B6 Play reaches the region; its notice appears in the client | pass |
| B7 stopping the current region: the client shows the reason and moves to another region by itself | pass |

**About the browser checks:** headless Chromium in the cloud sandbox used here cannot open
WebSockets at all, not even to localhost. The verifier therefore carries the page's
WebSocket traffic over the DevTools protocol to real WebSocket connections made from
Python. The archived client, gateway and region servers are unchanged. In a normal
browser on your machine the client connects to `ws://local.mope.io:700x` directly.

## Not in Phase 3

* **Gameplay (Phase 5):** spawning, world updates, the remaining opcodes.
* **Account server (Phase 4):** login, `@me`, shop, wardrobe. Until then the shop and
  login show their own error states.
* **Ping labels:** the client draws `(Ping ?ms)` until its next 60 s stats refresh. This
  is client behaviour, not a server issue.
