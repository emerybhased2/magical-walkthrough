# Image catalogue and decoder (`decode_images.ts`)

A Bun script that works out **which images the archived 2025-12-10 client can request**
(animals, food, game objects, abilities, disasters, shop, UI, themes), fetches **every
archived version** of them from non-Wayback archives, and **decodes each file** to prove it
is a complete, valid image. The images stay local in `archive/images/` (git-ignored). This
folder holds the documentation and the generated summaries ([`data/`](data/)), with no image
bytes.

```bash
bun install                                  # acorn + acorn-walk (JavaScript parser)
bun decode_images.ts                         # catalogue + discover + download + decode + report
bun decode_images.ts --catalog-only          # only decode the client's image paths (offline)
bun decode_images.ts --providers arquivo     # skip Common Crawl / vefsafn
bun decode_images.ts --versions nearest      # one version per image instead of every version
bun test tests/                              # 20 offline tests
```

Options: `--client`, `--output`, `--domain`, `--date` (default 2021-04-23, used to choose the
primary version), `--providers arquivo,commoncrawl,vefsafn`, `--cc-from` / `--cc-to` (crawl
years), `--versions all|nearest`, `--workers`, `--refresh` (ignore cached archive indexes).

## 1. How the client builds image paths

Line numbers refer to the readable rebuild
`snapshots/2025-12-10/working/client-js/20251210042757_arquivo/client.js`.

| Helper | Resolves `rel` to | Evidence |
|---|---|---|
| loaders `_0x43952a`, `_0x25f30f` | `<rel>?v=<gameVersion>` from the site root | L2331, L2348 (detected as "creates an `Image` and appends `?v=`") |
| `getSkinImageObj(rel)` | `./assets/s/<mopeSeasonID>/<rel>`, or `./assets/t/<skinThemeID>/<rel>` with a theme | L8505, L10727 |
| `getSkinImageObjForSeason(rel, s)` / `getObjImageObjForSeason` | `./assets/s/<s>/<rel>` | L8510, L8514 |
| `getSkinImagePath(rel)` | as above; `_dive` images always use season 2 | L10714 |
| `getImagePath` / `getImageObjForPath(rel, s)` | `./assets/s/<s>/<rel>` | L10736, L10740 |
| `getSkinImagePathForShop(rel)` | `./assets/s/<season>/skins/<rel>` | L10722 |

About 22 animal and object classes override `getSkinImageObj`, but each override only picks
a different season (for example season 1 for some species) and then calls
`getSkinImageObjForSeason`. Every override therefore keeps the `assets/s/<season>/` layout.
Older clients (2017–2020 captures) loaded `skins/…` and `img/…` from the site root, and the
archives contain both layouts.

`rel` itself is built from string concatenation, constants, ternaries and per-class path
helpers (`getWing`, `getHead`, `getSpeciesPath`, …). The script parses the client with acorn
and **evaluates these expressions symbolically, without running any client code**:

- constants are variables assigned exactly once, to a literal (1,310 found);
- a ternary expands to both branches, so `'img/coconut' + (outlined ? '_e' : '') + '.png'`
  gives `img/coconut.png` and `img/coconut_e.png`, and the `_e` "edible outline" variant
  marks the image as **food**;
- `this.method(...)` calls are resolved through the prototype chain
  (`X.prototype = Object.create(Y.prototype)`, 238 classes) and interpreted from their own
  syntax tree;
- a local variable defined once in the enclosing function is replaced by its definition;
- anything that depends on runtime state becomes a named hole, such as `{animalSpecies}` or
  `{speciesType}`.

It also decodes the client's animal table (the `switch` that sets `aniName`/`skinName`,
L804): **73 animals**, from 1 Mouse to 75 Toucan ([`data/animals.json`](data/animals.json)).

### Client references by category

| Category | Distinct path templates | Example |
|---|---|---|
| animals | 294 | `skins/land/peacock/{animalSpecies}/peacock_wing1.png` |
| game-objects | 52 | `gameobj/{oType}/{speciesType}/0.png` |
| abilities | 44 | `skins/ocean/squid/{animalSpecies}{getSubSpecies()}/ability.png` |
| world-and-effects | 35 | `img/pumpkin/{pumpkinType}/{frameNumber}.png` |
| food | 24 | `img/coconut[_e\|].png` |
| ui | 15 | `img/sound_on.png` |
| shop-ui | 14 | `shop/mopecoin.png` |
| disasters | 9 | `img/disasters/cloud1.png` |
| shop-skins | 1 | `assets/s/<season>/skins/{…}` |
| other | 118 | fragments whose folder is only known at runtime |

In total there are **829 image references and 606 distinct templates**
([`data/client_references.json`](data/client_references.json)). Almost every animal template
has `{animalSpecies}` and `{season}` holes. Each one therefore stands for one file per skin
(species) per season per theme, and the client can request thousands of files. Which species
and seasons existed was decided by the server's shop catalogue, which is not in the client.

## 2–4. Archives, downloads and decoding

