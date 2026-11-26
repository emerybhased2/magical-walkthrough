# Gameplay evidence ledger (Phase 5, task 29A)

Generated 2026-11-26T15:49:11Z by `extract_gameplay_evidence.py` from `snapshots/2025-12-10/working/client-js/20251210042757_arquivo/client.js` (syntax tree only; no client code runs). The JSON with every read sequence is [`data/gameplay_evidence.json`](data/gameplay_evidence.json).

## What the client fixes exactly

| Evidence | Count | Status in the local server |
|---|---|---|
| Object classes registered (`setCustomClassForGameObjType`) | 225 classes, 88 object types | 3 object types implemented (2, 3, 131) |
| Animal classes (object type 2 + animal type) | 114 | animal types 1, 2, 3 implemented |
| Animal table (name, skin, colour) | 73 | names used by the client itself |
| Ability definitions (name, image) | 155 definitions, **118 distinct names** | none implemented |
| Packet field switches (e.g. world-update header) | 7 | header fields 1, 3, 11, 16 sent |
| Client -> server messages | 28 | see phase6/COMPATIBILITY.md |

Exact means the byte layout and meaning come from the client's own reader code. **Not in the client (server-side, unknown):** movement speeds per animal, XP thresholds and rewards, damage, HP regeneration, spawn rates, food values, ability cooldowns and strengths. These stay configuration values marked as approximations.

## Animals: table, class and custom reader size

| Id | Name | Skin | Class | Custom reads on appear | Implemented |
|---|---|---|---|---|---|
| 1 | Mouse | `mouse` | `_0x446242` | 9 | yes |
| 2 | Rabbit | `rabbit` | `_0x1dd509` | 9 | yes |
| 3 | Pig | `pig` | `_0x136443` | 9 | yes |
| 4 | Fox | `fox` | `_0x30dce1` | 9 |  |
| 5 | Deer | `deer` | `_0x316478` | 9 |  |
| 6 | Mole | `mole` | `_0x380cee` | 9 |  |
| 7 | Zebra | `zebra` | `_0x41f91e` | 9 |  |
| 8 | Lion | `lion` | `_0x5aee2a` | 9 |  |
| 9 | CHEETAH | `bigcat/cheetah` | `_0x4ff4b7` | 9 |  |
| 10 | Bear | `bear` | `_0x174e28` | 9 |  |
| 11 | Croc | `croc` | `_0x5dd3c2` | 10 |  |
| 12 | Rhino | `rhino` | `_0x301198` | 9 |  |
| 13 | Hippo | `hippo` | `_0x65dbc4` | 9 |  |
| 14 | Dragon | `dragon` | `_0x4047ee` | 9 |  |
| 15 | Shrimp | `shrimp` | `_0x57d88f` | 9 |  |
| 16 | Trout | `trout` | `_0x48159f` | 9 |  |
| 17 | Crab | `crab` | `_0x302a00` | 9 |  |
| 18 | Squid | `squid` | `_0x26544c` | 9 |  |
| 19 | Shark | `shark` | `_0x52ef2e` | 9 |  |
| 20 | Stingray | `stingray` | `_0xdb49da` | 9 |  |
| 21 | Turtle | `turtle` | `_0x5430c0` | 9 |  |
| 22 | Sea-horse | `seahorse` | `_0x21c898` | 9 |  |
| 23 | Jellyfish | `jellyfish` | `_0x472004` | 9 |  |
| 24 | The Kraken | `kraken` | `_0x419e58` | 9 |  |
| 25 | Pufferfish | `pufferfish` | `_0x2753f0` | 9 |  |
| 26 | Killer Whale | `killerwhale` | `_0x207ca1` | 9 |  |
| 27 | Swordfish | `swordfish` | `_0xb7a117` | 9 |  |
| 28 | Gorilla | `gorilla` | `_0xaa8dc6` | 9 |  |
| 29 | Octopus | `octopus` | `_0x266c88` | 12 |  |
| 30 | Wolf | `arctic/wolf` | `_0x69be3f` | 9 |  |
| 31 | Arctic Hare | `arctic/arctichare` | `_0x54d09b` | 9 |  |
| 32 | The Yeti! | `arctic/yeti` | `_0x3ce401` | 10 |  |
| 33 | Chipmunk | `arctic/chipmunk` | `_0x1af7a0` | 9 |  |
| 34 | Muskox | `arctic/muskox` | `_0x60638d` | 9 |  |
| 35 | Penguin | `arctic/penguin` | `_0x1194dd` | 9 |  |
| 36 | Polar Bear | `arctic/bear/bear` | `_0x333baa` | 9 |  |
| 37 | Seal | `arctic/seal` | `_0x376ff9` | 9 |  |
| 38 | Snow leopard | `arctic/snowleopard` | `_0x20d0f5` | 9 |  |
| 39 | Walrus | `arctic/walrus` | `_0x533e81` | 9 |  |
| 40 | Reindeer | `arctic/reindeer` | `_0x237809` | 9 |  |
| 41 | Arctic Fox | `arctic/arcticfox` | `_0xccf2d3` | 9 |  |
| 42 | Wolverine | `arctic/wolverine` | `_0x287ec2` | 9 |  |
| 43 | Mammoth | `arctic/mammoth` | `_0x180a62` | 9 |  |
| 44 | Donkey | `donkey` | `_0xffe93c` | 9 |  |
| 45 | Snail | `snail` | `_0x30563c` | 9 |  |
| 46 | Black Dragon | `blackdragon` | `_0x284ccb` | 9 |  |
| 47 | Sabertooth Tiger | `sabertoothtiger` | `_0x3514b6` | 9 |  |
| 48 | Elephant | `elephant` | `_0xc71e73` | 9 |  |
| 49 | Blue Whale | `bluewhale` | `_0x3fc30b` | 9 |  |
| 50 | Cobra | `cobra` | `_0x3fe2d6` | 9 |  |
| 51 | Boa Constrictor | `boaConstrictor` | `_0x2cc8ca` | 9 |  |
| 52 | Giant Spider | `giantSpider` | `_0x329369` | 9 |  |
| 53 | T-REX | `trex` | `_0x4e32da` | 9 |  |
| 54 | Tiger | `tiger` | `_0x5f3139` | 9 |  |
| 55 | Giraffe | `giraffe` | `_0xac636e` | 10 |  |
| 56 | Eagle | `eagle` | `_0x1e7f32` | 9 |  |
| 57 | Hedgehog | `hedgehog` | `_0x376b8d` | 9 |  |
| 59 | Duckling | `duck/duckling` | `_0x561c96` | 9 |  |
| 60 | Lemming | `arctic/lemming` | `_0x363756` | 9 |  |
| 61 | King Crab | `kingcrab` | `_0x5430e0` | 9 |  |
| 62 | Frog | `frog/frog` | `_0x62a818` | 9 |  |
| 63 | Ostrich | `ostrich/ostrich` | `_0x467c39` | 9 |  |
| 64 | Pelican | `pelican/pelican` | `_0x53b4f8` | 9 |  |
| 65 | Falcon | `falcon/falcon` | `_0x412ede` | 9 |  |
| 66 | Snowy Owl | `snowyowl/snowyowl` | `_0x105fc6` | 9 |  |
| 68 | Phoenix | `phoenix/phoenix` | `_0x1adb70` | 9 |  |
| 69 | Baby Ostrich | `ostrich/baby-ostrich` | `_0x48bd6b` | 10 |  |
| 70 | Sea Monster | `seamonster` | `_0x49c246` | 9 |  |
| 71 | Land Monster | `landmonster` | `_0x2221a7` | 9 |  |
| 72 | Ice Monster | `icemonster` | `_0x5a8a01` | 9 |  |
| 73 | Dino Monster | `dinomonster` | `_0x25305a` | 9 |  |
| 74 | Pigeon | `pigeon/pigeon` | `_0x59e21b` | 9 |  |
| 75 | Toucan | `toucan/toucan` | `_0x5bc7be` | 9 |  |

