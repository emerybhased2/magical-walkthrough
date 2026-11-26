# Client version history

Generated 2026-11-26T20:59:19Z by `compare_builds.py` from the archived client builds in `working/client-js/` (Arquivo.pt captures, rebuilt and verified by `rebuild_client_js.py`), plus the live 2026 site's own data. Measured facts only; no client code runs. The full data is [`data/builds.json`](data/builds.json).

No archive used holds a 2021 client build. The two nearest are 2018-04-21 (3.0 years before the April 23, 2021 target) and 2025-09-05 (4.4 years after).

## Builds

| Build | Lines | Client → server | Server → client | Animals | Ability names | Object types | Food types | Classes | Regions | gameVersion |
|---|---|---|---|---|---|---|---|---|---|---|
| 2017-02-13 | 3,131 | 13 | 12 | 29 | 19 | 31 | 0 | — | — | — |
| 2018-04-21 | 4,355 | 10 | 11 | 61 | 0 | 62 | 21 | — | — | — |
| 2025-09-05 | 29,281 | 28 | 63 | 73 | 116 | 156 | 56 | 225 | 9 | 296 |
| 2025-12-10 | 29,322 | 28 | 63 | 73 | 116 | 156 | 56 | 225 | 9 | 296 |

Ability names are the literal names (computed ones are left out). Renames are *probable*: each pair is found by exactly one rule (skin name, same words, expanded abbreviation, one word of the old name, or a one-letter spelling fix) and shown with that rule.

Notes: the 2017 and 2018 builds predate the class registry (`setCustomClassForGameObjType`); their object types come from `oType` comparisons and switches only, and their server → client opcodes from `switch (reader.readUInt8())`. Region lists and `$config` only exist in the 2025 builds.

## 2017-02-13 → 2018-04-21

- Size: 3,131 → 4,355 lines.
- Client → server opcodes added: none; removed: 22, 26, 27.
- Server → client opcodes added: none; removed: 22.
- Animals added (32): Arctic Fox, Arctic Hare, Black Dragon, Blue Whale, Boa Constrictor, Chipmunk, Cobra, Donkey, Duck, Duckling, Eagle, Elephant, Giant Spider, Giraffe, Hedgehog, King Crab, Lemming, Mammoth, Muskox, Penguin, Polar Bear, Reindeer, Sabertooth Tiger, Seal, Snail, Snow leopard, Tiger, T-REX, Walrus, Wolf, Wolverine, The Yeti!.
- Animals removed (0): none.
- Animals renamed (probable, 0): none.
- Ability names: not comparable (no ability names found in 2018-04-21).
- Object types added (31): 1, 33, 34, 35, 36, 37, 38, 39, 40, 42, 44, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65.
- Object types removed (0): none.
- Regions: not comparable (region list only in the 2025 builds' $config).
- `$config`: not comparable ($config only exists in the 2025 builds).

## 2018-04-21 → 2025-09-05

- Size: 4,355 → 29,281 lines.
- Client → server opcodes added: 1, 103, 11, 113, 26, 27, 29, 30, 52, 56, 59, 60, 61, 62, 63, 64, 66, 67, 68, 71; removed: 2, 200.
- Server → client opcodes added: 100, 102, 103, 104, 105, 106, 107, 108, 109, 11, 111, 112, 113, 114, 12, 13, 16, 17, 255, 3, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 48, 5, 56, 58, 59, 6 … (+13); removed: 1.
- Animals added (13): Baby Ostrich, Dino Monster, Falcon, Frog, Ice Monster, Land Monster, Ostrich, Pelican, Phoenix, Pigeon, Sea Monster, Snowy Owl, Toucan.
- Animals removed (1): Duck.
- Animals renamed (probable, 1): Stringray → Stingray (skin name).
- Ability names: not comparable (no ability names found in 2018-04-21).
- Object types added (94): 66, 67, 68, 69, 70, 71, 72, 74, 75, 76, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 121, 122, 123, 125, 126, 127, 128, 129, 130 … (+34).
- Object types removed (0): none.
- Regions: not comparable (region list only in the 2025 builds' $config).
- `$config`: not comparable ($config only exists in the 2025 builds).

## 2025-09-05 → 2025-12-10

- Size: 29,281 → 29,322 lines.
- Client → server opcodes added: none; removed: none.
- Server → client opcodes added: none; removed: none.
- Animals added (0): none.
- Animals removed (0): none.
- Animals renamed (probable, 0): none.
- Ability names added (0): none.
- Ability names removed (0): none.
- Object types added (0): none.
- Object types removed (0): none.

## 2025-12-10 → live site (2026-11-26)

The live site is a rebuild on a different code base (bundled modules, WebP assets, new file layout), so it is compared by name only.

- Animals: 73 in 2025-12 vs 103 live; **65 shared** by name, 5 probable renames.
- Probable renames: Baby Ostrich → ostrich_baby (same words), Croc → crocodile (abbreviation), Rhino → rhinoceros (abbreviation), Hippo → hippopotamus (abbreviation), Boa Constrictor → boa (one word of the old name).
- Only in 2025-12 (3): Killer Whale, Duckling, Land Monster.
- Only live (33): angry_duck, armadillo, baby_duck, bee, bison, black_widow, camel, cassowary, chicken, desert_chipmunk, duck, fennec_fox, flamingo, gazelle, giant_scorpion, girabie, gobi_bear, hyena, kangaroo_rat, king_dragon, komodo_dragon, lava_monster, macaw, markhor, meerkat, momaffie, orca, peacock, pterodactyl, rattle_snake, vulture, warthog, woodpecker.
- Biomes: 2025-12 arctic, desert, forest, land, ocean, volcano; live arctic, desert, land, ocean, volcano (not in live: forest).
- Foods: 56 food object types in 2025-12; 25 food images on the live site (aloevera, banana, bone, cactus_pear, carrot, clam, cloud_berry, coconut, conch, date, duck_egg, flesh, kelp, mango, melon, mushroom, nut, orange, pear, pepper, raspberry, red_mushroom, sea_weed, starfish, watermelon).
- Pumpkins: live variants normal, jack, gold, emerald, ruby, diamond, onyx.
