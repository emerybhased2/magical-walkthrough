#!/usr/bin/env python3
"""
extract_gameplay_evidence.py -- Phase 5 task 29A: the gameplay evidence ledger.

Reads the readable archived client (rebuild_client_js.py output) with tree-sitter and records
everything the client fixes exactly about gameplay, so the server can be reconstructed exactly where
the evidence allows and marked as an approximation where it does not:

  registry     every setCustomClassForGameObjType(class, objectType[, subtype]) call: which class
               the client builds for each object type (and, for animals, each animal type)
  classes      the class chain (X.prototype = Object.create(Y.prototype)) and, per registered class,
               the EFFECTIVE readers the server must satisfy: readCustomData_onNewlyVisible,
               readUpdatedProperty (+ property ids), readUpdatedFlags -- resolved through superclass
               calls (Parent.prototype.m.call(this, ...))
  animals      the animal table (switch setting aniName/skinName/aniCol)
  abilities    the ability table (switch setting abilName/abilImg)
  switches     field switches that read from a packet (e.g. the world-update header fields)
  coverage     what the local server implements today (object types, animals, messages)

Outputs phase5/data/gameplay_evidence.json and phase5/EVIDENCE.md. No client code runs.

Usage: python extract_gameplay_evidence.py [--client path/to/readable/client.js]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import extract_client_protocol as ecp  # noqa: E402
from services import common, game_protocol as gp  # noqa: E402

DEFAULT_CLIENT = PROJECT / "snapshots/2025-12-10/working/client-js/20251210042757_arquivo/client.js"
READERS = ("readCustomData_onNewlyVisible", "readUpdatedProperty", "readUpdatedFlags", "readCustomStaticData")
FUNCTION_TYPES = ("function", "function_expression", "arrow_function", "generator_function")


class Evidence:
    def __init__(self, client: Path):
        self.src = ecp.Source(client)
        self.ex = ecp.Extractor(self.src)
        self.ex.collect_constants()
        self.parents: dict[str, str] = {}
        self.methods: dict[str, dict[str, object]] = defaultdict(dict)   # class -> method -> function node
        self._collect_classes()

    # -- class structure -------------------------------------------------------------------------------
    def _collect_classes(self) -> None:
        # The obfuscated client reuses alias variables before each class block:
        #   var _0x244a35 = <Class>, _0x3c3358 = <Parent>;  _0x244a35.prototype = Object.create(_0x3c3358.prototype)
        # so identifiers are resolved by source position (the latest preceding alias assignment wins).
        self.aliases: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for node in self.src.walk():
            if node.type == "variable_declarator":
                name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if name is not None and value is not None and name.type == "identifier" and value.type == "identifier":
                    self.aliases[self.src.text(name)].append((node.start_byte, self.src.text(value)))
            elif node.type == "assignment_expression":
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                if left is not None and right is not None and left.type == "identifier" and right.type == "identifier":
                    self.aliases[self.src.text(left)].append((node.start_byte, self.src.text(right)))
        for entries in self.aliases.values():
            entries.sort()
        for node in self.src.walk():
            if node.type != "assignment_expression":
                continue
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None or left.type != "member_expression":
                continue
            obj, prop = left.child_by_field_name("object"), left.child_by_field_name("property")
            if obj is None or prop is None:
                continue
            at = node.start_byte
            if obj.type == "identifier" and self.src.text(prop) == "prototype":
                owner = self.resolve_alias(self.src.text(obj), at)
                # X.prototype = Object.create(Y.prototype)
                if right.type == "call_expression" and self.src.text(right.child_by_field_name("function")) == "Object.create":
                    args = ecp.call_args(right)
                    if args and args[0].type == "member_expression":
                        parent = args[0].child_by_field_name("object")
                        if parent is not None and parent.type == "identifier":
                            self.parents[owner] = self.resolve_alias(self.src.text(parent), at)
                # X.prototype = { m: function () {...}, ... }
                elif right.type == "object":
                    for pair in right.named_children:
                        if pair.type == "pair":
                            key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
                            if key is not None and value is not None and value.type in FUNCTION_TYPES:
                                name = self.src.string_value(key) or self.src.text(key)
                                self.methods[owner].setdefault(name, value)
                        elif pair.type == "method_definition":
                            self.methods[owner].setdefault(self.src.text(pair.child_by_field_name("name")), pair)
            # X.superClass = Y (only when no prototype link was found)
            if obj.type == "identifier" and self.src.text(prop) == "superClass" and right.type == "identifier":
                owner = self.resolve_alias(self.src.text(obj), at)
                self.parents.setdefault(owner, self.resolve_alias(self.src.text(right), at))
            # X.prototype.m = function (...) {...}
            if obj.type == "member_expression" and right.type in FUNCTION_TYPES:
                owner, proto = obj.child_by_field_name("object"), obj.child_by_field_name("property")
                if owner is not None and owner.type == "identifier" and proto is not None and \
                        self.src.text(proto) == "prototype":
                    self.methods[self.resolve_alias(self.src.text(owner), at)][self.src.text(prop)] = right

    def resolve_alias(self, name: str, at: int, depth: int = 0) -> str:
        entries = self.aliases.get(name)
        if not entries or depth > 8:
            return name
        previous = [value for pos, value in entries if pos < at]
        if not previous:
            return name
        index = max(i for i, (pos, _) in enumerate(entries) if pos < at)
        return self.resolve_alias(previous[-1], entries[index][0], depth + 1)

    def chain(self, cls: str) -> list[str]:
        out, seen = [cls], {cls}
        while out[-1] in self.parents and self.parents[out[-1]] not in seen:
            seen.add(self.parents[out[-1]])
            out.append(self.parents[out[-1]])
        return out

    @staticmethod
    def params(fn) -> list[str]:
        node = fn.child_by_field_name("parameters")
        if node is None:
            return []
        return [c for c in node.named_children]

    def reader_of(self, cls: str, method: str) -> dict | None:
        """Effective reads of `method` for `cls`: the nearest definition in the chain, plus the parent's
        reads when it calls Parent.prototype.method.call(this, ...)."""
        for index, owner in enumerate(self.chain(cls)):
            fn = self.methods.get(owner, {}).get(method)
            if fn is None:
                continue
            params = [self.src.text(p) for p in self.params(fn)]
            reader = params[-1] if params else None
            body = fn.child_by_field_name("body")
            reads, _ = self.ex.read_sequence(body, reader)
            text = self.src.text(body)
            calls_super = bool(re.search(rf"\.prototype\.{method}\.call\(this", text))
            result = {"defined_in": owner, "line": self.src.line(fn), "reads": [self._op(r) for r in reads
                                                                                   if r.get("op") != "nested"],
                      "nested_calls": [r["call"] for r in reads if r.get("op") == "nested"]}
            if method == "readUpdatedProperty" and params:
                result["property_ids"] = self._compared_ids(body, params[0])
            if method == "readUpdatedFlags":
                result["flag_reads"] = len([r for r in reads if r.get("op") == "BitGroup"])
            if calls_super:
                parent = self.chain(cls)[index + 1] if index + 1 < len(self.chain(cls)) else None
                inherited = self.reader_of(parent, method) if parent else None
                result["calls_super"] = True
                result["inherited"] = inherited
            return result
        return None

    @staticmethod
    def _op(read: dict) -> dict:
        out = {"op": read["op"], "line": read["line"]}
        for key in ("into", "flags", "expression"):
            if key in read:
                out[key] = read[key]
        return out

    def _compared_ids(self, body, param: str) -> list[int]:
        ids = set()
        for node in self.src.walk(body):
            if node.type != "binary_expression":
                continue
            op = next((c for c in node.children if c.type in ("==", "===")), None)
            if op is None:
                continue
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            for a, b in ((left, right), (right, left)):
                if a is not None and self.src.text(a) == param:
                    value = self.ex.resolve(b)
                    if isinstance(value, int):
                        ids.add(value)
        return sorted(ids)

    # -- registry ----------------------------------------------------------------------------------------
    def registry(self) -> list[dict]:
        out = []
        for node in self.src.walk():
            if node.type != "call_expression":
                continue
            _, prop = ecp.callee_parts(self.src, node)
            if prop != "setCustomClassForGameObjType":
                continue
            args = ecp.call_args(node)
            if len(args) < 2 or args[0].type != "identifier":
                continue
            cls = self.resolve_alias(self.src.text(args[0]), node.start_byte)
            otype = self.ex.resolve(args[1])
            subtype = self.ex.resolve(args[2]) if len(args) > 2 else None
            if otype is None:
                continue
            out.append({"object_type": otype, "subtype": subtype, "class": cls, "line": self.src.line(node),
                        "chain": self.chain(cls)})
        out.sort(key=lambda r: (r["object_type"], r["subtype"] if r["subtype"] is not None else -1))
        return out

    # -- tables built by switch statements ------------------------------------------------------------------
    def switch_tables(self, keys: tuple[str, ...], marker: str) -> list[dict]:
        """Rows of the largest switch whose cases assign `marker` (plus `keys`) as string literals."""
        best: list[dict] = []
        for node in self.src.walk():
            if node.type != "switch_statement":
                continue
            body = node.child_by_field_name("body")
            rows, pending = [], []
            for case in (body.named_children if body is not None else []):
                if case.type != "switch_case":
                    continue
                value = case.child_by_field_name("value")
                key = self.ex.resolve(value) if value is not None else None
                if key is not None:
                    pending.append(key)
                fields = {}
                for inner in self.src.walk(case):
                    if inner.type != "assignment_expression":
                        continue
                    left, right = inner.child_by_field_name("left"), inner.child_by_field_name("right")
                    if left is None or left.type != "member_expression":
                        continue
                    name = self.src.text(left.child_by_field_name("property"))
                    if name in keys:
                        fields[name] = self.src.string_value(right) if right.type == "string" else \
                            f"<{self.src.short(right, 50)}>"
                has_statements = any(c.type not in ("case", ":", "comment") and c != value for c in case.children)
                if marker in fields:
                    for k in pending:
                        rows.append({"id": k, "line": self.src.line(case), **fields})
                    pending = []
                elif has_statements:
                    pending = []
            if len(rows) > len(best):
                best = rows
        return sorted(best, key=lambda r: r["id"])

    def field_switches(self, minimum_cases: int = 8) -> list[dict]:
        """Switches whose cases read packet fields (e.g. world-update header fields)."""
        out = []
        for node in self.src.walk():
            if node.type != "switch_statement":
                continue
            body = node.child_by_field_name("body")
            cases = []
            for case in (body.named_children if body is not None else []):
                if case.type != "switch_case":
                    continue
                value = case.child_by_field_name("value")
                key = self.ex.resolve(value) if value is not None else None
                reads, _ = self.ex.read_sequence(case, None)
                reads = [self._op(r) for r in reads if r.get("op") != "nested"]
                if key is not None and reads:
                    cases.append({"id": key, "reads": [r["op"] for r in reads], "line": self.src.line(case)})
            if len(cases) >= minimum_cases:
                fn = ecp.enclosing(node, FUNCTION_TYPES + ("function_declaration", "method_definition"))
                out.append({"line": self.src.line(node), "function": ecp.function_name(self.src, fn) if fn else None,
                            "discriminant": self.src.short(node.child_by_field_name("value"), 60), "cases": cases})
        return out


def field_chains(ev: Evidence, minimum: int = 6) -> list[dict]:
    """Functions (field, reader) that read packet fields in an if/else chain on the field id
    (the world-update header parser is written this way)."""
    out = []
    for node in ev.src.walk():
        if node.type not in FUNCTION_TYPES:
            continue
        params = [ev.src.text(p) for p in Evidence.params(node)]
        if len(params) < 2:
            continue
        field, reader = params[0], params[1]
        cases = {}
        for inner in ev.src.walk(node.child_by_field_name("body")):
            if inner.type != "if_statement":
                continue
            cond = inner.child_by_field_name("condition")
            cond = cond.named_children[0] if cond is not None and cond.type == "parenthesized_expression" and cond.named_children else cond
            if cond is None or cond.type != "binary_expression":
                continue
            left, right = cond.child_by_field_name("left"), cond.child_by_field_name("right")
            if left is None or ev.src.text(left) != field:
                continue
            key = ev.ex.resolve(right)
            if key is None or key in cases:
                continue
            reads, _ = ev.ex.read_sequence(inner.child_by_field_name("consequence"), reader)
            cases[key] = {"id": key, "reads": [r["op"] for r in reads if r.get("op") != "nested"], "line": ev.src.line(inner)}
        if len(cases) >= minimum:
            out.append({"line": ev.src.line(node), "function": ecp.function_name(ev.src, node),
                        "discriminant": field, "cases": sorted(cases.values(), key=lambda c: c["id"])})
    return out


def local_coverage(config: dict) -> dict:
    game = config.get("gameplay", {})
    object_types = {gp.OTYPE_ANIMAL, int(game.get("food_object_type", 3))}
    if int(game.get("pumpkins", {}).get("count", 0)):
        object_types.add(gp.OTYPE_PUMPKIN)
    animals = sorted({int(t["animal"]) for t in game.get("evolution", [])})
    return {"object_types": sorted(object_types), "animal_types": animals}


def build(client: Path) -> dict:
    ev = Evidence(client)
    registry = ev.registry()
    classes = {}
    for entry in registry:
        cls = entry["class"]
        if cls in classes:
            continue
        classes[cls] = {"chain": ev.chain(cls), **{m: ev.reader_of(cls, m) for m in READERS}}
    animals = ev.switch_tables(("aniName", "skinName", "aniCol"), "aniName")
    abilities = ev.switch_tables(("abilName", "abilImg"), "abilName")
    switches = ev.field_switches() + field_chains(ev)
    inventory = json.loads((PROJECT / "phase2" / "data" / "protocol_inventory.json").read_text(encoding="utf-8")) \
        if (PROJECT / "phase2" / "data" / "protocol_inventory.json").is_file() else {}
    config = common.load_stack_config()
    local = local_coverage(config)
    animal_registry = {e["subtype"]: e for e in registry if e["object_type"] == gp.OTYPE_ANIMAL and e["subtype"] is not None}
    ledger_animals = []
    for row in animals:
        reg = animal_registry.get(row["id"])
        reader = classes.get(reg["class"], {}).get("readCustomData_onNewlyVisible") if reg else None
        ledger_animals.append({
            "id": row["id"], "name": row.get("aniName"), "skin": row.get("skinName"), "colour": row.get("aniCol"),
            "class": reg["class"] if reg else None,
            "custom_reads": len(flatten(reader)) if reader else 0,
            "implemented": row["id"] in local["animal_types"],
        })
    objects = []
    for entry in registry:
        if entry["object_type"] == gp.OTYPE_ANIMAL:
            continue
        info = classes.get(entry["class"], {})
        reader = info.get("readCustomData_onNewlyVisible")
        objects.append({
            "object_type": entry["object_type"], "subtype": entry["subtype"], "class": entry["class"],
            "line": entry["line"], "custom_reads": [r["op"] for r in flatten(reader)] if reader else [],
            "update_properties": (info.get("readUpdatedProperty") or {}).get("property_ids", []),
            "implemented": entry["object_type"] in local["object_types"],
        })
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "client": str(client.relative_to(PROJECT)) if client.is_relative_to(PROJECT) else str(client),
        "counts": {
            "registered_classes": len(registry), "object_types": len({e["object_type"] for e in registry}),
            "animal_classes": len(animal_registry), "animal_table": len(animals), "abilities": len(abilities),
            "field_switches": len(switches),
            "client_to_server_opcodes": sum(1 for o in inventory.get("opcodes", []) if o.get("client_to_server")),
        },
        "local": local,
        "registry": registry,
        "classes": classes,
        "animals": ledger_animals,
        "abilities": abilities,
        "objects": objects,
        "switches": switches,
    }


def flatten(reader: dict | None) -> list[dict]:
    if not reader:
        return []
    inherited = flatten(reader.get("inherited")) if reader.get("calls_super") else []
    return inherited + reader.get("reads", [])


def markdown(data: dict) -> str:
    c = data["counts"]
    local = data["local"]
    lines = [
        "# Gameplay evidence ledger (Phase 5, task 29A)", "",
        f"Generated {data['generated_at']} by `extract_gameplay_evidence.py` from `{data['client']}` "
        "(syntax tree only; no client code runs). The JSON with every read sequence is "
        "[`data/gameplay_evidence.json`](data/gameplay_evidence.json).", "",
        "## What the client fixes exactly", "",
        "| Evidence | Count | Status in the local server |", "|---|---|---|",
        f"| Object classes registered (`setCustomClassForGameObjType`) | {c['registered_classes']} classes, "
        f"{c['object_types']} object types | {len(local['object_types'])} object types implemented "
        f"({', '.join(map(str, local['object_types']))}) |",
        f"| Animal classes (object type 2 + animal type) | {c['animal_classes']} | animal types "
        f"{', '.join(map(str, local['animal_types']))} implemented |",
        f"| Animal table (name, skin, colour) | {c['animal_table']} | names used by the client itself |",
        f"| Ability table (name, image) | {c['abilities']} | none implemented |",
        f"| Packet field switches (e.g. world-update header) | {c['field_switches']} | header fields 1, 3, 11, 16 sent |",
        f"| Client -> server messages | {c['client_to_server_opcodes']} | see phase6/COMPATIBILITY.md |",
        "",
        "Exact means the byte layout and meaning come from the client's own reader code. **Not in the client "
        "(server-side, unknown):** movement speeds per animal, XP thresholds and rewards, damage, HP regeneration, "
        "spawn rates, food values, ability cooldowns and strengths. These stay configuration values marked as "
        "approximations.", "",
        "## Animals: table, class and custom reader size", "",
        "| Id | Name | Skin | Class | Custom reads on appear | Implemented |", "|---|---|---|---|---|---|",
    ]
    for a in data["animals"]:
        lines.append(f"| {a['id']} | {a['name']} | `{a['skin']}` | `{a['class'] or '-'}` | {a['custom_reads']} | "
                     f"{'yes' if a['implemented'] else ''} |")
    lines += ["", "## Object types: class and exact custom fields", "",
              "Fields read after the common object fields when an object becomes visible (the effective "
              "`readCustomData_onNewlyVisible`, superclass calls resolved), and the property ids its "
              "`readUpdatedProperty` accepts.", "",
              "| Type | Subtype | Class | Custom fields on appear | Update properties | Implemented |",
              "|---|---|---|---|---|---|"]
    for o in data["objects"]:
        fields = ", ".join(o["custom_reads"]) if o["custom_reads"] else "none"
        props = ", ".join(map(str, o["update_properties"])) if o["update_properties"] else ""
        lines.append(f"| {o['object_type']} | {o['subtype'] if o['subtype'] is not None else ''} | `{o['class']}` | "
                     f"{fields[:120]} | {props} | {'yes' if o['implemented'] else ''} |")
    lines += ["", "## Abilities (client table)", "", "| Key | Name | Image |", "|---|---|---|"]
    for a in data["abilities"]:
        lines.append(f"| {a['id']} | {a.get('abilName', '')} | {str(a.get('abilImg', ''))[:60]} |")
    lines += ["", "## Packet field switches", ""]
    for s in data["switches"]:
        ids = ", ".join(f"{k['id']}:{'+'.join(k['reads'])}" for k in s["cases"][:40])
        lines.append(f"- L{s['line']} `{s['function'] or '?'}` switch on `{s['discriminant']}`: {len(s['cases'])} "
                     f"cases — {ids}")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Phase 5 gameplay evidence ledger.")
    parser.add_argument("--client", default=str(DEFAULT_CLIENT))
    args = parser.parse_args(argv)
    client = Path(args.client).resolve()
    if not client.is_file():
        print(f"error: readable client not found: {client} (run rebuild_client_js.py)", file=sys.stderr)
        return 2
    started = time.time()
    data = build(client)
    out_json = PROJECT / "phase5" / "data" / "gameplay_evidence.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    (PROJECT / "phase5" / "EVIDENCE.md").write_text(markdown(data), encoding="utf-8")
    c = data["counts"]
    print(f"{c['registered_classes']} registered classes over {c['object_types']} object types, "
          f"{c['animal_classes']} animal classes, {c['animal_table']} animals in the table, {c['abilities']} abilities, "
          f"{c['field_switches']} field switches ({time.time() - started:.1f}s)")
    print("wrote phase5/data/gameplay_evidence.json and phase5/EVIDENCE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
