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

    # -- every ability definition ---------------------------------------------------------------------------
    def abilities_all(self, registry: list[dict]) -> list[dict]:
        """Every place that defines an ability name: `x.abilName = '...'` and `{abilName: '...'}`, with its
        context (switch key, owning class -> animal types, the conditions that select it) and its image."""
        animals_of: dict[str, list[int]] = defaultdict(list)
        for entry in registry:
            if entry["object_type"] == gp.OTYPE_ANIMAL and entry["subtype"] is not None:
                animals_of[entry["class"]].append(entry["subtype"])
        out = []
        for node in self.src.walk():
            name_node = None
            if node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "member_expression" and \
                        self.src.text(left.child_by_field_name("property")) == "abilName":
                    name_node = node.child_by_field_name("right")
            elif node.type == "pair":
                key = node.child_by_field_name("key")
                if key is not None and (self.src.string_value(key) or self.src.text(key)) == "abilName":
                    name_node = node.child_by_field_name("value")
            if name_node is None:
                continue
            row = {"name": self.src.string_value(name_node) if name_node.type == "string" else
                   f"<{self.src.short(name_node, 50)}>", "line": self.src.line(node)}
            image = self._sibling_value(node, "abilImg")
            if image:
                row["image"] = image
            context = self._context(node)
            row.update(context)
            owner = context.get("class")
            if owner:
                row["animal_types"] = sorted(animals_of.get(owner, []))
            out.append(row)
        return out

    def _sibling_value(self, node, key: str) -> str | None:
        scope = node.parent
        for _ in range(3):
            if scope is None:
                break
            for inner in self.src.walk(scope):
                if inner.type == "assignment_expression":
                    left = inner.child_by_field_name("left")
                    if left is not None and left.type == "member_expression" and \
                            self.src.text(left.child_by_field_name("property")) == key:
                        right = inner.child_by_field_name("right")
                        return self.src.string_value(right) if right.type == "string" else f"<{self.src.short(right, 70)}>"
                elif inner.type == "pair":
                    k = inner.child_by_field_name("key")
                    if k is not None and (self.src.string_value(k) or self.src.text(k)) == key:
                        v = inner.child_by_field_name("value")
                        return self.src.string_value(v) if v.type == "string" else f"<{self.src.short(v, 70)}>"
            scope = scope.parent
        return None

    def _context(self, node) -> dict:
        context: dict = {"conditions": []}
        child, parent = node, node.parent
        while parent is not None:
            if parent.type == "switch_case" and "case" not in context:
                value = parent.child_by_field_name("value")
                key = self.ex.resolve(value) if value is not None else None
                context["case"] = key if key is not None else self.src.short(value, 40)
                switch = parent.parent.parent if parent.parent is not None else None
                if switch is not None and switch.type == "switch_statement":
                    context["switch_on"] = self.src.short(switch.child_by_field_name("value"), 50)
            elif parent.type in ("if_statement", "ternary_expression") and len(context["conditions"]) < 3:
                cond = parent.child_by_field_name("condition")
                body = parent.child_by_field_name("consequence")
                negated = body is not None and not (body.start_byte <= child.start_byte < body.end_byte)
                if cond is not None:
                    context["conditions"].append(("NOT " if negated else "") + self._condition_text(cond))
            elif parent.type == "assignment_expression" and "method" not in context:
                left, right = parent.child_by_field_name("left"), parent.child_by_field_name("right")
                if right is not None and right.type in FUNCTION_TYPES and left is not None and left.type == "member_expression":
                    obj = left.child_by_field_name("object")
                    if obj is not None and obj.type == "member_expression" and \
                            self.src.text(obj.child_by_field_name("property")) == "prototype":
                        owner = obj.child_by_field_name("object")
                        context["class"] = self.resolve_alias(self.src.text(owner), parent.start_byte)
                        context["method"] = self.src.text(left.child_by_field_name("property"))
            elif parent.type == "function_declaration" and "method" not in context:
                context["function"] = self.src.text(parent.child_by_field_name("name"))
            child, parent = parent, parent.parent
        if not context["conditions"]:
            del context["conditions"]
        return context

    def _condition_text(self, cond) -> str:
        text = self.src.short(cond, 90)
        for ident in set(re.findall(r"_0x[0-9a-f]+", text)):
            values = self.ex.consts.get(ident)
            if values and len(values) == 1:
                text = text.replace(ident, str(next(iter(values))))
        return text

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


ASSET = re.compile(r"^(img|gameobj|skins|assets)/|\.(png|jpg|svg)$")


