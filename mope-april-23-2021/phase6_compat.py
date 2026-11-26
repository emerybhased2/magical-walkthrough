#!/usr/bin/env python3
"""
phase6_compat.py -- Phase 6 (task 34): historical compatibility report.

Compares the reconstructed local stack with the April 23, 2021 archive and with the archived
client's own protocol, and writes:

  phase6/data/compatibility.json   every measured fact
  phase6/COMPATIBILITY.md          the report (measured sections + the feature matrix)

Measured (re-run after changes):
  1. baselines         the 2021 anchor capture and its deviation; the 2025 client actually served
  2. homepage          element ids and resources of the 2021 homepage vs the served 2025 homepage;
                       which 2021 resources the local gateway can serve
  3. protocol          client->server opcodes the client sends vs the ones the region server handles;
                       server->client opcodes the client understands vs the ones the server sends
  4. assets            the client's image references vs archived images (decode_images.ts report)
  5. verification      the latest phase6_verify.py report
The feature matrix (completed / partial / unresolved) is maintained in this file with evidence.
"""

from __future__ import annotations

import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from services import common, game_protocol as gp  # noqa: E402

ARCHIVE_2021 = PROJECT / "archive"
INVENTORY = PROJECT / "phase2" / "data" / "protocol_inventory.json"
OUT_MD = PROJECT / "phase6" / "COMPATIBILITY.md"
OUT_JSON = PROJECT / "phase6" / "data" / "compatibility.json"

# Opcode names established in Phases 2-5, per direction (others come from the client's own strings).
KNOWN_C2S = {1: "join", 5: "cursor position", 11: "leave", 17: "resize", 19: "chat", 64: "captcha token",
             113: "play", 255: "ping"}
KNOWN_S2C = {2: "server info", 3: "connected", 4: "world update", 6: "alive in game", 11: "disconnect",
             14: "you died", 16: "notification", 17: "game size", 18: "your animal changed", 19: "chat message",
             23: "hurt / effect text", 56: "spectate mode", 100: "snowfall", 107: "minimap",
             113: "rate-limit disconnect", 255: "pong"}


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.resources: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.add(value)
            if name in ("src", "href") and value and not value.startswith(("#", "javascript:", "mailto:", "data:")):
                self.resources.add(value)


def page(path: Path) -> Ids:
    parser = Ids()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return parser


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# -- 1 + 2. baselines and homepage ------------------------------------------------------------------
def homepage_section(config: dict) -> dict:
    selection = load_json(ARCHIVE_2021 / "manifests" / "selection.json", {}) or {}
    report = load_json(ARCHIVE_2021 / "manifests" / "archive_report.json", {}) or {}
    old_index = ARCHIVE_2021 / "original" / "mope.io" / "index.html"
    served_root = common.project_path(config, config["client_root"])
    fallbacks = [common.project_path(config, d) for d in config["web"].get("asset_fallbacks", [])]
    out = {"anchor": selection.get("anchor") or selection, "archive_report": {
        k: report.get(k) for k in ("selection", "historical_fidelity", "validation") if k in report}}
    if not old_index.is_file():
        out["error"] = "no 2021 homepage in archive/original (run fetch_mope_archive.py --timestamp 20210417072725 --allow-off-date --all)"
        return out
    old, new = page(old_index), page(served_root / "index.html")
    out["element_ids"] = {
        "2021": len(old.ids), "served_2025": len(new.ids), "shared": sorted(old.ids & new.ids),
        "only_2021": sorted(old.ids - new.ids), "only_2025": sorted(new.ids - old.ids),
    }
    local = []
    for ref in sorted(old.resources):
        if ref.startswith(("http://", "https://", "//")):
            host = re.sub(r"^(https?:)?//", "", ref).split("/")[0]
            if host not in ("mope.io", "www.mope.io"):
                local.append({"resource": ref, "served": False, "reason": f"third-party host {host} (blocked by design)"})
                continue
            ref_path = "/" + re.sub(r"^(https?:)?//[^/]+/?", "", ref)
        else:
            ref_path = "/" + ref.lstrip("./")
        rel = ref_path.split("?")[0].lstrip("/")
        where = next((name for name, root in [("served snapshot", served_root), *[(f"fallback {d.name}", d) for d in fallbacks]]
                      if rel and (root / rel).is_file()), None)
        in_2021 = (ARCHIVE_2021 / "original" / "mope.io" / rel).is_file() if rel else False
        local.append({"resource": ref, "served": where is not None, "from": where, "in_2021_archive": in_2021})
    out["resources_2021"] = local
    out["resources_served"] = sum(1 for r in local if r["served"])
    return out


