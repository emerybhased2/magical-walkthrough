# Phase 2 — Client reverse engineering (2025-12-10 snapshot)

**Baseline.** Phase 2 works from the complete, same-day **2025-12-10** Arquivo.pt
snapshot. No 2021 `client.js` exists outside the Wayback Machine, so this baseline was
chosen for Phase 2. The April 2021 archive is unchanged and remains the historical
record. Every file in this baseline was captured on 2025-12-10 (`archive_report.json` →
"Every file comes from a capture dated 2025-12-10").

| Item | Value |
|---|---|
| Snapshot root | `snapshots/2025-12-10/` (git-ignored data) |
| Homepage capture | Arquivo.pt `20251210042802` |
| `client.js` capture | Arquivo.pt `20251210042757`, sha256 `e62f3ccc…cee39e`, 2,697,674 bytes |
| Readable rebuild | `working/client-js/20251210042757_arquivo/client.js`, sha256 `82f20287…eb63`, 29,322 lines. **All line numbers below refer to this file.** |
| Game version | `$config.gameVersion = 296`, `env: 'production'` |

## Reproduce

```bash
python fetch_mope_archive.py --date 2025-12-10 --output snapshots/2025-12-10 --provider arquivo --all
python rebuild_client_js.py --output snapshots/2025-12-10 --timestamp 20251210042757 --provider arquivo
python extract_client_protocol.py --project snapshots/2025-12-10       # tasks 9-13 inventory
python phase2_probe_client.py --project snapshots/2025-12-10 --click-play   # task 8
```

Outputs (copied here): [`OPCODES.md`](OPCODES.md), [`data/protocol_inventory.json`](data/protocol_inventory.json),
[`data/regions.json`](data/regions.json), [`data/task8_probe.json`](data/task8_probe.json).

---

## Task 8 — Archived client environment

`phase2_probe_client.py` serves the snapshot in headless Chromium and records everything
the page does. It never contacts production. The archive host `local.mope.io` is served
straight from disk. Other hosts get a local copy or stand-in where one exists and are
otherwise blocked and logged. WebSockets are captured inside the page.

**Result:** the client runs locally (game version 296). It builds the menu with all
9 regions, runs its ping test, picks the fastest region and opens the game connection.
That connection then waits on "Connecting…" because no game server answers yet
(Phase 3).

Four obstacles had to be solved without modifying `client.js`:

| Obstacle | What happens | How the probe handles it (local testing only) |
|---|---|---|
| **Cloudflare Turnstile gate** | `index.html` only loads `client.js` from Turnstile's `onload=run` callback (`<script src=".../turnstile/v0/api.js?onload=run">`, `function run()` at the end of the page). Without Turnstile the game never starts. | [`../shims/turnstile_local.js`](../shims/turnstile_local.js) offers the same API (`render`, `remove`, …), calls `run()`, and returns an obviously fake token. |
| **Domain lock** (obfuscator) | `a0_0xab04ee.run()` (L83) compares `document.domain`/`location.hostname` with an obfuscated allow-list: `.mope.io`, `.m0pe.io`, `.learninganimals.club`, `.tailbite.me`, `.animalfun.club`, `.zooeducation.space`. On any other host it sets `location = "about:blank"`. | Serve under `local.mope.io`. On your own machine: add `127.0.0.1 local.mope.io` to the hosts file. |
| **Console disabled** (obfuscator) | Right after the lock, `console.log/warn/info/error/...` are replaced with no-ops. | The test page keeps the original console methods so logs stay visible. |
| **WebSockets** | Playwright's own WebSocket mocking doesn't work on this page (its in-page helpers return nothing), and this cloud sandbox has no browser WebSocket egress, even to localhost. | An in-page test double records every frame the client sends and answers `/ping` probes with the 1-byte reply (`255`) the client expects. |

**Observed requests** (`data/task8_probe.json`):

* Snapshot files are all served; only 3 local paths are missing: `/updates.json` (fetched
  by the client for the Updates panel), `/audio/click.mp3` and `/none`.