def object_catalogue(ev: Evidence, registry: list[dict], classes: dict) -> list[dict]:
    """Every object type id the client uses, with the evidence that says what it is:
    its class and exact appear fields, the asset names drawn for it, an edible-outline variant (food),
    its minimap colour (terrain shown on the minimap) and its biome-skin case."""
    src = ev.src
    strings: dict[str, str] = {}
    for node in src.walk():
        if node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and value is not None and value.type == "string":
                strings.setdefault(src.text(name), src.string_value(value))
    info: dict[int, dict] = defaultdict(lambda: {"assets": set(), "edible_variant": False, "uses": 0,
                                                  "minimap": None, "biome_skin": False})

    def scan(body, types: list[int]):
        assets, edible = set(), False
        for n in src.walk(body):
            if n.type == "string":
                v = src.string_value(n) or ""
                if ASSET.search(v) and len(v) > 4:
                    assets.add(v)
            elif n.type == "identifier" and src.text(n) == "isEdibleOutlined":
                edible = True
            elif n.type == "property_identifier" and src.text(n) == "isEdibleOutlined":
                edible = True
        for t in types:
            info[t]["assets"] |= assets
            info[t]["edible_variant"] |= edible or any(a.endswith("_e") or "_e." in a for a in assets)
            info[t]["uses"] += 1

    for node in src.walk():
        if node.type == "switch_statement" and "oType" in src.text(node.child_by_field_name("value")):
            fn = ecp.enclosing(node, FUNCTION_TYPES + ("method_definition",))
            fn_name = ecp.function_name(src, fn) if fn else ""
            pending: list[int] = []
            for case in node.child_by_field_name("body").named_children:
                if case.type != "switch_case":
                    continue
                value = ev.ex.resolve(case.child_by_field_name("value"))
                if isinstance(value, int):
                    pending.append(value)
                    if fn_name and "getBiomeSkin" in fn_name:
                        info[value]["biome_skin"] = True
                statements = [c for c in case.named_children if c != case.child_by_field_name("value")]
                if statements:
                    for stmt in statements:
                        scan(stmt, pending)
                    pending = []
        elif node.type == "binary_expression":
            op = next((c for c in node.children if c.type in ("==", "===")), None)
            if op is None:
                continue
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None or not src.text(left).endswith(".oType"):
                continue
            value = ev.ex.resolve(right)
            if not isinstance(value, int):
                continue
            guard = node.parent
            while guard is not None and guard.type in ("parenthesized_expression", "binary_expression"):
                guard = guard.parent
            body = None
            if guard is not None and guard.type == "if_statement":
                body = guard.child_by_field_name("consequence")
            if body is not None and body.end_byte - body.start_byte < 3000:
                scan(body, [value])
            else:
                info[value]["uses"] += 1
    # minimap colours (getColorForObjType)
    for node in src.walk():
        if node.type == "method_definition" and src.text(node.child_by_field_name("name")) == "getColorForObjType":
            switch = next((n for n in src.walk(node) if n.type == "switch_statement"), None)
            pending = []
            for case in switch.child_by_field_name("body").named_children if switch else []:
                value = ev.ex.resolve(case.child_by_field_name("value")) if case.type == "switch_case" else None
                if isinstance(value, int):
                    pending.append(value)
                ret = next((n for n in src.walk(case) if n.type == "return_statement"), None)
                if ret is not None and pending:
                    colours = []
                    for n in src.walk(case):
                        if n.type == "return_statement" and n.named_children:
                            v = n.named_children[0]
                            colours.append(src.string_value(v) if v.type == "string" else strings.get(src.text(v), src.text(v)))
                    for t in pending:
                        info[t]["minimap"] = colours[-1] if colours else None
                        if len(colours) > 1:
                            info[t]["minimap_by_biome"] = colours[:-1]
                    pending = []
    # classes: their own image names
    by_type: dict[int, list[dict]] = defaultdict(list)
    for entry in registry:
        by_type[entry["object_type"]].append(entry)
    for t, entries in by_type.items():
        if t == gp.OTYPE_ANIMAL:
            continue
        for entry in entries:
            for fn in ev.methods.get(entry["class"], {}).values():
                scan(fn, [t])
    rows = []
    for t in sorted(set(info) | set(by_type)):
        entries = by_type.get(t, [])
        cls = entries[0]["class"] if entries else None
        reader = classes.get(cls, {}).get("readCustomData_onNewlyVisible") if cls else None
        assets = sorted(info[t]["assets"])
        names = sorted({re.sub(r"(_e)?\.(png|jpg|svg)$", "", a.split("/")[-1]) or a.split("/")[-2]
                        for a in assets if a.startswith(("img/", "skins/", "assets/"))} - {""})
        rows.append({
            "object_type": t, "classes": [e["class"] for e in entries], "subtypes": sorted(e["subtype"] for e in entries
                                                                                          if e["subtype"] is not None),
            "appear_reads": [dict(op=r["op"], **({"flags": r["flags"]} if r.get("flags") else {}))
                             for r in flatten(reader)] if reader else [],
            "assets": assets[:12], "label": ", ".join(names[:3]) if names else
            (f"gameobj/{t}" if any(a.startswith("gameobj/") for a in assets) else ""),
            "edible_variant": info[t]["edible_variant"], "minimap": info[t]["minimap"],
            **({"minimap_by_biome": info[t]["minimap_by_biome"]} if "minimap_by_biome" in info[t] else {}),
            "biome_skin": info[t]["biome_skin"], "references": info[t]["uses"],
        })
    for row in rows:
        row["category"] = categorise_object(row)
    return rows


