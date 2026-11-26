#!/usr/bin/env python3
"""
import_live_skins.py -- add today's live mope.io shop skins to the 2025-12-10 client's local shop.

Sources (public, read-only):
  https://api.mope.io/store                                     the live shop (shelves + catalogue)
  https://s3.mope.io/mope-prod/items/<itemId>/<animal>.ui.webp  each skin's shop picture

How the 2025 client shows them: its shop never reads an image URL from an item. It builds
the picture path itself, `assets/s/<season>/skins/<animal skin path>.ui.png`, from the item's
animalType and season. Each live skin therefore gets
  * the 2025 animalType of its animal (looked up in the snapshot's own client.js), and
  * its own season number (SEASON_START + n, stable: ordered by item id),
and the local web gateway answers `/assets/s/<that season>/...png` with the downloaded picture.
The client itself is not changed.

Limits: the tile shows the animal's name (the client overwrites skinName), the season badge
shows the assigned number, and the skins cannot be worn in game (gameplay is Phase 5).
Skins for animals the 2025 client does not have are skipped and listed.

Output (git-ignored, stays on this machine):
  <snapshot>/live-skins/catalog.json    shop items, merged into /shop/get by the account server
  <snapshot>/live-skins/manifest.json   season -> picture, read by the web gateway
  <snapshot>/live-skins/images/<itemId>.webp
  <snapshot>/live-skins/report.json     what was imported or skipped, and why

Usage:
  python import_live_skins.py                       # fetch the shop, download missing pictures
  python import_live_skins.py --dry-run             # show the mapping, download nothing
  python import_live_skins.py --refresh-images      # download every picture again
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parent
STORE_URL = "https://api.mope.io/store"
IMAGE_URL = "https://s3.mope.io/mope-prod/items/{item}/{animal}.ui.webp"
USER_AGENT = "mope-local-archive/1.0 (personal archive; low rate)"
SEASON_START = 1000
CURRENCIES = {"MOPECOINS": 0, "COINS": 0, "GEMS": 1}
# live forAnimal (underscores removed) -> the 2025 client's name, where they differ
ALIASES = {"crocodile": "croc", "hippopotamus": "hippo", "rhinoceros": "rhino", "boa": "boaconstrictor",
           "orca": "killerwhale"}


# -- the 2025 client's animal table ---------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def animal_types(client_js: Path) -> dict[str, int]:
    """Animal name -> animalType, from the client's getAnimalName() and animalInfo() tables."""
    src = client_js.read_text(encoding="utf-8")
    info_at = src.index("var _0x373b4f = function")
    consts = collections.defaultdict(set)
    for key, value in re.findall(r"\b(_0x[0-9a-f]+|a_[A-Za-z]+) = (\d+)\b", src[:info_at]):
        consts[key].add(int(value))

    def resolve(token: str) -> int | None:
        values = {int(token)} if token.isdigit() else consts.get(token, set())
        return next(iter(values)) if len(values) == 1 else None

    names: dict[str, int] = {}
    start = src.index("_0x74a506.prototype.getAnimalName = function")
    block = src[start:src.index("_0x74a506.prototype.getSkinPath = function", start)]
    for token, name in re.findall(r"case (_0x[0-9a-f]+|\d+):\s*return '([^']+)'", block):
        if (value := resolve(token)) is not None:
            names.setdefault(_norm(name), value)
    block = src[info_at:src.index("return _0x276957", info_at)]
    pattern = (r"case (_0x[0-9a-f]+|a_[A-Za-z]+|\d+):\s*(?:case [^:]+:\s*)*_0x276957\.aniName = "
               r"('[^']*'|\"[^\"]*\")[^;]*?skinName = '([^']+)'")
    for token, display, skin in re.findall(pattern, block):
        if (value := resolve(token)) is not None:
            for key in (display.strip("'\""), skin.split("/")[-1]):
                names.setdefault(_norm(key), value)
    if len(names) < 50:
        raise SystemExit(f"error: only {len(names)} animals found in {client_js}; is it the rebuilt 2025 client?")
    return names


def find_client_js(snapshot: Path) -> Path:
    candidates = [snapshot / "working" / "client" / "mope.io" / "client.js",
                  *sorted((snapshot / "working" / "client-js").glob("*/client.js"))]
    for path in candidates:
        if path.is_file() and "_0x74a506.prototype.getAnimalName" in path.read_text(encoding="utf-8", errors="ignore"):
            return path
    raise SystemExit(f"error: no readable 2025 client.js under {snapshot}/working (run rebuild_client_js.py first, "
                     "see phase2/README.md)")


# -- live shop ---------------------------------------------------------------------------------

