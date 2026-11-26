# Pumpkin variants (`fetch_pumpkins.rb`)

The archived 2025 client draws pumpkins as **object type 131**:

- The factory registers the pumpkin class for type 131 (`setCustomClassForGameObjType(_0x20f7aa, 131)`, L24964).
- When a pumpkin appears, the client reads **UInt8 isRolling** and **UInt8 pumpkinType**
  (L24956) after the standard object fields.
- It loads `./assets/s/<season>/img/pumpkin/<pumpkinType>/<frame>.png`.
- While rolling, it cycles **frames 0–4** every 120 ms (L24937).

No archive holds those files, and the original server's numbering of pumpkin types is lost.
`fetch_pumpkins.rb` fills the folder from two sources and the region servers spawn pumpkins of
every installed type.

```bash
ruby fetch_pumpkins.rb               # fetch, convert, install, write the manifest
ruby fetch_pumpkins.rb --dry-run     # only read the live bundle and print the plan
ruby fetch_pumpkins.rb --custom-frames still   # custom variants without the spin
ruby tests/test_pumpkins.rb
./start-all.sh restart regions       # the regions read the installed manifest at start-up
```

## Sources

1. **The live mope.io site (2026 build).**
   - Its pumpkin class names 7 variants and 5 frames each (`terrain/pumpkin/${[…][subspecies]}`
     plus its frame loop). The script reads both from the newest live bundle snapshot
     (`archive/live/*/site`, or downloads the bundles with `--refresh-site`), so the list is never
     hard-coded.
   - All **35 frames** are downloaded from `https://mope.io/assets/terrain/pumpkin/<variant>/<frame>.webp`
     and converted to PNG.
2. **s3.mope.io.** The bundle's bucket (`s3PublicUrl` = `https://s3.mope.io/mope-prod`) is
   probed with the same paths, in two layouts. On 2026-11-26 it answered **403 to all 14
   probes**: it refuses unknown keys and listing. Nothing on it can be found, so nothing was
   taken from it. The script uses the bucket automatically for any frame the site lacks, should
   that change.
3. **Custom variants** from `custom_assets/pumpkins/`:
   - amber, sapphire, amethyst and black hole (the originals are 1254×1254 PNG on black);
   - the black background is made transparent by flood-filling from the four corners, so dark
     areas inside the outline (the black hole's interior) stay. The script checks this: centre
     opaque, corners transparent;
   - each is fitted to the live frame size (284 px);
   - five frames are made with a 72° turn each, or `--custom-frames still` for five identical
     frames.

## Installed types (local numbering)

| pumpkinType | Variant | Source |
|---|---|---|
| 0 | normal | live mope.io |
| 1 | jack | live mope.io |
| 2 | gold | live mope.io |
| 3 | emerald | live mope.io |
| 4 | ruby | live mope.io |
| 5 | diamond | live mope.io |
| 6 | onyx | live mope.io |
| 7 | amber | custom |
| 8 | sapphire | custom |
| 9 | amethyst | custom |
| 10 | black hole | custom |

The files are installed into the **2025 working snapshot**
(`snapshots/2025-12-10/working/client/mope.io/assets/s/1/img/pumpkin/`), which the gateway serves.
That folder also gets `pumpkins.json`, with a frame list, sources and sha256 values, and a copy is
committed as [`data/manifest.json`](data/manifest.json). The original archive (`archive/original`)
is never written. The downloaded WebP files stay in `archive/live/pumpkins/` (git-ignored).

## In the game

`gameplay.pumpkins` in `config/local_stack.json` is set to `count 44, radius 30, rolling true`.
Each region spawns that many pumpkins, cycling through every type whose frames are all installed,
so each type appears 4 times. With `count 0`, or with no manifest, there are no pumpkins.

A real-client check found:

- all five frames requested and served (HTTP 200) for every pumpkin type in view;
- no page errors;
- the custom and live variants drawn in the world, rolling, at their own angles.

Pumpkins are scenery for now. Carrying, pushing and the Halloween goal mode are not
reconstructed.