* Blocked external dependencies, none needed to reach the menu:

  | Kind | Hosts |
  |---|---|
  | Accounts / server stats | `prod-accounts.mathstudyguides.com` (`/servers/get`, `/shop/get`, `/xsolla/getPackets`, `/xsolla/getVip`) |
  | Sign-in SDKs | `accounts.google.com`, `apis.google.com`, `connect.facebook.net`, `appleid.cdn-apple.com` |
  | Ads | `s.nitropay.com`, `api.adinplay.com` |
  | Analytics | `googletagmanager.com`, `static.cloudflareinsights.com`, `b.scorecardresearch.com`, `youtube.com` (subscribe widget) |
  | Payments (when used) | `xsolla` |
  | CAPTCHA | Cloudflare Turnstile (stand-in above) |
* Page errors are all benign: `JSON.parse("undefined")` for empty localStorage keys, the
  missing `updates.json`, and the missing audio file.

## Task 9 — Client initialization and structure

`client.js` is one script with three parts:

1. `$config = {...}` (L1–82): plain configuration. See task 10 and the table below.
2. `const a0_0xab04ee = { async run() {...} }` (L83) with `a0_0xab04ee.run()` on the last
   line. Inside it come the obfuscator's runtime guards (domain lock, console disabling),
   then the whole game client.
3. Inside `run()`, in order of execution:
   * L268: account server base. `https://prod-accounts.mathstudyguides.com`, or
     `$config.devAccountServerUrl` when `$config.useDevAccountServer` is true. Logged as
     "Account server domain became:".
   * L288 `_0x5be1b9()`: **built-in local sandbox hook**, `addServer('LOCAL', 'localhost',
     'SANDBOX')`. It is never called in this build, but it shows the client supports a
     `localhost` server in a `SANDBOX` region.
   * L1547 / L1586: binary reader `_0x4dbf94` and writer `_0x2293a6` (see task 12).
   * L2879–3170: message classes (`class X extends Base { static type = N; static send()/handle() }`).
   * L3580–3620: server objects and the server list built from `$config.gameServers`
     (`_0x12bd72(name, id, url, region, gm)`, `_0x4b0a86` = addServer, `_0x1729f1` = build list).
   * L3180–3330: ping test (`/ping` sockets, L3186), fastest-region choice (L3243), ad checks.
   * L3701–3740 `_0x41124e(server)`: connect to a game server. L3798 `_0x3e7c4e(DataView)`:
     the incoming-message dispatcher.
   * L6000–6700: accounts (login, `@me`, shop, Xsolla), `/servers/get` stats (L6352, every 60 s).
   * L29301: Turnstile helper `_0x1fe974(sitekey)` (render → token).

`$config` keys used by the code (`data/protocol_inventory.json` → `config_uses`):

| Key | Used for |
|---|---|
| `gameVersion` | compared with the server's version in message 2 |
| `isTestingMode` | testing behaviour |
| `env` | environment label |
| `isBetaMode` | server-full thresholds |
| `useDevAccountServer`, `devAccountServerUrl` | account server switch |
| `turnstileKey` | CAPTCHA site key |
| `authentication.{discord,facebook,google,apple}` | OAuth client IDs |
| `gameServers` | region list |

LocalStorage keys:
* Login: `nick`, `login_userId`, `login_passwordToken`, `login_name`, `login_socialNetworkName`, `login_profilePicURL`.
* Options: `options_*`.
* Ads: `gamesSinceAd`, `lastAdShowT`.
* Other: `chatbox`.

## Task 10 — Server regions

The region list is **static in `$config.gameServers`**; the client does not ask a
master server for it. Full list: [`data/regions.json`](data/regions.json).

| id | name | region | url (WebSocket host) | gm |
|---|---|---|---|---|
| prod-lnd-us-ny-1 | New York 1 | New York, USA | prod-lnd-us-ny-1.mathstudyguides.com | 0 |
| prod-lnd-us-da-1 | Dallas 1 | Dallas, USA | prod-lnd-us-da-1.mathstudyguides.com | 0 |
| prod-lnd-us-la-1 | Los Angeles 1 | Los Angeles, USA | prod-lnd-us-la-1.mathstudyguides.com | 0 |
| prod-lnd-br-sp-1 | Sao Paulo 1 | Sao Paulo, Brazil | prod-lnd-br-sp-1.mathstudyguides.com | 0 |
| prod-ovh-de-ff-1 | Frankfurt 1 | Frankfurt, Germany | prod-ovh-de-ff-1.mathstudyguides.com | 0 |
| prod-ovh-fr-pr-1 | Paris 1 | Paris, France | prod-ovh-fr-pr-1.mathstudyguides.com | 0 |
| prod-lnd-sw-sh-1 | Stockholm 1 | Stockholm, Sweden | prod-lnd-sw-sh-1.mathstudyguides.com | 0 |
| prod-lnd-sg-sg-1 | Singapore 1 | Singapore, Singapore | prod-lnd-sg-sg-1.mathstudyguides.com | 0 |
| prod-lnd-au-sd-1 | Sydney 1 | Sydney, Australia | prod-lnd-au-sd-1.mathstudyguides.com | 0 |

