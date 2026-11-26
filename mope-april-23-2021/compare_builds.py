#!/usr/bin/env python3
"""
compare_builds.py -- version history of the archived mope.io client builds (preservation analysis).

Runs the Phase 2 protocol extractor and the Phase 5 evidence extractor over every readable client
build in working/client-js/ (rebuild_client_js.py --all-captures: 2017-02-13, 2018-04-21,
2025-09-05, 2025-12-10), adds what the live 2026 site's own data says (live/data, pumpkins/data),
and writes:

  history/data/builds.json   every measured fact per build and every difference
  history/README.md          the version history

Per build: size, protocol opcodes in both directions, $config and regions, the animal table,
distinct ability names, object type ids (with the asset label the client draws), registered
classes. Between consecutive builds: what was added and removed. 2025-12 vs live 2026: shared and
renamed animals, biomes and foods, matched by name (the live site is a different code base).

Old builds dispatch server messages with `switch (reader.readUInt8())`; their opcodes are taken
from that switch. No client code runs.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import extract_client_protocol as ecp  # noqa: E402
import extract_gameplay_evidence as ege  # noqa: E402

BUILDS = PROJECT / "working" / "client-js"
OUT_JSON = PROJECT / "history" / "data" / "builds.json"
OUT_MD = PROJECT / "history" / "README.md"
TARGET = "2021-04-23"


# -- names ------------------------------------------------------------------------------------------
def norm(name: str | None) -> str:
    """'The Kraken' -> 'kraken', 'Sea-horse' -> 'seahorse', 'T-REX' -> 'trex', 'kangaroo_rat' -> 'kangaroorat'."""
    text = (name or "").lower().strip()
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def tokens(name: str | None) -> list[str]:
    """'Boa Constrictor' / 'boaConstrictor' -> ['boa', 'constrictor']; 'ostrich/baby-ostrich' -> ['baby', 'ostrich']."""
    text = (name or "").split("/")[-1]
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower()
    return [t for t in re.split(r"[^a-z0-9]+", text) if t and t != "the"]


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


RENAME_RULES = [
    ("skin name", lambda o, n: bool(o.get("skin")) and norm(o["skin"].split("/")[-1]) == norm(n["name"])),
    ("same words", lambda o, n: sorted(tokens(o["name"])) == sorted(tokens(n["name"])) or
     (bool(o.get("skin")) and sorted(tokens(o["skin"])) == sorted(tokens(n["name"])))),
    ("abbreviation", lambda o, n: len(norm(o["name"])) >= 4 and norm(n["name"]).startswith(norm(o["name"]))),
    ("one word of the old name", lambda o, n: len(norm(n["name"])) >= 3 and
     (norm(n["name"]) in tokens(o["name"]) or norm(n["name"]) in tokens(o.get("skin")))),
    ("spelling", lambda o, n: min(len(norm(o["name"])), len(norm(n["name"]))) >= 6 and
     levenshtein(norm(o["name"]), norm(n["name"])) <= 1),
]


def pair_renames(removed: list[dict], added: list[dict]) -> list[dict]:
    """Probable renames between animals that only one side has: each rule must find exactly one partner."""
    pairs, free = [], list(added)
    for rule, test in RENAME_RULES:
        for old in [o for o in removed if not any(p["old"] == o["name"] for p in pairs)]:
            candidates = [n for n in free if test(old, n)]
            if len(candidates) == 1:
                pairs.append({"old": old["name"], "new": candidates[0]["name"], "rule": rule})
                free.remove(candidates[0])
    return pairs


def animal_keys(animal: dict) -> set[str]:
    return {k for k in (norm(animal.get("name")), norm(animal.get("skin"))) if k}


def diff(old: set, new: set) -> dict:
    return {"added": sorted(new - old, key=str), "removed": sorted(old - new, key=str)}


# -- one archived build ---------------------------------------------------------------------------------
def readuint8_dispatch(ev: ege.Evidence) -> list[int]:
    """Case labels of the largest `switch (x.readUInt8())` (how 2017/2018 builds dispatch messages)."""
    best: list[int] = []
    for node in ev.src.walk():
        if node.type != "switch_statement":
            continue
        value = node.child_by_field_name("value")
        if value is None or not re.fullmatch(r"\(\s*\w+\.readUInt8\(\)\s*\)", ev.src.text(value)):
            continue
        cases = []
        for case in node.child_by_field_name("body").named_children:
            if case.type == "switch_case":
                key = ev.ex.resolve(case.child_by_field_name("value"))
                if isinstance(key, int):
                    cases.append(key)
        if len(cases) > len(best):
            best = cases
    return sorted(set(best))


def biome_names(ev: ege.Evidence) -> dict[int, str]:
    """The client's biome id -> name switch (cases returning 'land', 'ocean', ...)."""
    known = {"land", "ocean", "arctic", "desert", "volcano", "forest", "swamp", "jungle"}
    best: dict[int, str] = {}
    for node in ev.src.walk():
        if node.type != "switch_statement":
            continue
        found = {}
        for case in node.child_by_field_name("body").named_children:
            if case.type != "switch_case":
                continue
            key = ev.ex.resolve(case.child_by_field_name("value"))
            ret = next((n for n in case.named_children if n.type == "return_statement"), None)
            if isinstance(key, int) and ret is not None and ret.named_children and ret.named_children[0].type == "string":
                value = ev.src.string_value(ret.named_children[0])
                if value in known:
                    found[key] = value
        if len(found) > len(best):
            best = found
    return dict(sorted(best.items()))