# -- 3. protocol ------------------------------------------------------------------------------------------------
def protocol_section() -> dict:
    inv = load_json(INVENTORY, {}) or {}
    labels: dict[int, str] = {}
    client_sends: set[int] = set()
    client_handles: set[int] = set()
    for entry in inv.get("opcodes", []):
        if entry.get("client_to_server"):
            client_sends.add(int(entry["opcode"]))
        if entry.get("server_to_client"):
            client_handles.add(int(entry["opcode"]))
    for send in inv.get("raw_sends", []):
        if isinstance(send.get("opcode"), int):
            client_sends.add(send["opcode"])
    for dispatcher in inv.get("dispatchers", []):
        for case in dispatcher.get("cases", []):
            if isinstance(case.get("opcode"), int):
                client_handles.add(case["opcode"])
                label = next((s for s in case.get("strings", []) if re.match(r"^(Msg|MSG)_?\w+$", s)), None)
                if label and case["opcode"] not in labels:
                    labels[case["opcode"]] = label
    # server side, measured from the code: what the region server dispatches and what it sends
    source = (PROJECT / "services" / "region_server.py").read_text(encoding="utf-8")
    handled = set()
    for module_name, module in (("common", common), ("gp", gp)):
        for name in re.findall(rf"opcode == {module_name}\.(OP_\w+)", source):
            handled.add(int(getattr(module, name)))
    builders = {
        "msg_server_info": common.msg_server_info("s", 1, 0, 1, "r", alive=False),
        "msg_connected": common.msg_connected(), "msg_notification": common.msg_notification("x"),
        "msg_disconnect": common.msg_disconnect("x", code=0), "msg_game_size": gp.msg_game_size(1, 1, 0, 0, 1),
        "msg_minimap": gp.msg_minimap(1, 1), "msg_your_animal": gp.msg_your_animal(1, 1), "msg_alive": gp.msg_alive(),
        "msg_world_update": gp.msg_world_update((0, 0), 1, True, [], [], []),
    }
    world_source = (PROJECT / "services" / "game_world.py").read_text(encoding="utf-8")
    sent = {data[0] for name, data in builders.items() if name in source or name in world_source}
    if "OP_PING" in source:
        sent.add(common.OP_PING)
        handled.add(common.OP_PING)
    if "common.OP_JOIN" in source:   # the join is checked as the first packet, outside _dispatch
        handled.add(common.OP_JOIN)

    def rows(ops, known):
        return [{"opcode": op, "name": known.get(op) or labels.get(op, "")} for op in sorted(ops)]
    return {
        "client_sends": rows(client_sends, KNOWN_C2S), "server_handles": rows(handled, KNOWN_C2S),
        "client_sends_unhandled": rows(client_sends - handled, KNOWN_C2S),
        "client_handles": rows(client_handles, KNOWN_S2C), "server_sends": rows(sent, KNOWN_S2C),
        "client_handles_never_sent": rows(client_handles - sent, KNOWN_S2C),
        "coverage": {"c2s": f"{len(client_sends & handled)}/{len(client_sends)}",
                     "s2c": f"{len(client_handles & sent)}/{len(client_handles)}"},
    }


# -- 4 + 5. assets and verification ----------------------------------------------------------------------------------
def assets_section() -> dict:
    report = load_json(PROJECT / "images" / "data" / "report.json", {}) or {}
    live = load_json(PROJECT / "live" / "data" / "report.json", {}) or {}
    return {
        "client_references": report.get("clientReferences"), "client_templates": report.get("clientTemplates"),
        "archived_urls": report.get("urlsWithImage"), "archived_versions": report.get("versionsStored"),
        "coverage": report.get("coverage"), "by_category": report.get("byCategory"),
        "live_2026_files": live.get("files_found"),
    }


def verification_section() -> dict:
    for path in (PROJECT / "working" / "phase6" / "verify_report.json", PROJECT / "phase6" / "data" / "verify_report.json"):
        report = load_json(path)
        if report:
            return {"generated_at": report.get("generated_at"), "passed": report.get("passed"),
                    "failed": report.get("failed"),
                    "checks": [{"id": c["id"], "ok": c["ok"], "name": c["name"], "detail": c["detail"][:300]}
                               for c in report.get("checks", [])]}
    return {"error": "run phase6_verify.py first"}


