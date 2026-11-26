# Historical compatibility report (Phase 6, task 34)

Generated 2026-11-26T15:28:17Z by `phase6_compat.py`. Re-run it after changes; the numbers below are measured, and the feature matrix is maintained in the script with its evidence.

## 1. Baselines

| | |
|---|---|
| Historical target | 2021-04-23 |
| Closest archived homepage | 2021-04-17 07:27 UTC, Common Crawl revisit (HTTP 304: unchanged since the 2021-03-06 capture), content from Arquivo.pt / Common Crawl March 2021 |
| Client the stack runs | 2025-12-10 Arquivo.pt capture (no 2021 `client.js` exists outside the Wayback Machine) |

## 2. Homepage: April 2021 vs. the served 2025 page

- Element ids: 92 in 2021, 75 in the served page, **65 shared**.
- Only in 2021 (27): `addCoins`, `btnRegister`, `cdm-zone-01`, `cdm-zone-02`, `chromebugdiv`, `chromebugp`, `graphicsBut_mid`, `loginTestAccBtn`, `loginTestAccBtn2`, `loginTestAccBtn3`, `loginTestAccBtn4`, `loginTestAccBtn5`, `loginTestAccBtn6`, `login_SBLabel`, `logoutbut`, `moneyRectBottomWrapUpgrade`, `moneyRectBottomWrapUpgrade_sidebanner`, `mope-io_300x600`, `mope-io_970x90`, `my-content-2`, `plusCoins`, `preroll`, `pvVidContainer`, `resetAccount`, `testingButs`, `upgradescreenads`, `upgradescreenads_sidebanner`
- Only in 2025 (10): `appleLoginBtn`, `btnContinueEndScreen`, `discordLoginBtn`, `graphicsBut_low`, `graphicsBut_med`, `loginGems`, `login_gems`, `moreGames`, `shopbutton3`, `turnstile-script`

Resources the 2021 homepage references: 28; served by the local gateway: **16**.

| 2021 resource | Served locally | From | In the 2021 archive |
|---|---|---|---|
| `./img/appAd_and.png` | yes | served snapshot | yes |
| `./img/appAd_ios.png` | yes | served snapshot | yes |
| `/assets/s/1/skins/mouse.png` | yes | fallback mope.io | yes |
| `/cdn-cgi/apps/head/X2NGd4C5a3tonHwpUwe1lo9cVCc.js` | no |  | yes |
| `/dfp.js` | yes | served snapshot | yes |
| `adblockdet/ads.js` | yes | served snapshot | yes |
| `assets/s/1/skins/land/aiboss/gujjar.png` | yes | fallback mope.io | yes |
| `assets/s/1/skins/land/aiboss/sultan.png` | yes | fallback mope.io | yes |
| `favicon.ico` | yes | served snapshot | yes |
| `http://addictinggames.com` | no | third-party host addictinggames.com (blocked by design) | no |
| `http://www.mope.io/updates.txt` | no |  | no |
| `https://apis.google.com/js/platform.js` | no | third-party host apis.google.com (blocked by design) | no |
| `https://goo.gl/i3xQWQ` | no | third-party host goo.gl (blocked by design) | no |
| `https://goo.gl/ofAUPJ` | no | third-party host goo.gl (blocked by design) | no |
| `https://iogames.space/` | no | third-party host iogames.space (blocked by design) | no |
| `https://www.addictinggames.com/sites/default/files/logo.png` | no | third-party host www.addictinggames.com (blocked by design) | no |
| `https://www.google.com/intl/en_ca/chrome/canary/` | no | third-party host www.google.com (blocked by design) | no |
| `https://www.reddit.com/r/mopeio/` | no | third-party host www.reddit.com (blocked by design) | no |
| `https://www.youtube.com/channel/UCNfK9bZk3Br2KDjXXdKuM1g/community?view_as=subsc` | no | third-party host www.youtube.com (blocked by design) | no |
| `img/logo.png` | yes | served snapshot | yes |
| `img/settings.png` | yes | served snapshot | yes |
| `img/sound_on.png` | yes | served snapshot | yes |
| `img/x.png` | yes | served snapshot | yes |
| `jquery_3_3_1.js` | yes | served snapshot | yes |
| `main_nerged.css?v=live.257` | yes | served snapshot | yes |
| `privacy/index.html` | no |  | no |
| `shop/close.png` | yes | served snapshot | yes |
| `shop/mopecoin.png` | yes | served snapshot | yes |

## 3. Protocol coverage

Client side from the Phase 2 inventory of the archived client (`phase2/data/protocol_inventory.json`); server side measured from `services/region_server.py`.

- Client -> server: the server handles **7/28** of the opcodes the client can send.
  - Handled: 1 (join), 5 (cursor position), 11 (leave), 17 (resize), 64 (captcha token), 113 (play), 255 (ping)
  - Not handled yet: 19 (chat), 20, 21, 24, 26, 27, 28, 29, 30, 52, 56, 59, 60, 61, 62 (Msg_adblockCheckCheck), 63, 66, 67, 68, 71, 103
- Server -> client: the server sends **10/64** of the opcodes the client understands.
  - Sent: 2 (server info), 3 (connected), 4 (world update), 6 (alive in game), 11 (disconnect), 16 (notification), 17 (game size), 18 (your animal changed), 107 (minimap), 255 (pong)
  - Never sent yet: 5, 7, 8, 9, 10, 12, 13, 14 (you died), 19 (chat message), 23 (hurt / effect text), 24, 25, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45 (Msg_battleroyal_endScreen), 46, 48, 56 (spectate mode), 58, 59, 62 (Msg_adblockCheckCheck), 63, 64, 65 (Msg_readyToPlay), 67, 69, 72 (Msg_sendLoginCred), 81, 82, 83, 84, 100 (snowfall), 102 (Msg_client_loadUserData), 103, 104, 105, 106, 108, 109, 111, 112 (MSG_GAMEROOM_PROPERTY_UPDATE), 113 (rate-limit disconnect), 114, 115

## 4. Assets

- The client can build 829 image references (606 distinct templates). Non-Wayback archives hold 82 mope.io images (102 versions); all are served after the snapshot.
- The live 2026 site snapshot (622 files) is a different build and is not used.
- Audio: none archived.

## 5. Integration status (latest `phase6_verify.py`)

12/12 checks passed (2026-11-26T04:54:47Z).

| Check | Result | Detail |
|---|---|---|
| C1 configuration validates | PASS | configuration OK |
| S1 every service up (master, account, gateway, all regions online in the master) | PASS | web True, master True, account True, regions online 20/20 |
| R1 region selection: /servers/get and the served $config.gameServers list every enabled region | PASS | /servers/get 20/20, $config.gameServers 20/20 |
| G1 every region: join, Play, spawn (17, 107, 18, 6 + own animal) | PASS | 20/20 regions |
| G2 multiplayer in every region: two players see each other; a leaver is removed | PASS | 20/20 regions |
| A1 account registration, login, @me and a refused wrong password (through the gateway) | PASS | register 200, login 200, @me 200, wrong password 400 |
| W1 assets: snapshot + archived-image fallback served, season-1 sprite present, traversal refused | PASS | /index.html 200, /img/logo.png 200 snapshot, /assets/s/1/skins/land/mouse/0/mouse.png 200 snapshot, /img/update/promo3.png 200 fallback:images/files, /skins/bea |
| B1 real client session: only known gaps fail, no unhandled client messages, no page errors | PASS | known gaps ['404 /audio/click.mp3', '404 /audio/music_game.mp3', '404 /audio/nom.mp3', '404 /img/howToPlay1.png', '404 /none'], unexpected [], unhandled opcodes |
| V3 phase3_verify.py passes in full | PASS | 34/34 checks |
| V4 phase4_verify.py passes in full | PASS | 8/8 checks |
| V5 phase5_verify.py passes in full | PASS | 8/8 checks |
| U1 unit tests: Python, Bun, Ruby | PASS | python ok (93 tests), bun ok (20 tests), ruby ok (1852 tests) |

## 6. Feature matrix

| Area | Feature | Status | Evidence |
|---|---|---|---|
| Archive | April 2021 homepage, CSS, images (non-Wayback) | **partial** | Anchor 2021-04-17 (-5.7 days, Common Crawl revisit = homepage unchanged since 2021-03-06); 22 files |
| Archive | April 2021 client.js | **unresolved** | Not in Arquivo.pt, Common Crawl or vefsafn.is; the stack runs the 2025-12-10 client (README 'What the archives hold') |
| Client | Readable, verified rebuild of the archived client | **completed** | rebuild_client_js.py (syntax-tree equivalence) |
| Client | Client served locally with no production traffic | **completed** | web gateway: $config overlay, Turnstile shim, domain lock satisfied (phase3 B checks) |
| Master | Region registry, heartbeats, /servers/get, failover | **completed** | phase3_verify.py 34/34 |
| Regions | 20 regions (9 archived + 11 added), join/leave/resume/shutdown | **completed** | phase6 S1, G1 |
| Regions | Region selection by the client (ping + server list) | **completed** | phase3 B checks, phase6 R1 |
| Accounts | Local registration, login, @me, logout, forged-token rejection | **completed** | phase4_verify.py 8/8, phase6 A1 |
| Accounts | Social sign-in (Google, Facebook, Apple, Discord) | **unresolved** | Needs mope.io's OAuth apps; refused with 501 and a pointer to local accounts |
| Accounts | Shop, coins, gems (local catalogue) | **partial** | Catalogue is local data (config/shop_catalog.json); purchases with real payment providers are not possible |
| Gameplay | Spawn, movement, camera, minimap, interpolation-compatible updates | **completed** | phase5_verify.py, phase6 G1 |
| Gameplay | Food, eating, XP and water HUD | **partial** | Food type 3 only; water stays at 100 % (no drinking) |
| Gameplay | Evolution | **partial** | Mouse -> Rabbit -> Pig; thresholds/radii/speeds are documented approximations |
| Gameplay | Multiplayer visibility in every region | **completed** | phase6 G2 (all regions) |
| Gameplay | Animals 4+ / per-animal reader fields | **unresolved** | Types 7+ need their readCustomData fields |
| Gameplay | Predators, combat, HP, death screen, respawn | **unresolved** | S->C 14 (you died) and HP updates not sent |
| Gameplay | Biomes, water sources, hills, holes, mud, trees | **unresolved** | Single land world; minimap has no shapes |
| Gameplay | Abilities (W / S keys) | **unresolved** | Ability fields of message 18 are 0; C->S ability opcodes unhandled |
| Gameplay | Leaderboard, player count, chat, emotes, teams | **unresolved** | See protocol coverage below |
| Assets | Archived images served to the client | **partial** | 82 images (snapshot first, then archive/images fallback); most in-game sprites were never archived outside Wayback |
| Assets | Audio | **unresolved** | No mope.io audio in any archive used; the client logs 404s for audio/*.mp3 |
| Config | Sandbox and developer controls | **partial** | Reconstructed only where the client shows evidence (phase4/README.md); server-side dev tools unknown |
| Config | Original server-side balance data (XP tables, speeds, spawn rates) | **unresolved** | Not in the client; local values are configuration, marked as approximations |
| Operations | start-all.sh: setup, .env, start/stop/restart/status/logs/verify | **completed** | phase6/README.md |

Totals: 9 completed, 6 partial, 9 unresolved.