## Object catalogue (every object type id the client uses)

156 object type ids: animal 1, effect 16, food 56, object 60, terrain 23. The label comes from the asset names the client draws for the type; *food* = it has an edible-outline (`_e`) variant; *terrain* = shown on the minimap or drawn with a biome skin; *effect* = drawn from an animal's or a disaster's assets (not placed in the world on its own).

| Type | Category | Label (client assets) | Class | Extra fields on appear | Minimap colour |
|---|---|---|---|---|---|
| 1 | terrain |  | base | - | #1aad41 |
| 2 | animal |  | `_0x30f58c` | - |  |
| 3 | food |  | base | - | #1aad41 |
| 4 | terrain |  | base | - | #4E66E4 |
| 5 | terrain |  | base | - |  |
| 6 | food |  | base | - |  |
| 7 | terrain |  | base | - | #907A33 |
| 8 | food |  | base | - | #8C9688 |
| 9 | terrain |  | base | - |  |
| 10 | terrain |  | `_0x1a129c` | UInt8+UInt8+UInt16+UInt16?+UInt16? | _0x209a1f |
| 11 | terrain |  | base | - | #b0a13b |
| 12 | terrain |  | base | - | _0x209a1f |
| 13 | object |  | `_0x48724b` | UInt16? |  |
| 14 | effect | ability_claw, ability_pounce1, ability_pounce2 | `_0x43af39` | UInt8+UInt8+UInt8+UInt8 |  |
| 15 | terrain |  | base | - | #09992F |
| 16 | terrain |  | base | - | white |
| 17 | terrain |  | base | - | #7BB7BB |
| 18 | object | fire, fire2 | `_0x96739a` | UInt16?+UInt16+UInt16 |  |
| 19 | object | snowball | base | - |  |
| 20 | object |  | base | - |  |
| 21 | food |  | base | - |  |
| 22 | food |  | base | - |  |
| 23 | food | lillypad | base | - |  |
| 24 | food |  | base | - |  |
| 25 | food |  | base | - |  |
| 26 | object |  | base | - |  |
| 27 | food |  | base | - | #F35F53 |
| 28 | object |  | base | - |  |
| 29 | food | banana | base | - |  |
| 30 | food | coconut | base | - |  |
| 31 | food | rasp | base | - |  |
| 32 | food | pear | base | - |  |
| 33 | terrain |  | base | - | #bfaf43 |
| 34 | object |  | base | - |  |
| 35 | food | seaweed | base | - |  |
| 36 | food | starfish | base | - |  |
| 37 | food | kelp | base | - |  |
| 38 | food | clam | base | - |  |
| 39 | food | conch | base | - |  |
| 40 | terrain | riverCurrent | `_0x473063` | UInt16? | _0x209a1f |
| 42 | terrain | arcticano | `_0x378d83` | UInt16?+UInt8+UInt8+UInt8+UInt16 | #604729 |
| 44 | terrain |  | base | - | #ff6000 |
| 46 | food | healingStone | base | - |  |
| 47 | terrain |  | base | - | #604729 |
| 48 | food | arcticNut | base | - |  |
| 49 | food | carrot | base | - |  |
| 50 | food | watermelon | base | - |  |
| 51 | food | watermelonSlice | base | - |  |
| 52 | food | meat | base | - |  |
| 53 | food | meat | base | - |  |
| 54 | food | meat | base | - |  |
| 55 | object |  | base | - |  |
| 56 | food |  | base | - |  |
| 57 | object | web | `_0x193dec` | UInt8+UInt8 |  |
| 58 | object |  | base | - |  |
| 59 | food |  | base | - |  |
| 60 | food | cloudberry | base | - |  |
| 61 | object |  | base | - |  |
| 62 | object |  | base | - |  |
| 63 | food |  | base | - |  |
| 64 | effect | sleigh | `_0x4be5fa` | UInt16? |  |
| 65 | object | quill | base | - |  |
| 66 | food | ostrich | `_0x4f84a8` | UInt16?+UInt32+UInt8 |  |
| 67 | object |  | base | - |  |
| 68 | food | beehive | base | - |  |
| 69 | food | honeycomb | base | - |  |
| 70 | object | gameobj/70 | `_0x36268a` | UInt16?+UInt16+UInt16 |  |
| 71 | effect | phoenix | base | - |  |
| 72 | object |  | `_0x2bcd55` | UInt16? |  |
| 74 | object | gameobj/74 | `_0x4920b8` | UInt8+UInt16+UInt8+String+UInt8+UInt16? |  |
| 75 | food | gameobj/75 | `_0x262393` | UInt8 |  |
| 76 | object |  | `_0x5b3c48` | UInt32 |  |
| 78 | object | gameobj/78 | `_0x54eee1` | UInt16?+UInt8 |  |
| 79 | terrain |  | base | - | #c8b745 |
| 80 | effect | icemonster | `_0x4d6017` | UInt16?+UInt8 |  |
| 81 | effect | camel | `_0x523fd5` | UInt16? |  |
| 82 | food | gameobj/82 | `_0x469375` | UInt16? |  |
| 83 | object |  | `_0x6e4cfd` | UInt16? |  |
| 84 | object | gameobj/84 | `_0x2b4455` | UInt16? |  |
| 85 | object |  | `_0x1bd7c2` | UInt16? |  |
| 86 | object |  | `_0x316e54` | UInt16? |  |
| 87 | object |  | `_0x8170ef` | UInt16? |  |
| 88 | object |  | `_0xd39084` | UInt16?+BitGroup+UInt16+UInt16 |  |
| 89 | food | gameobj/89 | `_0x30c90d` | UInt16? |  |
| 90 | food | gameobj/90 | `_0x482208` | UInt16? |  |
| 91 | food | gameobj/91 | `_0x10ad4a` | UInt16? |  |
| 92 | food | gameobj/92 | `_0x3f37e5` | UInt16? |  |
| 93 | food | gameobj/93 | `_0x38402e` | UInt16? |  |
| 95 | food | gameobj/95 | `_0x395126` | UInt16? |  |
| 96 | food | gameobj/96 | `_0x560027` | UInt16? |  |
| 97 | food | gameobj/97 | `_0x5476e3` | UInt16? |  |
| 98 | object | anthill | base | - |  |
| 99 | terrain |  | `_0x3865d9` | UInt16? | #b0a13b |
| 100 | terrain |  | `_0x5195e8` | UInt16?+UInt8+UInt16? | #007fa9 |
| 101 | terrain | gameobj/101 | `_0x4d3d89` | UInt16+UInt8+UInt8 | #00b01a |
| 102 | object |  | base | - |  |
| 103 | effect | disasters | `_0x4218b5` | UInt8 |  |
| 104 | object |  | base | - |  |
| 105 | effect | cloud, cloud1 | `_0x5983fa` | UInt8 |  |
| 106 | object | snowball | `_0x4fedbe` | - |  |
| 107 | object |  | base | - |  |
| 108 | object |  | base | - |  |
| 109 | object |  | base | - |  |
| 110 | effect | cloud, cloud1 | `_0x1c7dba` | UInt16? |  |
| 111 | effect | raindrop | `_0x2844c8` | UInt16? |  |
| 112 | object | gameobj/112 | `_0xd9a02b` | UInt16? |  |
| 113 | object |  | base | - |  |
| 114 | object |  | `_0x58f1c4` | UInt16?+UInt16 |  |
| 115 | object |  | `_0x3d2bb6` | UInt16?+UInt16 |  |
| 116 | object |  | base | - |  |
| 117 | effect | segment_ | `_0x34b56d` | - |  |
| 118 | terrain |  | `_0xb57184` | UInt16? | #07631f |
| 119 | object |  | `_0x3ebe4d` | UInt16? |  |
| 121 | effect | bigfoot | `_0x129406` | UInt16? |  |
| 122 | object |  | base | - |  |
| 123 | effect | virus | `_0x54ce24` | UInt16? |  |
| 125 | food | viralpandemic | `_0x38f253` | UInt8 |  |
| 126 | object |  | `_0x2b23c1` | UInt16? |  |
| 127 | food | camera | `_0x28e049` | UInt16? |  |
| 128 | object | leg_zebra, pad, rug_zebra | `_0x1ffbc1` | UInt16? |  |
| 129 | object |  | `_0x14e52a` | UInt32+String |  |
| 130 | object |  | `_0x3c2aec` | String+String+UInt8+BitGroup+UInt8? |  |
| 131 | object | pumpkin | `_0x20f7aa` | UInt8+UInt8 |  |
| 132 | food | coin | `_0x534412` | UInt16? |  |
| 133 | effect | skins | `_0x480d6f` | UInt16? |  |
| 134 | food | turkey | `_0x2eefba` | UInt16? |  |
| 135 | food | gameobj/135 | `_0x149ca1` | UInt16? |  |
| 136 | food | gameobj/136 | `_0x3354c3` | UInt16? |  |
| 137 | food | gameobj/137 | `_0x411b29` | UInt16? |  |
| 138 | terrain | gameobj/138 | `_0x32fdd9` | UInt16? | brown |
| 139 | object |  | `_0x566472` | UInt16? |  |
| 140 | terrain | gameobj/140 | `_0x2dcb47` | UInt16? | brown |
| 141 | object |  | `_0xd5c49d` | UInt16? |  |
| 142 | food | gameobj/142 | `_0x3fc490` | UInt8 |  |
| 143 | food | xmasgifts | `_0x3b0492` | UInt8+UInt8 |  |
| 144 | object | floaters | `_0x2067a8` | UInt8 |  |
| 145 | object | beachball | `_0x5e5982` | UInt8+UInt8+UInt8 |  |
| 146 | object | umbrella | `_0x31652d` | UInt8+UInt8+UInt8 |  |
| 147 | object | gameobj/147 | `_0x427869` | UInt8+String+UInt16+BitGroup |  |
| 148 | object | wave | `_0x221fb6` | UInt8 |  |
| 149 | object |  | `_0x1bc9ab` | UInt8 |  |
| 150 | object |  | base | - |  |
| 151 | object | gameobj/151 | `_0x16c7ec` | UInt16? |  |
| 152 | food | outline, upgradeshell | `_0x23f0f` | UInt16?+UInt32+String? |  |
| 153 | object | shellpart | `_0x43501b` | UInt16? |  |
| 154 | effect | gameobj/154 | `_0xc8112f` | UInt16? |  |
| 155 | object | gameobj/155 | `_0x57b98e` | UInt16? |  |
| 157 | object | gameobj/157 | `_0x2acd17` | UInt16? |  |
| 158 | food | gameobj/158 | `_0x2be80e` | UInt16?+UInt16+UInt16 |  |
| 159 | food | gameobj/159 | `_0x505f66` | UInt16?+UInt16+UInt16 |  |
| 160 | food | gameobj/160 | `_0x3d60bc` | UInt8 |  |
| 161 | object |  | `_0x23fab6` | UInt8 |  |
| 162 | object |  | `_0x3e84b5` | UInt16? |  |
| 163 | effect | birdmonster | `_0x2052c4` | UInt16? |  |
| 164 | effect | birdmonster | `_0x4461e5` | UInt16? |  |
| 165 | object | gameobj/165 | `_0x1d6fee` | UInt8+UInt8+UInt8 |  |