Each region holds one server. `gm` is the game mode: `gameMode == 2` shows battle-royale
status text in the list (L3589).

Phase 3 can keep these 9 ids and names and point each `url` at a local host:port, plus
the client's own `LOCAL` / `localhost` / `SANDBOX` entry.

## Task 11 — "Master server"

There is **no separate master server in this build**:

* **Region list:** static (`$config.gameServers`).
* **Player counts:** `GET {ACCOUNT_SERVER}/servers/get` (L6352), polled every 60 s
  (L3789). The response is a JSON array of `{id, totalPlayerCount}`, matched to servers
  by `id`. A server missing from the response shows `players = -1`. The client sets
  `gotMasterServerStats = true` and sums the total for display. On error it logs
  "Error getting master server stats:" and keeps `...`.
* **Region choice:**
  1. Ping test: open `ws(s)://<url>/ping` for each server, send `[255]`, expect `[255]`
     back, 4 rounds. The fastest region wins ("Fastest region is … with ping …ms").
  2. Within that region, `_0x28e071` (L3622, "Joining best server…") prefers the busiest server
     still under 400 players (900 when `isBetaMode`). With none left it picks a random
     server ("No acceptable servers left in region…").
  3. Servers at 500 or more players (800 in beta) show `-FULL!`.
* **Manual choice:** the `<select id="serverSelect">` (`optgroup` per region,
  "`<name> [<n> players]`") calls `_0x41124e(server)`.

## Task 12 — Networking protocol

**Transport.** `new WebSocket((https ? 'wss://' : 'ws://') + server.url)` (L3713). The
URL is the host only, with no path. Sockets are binary (`binaryType = 'arraybuffer'`)
and each message is one packet.

**Wire format** (reader L1547, writer L1586):
* Byte 0 is the opcode (UInt8).
* Integers are **big-endian**: UInt8, UInt16, Int16, UInt32.
* Client→server strings: UInt16 byte length, then UTF-8 bytes.
* **Server→client strings:** UInt16 length, then that many bytes. The client **drops
  the last byte** (the length counts a trailing terminator), then UTF-8-decodes the
  rest. A server must therefore send `len(utf8)+1` and one extra trailing byte.
* `readBitGroup()`: bytes are read until one has bit 0 clear. Bits 1–7 of each byte
  carry booleans or small integers (`getBool`, `getIntWithXBits`).

**Handshake, as observed** (`data/task8_probe.json`; bytes in hex):

| # | Direction | Packet | Meaning |
|---|---|---|---|
| 1 | C→S | `ff` on `/ping` sockets, ×4 per server | ping; server echoes `ff` |
| 2 | C→S | `01 0500 0320 0500 0000 0000` (11 bytes) | **join** (type 1): canvas width 1280, canvas height 800, `window.innerWidth` 1280, session id `""` (sent on reconnect to resume a session). The buffer is allocated as `9 + 2 + len(session)`, so 2 zero bytes trail the string. |
| 3 | S→C | type 2 | **server info**: `sessionId:String, gameVersion:UInt16, gameMode:UInt8, season:UInt8, serverId:String, isAliveInGame:UInt8`, then UInt16 count + static world objects. If `gameVersion > 296` the client alerts "mope.io has been updated!". |
| 4 | S→C | type 3 | connected: shows the menu, hides "Connecting…" after 1 s |
| 5 | S→C | type 17 | world size + camera: `gameWidth, gameHeight, camX, camY, camZoom` (UInt16 each) |
| 6 | S↔C | type 64 | CAPTCHA: the server asks, the client replies `64, String token` (Turnstile) |
| 7 | C→S | `71 0500 0320 0007 6d6f70652e696f` | **play** (type 113): width 1280, height 800, nickname `"mope.io"` (the default when the name box is empty) |
| 8 | S→C | type 6 | "MsgPlayerAliveInGame": you spawned |
| — | C→S | type 11 | clean leave ("instantly deletes player in-game") |
| — | S→C | type 11 | disconnected: `UInt8, String reason, UInt8` |
| — | S→C | type 113 | `DISCONNECT_ON_EXCEEDING_RATE_LIMIT` |

