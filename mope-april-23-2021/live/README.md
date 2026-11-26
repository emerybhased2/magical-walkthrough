# Live mope.io image snapshot (`fetch_live_assets.rb`)

A Ruby script, using only the standard library, that saves the images of the **live
mope.io site** as it is today.

**This is not the archived client's art set.** The live site is a 2026 rebuild. It is
another game build with its own file layout and WebP files. The archived 2025 client that
the local stack runs requests `assets/s/<season>/skins/…png`, `img/…` and `shop/…`. On the
live site, all of those paths now return the site's HTML app shell. The live set is kept
separate as a reference: for comparison, and for a possible later step that maps names
between the two builds. The images stay local in `archive/live/` (git-ignored). This folder
holds only the documentation and summaries ([`data/`](data/)).

```bash
ruby fetch_live_assets.rb                      # snapshot, plan, fetch, report (resumes an unfinished run)
ruby fetch_live_assets.rb --plan-only          # bundles + plan only, no image requests
ruby fetch_live_assets.rb --workers 20 --rate 0  # fast: 20 connections, no per-second cap
ruby tests/test_live_assets.rb                 # offline tests (minitest)
```

Options: `--site`, `--output` (default `archive/live`), `--snapshot` (default: the UTC date),
`--rate` (requests per second over all workers, default 3; `0` = no cap), `--workers` (concurrent
connections, default 2, max 64), `--budget`
(maximum requests per run, default 12,000), `--refresh`, `--plan-only`.

## How the paths are found

The site has no file listing, and it answers **every** unknown path with its HTML page and
HTTP 200, so a guessed path never returns a 404. The script therefore takes paths from the
site's own JavaScript, read as text and **never executed**, and accepts a download only if
its bytes are a real image.

1. **Site snapshot.** Download `index.html`, every bundle and stylesheet it loads, and the
   chunks those import (36 files, 2.9 MB). They are saved in `archive/live/<date>/site/`.
2. **Direct paths.**
   - literal `assets/…` paths in the JavaScript and the CSS `url(…)` values;
   - literal names given to the texture loader, which turns `name` into `./assets/name.webp`.
     Textures the bundle draws itself at start-up (`circle_*`, `triangle_*`, `square`) are
     skipped because they are not files;
   - the food list, which is keyed by biome (`land:[`watermelon/edible`, …]`) and gives
     `assets/foods/<name>.webp`;
   - the bestiary scenes (`assets/UI/bestiary/scene-<biome>.webp`).
3. **Animals.** The bundle's animal table (103 animals) gives each animal's biome and its
   subspecies enum (62 compiled TypeScript enums, 151 animal/subspecies folders). The
   bundle builds an animal's folder like this:

       animals/<"volcano" for lava animals, else biome>/<animal>/<subspecies>/

   where `<subspecies>` is the enum name lower-cased, and empty for `DEFAULT`. In-game parts
   are that folder plus a literal part name (`this.<folder>+`tail``). The script collects
   all 139 part names and splits them into:
   - parts named after one animal (`kangaroorat1` belongs to `kangaroo_rat`), tried only in
     that animal's folder;
   - shared parts (`ability`, `tail`, `head`, `legs`, …), tried in every animal's folder.

   It also tries the UI icon `<animal>.ui.webp` in every folder. Each default folder is tried
   with all its candidate parts. Each subspecies folder is tried only with the parts its
   default folder turned out to have, plus the UI icon.
4. **Checking.** Every candidate is fetched politely:
   - at most 3 requests per second in total, from 2 workers;
   - back-off on 429 and 5xx, with `Retry-After` honoured;
   - the run stops if the site refuses 10 requests in a row;
   - a request budget caps each run;
   - a generic User-Agent, with no personal details.

   A file counts only if its bytes are WebP, PNG, JPEG, GIF or SVG and its header decodes
   (WebP: RIFF size and VP8/VP8L/VP8X header). Found files are saved read-only.
5. **Resume.** Each result is appended to `manifests/results.jsonl`, so an interrupted run
   continues where it stopped.

Not fetched: shop items on `s3.mope.io/mope-prod/items/<id>/…`. The bucket refuses listing
(HTTP 403), and item ids are not in the bundle (they come from the live account API).

## Results (snapshot 2026-11-26)

[`data/report.json`](data/report.json), [`data/files.json`](data/files.json) (path, rule,
format, size, sha256 per file), [`data/animals.json`](data/animals.json).

| | |
|---|---|
| Site code | 36 bundles and stylesheets (2.9 MB) |
| Plan | 86 direct paths; 103 animals in 151 animal/subspecies folders; 139 part names; 25 foods |
| Paths checked | 4,479 |
| **Images found** | **622 (9.9 MB)**: 604 WebP, 17 SVG, 1 PNG. All headers decode; none is the HTML fallback |
| Animals with files | **103 / 103**; 44 subspecies folders hold their own files |
| Errors | none: no 429, 403, 5xx or network errors |

| Category | Files | | Found by rule | Files |
|---|---|---|---|---|
| animals | 549 | | animal part, default folder | 377 |
| ui | 39 | | animal part, subspecies folder | 170 |
| foods | 25 | | literal path in JS/CSS | 35 |
| abilities | 3 | | food list | 25 |
| terrain | 3 | | texture-loader name | 10 |
| other (logos, promo) | 3 | | bestiary scene | 5 |

The animals with the most files are Eagle (36), Lion (34), Falcon (27), Macaw (18) and
Markhor (16). The fewest have only a body and a UI icon (2 files each, for example Fox and
T-Rex).

Only 11 direct paths were missed. 10 are achievement medal names guessed from the rarity
names, which turned out not to match the real file names. The other is
`assets/placeholder.webp`, which the bundle names but the site does not serve.

Speed:
- At the default polite rate (3 requests/s, 2 workers): about 2.5 checks per second.
- With `--workers 20 --rate 0` (20 kept-alive connections, no per-second cap): about 67
  checks per second. The last 2,212 checks took 39 seconds, with no throttling.
- `--workers` accepts up to 64. The refusal guard still stops a run if the site starts
  returning 429/403.

Shop items on `s3.mope.io` are not included (see above). The live account API was not
called.

## Output (`archive/live/<snapshot-date>/`, git-ignored)

```
site/                    index.html + every bundle/stylesheet (text, never executed)
files/assets/...         every image found, original bytes, read-only
manifests/site.json      bundle list with sha256
manifests/animals.json   the bundle's animal table (name, biome folder, subspecies, lava)
manifests/plan.json      direct paths with the rule that produced them, part names, foods, folders
manifests/results.jsonl  one line per checked path (status, format, size, sha256, rule)
manifests/files.json     the files found
manifests/report.json    summary (also in live/data/report.json)
```