def categorise_object(row: dict) -> str:
    t, assets = row["object_type"], " ".join(row["assets"])
    if t == gp.OTYPE_ANIMAL:
        return "animal"
    if row["edible_variant"]:
        return "food"
    if row["minimap"] is not None:
        return "terrain"
    if re.search(r"skins/|abilit|disasters/|raindrop|spit|meteorite|viralpandemic|camera", assets):
        return "effect"
    if row["biome_skin"]:
        return "terrain"
    return "object"


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
    abilities = ev.abilities_all(registry)
    catalogue = object_catalogue(ev, registry, classes)
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
            "object_type_ids": len(catalogue),
            "ability_definitions": len(abilities),
            "ability_names": len({a["name"] for a in abilities}),
            "field_switches": len(switches),
            "client_to_server_opcodes": sum(1 for o in inventory.get("opcodes", []) if o.get("client_to_server")),
        },
        "local": local,
        "registry": registry,
        "classes": classes,
        "animals": ledger_animals,
        "abilities": abilities,
        "objects": objects,
        "catalogue": catalogue,
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
        f"| Ability definitions (name, image) | {c['abilities']} definitions, **{c['ability_names']} distinct names** "
        f"| none implemented |",
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
    cat = data["catalogue"]
    counts = defaultdict(int)
    for row in cat:
        counts[row["category"]] += 1
    lines += ["", "## Object catalogue (every object type id the client uses)", "",
              f"{len(cat)} object type ids: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ". The label comes "
              "from the asset names the client draws for the type; *food* = it has an edible-outline (`_e`) variant; "
              "*terrain* = shown on the minimap or drawn with a biome skin; *effect* = drawn from an animal's or a "
              "disaster's assets (not placed in the world on its own).", "",
              "| Type | Category | Label (client assets) | Class | Extra fields on appear | Minimap colour |",
              "|---|---|---|---|---|---|"]
    for row in cat:
        reads = "+".join(r["op"] + ("?" if r.get("flags") else "") for r in row["appear_reads"]) or "-"
        lines.append(f"| {row['object_type']} | {row['category']} | {row['label'][:50]} | "
                     f"{'`' + row['classes'][0] + '`' if row['classes'] else 'base'} | {reads[:60]} | {row['minimap'] or ''} |")
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
    abilities = data["abilities"]
    table = [a for a in abilities if "case" in a and not a.get("class")]
    per_animal = [a for a in abilities if a.get("class")]
    other = [a for a in abilities if a not in table and a not in per_animal]
    lines += ["", "## Abilities", "",
              f"{len(abilities)} definitions, {len({a['name'] for a in abilities})} distinct names: {len(table)} in the "
              f"ability-type table, {len(per_animal)} in per-animal `getAbilityInfo` methods, {len(other)} elsewhere "
              "(the helper that adds the universal Dash).", "",
              "### Ability-type table", "", "| Key | Name | Image |", "|---|---|---|"]
    for a in sorted(table, key=lambda x: (str(type(x["case"])), x["case"])):
        lines.append(f"| {a['case']} | {a['name']} | {str(a.get('image', ''))[:60]} |")
    lines += ["", "### Per-animal abilities", "", "| Animal types | Class | Name | Selected when | Line |", "|---|---|---|---|---|"]
    for a in sorted(per_animal, key=lambda x: (x.get("animal_types") or [999], x["line"])):
        types = ", ".join(map(str, a.get("animal_types") or [])) or "(base class: by animal type)"
        when = "; ".join(a.get("conditions", []))[:90].replace("|", "/")
        lines.append(f"| {types} | `{a['class']}` | {a['name']} | {when} | {a['line']} |")
    lines += ["", "### Elsewhere", ""]
    lines += [f"- L{a['line']} {a['name']} ({a.get('function') or a.get('method') or ''})" for a in other]
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