**Full opcode table:** [`OPCODES.md`](OPCODES.md). It is generated from the syntax tree
and lists, for each opcode, the client→server layout and server→client fields with line
numbers. Summary:
* 56 top-level opcodes: 28 client→server, 41 server→client.
* Opcode 23 has a status-effect sub-switch: tail bitten, jellyfish, stingray, ink,
  frozen, fire, …
* Opcode 81 carries a battle-royale sub-protocol (values 33–48, 82–84): countdowns,
  heatwave, win/lose, end screen.
* Quirk: `case 102` appears twice in the dispatcher (L3860 "Msg_client_loadUserData" and
  L3902 "msg 102"). Only the first is reachable.

**Errors and reconnects:**
* `onclose` (L3718) → `_0x5e1846()` (L5723).
* While alive, the session id from message 2 is kept, and the next join sends it to
  resume ("Reconnecting to game session on connection loss…").
* Server type 11 shows the reason and re-joins the best server.

## Task 13 — Accounts and backend

Base URL: `{ACCOUNT_SERVER}` = `https://prod-accounts.mathstudyguides.com`, or
`$config.devAccountServerUrl` when `useDevAccountServer` is true. **That switch is the
hook a local account server can use without patching URLs.**

| Endpoint | Method | Request (JSON keys) | Use |
|---|---|---|---|
| `/auth/login/<provider>` | POST | `sessionId, serverId, accessToken, platform` | social login (google, discord, facebook, apple); returns `{success, data:{userId, token, userName, avatar, …}}` |
| `/auth/@me` | POST | `userId, token, sessionId, serverId, isRefresh` | verify or refresh the stored login; `data` holds `coins, level, xp, xpNextLvl, …`; `coins == 'BANNED'` shows a ban |
| `/auth/logout` | POST | `userId, sessionId, serverId` | logout |
| `/servers/get` | GET | — | player counts (task 11) |
| `/shop/get` | GET | — | shop catalogue |
| `/shop/purchases` | POST | `userId, token, sessionId, serverId` | owned items |
| `/shop/buy` | POST | `userId, token, shopItem, sessionId, serverId` | purchase with coins |
| `/shop/wardrobe/save` | POST | `userId, token, sessionId, serverId, data` | equipped skins |
| `/addCoins?userId=&passwordToken=` | GET | — | admin/dev |
| `/resetAccount?userId=&passwordToken=` | GET | — | admin/dev |
| `/playerSettings_update?userId=&passwordToken=&itemId=` | GET | — | settings |
| `/xsolla/getToken`, `/xsolla/cancelSubscription` | POST | `userId, token, …` | real-money payments (not reconstructed) |
| `/xsolla/getPackets`, `/xsolla/getVip` | GET | — | payment catalogue / VIP |

**Login flow.** An OAuth popup (Google Identity Services, Discord implicit grant,
Facebook SDK, Apple JS) produces an access token. The client POSTs it to
`/auth/login/<provider>` and stores `userId` and `token` in localStorage
(`login_userId`, `login_passwordToken`). The game server can also ask for credentials
with message 72 ("Msg_sendLoginCred") or trigger a reload with message 102
("Msg_client_loadUserData").

For a local reconstruction the account server needs only the `/auth/*`, `/shop/*` and
`/servers/get` shapes above. The social providers can be replaced by a local
username/password or guest provider behind the same `/auth/login/<provider>` route.

## Open questions (carry into Phase 3)

* Opcode semantics marked only by layout in `OPCODES.md` (input bitfields 20/26–30/68,
  types 5, 17, 21, 52, 59–61, 66): name them while building the server, by sending
  packets to the client and watching the result.
* Message 2's static-object block (`createStaticObj`) and the per-tick world update
  format: the next reverse-engineering target, needed to render a world.
* `/updates.json` format (fetched by the Updates panel; not archived).