def analyse_build(folder: Path) -> dict:
    client = folder / "client.js"
    stamp, provider = folder.name.split("_", 1)
    report = json.loads((folder / "rebuild_report.json").read_text(encoding="utf-8")) \
        if (folder / "rebuild_report.json").is_file() else {}
    started = time.time()
    build = {
        "build": folder.name, "timestamp": stamp, "provider": provider,
        "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}",
        "lines": sum(1 for _ in client.open(encoding="utf-8", errors="replace")),
        "bytes": client.stat().st_size,
        "verified": report.get("verification", {}).get("equivalent", report.get("verified")),
        "original_sha256": report.get("source", {}).get("sha256") or report.get("sha256"),
    }
    try:
        inv = ecp.build_inventory(client)
    except Exception as exc:  # noqa: BLE001 - old builds may not match every Phase 2 pattern
        inv = {"error": repr(exc)}
    c2s = {o["opcode"] for o in inv.get("opcodes", []) if o.get("client_to_server")}
    c2s |= {s["opcode"] for s in inv.get("raw_sends", []) if isinstance(s.get("opcode"), int)}
    s2c = {c["opcode"] for d in inv.get("dispatchers", []) for c in d.get("cases", []) if isinstance(c.get("opcode"), int)}
    ev = ege.Evidence(client)
    if not s2c:
        s2c = set(readuint8_dispatch(ev))
        build["s2c_source"] = "switch (reader.readUInt8())"
    registry = ev.registry()
    animals = [{"id": a["id"], "name": a.get("aniName"), "skin": a.get("skinName")}
               for a in ev.switch_tables(("aniName", "skinName", "aniCol"), "aniName")]
    abilities = ev.abilities_all(registry)
    catalogue = ege.object_catalogue(ev, registry, {})
    config = inv.get("config") or {}
    build.update({
        "c2s": sorted(c2s), "s2c": sorted(s2c),
        "config": {k: v for k, v in config.items() if k not in ("authentication", "gameServers", "turnstileKey")},
        "regions": [r.get("id") or r.get("name") for r in inv.get("regions", [])],
        "animals": animals,
        "ability_names": sorted({a["name"] for a in abilities if not a["name"].startswith("<")}),
        "ability_definitions": len(abilities),
        "object_types": {str(r["object_type"]): r.get("label") or "" for r in catalogue},
        "food_types": sorted(r["object_type"] for r in catalogue if r["category"] == "food"),
        "registered_classes": len(registry),
        "biomes": {str(k): v for k, v in biome_names(ev).items()},
        "detected": {"abilities": bool(abilities), "foods": any(r["category"] == "food" for r in catalogue),
                     "config": bool(config), "regions": bool(inv.get("regions")), "biomes": bool(biome_names(ev))},
        "seconds": round(time.time() - started, 1),
    })
    return build


