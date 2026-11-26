# Phase 5 — Local gameplay servers (first playable milestone)

The archived 2025-12-10 client now **plays** on every local region server. Pressing Play
spawns a mouse. It follows the cursor, eats food, gains XP and evolves (Mouse → Rabbit →
Pig by default). Other players in the same region appear with their nicknames. The
client's own reader code drives everything on screen: the server sends exactly the byte
layouts those readers expect, so the sprites, upgrade texts, XP bar and minimap are the
client's.

**Verification: 8/8 checks** ([`data/verify_report.json`](data/verify_report.json)), of which 6
are protocol checks and 1 uses the real archived client in Chromium. There are also 17
offline tests (`tests/test_gameplay.py`).

```bash
python localstack.py up          # or: python localstack.py restart regions
# http://local.mope.io:8080/ -> type a nickname -> PLAY
python phase5_verify.py          # --region <id>, --no-browser
python -m unittest tests.test_gameplay -v
```

| File | Role |
|---|---|
| `services/game_protocol.py` | Encoders for S→C 17, 18, 6, 107 and 4, the C→S 5 parser, and decoders that mirror the client's readers (used by the tests and the verifier) |
| `services/game_world.py` | The authoritative per-region simulation: entities, movement, eating, XP, evolution, per-client visibility and update diffs |
| `services/region_server.py` | Spawns on Play, applies cursor input, runs the tick loop (`gameplay.tick_seconds`), announces evolutions |
| `config/local_stack.json` → `gameplay` | World size, zoom, food, evolution chain (all marked as approximations) |
| `phase5_verify.py` | The 8 checks below |

## Task 29 — Spawn and world messages (derived from the client)

Line numbers refer to the readable rebuild
`snapshots/2025-12-10/working/client-js/20251210042757_arquivo/client.js`. Integers are
big-endian. "x4" means the value is sent multiplied by 4 and divided again by the client.

### Spawn sequence (after C→S 113 Play)

| Order | Message | Layout | Client evidence |
|---|---|---|---|
| 1 | S→C **17** world size | UInt16 width, UInt16 height, UInt16 camX x4, UInt16 camY x4, UInt16 zoom x1000 | handler at L2956 |
| 2 | S→C **107** minimap | UInt8 season (1 or 2), then the `loadMiniMap` block: UInt16 ×6 sizes, UInt16 shape-group count, shapes | L2698–2775 (create L2708, load L2725) |
| 3 | S→C **18** your animal | UInt8 animalType, UInt8 species, UInt16 subSpecies, UInt8 changeKind, UInt8, **UInt32 my object id**, UInt32 XP for the next animal, UInt16 ×2 ability info, then 4 lists (UInt8 count + UInt8 entries): dangerous animals, edible animals, a third list, edible object types | dispatcher case 18, L3981 |
| 4 | S→C **6** alive | no payload: hides the start menu (`startMenuWrapper`, L5514) | case 6, L3892 |

The minimap is not optional. The HUD reads `miniMapCanvas` every frame, and without
message 107 the client throws `Cannot read properties of null` and stops drawing.
`changeKind` selects the client's own text: 1 = "UPGRADED to …" with the animal's tip
line, 0 = downgrade text, 2 = none (used for the first spawn).

### S→C 4 world update (`_0x7394b3`, L4542)

1. **Header** (`_0x51aed3`, L4423): UInt8 field count, then for each field a UInt8 id and its
   data. Field layouts were read from L4396–4500. The server sends:

   | Id | Data | Shown as |
   |---|---|---|
   | 1 | UInt16 zoom x1000 | camera zoom |
   | 3 | UInt16 x x4, UInt16 y x4 | camera position |
   | 11 | UInt32 XP | XP bar text (L5164) and the "+5 XP" popups |
   | 16 | UInt8 water percent | water bar; ≤ 25 shows "LOW WATER" (L5132) |

   Fields the client knows but this server does not send yet: 2, 7, 9, 10, 12–15 and 17–21.
2. **Alive-only tail**: a BitGroup of developer flags (`_0x2f5263`, L4515), a UInt8-counted
   tree list (`_0x33daed`, L28609) and a UInt8-counted id list. All are empty here.