Run of 2026-11-26 ([`data/report.json`](data/report.json),
[`data/archived_images.json`](data/archived_images.json)):

| Archive | Captures under mope.io (all subdomains) | Image captures | Result |
|---|---|---|---|
| Arquivo.pt | 1,010 | 271 | **82 distinct image URLs, 102 distinct versions downloaded** |
| Common Crawl (107 crawls, 2016–2026) | 567 | **0** | Common Crawl stored only pages and scripts of mope.io |
| vefsafn.is | 254 | 148 | discovery only (CAPTCHA); 14 URLs exist only here (listed in `report.json`) |

Integrity of the 102 downloaded versions:

- **102/102 decode completely.** PNG data is inflated and every chunk CRC and row filter is
  checked; JPEG, GIF and ICO structure is checked.
- **102/102 match the archive's payload digest** (SHA-1).
- No HTML error pages were saved as images.
- Formats: 96 PNG, 3 ICO, 2 GIF, 1 JPEG.
- 17 URLs have more than one archived version. For each URL, the version nearest
  2021-04-23 is the primary copy in `files/`.
- Capture years of the primary copies: 2017 (3), 2018 (2), 2019 (24), 2020 (32), 2021 (12),
  2022 (4), 2025 (5).
- Layouts: 71 files use the old root layout (`skins/…`, `img/…`); 11 use the current
  `assets/s/1/…` layout.

| Category | Archived files | Notes |
|---|---|---|
| animals | 43 | 24 matched to the decoded table: Mouse 5, Lion 5, Giraffe 4, Shark 2, and 1 each for Bear, Black Dragon, Cheetah, Dragon, Pufferfish, Rhino, Shrimp and Snow leopard. The other 19 are animals outside the 73-entry table (turkey, peacock, macaw, big cats, cassowary, chicken, flamingo, woodpecker, giant scorpion, snowman, AI bosses) |
| promo | 15 | `img/update/…`, `img/updates/…` announcement images |
| ui | 14 | logo, settings, sound, mode buttons, app badges, favicons |
| shop-ui | 6 | `shop/…` buttons and currency icons |
| cloudflare | 4 | `cdn-cgi/` challenge-page images (not game assets) |

### Coverage of what the client requests

| Category | Templates with any archived file | Exact client paths archived |
|---|---|---|
| animals | 1 / 294 | 0 / 38 |
| food | 0 / 24 | 0 / 42 |
| game-objects | 0 / 52 | 0 / 17 |
| abilities | 0 / 44 | 0 / 9 |
| world-and-effects | 0 / 35 | 0 / 22 |
| disasters | 0 / 9 | 0 / 6 |
| shop-ui | 4 / 14 | 5 / 13 |
| ui | 1 / 15 | 1 / 10 |

"Exact client paths" are references with no runtime holes, such as `img/coconut_e.png` or
`img/disasters/cloud1.png`. They are checked in every season folder and in the old root
layout ([`data/coverage.json`](data/coverage.json) lists each one).

**What this means.** The non-Wayback archives hold **82 mope.io images, not thousands**.
They mostly hold the homepage's own images (logo, buttons, promos) and a few skins that the
homepage or shop linked to directly. The in-game sprites (foods, game objects, abilities,
effects, and the species × season × theme skin sets) were loaded by the game client at
runtime from `assets/s/<season>/…`. Arquivo.pt and Common Crawl crawl pages, not the files a
running game fetches, so they captured almost none of them. The catalogue still gives the
exact path patterns for those images, so any further source can be checked against it.

## Output (`archive/images/`, git-ignored)

```
files/<host>/<path>                          primary version (nearest the target date), original bytes, read-only
versions/<host>/<path>/<ts>-<provider>-<digest><ext>   every distinct archived version
rejected/...                                 archived responses that are not images (soft 404 pages)
manifests/client_references.json             the decoded client paths (also in images/data/)
manifests/animals.json                       the client's animal table
manifests/captures.json                      every image capture each archive lists
manifests/images.json                        per URL: category, animal, client template + lines, versions with decode results
manifests/coverage.json                      per template: archived files; per exact client path: archived or missing
manifests/report.json                        the summary (also in images/data/report.json)
cache/                                       archive index responses (reused on later runs unless --refresh)
```

Re-running is cheap: archive indexes are cached, and versions already downloaded and still
on disk are not fetched again.

## Limits

- **Sources.** Only non-Wayback archives are used, by project rule. The images the client
  requests but no archive holds are listed in `manifests/coverage.json`, so another source can
  be checked against them.
- **Holes.** Templates with runtime holes (`{animalSpecies}`, `{season}`, `{skinThemeID}`) cannot
  be expanded into exact file names without the server's shop catalogue, which says which
  species, seasons and themes existed.
- **Animal table.** The decoded table is the client's main `aniName` switch: 73 animals, ids
  1–75. Animals added later (peacock, macaw, turkey, …) are defined elsewhere in the client.
  Their files are still categorised as animals by their `skins/` path, but are not matched to
  a table id.