# -- the live 2026 site (data level) ------------------------------------------------------------------------
def live_site() -> dict | None:
    animals_file = PROJECT / "live" / "data" / "animals.json"
    if not animals_file.is_file():
        return None
    animals = json.loads(animals_file.read_text(encoding="utf-8"))
    files = json.loads((PROJECT / "live" / "data" / "files.json").read_text(encoding="utf-8")) \
        if (PROJECT / "live" / "data" / "files.json").is_file() else []
    report = json.loads((PROJECT / "live" / "data" / "report.json").read_text(encoding="utf-8")) \
        if (PROJECT / "live" / "data" / "report.json").is_file() else {}
    foods = sorted({f["path"].split("/")[2] for f in files if f["path"].startswith("assets/foods/")})
    pumpkins = json.loads((PROJECT / "pumpkins" / "data" / "manifest.json").read_text(encoding="utf-8")) \
        if (PROJECT / "pumpkins" / "data" / "manifest.json").is_file() else {}
    return {
        "snapshot": report.get("snapshot"), "animals": [{"name": a["name"], "biome": a["biome_folder"],
                                                        "subspecies": a["subspecies"]} for a in animals],
        "biomes": sorted({a["biome_folder"] for a in animals}), "foods": foods,
        "pumpkin_variants": [t["name"] for t in pumpkins.get("types", []) if str(t.get("origin", "")).startswith("live")],
        "image_files": report.get("files_found"),
    }


def compare_live(last: dict, live: dict) -> dict:
    live_by_key = {norm(a["name"]): a["name"] for a in live["animals"]}
    shared, only_old = [], []
    for animal in last["animals"]:
        match = next((live_by_key[k] for k in animal_keys(animal) if k in live_by_key), None)
        if match:
            shared.append({"2025": animal["name"], "live": match})
        else:
            only_old.append(animal)
    matched = {s["live"] for s in shared}
    only_live = [{"name": n} for n in sorted(live_by_key.values()) if n not in matched]
    renames = pair_renames(only_old, only_live)
    renamed_old, renamed_new = {r["old"] for r in renames}, {r["new"] for r in renames}
    return {
        "animals_shared": shared, "animals_renamed": renames,
        "animals_only_2025": [a["name"] for a in only_old if a["name"] not in renamed_old],
        "animals_only_live": [a["name"] for a in only_live if a["name"] not in renamed_new],
        "biomes": diff(set(last["biomes"].values()), set(live["biomes"])),
    }


