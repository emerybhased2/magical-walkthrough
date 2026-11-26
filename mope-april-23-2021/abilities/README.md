# Ability frames (`fetch_ability_frames.rb`)

Downloads every ability image of the **live** mope.io game (the 2026 build): the ability frames of
every animal and subspecies from `mope.io`, those of every shop skin from `s3.mope.io`, and the
ability effect sprites both hosts serve.

```bash
ruby fetch_ability_frames.rb                  # everything (first run ~4,000 requests, ~10 min)
ruby fetch_ability_frames.rb --plan-only      # candidate counts only
ruby fetch_ability_frames.rb --only skin      # one group: animal, skin, effect
ruby fetch_ability_frames.rb --refresh        # download again and retry known misses
ruby tests/test_ability_frames.rb
```

Neither host can be listed, and the site answers unknown paths with its HTML page, so every path comes
from the live bundle (read as text, never executed) or the live shop (`api.mope.io/store`). Only real
image bytes count. The rules:

| Group | Paths | Source in the bundle |
|---|---|---|
| animal | `assets/animals/<biome>/<animal>/<subspecies>/ability.webp`, `ability2.webp`, `ability1`-`6`, `ability_crabSmash0`, `ability_pelican`, `ability_tigerJump` | HUD template `…/${animalSubspeciesName}ability.webp`, part names (`SiteParser`) |
| skin | `<s3>/items/<itemId>/ability.webp`, `ability2.webp`, plus the parts its animal has | HUD template `items/${equippedItemId}/ability.webp`; item ids from the live shop |
| effect | `assets/abilities/...` and `<s3>/items/abilities/...` literals; `abilities/<ability>/<Er>`; s3 variants per enum (`stomp/<variant>`, `claw/<variant>`, `bite/<variant>/ability_trexBite`, ability-object subspecies); `fireball/<itemId>/{dynamic,static}/N` | ability-object enum + each class's `Er`/`subspeciesEnum`; skin enums such as `GS[e.Nb[itemId]]` |
| frames | a found file ending in a number pulls in the next number until one is missing | (animation frames are never guessed) |

Output (git-ignored): `archive/live/abilities/<date>/` with the files under their host and path,
`manifest.json` (owner, rule, url, size, sha256 per file) and `misses.txt` (so re-runs skip known
misses). The summary is in [`data/report.json`](data/report.json).

## Result (2026-11-26)

**425 images (5.8 MB)** from 4,097 checked paths:

- **animal: 73 files**, 41 animals in 64 animal/subspecies folders. The other animals have no image at
  any of the bundle's ability paths.
- **skin: 158 files** for 140 of the 356 shop skins. The other skins have no ability image of their own
  on s3.
- **effect: 194 files**: fireball 145 (dynamic and static frames for the dragon skins), fire tornado 10,
  bite 8 (T-rex skins), ice crystal 8, claw 5, constrict 5, spider web 3, stomp 2, laugh 2, and camel,
  giant whirlpool, quill, rattle, sinkhole and wave 1 each.

These are live-build WebP images, not the archived 2025 client's art: that client does not load them
(see `live/README.md`).