## Object types: class and exact custom fields

Fields read after the common object fields when an object becomes visible (the effective `readCustomData_onNewlyVisible`, superclass calls resolved), and the property ids its `readUpdatedProperty` accepts.

| Type | Subtype | Class | Custom fields on appear | Update properties | Implemented |
|---|---|---|---|---|---|
| 10 |  | `_0x1a129c` | UInt8, UInt8, UInt16, UInt16, UInt16 | 15, 16, 118 |  |
| 13 |  | `_0x48724b` | UInt16 |  |  |
| 14 |  | `_0x43af39` | UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 31 | `_0x1b0fc2` | UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 59 | `_0x14f5eb` | UInt8, UInt8, UInt8, UInt8 | 27, 101, 102, 119 |  |
| 14 | 62 | `_0x3cba74` | UInt8, UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 68 | `_0xe7ba69` | UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, String, String, UInt8, UInt8, UInt8, UInt16, UInt16, UInt8, UInt8, UInt | 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62 |  |
| 14 | 86 | `_0x272bc0` | UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 87 | `_0x232bbf` | UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 92 | `_0x624c25` | UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8 | 33, 103 |  |
| 14 | 101 | `_0x2b15d5` | UInt8 | 38 |  |
| 14 | 102 | `_0xb5bcd8` | UInt8 | 38 |  |
| 14 | 104 | `_0x9bac4b` | UInt8 | 38 |  |
| 14 | 105 | `_0x681f9f` | UInt8 | 38 |  |
| 14 | 112 | `_0x5f13a8` | UInt8, UInt8, UInt8, UInt8, UInt8, UInt32, UInt8 | 34, 35, 37 |  |
| 14 | 125 | `_0x2a0976` | UInt8, UInt8, UInt8, UInt8, UInt16, String, UInt8 | 32 |  |
| 14 | 128 | `_0x41bb71` | UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 144 | `_0x55566d` | UInt8, UInt8, UInt8, UInt8 |  |  |
| 14 | 145 | `_0x187391` | UInt8 | 38 |  |
| 18 |  | `_0x96739a` | UInt16, UInt16, UInt16 |  |  |
| 40 |  | `_0x473063` | UInt16 | 10 |  |
| 42 |  | `_0x378d83` | UInt16, UInt8, UInt8, UInt8, UInt16 | 20 |  |
| 57 |  | `_0x193dec` | UInt8, UInt8 | 33 |  |
| 64 |  | `_0x4be5fa` | UInt16 |  |  |
| 66 |  | `_0x4f84a8` | UInt16, UInt32, UInt8 |  |  |
| 70 |  | `_0x36268a` | UInt16, UInt16, UInt16 |  |  |
| 72 |  | `_0x2bcd55` | UInt16 |  |  |
| 74 |  | `_0x4920b8` | UInt8, UInt16, UInt8, String, UInt8, UInt16 | 8, 21 |  |
| 75 |  | `_0x262393` | UInt8 |  |  |
| 76 |  | `_0x5b3c48` | UInt32 | 96 |  |
| 78 |  | `_0x54eee1` | UInt16, UInt8 | 109, 112, 113, 114 |  |
| 80 |  | `_0x4d6017` | UInt16, UInt8 |  |  |
| 81 |  | `_0x523fd5` | UInt16 |  |  |
| 82 |  | `_0x469375` | UInt16 |  |  |
| 83 |  | `_0x6e4cfd` | UInt16 |  |  |
| 84 |  | `_0x2b4455` | UInt16 |  |  |
| 85 |  | `_0x1bd7c2` | UInt16 |  |  |
| 86 |  | `_0x316e54` | UInt16 |  |  |
| 87 |  | `_0x8170ef` | UInt16 |  |  |
| 88 |  | `_0xd39084` | UInt16, BitGroup, UInt16, UInt16 | 22, 23, 24 |  |
| 89 |  | `_0x30c90d` | UInt16 |  |  |
| 90 |  | `_0x482208` | UInt16 |  |  |
| 91 |  | `_0x10ad4a` | UInt16 |  |  |
| 92 |  | `_0x3f37e5` | UInt16 |  |  |
| 93 |  | `_0x38402e` | UInt16 |  |  |
| 95 |  | `_0x395126` | UInt16 |  |  |
| 96 |  | `_0x560027` | UInt16 |  |  |
| 97 |  | `_0x5476e3` | UInt16 |  |  |
| 99 |  | `_0x3865d9` | UInt16 |  |  |
| 100 |  | `_0x5195e8` | UInt16, UInt8, UInt16 | 36 |  |
| 101 |  | `_0x4d3d89` | UInt16, UInt8, UInt8 | 19 |  |
| 103 |  | `_0x4218b5` | UInt8 |  |  |
| 105 |  | `_0x5983fa` | UInt8 | 38 |  |
| 106 |  | `_0x4fedbe` | none | 115, 116, 117 |  |
| 110 |  | `_0x1c7dba` | UInt16 | 38 |  |
| 111 |  | `_0x2844c8` | UInt16 |  |  |
| 112 |  | `_0xd9a02b` | UInt16 |  |  |
| 114 |  | `_0x58f1c4` | UInt16, UInt16 |  |  |
| 115 |  | `_0x3d2bb6` | UInt16, UInt16 |  |  |
| 117 |  | `_0x34b56d` | none |  |  |
| 117 |  | `_0x395f7f` | UInt16, UInt8, UInt8, UInt8, UInt8 |  |  |
| 118 |  | `_0xb57184` | UInt16 |  |  |
| 119 |  | `_0x3ebe4d` | UInt16 |  |  |
| 121 |  | `_0x129406` | UInt16 |  |  |
| 123 |  | `_0x54ce24` | UInt16 | 28 |  |
| 125 |  | `_0x38f253` | UInt8 |  |  |
| 126 |  | `_0x2b23c1` | UInt16 |  |  |
| 127 |  | `_0x28e049` | UInt16 |  |  |
| 128 |  | `_0x1ffbc1` | UInt16 |  |  |
| 128 | 1 | `_0x5881e5` | UInt16 |  |  |
| 128 | 2 | `_0x40e1f7` | UInt16 |  |  |
| 128 | 4 | `_0x1786fc` | UInt16 |  |  |
| 128 | 5 | `_0x1a4a75` | UInt16 |  |  |
| 129 |  | `_0x14e52a` | UInt32, String |  |  |
| 130 |  | `_0x3c2aec` | String, String, UInt8, BitGroup, UInt8 |  |  |
| 131 |  | `_0x20f7aa` | UInt8, UInt8 | 17, 18 | yes |
| 132 |  | `_0x534412` | UInt16 |  |  |
| 133 |  | `_0x480d6f` | UInt16 |  |  |
| 134 |  | `_0x2eefba` | UInt16 |  |  |
| 135 |  | `_0x149ca1` | UInt16 |  |  |
| 136 |  | `_0x3354c3` | UInt16 |  |  |
| 137 |  | `_0x411b29` | UInt16 |  |  |
| 138 |  | `_0x32fdd9` | UInt16 | 14 |  |
| 139 |  | `_0x566472` | UInt16 |  |  |
| 140 |  | `_0x2dcb47` | UInt16 | 14 |  |
| 141 |  | `_0xd5c49d` | UInt16 |  |  |
| 142 |  | `_0x3fc490` | UInt8 |  |  |
| 143 |  | `_0x3b0492` | UInt8, UInt8 |  |  |
| 144 |  | `_0x2067a8` | UInt8 | 18 |  |
| 145 |  | `_0x5e5982` | UInt8, UInt8, UInt8 | 17, 18 |  |
| 146 |  | `_0x31652d` | UInt8, UInt8, UInt8 | 17, 18 |  |
| 147 |  | `_0x427869` | UInt8, String, UInt16, BitGroup |  |  |
| 148 |  | `_0x221fb6` | UInt8 | 38 |  |
| 149 |  | `_0x1bc9ab` | UInt8 | 38 |  |
| 151 |  | `_0x16c7ec` | UInt16 | 26 |  |
| 152 |  | `_0x23f0f` | UInt16, UInt32, String | 12, 13 |  |
| 153 |  | `_0x43501b` | UInt16 |  |  |
| 154 |  | `_0xc8112f` | UInt16 | 110, 111 |  |
| 155 |  | `_0x57b98e` | UInt16 |  |  |
| 157 |  | `_0x2acd17` | UInt16 |  |  |
| 158 |  | `_0x2be80e` | UInt16, UInt16, UInt16 |  |  |
| 159 |  | `_0x505f66` | UInt16, UInt16, UInt16 |  |  |
| 160 |  | `_0x3d60bc` | UInt8 |  |  |
| 161 |  | `_0x23fab6` | UInt8 | 38 |  |
| 162 |  | `_0x3e84b5` | UInt16 |  |  |
| 163 |  | `_0x2052c4` | UInt16 |  |  |
| 164 |  | `_0x4461e5` | UInt16 |  |  |
| 165 |  | `_0x1d6fee` | UInt8, UInt8, UInt8 | 17, 18 |  |