# -- report -------------------------------------------------------------------------------------------------------
def transitions(builds: list[dict]) -> list[dict]:
    out = []
    for old, new in zip(builds, builds[1:]):
        old_animals = {norm(a["name"]): a for a in old["animals"]}
        new_animals = {norm(a["name"]): a for a in new["animals"]}
        animal_diff = diff(set(old_animals), set(new_animals))
        removed = [old_animals[k] for k in animal_diff["removed"]]
        added = [new_animals[k] for k in animal_diff["added"]]
        renames = pair_renames(removed, added)
        types_old, types_new = set(old["object_types"]), set(new["object_types"])

        def comparable(key: str):
            return old["detected"][key] and new["detected"][key]
        out.append({
            "from": old["date"], "to": new["date"],
            "lines": [old["lines"], new["lines"]],
            "c2s": diff(set(old["c2s"]), set(new["c2s"])), "s2c": diff(set(old["s2c"]), set(new["s2c"])),
            "animals": {"added": [a["name"] for a in added if a["name"] not in {r["new"] for r in renames}],
                        "removed": [a["name"] for a in removed if a["name"] not in {r["old"] for r in renames}],
                        "renamed": renames},
            "abilities": diff(set(old["ability_names"]), set(new["ability_names"])) if comparable("abilities") else
            {"not_comparable": "no ability names found in " + (old["date"] if not old["detected"]["abilities"] else new["date"])},
            "object_types": {"added": sorted((types_new - types_old), key=int),
                             "removed": sorted((types_old - types_new), key=int)},
            "regions": diff(set(old["regions"]), set(new["regions"])) if comparable("regions") else
            {"not_comparable": "region list only in the 2025 builds' $config"},
            "config": {k: [old["config"].get(k), new["config"].get(k)] for k in sorted(set(old["config"]) | set(new["config"]))
                       if old["config"].get(k) != new["config"].get(k)} if comparable("config") else
            {"not_comparable": "$config only exists in the 2025 builds"},
        })
    return out


def short(items: list, limit: int = 40) -> str:
    items = [" / ".join(str(i).split("\n")) for i in items]
    return (", ".join(items[:limit]) + (f" … (+{len(items) - limit})" if len(items) > limit else "")) if items else "none"