3. **Newly visible objects** (UInt16 count; base reader L8276, animal reader L10572–10598):
   - UInt16 object type, plus a UInt16 sub-type for types {2, 14, 117, 128} (object factory
     L633–690);
   - UInt32 id, UInt32 radius x4, UInt16 x x4, UInt16 y x4, UInt8 biome;
   - non-animals only: UInt8 season and UInt16 ×3 (animal/species ids);
   - a BitGroup (spawnedBy, isRect, getsAngleUpdate, batchDraw), a UInt8 angle (degrees / 2)
     and two UInt8 spec types;
   - inner radius for types 5, 9 and 13 (L8267);
   - animals only: nickname, UInt16 species, UInt16 sub-species, 4 × UInt8 (premium skin,
     theme, season, VIP), a 52-flag BitGroup (53 in dev mode, L10594), UInt16 1v1 wins and
     UInt8 team id.
4. **Updates** (UInt16 count; L8316): UInt32 id, a flag BitGroup (first bit = hurt), then a
   UInt8 count of (UInt16 property, value) pairs, from `readUpdatedPropertyItem` (L8325):

   | Property | Value |
   |---|---|
   | 1 position | UInt16 x x4, UInt16 y x4 |
   | 2 radius | UInt16 x10 |
   | 3 angle | UInt16 degrees x182 |
   | 4 HP | UInt8 percent |
   | 66 biome | UInt8 |

5. **Removals** (UInt16 count; L8364): UInt32 id, UInt8 has-killer, and the UInt32 killer id
   if set. With a killer id, the client animates the object into the killer's mouth.

**BitGroup** (`_0x3fa214`, L1606): each byte carries 7 flags in bits 1–7, and bit 0 means another byte
follows. Bits the client reads past the end are false, so the encoder drops trailing false
flags.

**Strings** (S→C): UInt16 length followed by UTF-8, where the length includes one trailing
byte that the client drops.

### C→S 5 cursor (`_0x443ad0`, L29035)

The client sends the cursor's **world** position as Int16 x, Int16 y. The server moves the
player's animal toward it.

## Task 30 — The simulation

- **Tick**: every `gameplay.tick_seconds` (0.1 s) the world advances. Each client then gets
  its own world update. The client interpolates over about 0.175 s, so 10 updates per second
  look smooth.
- **Visibility**: each client sees objects inside its canvas (from its join packet) divided by
  the zoom, plus a 120-unit margin. An update lists objects that came into view, changes to
  objects in view (position, radius, angle), and objects that left view or were removed.
- **Food**: `food_count` objects of type `food_object_type` (3) spread at random. An animal
  eats food it touches, gaining `food_xp`; the food is removed with the eater as killer and
  respawns elsewhere. S→C 18 lists type 3 as edible so the client shows it as food.
- **Evolution**: when XP reaches the next threshold in `gameplay.evolution`, the old animal
  object is removed and a new one, with the new animal type, radius and speed, is created at
  the same place. S→C 18 (`changeKind` 1) then carries the new object id and the next
  threshold. At the end of the chain the next threshold is 0 ("0 xp Next Animal").
- **Water** is sent as 100 % because drinking and water sources are not simulated yet.

### What is evidence and what is approximation

| Item | Status |
|---|---|
| All message layouts above | **from the client** (reader code, line numbers given) |
| Animal ids 1 Mouse, 2 Rabbit, 3 Pig, 4 Fox, 5 Deer, 6 Mole … | **from the client** (animal table `_0x373b4f`, L804) |
| Animal classes 1–6 need no extra per-animal fields | **from the client**: no reader overrides for these types |
| Evolution order, XP thresholds (0/30/90), radii, speeds | **approximation**: the original server decided these and they are not in the client |
| Food object type 3, density, 5 XP per food | **approximation**, same reason |
| World size 3000×3000, zoom 1.4 | **approximation** (the client accepts any size from message 17) |

These values are configuration, not code (`config/local_stack.json` → `gameplay`), so better
evidence can replace them without code changes.

## Task 31 — Verification (`phase5_verify.py`)

| Check | What it proves |
|---|---|
| C1 | configuration validates |
| P1 | Play gives 17, 107, 18, 6 in that order, then a world update with the player's own animal (type 1, nickname) |
| P2 | C→S 5 moves the animal toward the cursor at the configured speed (±40 %) |
| P3 | food the player touches is removed with the player as killer; header XP rises; water is 100 |
| P4 | at the threshold: S→C 18 with upgrade text, new animal type and new object id, next threshold; old object removed |
| P5 | two players in the same region see each other with nicknames; a leaving player is removed for the other |
| P6 | every S→C frame (about 160) decodes with the decoders that mirror the client's readers, with no updates or removals for unknown objects |
| B1 | the **real client** spawns ("You spawned alive ingame!"), hides the menu, eats and evolves, while the verifier steers its cursor toward the food it decodes from the bridged frames. The only page errors are the missing audio files. Screenshot: `working/phase5/phase5_ingame.png` |

Phases 3 and 4 still pass in full (34/34 and 8/8); Phase 3 check B6 now expects the
"early gameplay" notice.

## Phase 5 expansion — full gameplay reconstruction

The first Phase 5 milestone made the archived client playable. The expansion aims at **exact**
gameplay. "Exact" has a hard limit, and the plan is built around it:

- **Exact, from the client.** Every message and object layout, every object and animal class, the
  animal and ability tables, the header fields, and every client-side rule (for example what the HUD
  shows, or when an animation plays). The client's own code defines these, so they can be matched
  byte for byte.
- **Not in the client.** The original server's numbers: speeds per animal, XP thresholds and
  rewards, damage and HP regeneration, spawn rates, food values, ability cooldowns. No archive used
  holds the server. These stay configuration, marked as approximations, and each one is listed in
  the ledger.

### Task 29A — Gameplay evidence ledger (done)

`extract_gameplay_evidence.py` reads the client's syntax tree (no client code runs) and writes
[`EVIDENCE.md`](EVIDENCE.md) + [`data/gameplay_evidence.json`](data/gameplay_evidence.json):

| Evidence | Found |
|---|---|
| Object classes registered with `setCustomClassForGameObjType` | **225**, over **88** object types |
| Animal classes (object type 2 + animal type) | **114**; all 73 animals in the table have one |
| Object types with their own fields on appear | 106 |
| Ability definitions (name, image) | **155 definitions, 118 distinct names**: 62 in the ability-type table, 91 in per-animal `getAbilityInfo` methods (82 animal types), 2 in the helper that adds the universal Dash |
| Object type ids used by the client (object catalogue: label, category, fields, minimap colour) | 156: 56 food, 23 terrain, 16 effect, 60 other, 1 animal |
| Packet field parsers (dispatchers, header if/else chain, property readers) | 7 |

For each registered class the ledger gives the **effective** readers the server must satisfy: the
fields read when the object appears (`readCustomData_onNewlyVisible`), the property ids its updates
accept (`readUpdatedProperty`), and its update flags. Superclass calls are resolved through the
obfuscator's alias variables (`var _0x244a35 = Class, _0x3c3358 = Parent`), which are resolved by
source position.

The ledger reproduces every layout Phase 5 had confirmed by hand:

- the animal appear fields;
- the pumpkin's `isRolling` + `pumpkinType`;
- the 20 world-update header fields (1–21, except 18).

`tests/test_gameplay_evidence.py` covers the alias resolution, the registry, the effective readers,
the tables and the field chains.

### Task 30A — Protocol-exact object and animal coverage (not implemented)

- **Goal.** Generate the object encoders from the ledger instead of writing them by hand, so every
  object type and every animal type can be sent with exactly the fields its class reads.
- **Tests.** A conformance test replays each class's read sequence against the encoder's bytes, for
  all 225 classes.
- **World.** The world can spawn any object and evolve into any of the 73 animals.
- **Done when:**
  - every registered class encodes and decodes with no bytes left over;
  - the real client renders a sample of every object type and every animal without page errors.

### Task 30B — Survival loop (not implemented)

- **Predation.** Uses the dangerous and edible lists of S→C 18: who may eat whom.
- **HP.** HP updates (property 4) and the hurt flag.
- **Death and respawn.** Death sends S→C 14 with the client's own death reasons; then respawn from
  the menu.
- **Water.** Drinking, with the header 16 water level and its low-water warning.
- **Biomes.** Biome ids on objects, plus header-field biome rules.
- **Feedback.** Leaderboard and player count.
- **Exactness.** Rules follow the client wherever it shows them (texts, thresholds it checks
  itself, such as LOW WATER at 25 % or below); the server's own numbers are configuration.
- **Done when:** a verifier plays a full life through the real client (eat, grow, get eaten, see
  the death screen, respawn), and every message involved decodes with the client's layouts.

## Not reconstructed yet (open work)

- Animals past the default chain, and the classes with extra reader fields (types 7 and up
  override `readCustomData_*`, so they need those fields before they can be sent).
- Predators, combat, HP, death and the death screen; respawn after death.
- Biomes (land, ocean, arctic, volcano), the biome field of objects, water sources and
  drinking, hills, hiding holes, mud, trees (the alive-only tree list).
- Abilities (the W and S ability fields of message 18 are 0).
- The leaderboard, the player count ("0 players online"), chat, emotes and teams.
- Header fields 2, 7, 9, 10, 12–15 and 17–21 (meanings partly identified in L4396–4500).
- Real minimap shapes: the minimap is sent with no shape groups.