## Abilities

155 definitions, 118 distinct names: 62 in the ability-type table, 91 in per-animal `getAbilityInfo` methods, 2 elsewhere (the helper that adds the universal Dash).

### Ability-type table

| Key | Name | Image |
|---|---|---|
| 1 | Hold to Dig | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 2 | Shell | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '2.p |
| 3 | Shock | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 4 | Ink | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 7 | Disguise | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 8 | Slide on ice | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 9 | Charge | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 10 | Inflate | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 12 | Howl | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 13 | Throw Snow | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 14 | Claw Slash | img/ability_claw.png |
| 15 | Extra Boost | <'skins/' + _0x513c78 + _0x4c6693.skinName + '.png'> |
| 16 | Drop Snow | img/snowball.png |
| 17 | Roll snow | img/snowball.png |
| 18 | Loud Noise | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 19 | Fire | img/fire.png |
| 21 | Bite Drag | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 22 | Burrow Hole | <'skins/' + _0x513c78 + _0x212882.animalInfo().skinName + '. |
| 23 | Pull from Hole | <'skins/' + _0x513c78 + _0x212882.animalInfo().skinName + '. |
| 24 | Throw Banana | <'skins/' + +_0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.p |
| 25 | Dig For Food | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 28 | Cause Wave | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 30 | Fire | img/fire.png |
| 31 | Trunk Hit | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 32 | Tail Slap | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 34 | Venom Spit | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 35 | Spin Web | img/spiderWeb.png |
| 36 | Suffocate prey | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 37 | Jaws Bite | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 38 | Ambush Attack | <'skins/' + _0x513c78 + _0x4c6693.skinName + '.png'> |
| 42 | Stomp | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 46 | Jaws Bite | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 47 | Fly with prey | <'skins/desert/gobibear/' + _0x212882.animalSpecies + '/gobi |
| 48 | Stink | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 49 | Spikes | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '2.p |
| 51 | Arm Smash | img/ability_crabSmashSkin.png |
| 54 | Command Babies | <'skins/' + _0x513c78 + 'ostrich/ostrich-baby.png'> |
| 55 | Fly with water | <'skins/pelican/' + _0x212882.animalSpecies + '/ability_peli |
| 57 | Sky dive | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 58 | Target prey | <'skins/' + _0x513c78 + _0x373b4f(_0x53a9dd).skinName + '.pn |
| 61 | Fire Tornado | img/firetornado.png |
| 63 | Giant Whirlpool | <'skins/' + _0x373b4f(_0x53a9dd).skinName + '.png'> |
| 65 | HOLD to fly | <'skins/' + _0x513c78 + _0x4c6693.skinName + '.png'> |
| 66 | Sink Hole | <'skins/' + _0x373b4f(_0x53a9dd).skinName + '.png'> |
| 69 | Freeze Prey | skins/monsters/icemonster/icemonster.png |
| 70 | Knock In Air | skins/monsters/dinomonster/dinomonster.png |
| 71 | HOLD to fly
& Mimic Animals | <'skins/' + _0x513c78 + _0x4c6693.skinName + '.png'> |
| 72 | HOLD in water
(find food) | <'skins/ocean/flamingo/' + _0x212882.animalSpecies + '/flami |
| 73 | Front Kick
(HOLD to Charge) | skins/cassowary/cassowary1.png |
| 75 | HOLD to Peck
/ Fly | <'skins/' + _0x212882.animalInfo().skinName + '.png'> |
| 76 | HOLD to Hypnotize
/ Fly from hills | <'skins/' + _0x212882.animalInfo().skinName + '.png'> |
| 77 | HOLD to Jump High
 | skins/desert/gazelle/gazelle1.png |