def markdown(data: dict) -> str:
    builds = data["builds"]
    lines = ["# Client version history", "",
             f"Generated {data['generated_at']} by `compare_builds.py` from the archived client builds in "
             "`working/client-js/` (Arquivo.pt captures, rebuilt and verified by `rebuild_client_js.py`), plus the live "
             "2026 site's own data. Measured facts only; no client code runs. The full data is "
             "[`data/builds.json`](data/builds.json).", "",
             "No archive used holds a 2021 client build. The two nearest are 2018-04-21 (3.0 years before the April 23, "
             "2021 target) and 2025-09-05 (4.4 years after).", "",
             "## Builds", "",
             "| Build | Lines | Client → server | Server → client | Animals | Ability names | Object types | Food types | Classes | Regions | gameVersion |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in builds:
        lines.append(f"| {b['date']} | {b['lines']:,} | {len(b['c2s'])} | {len(b['s2c'])} | {len(b['animals'])} | "
                     f"{len(b['ability_names'])} | {len(b['object_types'])} | {len(b['food_types'])} | "
                     f"{b['registered_classes'] or '—'} | {len(b['regions']) or '—'} | {b['config'].get('gameVersion', '—')} |")
    lines += ["", "Ability names are the literal names (computed ones are left out). Renames are *probable*: each pair is "
              "found by exactly one rule (skin name, same words, expanded abbreviation, one word of the old name, or a "
              "one-letter spelling fix) and shown with that rule.", "",
              "Notes: the 2017 and 2018 builds predate the class registry (`setCustomClassForGameObjType`); their "
              "object types come from `oType` comparisons and switches only, and their server → client opcodes from "
              "`switch (reader.readUInt8())`. Region lists and `$config` only exist in the 2025 builds.", ""]
    for t in data["transitions"]:
        lines += [f"## {t['from']} → {t['to']}", "",
                  f"- Size: {t['lines'][0]:,} → {t['lines'][1]:,} lines.",
                  f"- Client → server opcodes added: {short(t['c2s']['added'])}; removed: {short(t['c2s']['removed'])}.",
                  f"- Server → client opcodes added: {short(t['s2c']['added'])}; removed: {short(t['s2c']['removed'])}.",
                  f"- Animals added ({len(t['animals']['added'])}): {short(t['animals']['added'])}.",
                  f"- Animals removed ({len(t['animals']['removed'])}): {short(t['animals']['removed'])}.",
                  f"- Animals renamed (probable, {len(t['animals']['renamed'])}): " +
                  (", ".join(f"{r['old']} → {r['new']} ({r['rule']})" for r in t["animals"]["renamed"]) or "none") + ".",
                  *([f"- Ability names: not comparable ({t['abilities']['not_comparable']})."] if "not_comparable" in t["abilities"]
                    else [f"- Ability names added ({len(t['abilities']['added'])}): {short(t['abilities']['added'], 30)}.",
                          f"- Ability names removed ({len(t['abilities']['removed'])}): {short(t['abilities']['removed'], 30)}."]),
                  f"- Object types added ({len(t['object_types']['added'])}): {short(t['object_types']['added'], 60)}.",
                  f"- Object types removed ({len(t['object_types']['removed'])}): {short(t['object_types']['removed'], 60)}."]
        if "not_comparable" in t["regions"]:
            lines.append(f"- Regions: not comparable ({t['regions']['not_comparable']}).")
        elif t["regions"]["added"] or t["regions"]["removed"]:
            lines.append(f"- Regions added: {short(t['regions']['added'])}; removed: {short(t['regions']['removed'])}.")
        if "not_comparable" in t["config"]:
            lines.append(f"- `$config`: not comparable ({t['config']['not_comparable']}).")
        elif t["config"]:
            lines.append("- `$config` changes: " + "; ".join(f"`{k}` {v[0]!r} → {v[1]!r}" for k, v in t["config"].items()) + ".")
        lines.append("")
    live, cmp = data.get("live"), data.get("live_comparison")
    if live and cmp:
        lines += [f"## 2025-12-10 → live site ({live['snapshot']})", "",
                  "The live site is a rebuild on a different code base (bundled modules, WebP assets, new file layout), "
                  "so it is compared by name only.", "",
                  f"- Animals: {len(builds[-1]['animals'])} in 2025-12 vs {len(live['animals'])} live; "
                  f"**{len(cmp['animals_shared'])} shared** by name, {len(cmp['animals_renamed'])} probable renames.",
                  "- Probable renames: " + (", ".join(f"{r['old']} → {r['new']} ({r['rule']})" for r in cmp["animals_renamed"])
                                            or "none") + ".",
                  f"- Only in 2025-12 ({len(cmp['animals_only_2025'])}): {short(cmp['animals_only_2025'], 60)}.",
                  f"- Only live ({len(cmp['animals_only_live'])}): {short(cmp['animals_only_live'], 60)}.",
                  f"- Biomes: 2025-12 {short(sorted(builds[-1]['biomes'].values()))}; live {short(live['biomes'])} "
                  f"(not in live: {short(cmp['biomes']['removed'])}).",
                  f"- Foods: {len(builds[-1]['food_types'])} food object types in 2025-12; {len(live['foods'])} food "
                  f"images on the live site ({short(live['foods'], 30)}).",
                  f"- Pumpkins: live variants {short(live['pumpkin_variants'])}.",
                  ""]
    return "\n".join(lines)


def main() -> int:
    folders = sorted(p.parent for p in BUILDS.glob("*_*/client.js"))
    if not folders:
        print("error: no rebuilt client builds (run: python rebuild_client_js.py --all-captures)", file=sys.stderr)
        return 2
    builds = []
    for folder in folders:
        build = analyse_build(folder)
        builds.append(build)
        print(f"{build['date']}: {build['lines']:,} lines, C->S {len(build['c2s'])}, S->C {len(build['s2c'])}, "
              f"{len(build['animals'])} animals, {len(build['ability_names'])} ability names, "
              f"{len(build['object_types'])} object types ({build['seconds']}s)")
    data = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "target": TARGET,
            "builds": builds, "transitions": transitions(builds)}
    live = live_site()
    if live:
        data["live"] = live
        data["live_comparison"] = compare_live(builds[-1], live)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    OUT_MD.write_text(markdown(data), encoding="utf-8")
    print(f"wrote {OUT_MD.relative_to(PROJECT)} and {OUT_JSON.relative_to(PROJECT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