# -- feature matrix (maintained here, with evidence) ------------------------------------------------------------------------
MATRIX = [
    # (area, feature, status, evidence)
    ("Archive", "April 2021 homepage, CSS, images (non-Wayback)", "partial",
     "Anchor 2021-04-17 (-5.7 days, Common Crawl revisit = homepage unchanged since 2021-03-06); 22 files"),
    ("Archive", "April 2021 client.js", "unresolved",
     "Not in Arquivo.pt, Common Crawl or vefsafn.is; the stack runs the 2025-12-10 client (README 'What the archives hold')"),
    ("Client", "Readable, verified rebuild of the archived client", "completed", "rebuild_client_js.py (syntax-tree equivalence)"),
    ("Client", "Client served locally with no production traffic", "completed",
     "web gateway: $config overlay, Turnstile shim, domain lock satisfied (phase3 B checks)"),
    ("Master", "Region registry, heartbeats, /servers/get, failover", "completed", "phase3_verify.py 34/34"),
    ("Regions", "20 regions (9 archived + 11 added), join/leave/resume/shutdown", "completed", "phase6 S1, G1"),
    ("Regions", "Region selection by the client (ping + server list)", "completed", "phase3 B checks, phase6 R1"),
    ("Accounts", "Local registration, login, @me, logout, forged-token rejection", "completed", "phase4_verify.py 8/8, phase6 A1"),
    ("Accounts", "Social sign-in (Google, Facebook, Apple, Discord)", "unresolved",
     "Needs mope.io's OAuth apps; refused with 501 and a pointer to local accounts"),
    ("Accounts", "Shop, coins, gems (local catalogue)", "partial",
     "Catalogue is local data (config/shop_catalog.json); purchases with real payment providers are not possible"),
    ("Gameplay", "Spawn, movement, camera, minimap, interpolation-compatible updates", "completed", "phase5_verify.py, phase6 G1"),
    ("Gameplay", "Food, eating, XP and water HUD", "partial", "Food type 3 only; water stays at 100 % (no drinking)"),
    ("Gameplay", "Evolution", "partial", "Mouse -> Rabbit -> Pig; thresholds/radii/speeds are documented approximations"),
    ("Gameplay", "Multiplayer visibility in every region", "completed", "phase6 G2 (all regions)"),
    ("Gameplay", "Animals 4+ / per-animal reader fields", "unresolved", "Types 7+ need their readCustomData fields"),
    ("Gameplay", "Predators, combat, HP, death screen, respawn", "unresolved", "S->C 14 (you died) and HP updates not sent"),
    ("Gameplay", "Biomes, water sources, hills, holes, mud, trees", "unresolved", "Single land world; minimap has no shapes"),
    ("Gameplay", "Abilities (W / S keys)", "unresolved", "Ability fields of message 18 are 0; C->S ability opcodes unhandled"),
    ("Gameplay", "Leaderboard, player count, chat, emotes, teams", "unresolved", "See protocol coverage below"),
    ("Assets", "Archived images served to the client", "partial",
     "82 images (snapshot first, then archive/images fallback); most in-game sprites were never archived outside Wayback"),
    ("Assets", "Audio", "unresolved", "No mope.io audio in any archive used; the client logs 404s for audio/*.mp3"),
    ("Config", "Sandbox and developer controls", "partial",
     "Reconstructed only where the client shows evidence (phase4/README.md); server-side dev tools unknown"),
    ("Config", "Original server-side balance data (XP tables, speeds, spawn rates)", "unresolved",
     "Not in the client; local values are configuration, marked as approximations"),
    ("Operations", "start-all.sh: setup, .env, start/stop/restart/status/logs/verify", "completed", "phase6/README.md"),
]


