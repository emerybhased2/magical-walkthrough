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

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run offline. They cover the date policy, path containment, the Common Crawl
`cluster.idx` binary search, WARC parsing, revisit resolution, redirect and Memento
refusal, reference extraction and region/endpoint detection.