| 78 | HOLD to ROLL | <'skins/desert/armadillo/' + _0x212882.animalSpecies + '/arm |
| 79 | Spits Food! | <'skins/desert/chipmunk/' + _0x212882.animalSpecies + '/chip |
| 80 | HOLD to Dig | <'skins/desert/meerkat/' + _0x212882.animalSpecies + '/meerk |
| 81 | Whine! | <'skins/desert/fennecfox/' + _0x212882.animalSpecies + '/fen |
| 82 | Bear Attack | <'skins/desert/gobibear/' + _0x212882.animalSpecies + '/gobi |
| 83 | Spit! | skins/desert/camel/spit.png |
| 101 | Dive | img/ability_dive.png |
| 134 | HOLD to Fly | <'skins/' + _0x212882.animalInfo().skinName + '.png'> |
| 135 | Charge! | <'skins/' + _0x212882.animalInfo().skinName + '.png'> |
| 143 | Glide from
 Trees & Hills | <'skins/' + _0x212882.animalInfo().skinName + '.png'> |

### Per-animal abilities

| Animal types | Class | Name | Selected when | Line |
|---|---|---|---|---|
| 2 | `_0x1dd509` | Dig Hole! |  | 26616 |
| 3 | `_0x136443` | Stink cloud |  | 20697 |
| 4 | `_0x30dce1` | Pull from Hole |  | 26562 |
| 5 | `_0x316478` | Dig Food! |  | 20767 |
| 6 | `_0x380cee` | Hold to Dig |  | 25367 |
| 7 | `_0x41f91e` | Side Kick! |  | 20936 |
| 8 | `_0x5aee2a` | <'Loud ' + (this.animalSpecies == 2 ? 'Meow' : '...> |  | 20454 |
| 10 | `_0x174e28` | Claw Slash! |  | 21047 |
| 11 | `_0x5dd3c2` | Bite Grab! |  | 21106 |
| 12 | `_0x301198` | Charge! |  | 20611 |
| 13 | `_0x65dbc4` | Loud Grown! |  | 22221 |
| 14 | `_0x4047ee` | Fire |  | 22125 |
| 16 | `_0x48159f` | Extra Boost! |  | 26477 |
| 17 | `_0x302a00` | Hide in Shell! |  | 23126 |
| 18 | `_0x26544c` | INK! |  | 22879 |
| 19 | `_0x52ef2e` | Jaws Bite |  | 22929 |
| 20 | `_0xdb49da` | Shock! |  | 22729 |
| 21 | `_0x5430c0` | Hide in Shell! |  | 22989 |
| 22 | `_0x21c898` | Dash! |  | 26383 |
| 23 | `_0x472004` | Touch prey to sting! |  | 22649 |
| 24 | `_0x419e58` | Whirlpool |  | 22456 |
| 25 | `_0x2753f0` | Puff! |  | 22809 |
| 26 | `_0x207ca1` | Cause Wave |  | 22603 |
| 27 | `_0xb7a117` | Charge! |  | 23055 |
| 28 | `_0xaa8dc6` | Fruit Throw! |  | 22372 |
| 29 | `_0x266c88` | Disguise |  | 10826 |
| 30 | `_0x69be3f` | Howl! |  | 21652 |
| 32 | `_0x3ce401` | <'Yeti Roar' + (this.animalSpecies == 3 ? ' &\n ...> |  | 21345 |
| 34 | `_0x60638d` | Charge! |  | 21728 |
| 35 | `_0x1194dd` | Slide on ice |  | 21528 |
| 36 | `_0x333baa` | Claw Slash! |  | 26895 |
| 37 | `_0x376ff9` | Slide on ice |  | 21565 |
| 38 | `_0x20d0f5` | Extra Boost |  | 25476 |
| 39 | `_0x533e81` | Slide on ice |  | 21612 |
| 40 | `_0x237809` | Dig Food! |  | 21963 |
| 42 | `_0x287ec2` | Roar! |  | 21688 |
| 43 | `_0x180a62` | Roll snow |  | 21444 |
| 44 | `_0xffe93c` | Back Kick! |  | 20815 |
| 46 | `_0x284ccb` | Multi Fireballs |  | 11812 |
| 47 | `_0x3514b6` | Claw Slash! |  | 21491 |
| 48 | `_0xc71e73` | Trunk Hit |  | 22178 |
| 49 | `_0x3fc30b` | Tail Slap |  | 22518 |
| 50 | `_0x3fe2d6` | Venom Spit |  | 12348 |
| 51 | `_0x2cc8ca` | Suffocate prey |  | 22311 |
| 52 | `_0x329369` | Spin Web! |  | 22264 |
| 53 | `_0x4e32da` | Jaws Bite |  | 22022 |
| 55 | `_0xac636e` | Stomp! |  | 25599 |
| 56 | `_0x1e7f32` | Fly with prey | (this.animalData[this.animalSpecies] == undefined) | 10921 |
| 57 | `_0x376b8d` | Spikes! |  | 26657 |
| 61 | `_0x5430e0` | Arm Smash |  | 22560 |
| 63 | `_0x467c39` | Command Babies! |  | 11114 |
| 65 | `_0x412ede` | Sky dive |  | 11184 |
| 66 | `_0x105fc6` | Target Prey! |  | 11346 |
| 68 | `_0x1adb70` | Fire Tornado! |  | 11969 |
| 70 | `_0x49c246` | Giant Whirlpool |  | 17341 |
| 71 | `_0x2221a7` | Giant
Sinkhole! |  | 17396 |
| 71 | `_0x2221a7` | Giant
Sinkhole! |  | 17454 |
| 72 | `_0x5a8a01` | Freeze Prey! |  | 17534 |
| 73 | `_0x25305a` | Knock in Air |  | 17659 |
| 78 | `_0x67de7d` | Front Kick
(Hold to Charge) |  | 18008 |
| 79 | `_0x297495` | Run! |  | 18073 |
| 85 | `_0x44ee97` | HOLD to Jump High
 |  | 18525 |
| 88 | `_0x5544bd` | Spit! |  | 18881 |
| 88 | `_0x5544bd` | Cannon Shots! |  | 18884 |
| 88 | `_0x5544bd` | Water Balloons! |  | 18887 |
| 89 | `_0x26f276` | Dig Food
Sudden Charge |  | 19020 |
| 90 | `_0x221ae6` | Laughs! |  | 19083 |
| 91 | `_0x37f7b7` | Rattle! |  | 19210 |
| 92 | `_0x2169f8` | Fly/ Pickup Bones |  | 19267 |
| 93 | `_0x443c34` | Gores! |  | 19412 |
| 94 | `_0x40c662` | Poison Bite
(Sweat Prey) |  | 19509 |
| 95 | `_0x1a77e6` | Sting!
(Shivers Prey) |  | 19723 |
| 96 | `_0x3ff509` | Dive Attack
(Fly with Prey) |  | 20098 |
| 98 | `_0x1ff28a` | Spin Web & Bite
(Sweat Prey) |  | 19847 |
| 100 | `_0x560c40` | Firestream
& Tail slap |  | 21269 |
| 101 | `_0x181349` | Freeze Prey! |  | 21750 |
| 102 | `_0x18bba5` | Fly High! |  | 26057 |
| 102 | `_0x18bba5` | Gives Gifts! |  | 26060 |
| 102 | `_0x18bba5` | Flying Sleigh! |  | 26063 |
| 103 | `_0x3a9446` | Howl! |  | 26108 |
| 105 | `_0x228b5e` | Head Butt |  | 21885 |
| 106 | `_0x2522aa` | Throw Spear
 (Hold for fire!) |  | 23371 |
| 107 | `_0x4da0aa` | AI |  | 24532 |
| 108 | `_0x5aa9f8` | Loud Caw! |  | 25033 |
| 109 | `_0x5d0a21` | Hop! |  | 25323 |
| 111 | `_0x5017d0` | Stomp! |  | 25637 |
| 112 | `_0x21614e` | Hold to Fly | (this.flag_eff_isOnTree // this.flag_flying && this.flag_usingAbility) | 25662 |
| 112 | `_0x21614e` | Shoot Eggs! | this.flag_isClimbingHill; NOT (this.flag_eff_isOnTree // this.flag_flying && this.flag_usi | 25663 |
| 112 | `_0x21614e` | Front Kick
(Hold to Charge) | NOT this.flag_isClimbingHill; NOT (this.flag_eff_isOnTree // this.flag_flying && this.flag | 25663 |
| (base class: by animal type) | `_0x3447b6` | Claw Slash! | (this.animalType == 36) | 10659 |
| (base class: by animal type) | `_0x41a1bb` | Electro Swoop & Gale Charge |  | 28205 |

### Elsewhere

- L1042 Dash (_0x10db9e)
- L1385 Ability ()

## Packet field switches

- L3801 `_0x3e7c4e` switch on `(_0xb6eaf4)`: 23 cases — 103:String, 104:String, 105:UInt16+String, 102:UInt8+UInt8, 62:UInt16+String, 8:UInt16+String+UInt32+UInt8+UInt16+String+UInt32, 10:UInt16+UInt16, 67:String, 63:UInt32+String, 69:String, 18:UInt8+UInt8+UInt16+UInt8+UInt8+UInt32+UInt32+UInt16+UInt16+UInt8+UInt8+UInt8+UInt8+UInt8+UInt8+UInt8+UInt8, 14:UInt8+UInt32+String+UInt16+String+UInt32+UInt16+UInt32+UInt8, 19:UInt32+String, 100:UInt8, 16:String, 106:String+String+UInt8, 23:UInt8+String+UInt16, 25:UInt8+UInt32, 113:String, 11:UInt8+String+UInt8, 58:UInt8+String, 59:UInt8, 56:UInt8
- L13152 `_0x5ab805.prototype.handleMessage` switch on `(_0x360f25)`: 13 cases — 83:String, 82:UInt16+String, 84:UInt16, 33:UInt16, 36:UInt8+String+UInt32, 37:UInt16, 38:UInt16, 39:UInt16, 41:UInt8+UInt16, 42:UInt8, 44:String, 45:UInt16+String+UInt8, 46:UInt16
- L4431 `_0x51aed3` switch on `_0x2dd02b`: 20 cases — 1:UInt16, 2:UInt32+UInt32, 3:UInt16+UInt16, 4:UInt8, 5:UInt8, 6:UInt8, 7:UInt32, 8:UInt8, 9:UInt16, 10:UInt16, 11:UInt32, 12:UInt8, 13:UInt32, 14:String, 15:UInt8, 16:UInt8, 17:UInt32, 19:UInt16, 20:UInt8, 21:UInt8
- L8325 `_0x16a69f.prototype.readUpdatedPropertyItem` switch on `_0x173ab4`: 8 cases — 1:UInt16+UInt16, 2:UInt16, 3:UInt16, 4:UInt8, 5:UInt8, 6:UInt8, 7:UInt8, 66:UInt8
- L10598 `_0x3447b6.prototype.readUpdatedProperty` switch on `_0x3599c0`: 14 cases — 63:UInt16, 64:UInt8, 65:UInt8, 67:UInt8, 68:UInt8, 69:UInt16, 70:UInt8, 71:UInt8, 72:UInt8, 73:UInt32+UInt16, 74:UInt8, 77:UInt8, 102:UInt16+UInt16, 120:UInt16
- L17114 `_0xe7ba69.prototype.readUpdatedProperty` switch on `_0x10f67f`: 23 cases — 39:UInt8, 40:UInt8, 41:UInt16, 42:UInt16, 43:UInt16, 44:UInt8, 45:UInt8, 46:UInt16, 47:UInt16, 48:UInt8, 49:String, 50:UInt8, 51:UInt8, 52:UInt32, 53:UInt8, 54:UInt8, 55:UInt8, 56:UInt8, 57:String, 58:String, 59:UInt8, 60:UInt16, 61:UInt8
- L17542 `_0x5a8a01.prototype.customUpdatedProperty` switch on `_0x4b86c6`: 6 cases — 78:, 79:, 80:, 81:, 82:, 83:
