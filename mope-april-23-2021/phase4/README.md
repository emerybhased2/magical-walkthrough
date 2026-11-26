# Phase 4 — Account server and configuration reconstruction

The archived 2025-12-10 client now logs in, keeps its session, shows coins and gems, and
opens its shop against a **local account server**. Every configuration control is typed,
documented, validated at startup and tied to evidence in the client.
**Verification: 8/8 browser + API checks** ([`data/verify_report.json`](data/verify_report.json)) and
the offline unit tests (`tests/test_accounts.py`).

```bash
python localstack.py init            # creates .env with documented defaults (optional)
python localstack.py check-config    # validates config/local_stack.json + .env, shows every value and its source
python localstack.py up              # master, account, 20 regions, web
# http://localhost:8080/ -> LOGIN -> "Login with a local account"
python phase4_verify.py
```

## Tasks 21–23 — Account server, functionality and storage

`services/account_server.py` listens on `127.0.0.1:8091`. The gateway forwards account
paths to it, so the client sees them on its own origin: its `$config.devAccountServerUrl`
is set to the gateway through the client's own `useDevAccountServer` switch (Phase 3).
Every response uses the envelope the client's code actually reads (`phase2/README.md`,
task 13):

| Endpoint | Behaviour |
|---|---|
| `POST /auth/login/<provider>` | `google`/`facebook`/`apple`/`discord` → **501**, with a pointer to local accounts. Social sign-in needs mope.io's OAuth apps and cannot work offline. `<DEV_TEST_ACCOUNT_SECRET>` → test-account hook (task 27). |
| `POST /auth/@me` | `{success, data:{userId, userName, avatar, coins, gems, level, xp, xpNextLvl, hasVip, hasAutoRenew}, coins}`. Banned accounts get `coins: "BANNED"`, which the client checks for. A bad or expired token gets **401**, and the client then clears its stored login. |
| `POST /auth/logout` | `{success:true}`. The client sends only `userId`, so tokens are not revoked without the token itself. |
| `GET /shop/get` | `{items, settings}` from `config/shop_catalog.json` |
| `POST /shop/purchases` | `{success, data:{settings, purchases:[itemId…], wardrobe}}` |
| `POST /shop/buy` | Checks the price (`salePrice` when `saleType`) and currency (0 coins, 1 gems). Atomically deducts it and records the purchase. Returns `{success, reason}`. |
| `POST /shop/wardrobe/save` | Stores the wardrobe JSON (≤ 64 KB) and returns `{success, data}` |
| `GET /playerSettings_update?…` | Answers **as text** (`text/plain`), because the client calls `JSON.parse` on the body itself |
| `GET /addCoins`, `/resetAccount` | Developer endpoints, off by default (task 27) |
| `GET /xsolla/getPackets`, `/getVip` | `200 {success:false}`: nothing for sale, without an error toast on every page load |
| `POST /xsolla/*` | 501: real-money payments are not reconstructed |

**Local accounts.** These replace social sign-in, and the client stays unmodified.
* The gateway adds one button, "Login with a local account", to the client's login
  popup (a documented serve-time rewrite; it can be switched off). It opens
  `/auth/local`, a small login and registration page.
* On success the page stores the result under the **client's own localStorage keys**:
  `login_userId`, `login_passwordToken`, `login_name`, `login_profilePicURL`,
  `login_socialNetworkName`.
* The client then behaves exactly as after a social login. It shows the name, verifies
  the login with `/auth/@me` each time it joins a region, and shows the coins and gems
  from that answer.

**Storage** (`services/account_store.py`, SQLite at `ACCOUNT_DB_PATH`):

| Table | Holds |
|---|---|
| `users` | id, unique username, provider, password hash, coins, gems, level, xp, VIP flags, ban flag, timestamps |
| `tokens` | login tokens: only their SHA-256 is stored, with expiry, revocation and last use |
| `purchases`, `wardrobe`, `player_settings` | shop data |
| `login_failures` | per-username lockout counter |

* **Passwords:** scrypt (N=2¹⁴, r=8, p=1) with a random salt, checked in constant time.
* **Migrations:** versioned in `schema_version`, applied in order at startup, and safe
  to re-run (tested).

**Admin CLI** (local only, works on the database file directly):

```bash
python services/account_admin.py list
python services/account_admin.py create alice --password '...'
python services/account_admin.py coins alice +500      # or gems; N, +N, -N
python services/account_admin.py ban alice             # revokes logins; @me then reports BANNED
python services/account_admin.py grant alice someItemId
python services/account_admin.py set-password alice    # revokes existing logins
```

**Not reconstructable from evidence:**
* The shop catalogue (item ids, prices, bundles) was server-side data. It starts empty;
  the item fields the client reads are documented in `config/shop_catalog.json`.
* The XP / level curve (`xpNextLvl` is 0).
* Xsolla real-money payments.

## Task 24 — Master server (final)

There are no changes since Phase 3, except that `/master/client-config` now also
supplies the client flags from task 27 (`env`, `isProduction`, `isTestingMode`,
`isBetaMode`). Region registration, discovery, status and `/servers/get` are as
documented in `phase3/README.md`.

## Task 25 — `.env` (account-server controls)

Copy [`../.env.example`](../.env.example) to `.env`, or run `python localstack.py init`.
The file is generated from `services/settings.py`, so the two cannot drift apart.
Priority: defaults < `.env` < process environment. `.env` and `data/` are git-ignored.

| Variable | Default | Purpose |
|---|---|---|
| `ACCOUNT_DB_PATH` | `data/accounts.sqlite3` | account database |
| `ACCOUNT_REGISTRATION_ENABLED` | `true` | allow new local accounts |
| `ACCOUNT_LOGIN_ENABLED` | `true` | allow logins |
| `ACCOUNT_USERNAME_MIN_LENGTH` / `MAX_LENGTH` | `3` / `15` | the client's name box holds 15 characters |
| `ACCOUNT_PASSWORD_MIN_LENGTH` | `8` | |
| `ACCOUNT_SESSION_TTL_DAYS` | `30` | lifetime of a login token |
| `ACCOUNT_LOGIN_MAX_FAILURES` / `LOCKOUT_MINUTES` | `10` / `15` | brute-force lockout |
| `ACCOUNT_STARTING_COINS` / `GEMS` | `0` / `0` | |
| `ACCOUNT_SHOP_ENABLED`, `ACCOUNT_SHOP_CATALOG` | `true`, `config/shop_catalog.json` | |
| `CLIENT_ENV`, `CLIENT_TESTING_MODE`, `CLIENT_BETA_MODE` | `production`, `false`, `false` | client flags (task 27) |
| `WEB_UPDATES_FILE` | `config/updates.json` | the Updates panel |
| `DEV_TEST_ACCOUNTS_ENABLED`, `DEV_TEST_ACCOUNT_SECRET` | `false`, empty | test-account hook (task 27) |
| `DEV_ADMIN_ENDPOINTS_ENABLED`, `DEV_ADD_COINS_AMOUNT` | `false`, `1000` | `/addCoins`, `/resetAccount` |
| `SANDBOX_REGION_ENABLED`, `SANDBOX_REGION_PORT` | `false`, `7099` | sandbox region (task 26) |

## Tasks 26–27 — Sandbox and developer configuration, from evidence only

Every control below exists because the archived client contains it. Nothing was added
on the strength of "games usually have this".