def markdown(data: dict) -> str:
    lines = ["# Historical compatibility report (Phase 6, task 34)", "",
             f"Generated {data['generated_at']} by `phase6_compat.py`. Re-run it after changes; the numbers below "
             "are measured, and the feature matrix is maintained in the script with its evidence.", ""]
    home = data["homepage"]
    lines += ["## 1. Baselines", "",
              "| | |", "|---|---|",
              "| Historical target | 2021-04-23 |",
              "| Closest archived homepage | 2021-04-17 07:27 UTC, Common Crawl revisit (HTTP 304: unchanged since the 2021-03-06 capture), content from Arquivo.pt / Common Crawl March 2021 |",
              "| Client the stack runs | 2025-12-10 Arquivo.pt capture (no 2021 `client.js` exists outside the Wayback Machine) |",
              ""]
    if "element_ids" in home:
        ids = home["element_ids"]
        lines += ["## 2. Homepage: April 2021 vs. the served 2025 page", "",
                  f"- Element ids: {ids['2021']} in 2021, {ids['served_2025']} in the served page, "
                  f"**{len(ids['shared'])} shared**.",
                  f"- Only in 2021 ({len(ids['only_2021'])}): " + (", ".join(f"`{i}`" for i in ids["only_2021"][:60]) or "none")
                  + (" …" if len(ids["only_2021"]) > 60 else ""),
                  f"- Only in 2025 ({len(ids['only_2025'])}): " + (", ".join(f"`{i}`" for i in ids["only_2025"][:60]) or "none")
                  + (" …" if len(ids["only_2025"]) > 60 else ""),
                  "",
                  f"Resources the 2021 homepage references: {len(home['resources_2021'])}; served by the local gateway: "
                  f"**{home['resources_served']}**.", "",
                  "| 2021 resource | Served locally | From | In the 2021 archive |", "|---|---|---|---|"]
        for r in home["resources_2021"]:
            lines.append(f"| `{r['resource'][:80]}` | {'yes' if r['served'] else 'no'} | {r.get('from') or r.get('reason', '')} | "
                         f"{'yes' if r.get('in_2021_archive') else 'no'} |")
        lines.append("")
    else:
        lines += ["## 2. Homepage", "", home.get("error", ""), ""]
    proto = data["protocol"]
    fmt = lambda rows: ", ".join(f"{r['opcode']}" + (f" ({r['name']})" if r["name"] else "") for r in rows) or "none"  # noqa: E731
    lines += ["## 3. Protocol coverage", "",
              "Client side from the Phase 2 inventory of the archived client (`phase2/data/protocol_inventory.json`); "
              "server side measured from `services/region_server.py`.", "",
              f"- Client -> server: the server handles **{proto['coverage']['c2s']}** of the opcodes the client can send.",
              f"  - Handled: {fmt(proto['server_handles'])}",
              f"  - Not handled yet: {fmt(proto['client_sends_unhandled'])}",
              f"- Server -> client: the server sends **{proto['coverage']['s2c']}** of the opcodes the client understands.",
              f"  - Sent: {fmt(proto['server_sends'])}",
              f"  - Never sent yet: {fmt(proto['client_handles_never_sent'])}", ""]
    assets = data["assets"]
    lines += ["## 4. Assets", "",
              f"- The client can build {assets.get('client_references')} image references ({assets.get('client_templates')} "
              f"distinct templates). Non-Wayback archives hold {assets.get('archived_urls')} mope.io images "
              f"({assets.get('archived_versions')} versions); all are served after the snapshot.",
              f"- The live 2026 site snapshot ({assets.get('live_2026_files')} files) is a different build and is not used.",
              "- Audio: none archived.", ""]
    ver = data["verification"]
    lines += ["## 5. Integration status (latest `phase6_verify.py`)", ""]
    if "checks" in ver:
        lines += [f"{ver['passed']}/{ver['passed'] + ver['failed']} checks passed ({ver['generated_at']}).", "",
                  "| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {c['id']} {c['name']} | {'PASS' if c['ok'] else 'FAIL'} | {c['detail'][:160].replace('|', '/')} |"
                  for c in ver["checks"]]
    else:
        lines.append(ver["error"])
    lines += ["", "## 6. Feature matrix", "", "| Area | Feature | Status | Evidence |", "|---|---|---|---|"]
    for area, feature, status, evidence in MATRIX:
        lines.append(f"| {area} | {feature} | **{status}** | {evidence} |")
    counts = {s: sum(1 for m in MATRIX if m[2] == s) for s in ("completed", "partial", "unresolved")}
    lines += ["", f"Totals: {counts['completed']} completed, {counts['partial']} partial, {counts['unresolved']} unresolved.", ""]
    return "\n".join(lines)


def main() -> int:
    config = common.load_stack_config()
    data = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "homepage": homepage_section(config),
        "protocol": protocol_section(),
        "assets": assets_section(),
        "verification": verification_section(),
        "matrix": [{"area": a, "feature": f, "status": s, "evidence": e} for a, f, s, e in MATRIX],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(markdown(data), encoding="utf-8")
    proto = data["protocol"]["coverage"]
    ids = data["homepage"].get("element_ids", {})
    print(f"homepage: {len(ids.get('shared', []))} shared element ids, {len(ids.get('only_2021', []))} only in 2021, "
          f"{len(ids.get('only_2025', []))} only in 2025; 2021 resources served {data['homepage'].get('resources_served')}/"
          f"{len(data['homepage'].get('resources_2021', []))}")
    print(f"protocol: client->server handled {proto['c2s']}, server->client sent {proto['s2c']}")
    print(f"wrote {OUT_MD.relative_to(PROJECT)} and {OUT_JSON.relative_to(PROJECT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