def live_skins(session: requests.Session) -> list[dict]:
    resp = session.get(STORE_URL, timeout=30)
    resp.raise_for_status()
    store = resp.json()
    products = [p for shelf in store.get("shelves", []) for p in shelf.get("products", [])]
    products += store.get("catalog", [])
    skins = {}
    for product in products:
        if product.get("kind") == "SIMPLE" and product.get("itemType") == "SKIN" and product.get("itemId"):
            skins.setdefault(product["itemId"], product)
    return [skins[key] for key in sorted(skins)]


def build(skins: list[dict], types: dict[str, int]) -> tuple[list[dict], list[dict]]:
    items, skipped = [], []
    for product in skins:
        animal = product.get("forAnimal") or ""
        key = _norm(animal)
        animal_type = types.get(ALIASES.get(key, key))
        currency = CURRENCIES.get(str(product.get("currency", "")).upper())
        if animal_type is None:
            skipped.append({"itemId": product["itemId"], "forAnimal": animal,
                            "reason": "animal not in the 2025 client"})
            continue
        if currency is None:
            skipped.append({"itemId": product["itemId"], "forAnimal": animal,
                            "reason": f"currency {product.get('currency')!r} cannot be bought locally"})
            continue
        price = int(product.get("basePrice") or 0)
        item = {"id": product["itemId"], "itemId": product["itemId"], "itemType": 0, "shopCategory": 0,
                "animalType": animal_type, "animalSpecies": 0, "animalSubSpecies": 0,
                "season": SEASON_START + len(items), "price": price, "currencyType": currency,
                "live": {"forAnimal": animal, "rarity": product.get("itemRarity"),
                         "basePrice": product.get("basePrice"), "currency": product.get("currency")}}
        sale = product.get("salePrice")
        if isinstance(sale, int) and 0 <= sale < price:
            item.update(saleType=1, salePrice=sale)
        items.append(item)
    return items, skipped


# -- pictures ------------------------------------------------------------------------------------

def download(session: requests.Session, item: dict, images: Path, refresh: bool) -> str | None:
    target = images / f"{item['itemId']}.webp"
    if target.is_file() and target.stat().st_size > 0 and not refresh:
        return None
    url = IMAGE_URL.format(item=item["itemId"], animal=item["live"]["forAnimal"])
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200 and resp.content[:4] == b"RIFF" and resp.content[8:12] == b"WEBP":
                tmp = target.with_suffix(".part")
                tmp.write_bytes(resp.content)
                tmp.replace(target)
                return None
            if resp.status_code in (403, 404):
                return f"HTTP {resp.status_code}"
            error = f"HTTP {resp.status_code}" if resp.status_code != 200 else "not a WebP image"
        except requests.RequestException as exc:
            error = exc.__class__.__name__
        time.sleep(1.5 * (attempt + 1))
    return error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import the live mope.io shop skins into the 2025 snapshot's shop")
    parser.add_argument("--snapshot", default="snapshots/2025-12-10")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--refresh-images", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    snapshot = (PROJECT / args.snapshot).resolve()
    out = snapshot / "live-skins"
    types = animal_types(find_client_js(snapshot))
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    skins = live_skins(session)
    items, skipped = build(skins, types)
    print(f"live shop: {len(skins)} skins; {len(items)} map to 2025 animals, {len(skipped)} skipped")
    for entry in skipped:
        print(f"  skip {entry['itemId']:<32} {entry['forAnimal']:<16} {entry['reason']}")
    if args.dry_run:
        return 0

    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        errors = list(pool.map(lambda i: download(session, i, images, args.refresh_images), items))
    missing = []
    for item, error in zip(items, errors):
        if error:
            missing.append({"itemId": item["itemId"], "forAnimal": item["live"]["forAnimal"],
                            "reason": f"picture: {error}"})
    kept = [i for i, e in zip(items, errors) if not e]
    # renumber seasons over the kept items so they stay contiguous
    for n, item in enumerate(kept):
        item["season"] = SEASON_START + n

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source = {"store": STORE_URL, "images": IMAGE_URL, "fetched_at": generated}
    (out / "catalog.json").write_text(json.dumps({"_comment": "Generated by import_live_skins.py; merged into "
                                                  "/shop/get by the account server.", "source": source,
                                                  "items": kept}, indent=1), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"_comment": "season -> picture, served by the web gateway "
                                                   "for /assets/s/<season>/...png", "source": source,
                                                   "seasons": {str(i["season"]): f"images/{i['itemId']}.webp"
                                                               for i in kept}}, indent=1), encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"source": source, "live_skins": len(skins), "imported": len(kept),
                                                 "skipped": skipped + missing}, indent=1), encoding="utf-8")
    for entry in missing:
        print(f"  skip {entry['itemId']:<32} {entry['forAnimal']:<16} {entry['reason']}")
    print(f"imported {len(kept)} skins into {out.relative_to(PROJECT)} (seasons {SEASON_START}-"
          f"{SEASON_START + max(len(kept) - 1, 0)}); restart the account server and web gateway to pick them up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