| Control | Evidence in the client (line numbers: the Phase 2 rebuild) | Reconstruction |
|---|---|---|
| `CLIENT_ENV` | `$config.env` ('production'). L4536: `env == 'development'` shows a "[DEV] fatal error" handler. L4988: the Updates feed filters entries by `env` unless it is `development`. L6628: `development`/`staging` switch the payment widget to the payment provider's sandbox. | Put into `$config.env` (+ `isProduction`) |
| `CLIENT_TESTING_MODE` | `$config.isTestingMode` → `_0x2a1150`: verbose logging of account requests; refreshes the account after `/addCoins` | `$config.isTestingMode` |
| `CLIENT_BETA_MODE` | `$config.isBetaMode` → `_0x517c11`: "best server" threshold 900 instead of 400, "FULL" at 800 instead of 500 | `$config.isBetaMode` |
| `DEV_TEST_ACCOUNTS_*` | L6056–6068: with `?testAccountSecret=S&user=U` (U ≤ 2 chars) and no login, the client POSTs `/auth/login/S` with `accessToken = U`, and treats itself as logged in when its userId equals `S + U` | `/auth/login/<secret>` creates or logs in the account with userId `secret + user`, name `test-<user>`. Off by default; the secret is validated. |
| `DEV_ADMIN_ENDPOINTS_*` | `_0x4ab4f3` / `_0x54c3ec` call `/addCoins` and `/resetAccount` with `userId` and `passwordToken` | Implemented, token-checked, off by default |
| `useDevAccountServer` / `devAccountServerUrl` | `$config` keys, L268–269 | Always on: this is how the client reaches the local account server |
| `SANDBOX_REGION_*` | L288 `_0x5be1b9()`: `addServer('LOCAL', 'localhost', 'SANDBOX')`. Never called in this build. | An extra region **LOCAL** (id `localhost`, region label **SANDBOX**), registered with the master like any region |

**Evidenced but belonging to Phase 5** (sent by the game server, not configuration):
* `flag_isDevMode` / `devMode_num` / `flag_hideDevPrint`: per-animal developer flags read
  from object updates (L9954–9967, L10595–10601).
* `_0x458ed7`: a "developer" flag read from a server bit group (L4517). It enables the
  same [DEV] handler as `env == 'development'`.

**No evidence, so not invented:** sandbox game rules (free animal choice, god mode, …)
and developer gameplay commands.

## Task 28 — Configuration validation

`services/settings.py` is the single loader. It:
* parses `.env` (comments, quotes, `export`, inline `#`);
* applies environment overrides;
* converts types (bool, int with bounds, choice, path);
* checks cross-field rules (username min ≤ max; a valid secret when test accounts are
  on; catalogue and updates files exist);
* rejects unknown keys in `.env` (likely typos).

`common.load_stack_config` adds the stack checks (unique ports and ids, including the
sandbox region). Every service calls it at startup and refuses to run with the **full
list** of problems:

```
$ ACCOUNT_PASSWORD_MIN_LENGTH=2 CLIENT_ENV=prod DEV_TEST_ACCOUNTS_ENABLED=true SANDBOX_REGION_ENABLED=true \
  SANDBOX_REGION_PORT=7001 python localstack.py check-config
error: invalid configuration:
  - ACCOUNT_PASSWORD_MIN_LENGTH must be >= 4, got 2 (from environment)
  - CLIENT_ENV must be one of production, staging, development, got 'prod' (from environment)
  - DEV_TEST_ACCOUNTS_ENABLED=true needs DEV_TEST_ACCOUNT_SECRET of 8-64 characters (A-Z a-z 0-9 _ -)
  - port 7001 is used by more than one service: prod-lnd-us-ny-1, localhost
```

## Verification (`phase4_verify.py`, real archived client)

| Check | Result |
|---|---|
| C1 configuration validates | pass |
| A1 API via the gateway: register, `@me`, wrong password refused, social sign-in refused, dev endpoints off, `/updates.json` served | pass |
| B1 the client's login popup offers "Login with a local account" | pass |
| B2 registering on the local page signs the client in; name shown, coins from `/auth/@me` shown | pass |
| B3 the shop opens with data from the local account server | pass |
| B4 logging out through the client clears the login | pass |
| B5 forged stored credentials → `@me` 401 → the client clears them | pass |
| B6 the archived test-account hook logs in `test-ab` (userId = secret + user); the account server is restarted with test accounts on for this check, then restored | pass |
