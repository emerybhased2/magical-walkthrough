# Mope.io April 23, 2021 — Local Archive Project

Phase 1 of the master plan: the Python snapshot fetcher, `fetch_mope_archive.py`.
It finds, downloads, inventories and validates the historical mope.io client. It does
not rebuild the client or implement any server; later phases build on its output.

## Archive sources (no Wayback Machine)

This project deliberately does **not** use the Internet Archive / Wayback Machine.
Wayback hosts are rejected by the config validator. The fetcher uses:

| Provider      | Archive                                   | Discovery                                   | Raw content                                     |
|---------------|-------------------------------------------|---------------------------------------------|-------------------------------------------------|
| `arquivo`     | [Arquivo.pt](https://arquivo.pt) (Portuguese national web archive) | pywb CDX API                       | `id_` replay (original bytes)                   |
| `commoncrawl` | [Common Crawl](https://commoncrawl.org)   | CDX index server, or binary search of `cluster.idx` when the server is down | WARC records via HTTP range requests |
| `vefsafn`     | [vefsafn.is](https://vefsafn.is) (Icelandic Web Archive) | pywb CDX API                 | none (replay sits behind a CAPTCHA), so discovery only |

You can add any other pywb / OpenWayback-compatible archive as a `"type": "pywb"`
entry in `config.json` → `providers`.

## What the archives hold for April 2021 (checked 2026-11-25)

None of the reachable non-Wayback archives has a mope.io capture **on 2021-04-23**.

| Date (UTC)          | Archive     | What                                                                      |
|---------------------|-------------|---------------------------------------------------------------------------|
| 2021-03-06 07:04    | Common Crawl| full homepage (digest `YSQ4B5PO…`)                                         |
| 2021-03-10 09:17    | Arquivo.pt  | full homepage (same digest) + CSS, jQuery, images, ads scripts             |
| 2021-03-19 20:29    | vefsafn.is  | homepage (index entry only; replay needs a CAPTCHA)                        |
| **2021-04-17 07:27**| Common Crawl| `server-not-modified` revisit → homepage unchanged since 2021-03-06 (`Last-Modified: Fri, 05 Mar 2021`) |
| 2021-06-04 / 06-12  | Arquivo.pt  | later homepage versions                                                    |

The closest evidence is the April 17 revisit, 5.7 days before the target. On that
day the server confirmed that the homepage HTML matched the March 6 / March 10
captures byte for byte.

**`client.js` for 2021 is not in any of these archives.** Arquivo.pt only has 2017,
2018 and 2025 copies. The page loads it as `client.js?x=<timestamp>`, so most crawlers
never saved it. Region and endpoint inventories therefore stay incomplete until a
verified 2021 `client.js` is found, for example by adding another non-Wayback pywb
archive that has one.

## Quick start

```bash
cd mope-april-23-2021
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python fetch_mope_archive.py --discover --list-captures   # what exists, and how close to the target
python fetch_mope_archive.py --all                        # strict: stops (exit code 3) if nothing is on 2021-04-23
```

To build from a capture on another date, name it explicitly. It is never chosen silently:

```bash
python fetch_mope_archive.py --timestamp 20210417072725 --allow-off-date --all
```

## Commands

| Argument          | Function                                                        |
|-------------------|-----------------------------------------------------------------|
| `--discover`      | Query every enabled archive for mope.io and its subdomains      |
| `--list-captures` | Show homepage capture timestamps and their distance from the target |
| `--select`        | (Re)select the anchor homepage capture                          |
| `--download`      | Find resources (HTML/CSS/JS references plus same-day archive index) and download them |
| `--resume`        | Resume an interrupted download (verified files are reused, failures retried) |
| `--analyze`       | Scan `client.js` and other site JS/HTML for regions, endpoints, account, sandbox and dev references |
| `--validate`      | Check essential files, sizes, SHA-256 hashes, local references and the baseline |
| `--report`        | Regenerate every JSON report and print the summary              |
| `--working-copy`  | Create `working/` and record the historical baseline (forced even if validation fails) |
| `--all`           | Run the full pipeline (stages 1–10)                             |

Options: `--date`, `--domain`, `--output`, `--workers`, `--timestamp`, `--timezone`,
`--provider arquivo,commoncrawl`, `--allow-off-date`, `--max-date-distance DAYS`,
`--no-same-day-fallback`, `--no-external`, `--refresh-cache`, `--refresh-working`, `-v`.

Exit codes: `0` ok, `1` error, `2` configuration or dependency problem, `3` no acceptable
capture for the target date.

## Date policy

1. **Anchor:** a homepage capture from the target day, from an archive that serves raw
   content. If there is none, the script stops and lists the nearest captures. An
   off-date anchor needs `--timestamp … --allow-off-date`.
2. **Resources**, in order: same day at or before the anchor, then later the same day
   (`same_day_fallback`), then, only with `--allow-off-date`, the capture nearest the
   anchor within `max_date_distance_days`.
3. **No silent substitution:** archive redirects to a different capture are refused, and
   `Memento-Datetime` / `WARC-Date` must equal the requested timestamp. Arquivo.pt, for
   example, quietly redirects a request for 2021-04-23 to 2021-06-04. That redirect is
   rejected.
4. Each file records its provider, capture timestamp, selection tier and deviation from
   the target date. `archive_report.json` → `historical_fidelity` summarises them.

## Output

```
archive/original/            unmodified downloads (read-only once the baseline is recorded)
  mope.io/…                  original URL paths (query strings dropped; collisions get a hash suffix)
  external-hosts/<host>/…    third-party dependencies
archive/manifests/           capture_manifest, download_manifest, missing_resources,
                             endpoint_candidates, region_candidates, config_candidates,
                             selection, baseline, archive_report (.json)
archive/database/downloads.sqlite3   captures, download records, lookup cache, runs
archive/superseded/          earlier downloads, moved here when the anchor changes
working/client/              editable copy for Phase 2
working/{master-server,account-server,regions,sandbox,developer}/
logs/fetch.log, errors.log, validation.log
```

`archive/`, `working/` and `logs/` are git-ignored. Downloaded game files stay local.

## Rebuilding a readable client.js (`rebuild_client_js.py`)

No archive this project uses holds the 2021 `client.js`. `rebuild_client_js.py`
starts from a real archived copy instead and produces a readable version that is
checked to behave the same as the original:

```bash
python rebuild_client_js.py --list                 # archived client.js captures (non-Wayback)
python rebuild_client_js.py                        # newest: 2025-12-10 (hex-obfuscated build)
python rebuild_client_js.py --closest-to-target    # nearest 2021-04-23: the 2018-04-21 build
python rebuild_client_js.py --all-captures         # 2017, 2018, 2025-09, 2025-12
python rebuild_client_js.py --input path/to/client.js
```

Steps:

1. Fetch the original bytes from the archive and check the capture timestamp. The
   original is kept read-only under `archive/client-js/<timestamp>_<provider>/`.
2. De-obfuscate with edits guided by the JavaScript syntax tree (tree-sitter):
   decode `\x..`/`\u....` string escapes, turn `obj['name']` into `obj.name`, unquote
   `{'name': …}` keys, turn hex numbers into decimal, and replace `![]`/`!![]` with
   `false`/`true`.
3. Beautify with jsbeautifier.
4. Verify that the rebuilt syntax tree equals the original's, allowing only those
   rewrites, and run `node --check` when node is installed. The script writes nothing
   if verification fails.

Output: `working/client-js/<timestamp>_<provider>/client.js` (with a header that
records the source, sha256 and date gap) plus `rebuild_report.json`. The report
includes the client's own `$config` block when present: in the 2025 build that is
`gameVersion`, `useDevAccountServer`/`devAccountServerUrl`, the sign-in provider IDs
and the `gameServers` list. `--beautify-only`, `--utf8`, `--indent N` and `--no-verify`
adjust the output.

These builds are **not** the April 23, 2021 client. The 2018 build is 3.0 years
before the target and the 2025 builds are 4.4–4.6 years after it. Treat them as
references for structure and protocol, not as the historical baseline.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run offline. They cover the date policy, path containment, the Common Crawl
`cluster.idx` binary search, WARC parsing, revisit resolution, redirect and Memento
refusal, reference extraction and region/endpoint detection. For the client rebuild
they cover string escape decoding, every rewrite and its edge cases, and the
tree-equivalence check.

## Phase 2 — client reverse engineering (2025-12-10 baseline)

Phase 2 uses the complete same-day **2025-12-10** Arquivo.pt snapshot (`snapshots/2025-12-10/`,
git-ignored). No 2021 `client.js` exists outside the Wayback Machine. The April 2021 archive
stays unchanged as the historical record. Findings for tasks 8–13 are in
[`phase2/README.md`](phase2/README.md), with a generated opcode reference in
[`phase2/OPCODES.md`](phase2/OPCODES.md).

| Script | Task | What it does |
|---|---|---|
| `extract_client_protocol.py` | 9–13 | Reads the syntax tree of the rebuilt client and inventories opcodes, message layouts and field names, the dispatcher, HTTP/WebSocket endpoints, `$config` and regions. Writes JSON plus `OPCODES.md`. |
| `phase2_probe_client.py` | 8 | Serves the snapshot as `http://local.mope.io/` in headless Chromium with no production traffic. Records requests, console output and errors, and every WebSocket frame the client sends. |
| `shims/turnstile_local.js` | 8 | Local stand-in for Cloudflare Turnstile. The 2025 page only starts `client.js` from Turnstile's `onload` callback. Local testing only. |

```bash
python fetch_mope_archive.py --date 2025-12-10 --output snapshots/2025-12-10 --provider arquivo --all
python rebuild_client_js.py --output snapshots/2025-12-10 --timestamp 20251210042757 --provider arquivo
python extract_client_protocol.py --project snapshots/2025-12-10
python phase2_probe_client.py --project snapshots/2025-12-10 --click-play
```

## Phase 3 — local server infrastructure

The local stack (`localstack.py`, `services/`, `config/local_stack.json`) runs the 2025-12-10 client
against a local master server, a web gateway and one region server per region (9 archived + 11 local additions). No
production connections are made. Details and the 34-check verification are in
[`phase3/README.md`](phase3/README.md).

```bash
python localstack.py up          # then open http://localhost:8080/ (no hosts-file entry needed)
python phase3_verify.py          # checks against the running stack
```

## Phase 4 — accounts and configuration

A local account server (`services/account_server.py`, SQLite) implements the client's archived account API. A
"Login with a local account" page replaces social sign-in, and all settings live in a validated `.env`
(`.env.example`, `python localstack.py check-config`). Sandbox and developer controls are reconstructed only where
the client shows evidence. See [`phase4/README.md`](phase4/README.md); `python phase4_verify.py` runs the 8 checks.

## Phase 5 — local gameplay (first playable milestone)

Every local region now simulates a world. Pressing Play in the archived client spawns a mouse that follows the
cursor, eats food, gains XP and evolves (Mouse → Rabbit → Pig by default), and players in the same region see each
other. Message layouts (spawn 17/107/18/6, world update 4, cursor 5) come from the client's own reader code. The
evolution chain, thresholds and food settings are configurable approximations, because the original server
decided them. See [`phase5/README.md`](phase5/README.md); `python phase5_verify.py` runs the 8 checks, including
the real client evolving in Chromium.

## Image catalogue and decoder (`decode_images.ts`, Bun)

`bun decode_images.ts` decodes every image path the archived client can build: animals, food, game objects,
abilities, disasters, shop and UI. It then fetches every archived version of those images from Arquivo.pt and
Common Crawl (vefsafn.is is used for discovery only; no Wayback Machine). Each file is decoded from its own
bytes (PNG data inflated and CRC-checked; JPEG, GIF, WebP, SVG and ICO structure checked), and the files are
sorted by category and animal. Images stay local in `archive/images/` (git-ignored). Results and coverage are in
[`images/README.md`](images/README.md).

```bash
bun install                     # acorn (JavaScript parser)
bun decode_images.ts            # catalogue + discover + download + decode + report
bun decode_images.ts --catalog-only
bun test tests/
```

## Live-site image snapshot (`fetch_live_assets.rb`, Ruby)

The live mope.io site is a 2026 rebuild with a new file layout (WebP files under `assets/animals/…`,
`assets/foods/…`, `assets/UI/…`). `ruby fetch_live_assets.rb` reads the site's bundles as text, derives the image
paths from its own data (animal table, subspecies enums, part names, food list, texture names) and downloads every
file that exists. The site serves a 200 HTML page for unknown paths, so only real image bytes count. The 2026-11-26
snapshot holds 622 images, with files for all 103 animals. It is kept separate from the archived client's art,
which these files do not replace. Details: [`live/README.md`](live/README.md).

## Phase 6 — integration, final verification, startup and maintenance

```bash
./start-all.sh              # setup (venv, requirements, .env, checks) + start everything + status
./start-all.sh logs -f      # central logs, merged
./start-all.sh verify       # all unit tests + phase6_verify.py (every region, multiplayer, accounts, assets, real client)
./start-all.sh stop         # graceful shutdown
.venv/bin/python phase6_compat.py   # regenerate phase6/COMPATIBILITY.md
```

Integration found and repaired two incompatibilities: the asset season the server sent, and the client's
unhandled resize message. The gateway now serves the archived images after the snapshot.
[`phase6/README.md`](phase6/README.md) covers installation, startup, troubleshooting and maintenance.
[`phase6/COMPATIBILITY.md`](phase6/COMPATIBILITY.md) measures the stack against the April 2021 archive and lists every
feature as completed, partial or unresolved.

## Pumpkin variants (`fetch_pumpkins.rb`, Ruby)

`ruby fetch_pumpkins.rb` reads the live site's pumpkin class and fetches its 7 variants × 5 frames. It also
probes `s3.mope.io`, which refuses every path. It then adds the custom variants in `custom_assets/pumpkins/`
(amber, sapphire, amethyst, black hole) and installs all 11 types where the archived 2025 client looks for
them: `assets/s/1/img/pumpkin/<type>/<frame>.png` in the working snapshot. The region servers spawn pumpkins
(object type 131) of every installed type. Details: [`pumpkins/README.md`](pumpkins/README.md).

## Live shop skins (`import_live_skins.rb`, Ruby)

`ruby import_live_skins.rb` puts every skin of the live shop (`api.mope.io/store`: the catalogue, the shelves
and the skins inside bundles) into the archived 2025 client's shop. On 2026-11-26 that was **356 skins for 93
animals, all imported** (336 for coins, 20 for gems, 6 on sale).

How the 2025 client is fed without changing it (rebuilt `client.js`, shop load and `getImage`):

- Each skin becomes a `/shop/get` item with `shopCategory 0`, `canBuy: true` (the Skins tab hides anything
  else), the 2025 `animalType` of its animal and the live price, currency and sale price (`saleType 2`).
- The animal table is read from the snapshot's own `client.js` (constants, `getAnimalName()`, `animalInfo()`,
  the premium-skin table). Live names that differ are mapped: crocodile → croc, hippopotamus → hippo,
  rhinoceros → rhino, boa → boa constrictor, orca → killer whale, lava monster → land monster.
- The client builds the picture path itself (`assets/s/<season>/skins/<animal>.ui.png`), so each skin gets a
  season number of its own (1000 and up, kept stable between runs). The web gateway answers that path with the
  skin's picture from `s3.mope.io/mope-prod/items/<itemId>/<animal>.ui.webp`.

```bash
ruby import_live_skins.rb                  # read the live shop, download missing pictures (~1.5 min the first time)
ruby import_live_skins.rb --dry-run        # print the animal mapping only
ruby import_live_skins.rb --refresh-images # download every picture again
./start-all.sh restart                     # the account server and gateway pick the files up
ruby tests/test_live_skins.rb
```

Output goes to `snapshots/2025-12-10/live-skins/` (`SHOP_LIVE_SKINS_DIR`, git-ignored): `catalog.json`,
`manifest.json`, `images/` and `report.json`. Checked in Chromium against the running stack: all 356 tiles
in the Skins tab (72 pages) with their pictures, no client errors. Buying charges the sale price and checks
coins and gems.

Limits: a tile shows the animal's name, not the skin's (the client overwrites `skinName`). Its season badge
shows the assigned number (S1000–S1355). Bought skins cannot be worn in a game yet. Live bundles are not
offered as Deals.

## Ability frames (`fetch_ability_frames.rb`, Ruby)

`ruby fetch_ability_frames.rb` downloads every ability image of the live game: the HUD ability frames of every
animal and subspecies (`mope.io`), those of every shop skin (`s3.mope.io/mope-prod/items/<itemId>/ability*.webp`)
and the ability effect sprites (fireball, fire tornado, bite, claw, stomp, ...). Every path is derived from the live
bundle and shop, and numbered files are followed to their last frame. On 2026-11-26: 425 images. Details:
[`abilities/README.md`](abilities/README.md).

## Hosting the backend (Sevalla)

The account, master and region servers can run in one container behind one port (`cloud.py`, `services/edge.py`,
root `Dockerfile`). The game client stays on each player's machine and connects through a local relay when
`REMOTE_BACKEND_URL` is set. See [`DEPLOY_SEVALLA.md`](DEPLOY_SEVALLA.md).

## Client version history (`compare_builds.py`)

`python rebuild_client_js.py --all-captures && python compare_builds.py` rebuilds every archived client build
(2017-02-13, 2018-04-21, 2025-09-05, 2025-12-10) and compares them with the Phase 2 and Phase 5 extractors. It covers
protocol opcodes, animals (with probable renames), ability names, object types, regions and `$config`. It also compares
2025-12 with the live 2026 site by name. Results: [`history/README.md`](history/README.md).
