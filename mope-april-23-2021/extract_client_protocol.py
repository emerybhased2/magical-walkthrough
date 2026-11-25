#!/usr/bin/env python3
"""
extract_client_protocol.py -- Phase 2 (tasks 9-13): inventory the networking surface of a
mope.io client.js from its syntax tree.

Works on the readable rebuild written by rebuild_client_js.py (it also works on the
original, which has the same syntax tree). Produces JSON describing:

  * opcodes          numeric message-type constants and where they are used
  * message classes  `class X extends Base { static type = N; static send()/handle() }`
                     with the ordered binary layout (writeUInt8/readUInt16/... calls)
                     and, for incoming messages, the real field names they return
  * dispatcher       the client's onmessage switch: opcode -> handler
  * raw sends        other packets built with the binary writer (opcode + layout)
  * http             $.ajax / fetch calls (URL expression, method, payload keys)
  * websockets       `new WebSocket(...)` sites and how their URLs are built
  * config           the client's $config block, every $config.X use, region list

Everything references line numbers in the analysed file. Obfuscated identifiers
(_0x....) are kept as-is; names come only from the code itself (returned field names,
property names, strings), never invented.

Usage:
  python extract_client_protocol.py --project snapshots/2025-12-10
  python extract_client_protocol.py --client path/to/client.js --out-dir somewhere/
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import rebuild_client_js as rb  # noqa: E402  (parser, string decoding, $config extraction)

VERSION = "1.0.0"
FUNCTION_TYPES = ("function_declaration", "function_expression", "function", "arrow_function",
                  "method_definition", "generator_function_declaration")
CONTROL_TYPES = ("if_statement", "ternary_expression", "for_statement", "for_in_statement",
                 "while_statement", "do_statement", "switch_case", "binary_expression")
LOOP_TYPES = ("for_statement", "for_in_statement", "while_statement", "do_statement")


class Source:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        self.tree = rb.make_parser().parse(self.data)
        if rb.has_errors(self.tree):
            raise SystemExit(f"error: {path} does not parse cleanly ({rb.first_error(self.tree, self.data)})")

    def text(self, node) -> str:
        return rb.node_text(node, self.data)

    def short(self, node, limit: int = 160) -> str:
        value = re.sub(r"\s+", " ", self.text(node)).strip()
        return value if len(value) <= limit else value[:limit - 3] + "..."

    @staticmethod
    def line(node) -> int:
        return node.start_point[0] + 1

    def walk(self, root=None):
        stack = [root or self.tree.root_node]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def string_value(self, node):
        return rb.decode_js_string(self.text(node)) if node is not None and node.type == "string" else None


def callee_parts(src: Source, call) -> tuple[str | None, str | None]:
    """(object text, property) for obj.prop(...) calls; (name, None) for plain calls."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None, None
    if fn.type == "member_expression":
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        return (src.text(obj) if obj is not None else None), (src.text(prop) if prop is not None else None)
    return src.text(fn), None


def call_args(call) -> list:
    args = call.child_by_field_name("arguments")
    return [] if args is None else [a for a in args.named_children if a.type != "comment"]


def enclosing(node, types) -> object | None:
    node = node.parent
    while node is not None and node.type not in types:
        node = node.parent
    return node


def function_name(src: Source, node) -> str | None:
    fn = node if node.type in FUNCTION_TYPES else enclosing(node, FUNCTION_TYPES)
    while fn is not None:
        name = fn.child_by_field_name("name")
        if name is not None:
            cls = enclosing(fn, ("class_declaration", "class"))
            cls_name = cls.child_by_field_name("name") if cls is not None else None
            prefix = (src.text(cls_name) + ".") if cls_name is not None and fn.type == "method_definition" else ""
            return prefix + src.text(name)
        parent = fn.parent
        if parent is not None and parent.type == "variable_declarator":
            return src.text(parent.child_by_field_name("name"))
        if parent is not None and parent.type in ("assignment_expression", "pair"):
            target = parent.child_by_field_name("left") or parent.child_by_field_name("key")
            return src.short(target, 60) if target is not None else None
        fn = enclosing(fn, FUNCTION_TYPES)
    return None


def nearby_strings(src: Source, scope, limit: int = 6) -> list[str]:
    """Human-readable string literals inside a scope (console messages, UI text, URLs)."""
    found = []
    for node in src.walk(scope):
        if node.type == "string":
            value = src.string_value(node) or ""
            if len(value) >= 4 and re.search(r"[A-Za-z]{3}", value) and value not in found:
                found.append(value[:100])
                if len(found) >= limit:
                    break
    return found


def flags_for(node, stop) -> list[str]:
    flags = set()
    cur = node.parent
    while cur is not None and cur is not stop and not (cur.start_byte == stop.start_byte and cur.type == stop.type):
        if cur.type in LOOP_TYPES:
            flags.add("in_loop")
        elif cur.type in ("if_statement", "ternary_expression", "switch_case"):
            flags.add("conditional")
        elif cur.type == "binary_expression" and any(c.type in ("&&", "||") for c in cur.children):
            flags.add("conditional")
        cur = cur.parent
    return sorted(flags)


# =============================================================================
# Extraction
# =============================================================================


class Extractor:
    def __init__(self, src: Source):
        self.src = src
        self.consts: dict[str, set] = defaultdict(set)
        self.const_lines: dict[str, int] = {}
        self.classes: dict[str, dict] = {}
        self.writer_names: set[str] = set()
        self.reader_names: set[str] = set()

    # -- basics ---------------------------------------------------------------
    def collect_constants(self) -> None:
        for node in self.src.walk():
            if node.type == "variable_declarator":
                name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if name is not None and value is not None and name.type == "identifier" and value.type == "number":
                    try:
                        number = rb.js_number(self.src.text(value))
                    except ValueError:
                        continue
                    if isinstance(number, float) and number.is_integer():
                        self.consts[self.src.text(name)].add(int(number))
                        self.const_lines.setdefault(self.src.text(name), self.src.line(node))

    def resolve(self, node) -> int | str | None:
        """Number for a literal, a constant identifier or `Class.type`; else None."""
        if node is None:
            return None
        if node.type == "number":
            value = rb.js_number(self.src.text(node))
            return int(value) if isinstance(value, float) and value.is_integer() else None
        if node.type == "identifier":
            values = self.consts.get(self.src.text(node))
            if values and len(values) == 1:
                return next(iter(values))
            return None
        if node.type == "member_expression":
            obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
            if prop is not None and self.src.text(prop) == "type" and obj is not None:
                cls = self.classes.get(self.src.text(obj))
                if cls is not None:
                    return cls.get("type")
        return None

    def find_codec_classes(self) -> None:
        """Function-style classes that define this.writeUInt8 / this.readUInt8."""
        for node in self.src.walk():
            if node.type != "function_declaration":
                continue
            body = self.src.text(node)
            name = self.src.text(node.child_by_field_name("name"))
            if "this.writeUInt8 =" in body and "this.writeString =" in body:
                self.writer_names.add(name)
            if "this.readUInt8 =" in body and "this.readString =" in body:
                self.reader_names.add(name)

    # -- message classes -------------------------------------------------------
    def collect_classes(self) -> None:
        for node in self.src.walk():
            if node.type != "class_declaration":
                continue
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)
            info = {"class": self.src.text(name_node), "line": self.src.line(node),
                    "extends": self.src.short(heritage, 60).replace("extends ", "") if heritage is not None else None,
                    "type": None, "methods": {}}
            body = node.child_by_field_name("body")
            for member in (body.named_children if body is not None else []):
                is_static = any(c.type == "static" for c in member.children)
                if member.type == "field_definition" and is_static:
                    prop, value = member.child_by_field_name("property"), member.child_by_field_name("value")
                    if prop is not None and self.src.text(prop) == "type" and value is not None and \
                            value.type == "number":
                        info["type"] = int(rb.js_number(self.src.text(value)))
                elif member.type == "method_definition" and is_static:
                    mname = self.src.text(member.child_by_field_name("name"))
                    if mname in ("send", "handle", "validate"):
                        info["methods"][mname] = member
            self.classes[info["class"]] = info

    def write_sequence(self, scope, writer_var: str | None = None) -> list[dict]:
        out = []
        for node in self.src.walk(scope):
            if node.type != "call_expression":
                continue
            obj, prop = callee_parts(self.src, node)
            if not prop or not re.fullmatch(r"write(UInt8|UInt16|Int16|UInt32|Int32|Float32|String|Bool\w*)", prop):
                continue
            if writer_var is not None and obj != writer_var:
                continue
            args = call_args(node)
            entry = {"op": prop.replace("write", ""), "value": self.src.short(args[0], 80) if args else None,
                     "line": self.src.line(node)}
            literal = self.resolve(args[0]) if args else None
            if literal is not None:
                entry["resolved"] = literal
            flags = flags_for(node, scope)
            if flags:
                entry["flags"] = flags
            out.append(entry)
        return out

    def read_sequence(self, scope, reader_var: str | None = None) -> tuple[list[dict], dict]:
        """Ordered reads plus the object literal the scope returns ({field: expression})."""
        reads, assigned = [], {}
        for node in self.src.walk(scope):
            if node.type != "call_expression":
                continue
            obj, prop = callee_parts(self.src, node)
            if not prop:
                continue
            if re.fullmatch(r"read(UInt8|UInt16|Int16|UInt32|Int32|Float32|String|BitGroup)", prop) and \
                    (reader_var is None or obj == reader_var):
                target = None
                parent = node.parent
                while parent is not None and parent.type in ("binary_expression", "parenthesized_expression",
                                                             "unary_expression", "call_expression"):
                    parent = parent.parent
                if parent is not None and parent.type == "variable_declarator":
                    target = self.src.text(parent.child_by_field_name("name"))
                elif parent is not None and parent.type == "assignment_expression":
                    target = self.src.short(parent.child_by_field_name("left"), 60)
                entry = {"op": prop.replace("read", ""), "line": self.src.line(node)}
                if target:
                    entry["into"] = target
                    assigned[target] = entry
                if self.src.text(node.parent) != self.src.text(node) and node.parent.type == "binary_expression":
                    entry["expression"] = self.src.short(node.parent, 60)
                flags = flags_for(node, scope)
                if flags:
                    entry["flags"] = flags
                reads.append(entry)
            elif prop not in ("push", "log", "error") and any(
                    a.type == "identifier" and self.src.text(a) == reader_var for a in call_args(node)):
                reads.append({"op": "nested", "call": self.src.short(node.child_by_field_name("function"), 60),
                              "line": self.src.line(node), **({"flags": flags_for(node, scope)}
                                                              if flags_for(node, scope) else {})})
        returned = {}
        for node in self.src.walk(scope):
            if node.type == "return_statement":
                value = next((c for c in node.named_children), None)
                if value is not None and value.type == "object":
                    for pair in value.named_children:
                        if pair.type == "pair":
                            key = pair.child_by_field_name("key")
                            val = pair.child_by_field_name("value")
                            returned[self.src.string_value(key) or self.src.text(key)] = self.src.short(val, 60)
                        elif pair.type == "shorthand_property_identifier":
                            returned[self.src.text(pair)] = self.src.text(pair)
        fields = []
        for name, expr in returned.items():
            read = assigned.get(expr)
            fields.append({"name": name, "type": read["op"] if read else None,
                           **({"expression": expr} if read is None else {})})
        return reads, {"fields": fields}

    def describe_classes(self) -> list[dict]:
        out = []
        for info in self.classes.values():
            if info["type"] is None or not info["methods"]:
                continue
            entry = {"class": info["class"], "extends": info["extends"], "type": info["type"], "line": info["line"]}
            send = info["methods"].get("send")
            if send is not None:
                layout = self.write_sequence(send)
                if layout and layout[0]["op"] == "UInt8" and layout[0].get("resolved") == info["type"]:
                    layout = layout[1:]
                entry["client_to_server"] = {"line": self.src.line(send), "layout": layout,
                                             "parameters": self.src.short(send.child_by_field_name("parameters"))}
            handle = info["methods"].get("handle")
            if handle is not None:
                params = handle.child_by_field_name("parameters")
                reader = self.src.text(params.named_children[0]) if params is not None and params.named_children \
                    else None
                reads, returned = self.read_sequence(handle, reader)
                entry["server_to_client"] = {"line": self.src.line(handle), "reads": reads, **returned}
            out.append(entry)
        return sorted(out, key=lambda e: e["type"])

    # -- dispatcher ------------------------------------------------------------
    def find_dispatchers(self) -> list[dict]:
        out = []
        for node in self.src.walk():
            if node.type != "switch_statement":
                continue
            value = node.child_by_field_name("value")
            body = node.child_by_field_name("body")
            if value is None or body is None:
                continue
            disc = self.src.text(value).strip("() ")
            fn = enclosing(node, FUNCTION_TYPES)
            if fn is None or not re.search(rf"\b{re.escape(disc)}\s*=\s*[\w$]+\.readUInt8\(\)", self.src.text(fn)):
                continue
            cases = [c for c in body.named_children if c.type == "switch_case"]
            if len(cases) < 8:
                continue
            reader_match = re.search(rf"\b{re.escape(disc)}\s*=\s*([\w$]+)\.readUInt8\(\)", self.src.text(fn))
            reader_var = reader_match.group(1) if reader_match else None
            entries = []
            for case in cases:
                case_value = case.child_by_field_name("value")
                handlers, calls = [], []
                for sub in self.src.walk(case):
                    if sub.type != "call_expression":
                        continue
                    obj, prop = callee_parts(self.src, sub)
                    if prop == "handle" and obj in self.classes:
                        handlers.append(obj)
                    elif prop in ("handleMessage",) or (prop is None and obj and obj.startswith("_0x")):
                        calls.append(self.src.short(sub.child_by_field_name("function"), 60))
                reads, _ = self.read_sequence(case, reader_var)
                entries.append({
                    "opcode": self.resolve(case_value), "case": self.src.short(case_value, 40) if case_value else
                    "default", "line": self.src.line(case), "handler_classes": handlers,
                    "calls": sorted(set(calls))[:12],
                    "direct_reads": [r for r in reads if r["op"] != "nested"][:40],
                    "strings": nearby_strings(self.src, case, 4),
                })
            out.append({"function": function_name(self.src, node), "line": self.src.line(node),
                        "discriminant": disc, "reader": reader_var, "cases": entries, "_node": node})
        # A switch inside another dispatcher's case is a sub-switch of that opcode
        # (e.g. status-effect kinds), not a separate message dispatcher.
        for dispatcher in out:
            parent = dispatcher["_node"].parent
            while parent is not None:
                if parent.type == "switch_case":
                    outer = next((d for d in out if d is not dispatcher and
                                  d["_node"].start_byte < parent.start_byte < d["_node"].end_byte), None)
                    if outer is not None:
                        dispatcher["nested_in_opcode"] = self.resolve(parent.child_by_field_name("value"))
                        break
                parent = parent.parent
            dispatcher["role"] = "sub-switch" if "nested_in_opcode" in dispatcher else (
                "message dispatcher" if dispatcher["function"] and "handleMessage" not in dispatcher["function"]
                else "sub-protocol handler")
        for dispatcher in out:
            del dispatcher["_node"]
        return out

    # -- raw sends -------------------------------------------------------------
    def raw_sends(self) -> list[dict]:
        out = []
        for node in self.src.walk():
            if node.type != "new_expression":
                continue
            ctor = node.child_by_field_name("constructor")
            if ctor is None or self.src.text(ctor) not in self.writer_names:
                continue
            parent = node.parent
            var = None
            if parent.type == "variable_declarator":
                var = self.src.text(parent.child_by_field_name("name"))
            elif parent.type == "assignment_expression":
                var = self.src.text(parent.child_by_field_name("left"))
            scope = enclosing(node, FUNCTION_TYPES)
            if var is None or scope is None:
                continue
            cls = enclosing(scope, ("class_declaration",))
            if cls is not None and scope.type == "method_definition" and \
                    self.src.text(scope.child_by_field_name("name")) == "send":
                continue  # already covered by describe_classes()
            layout = [w for w in self.write_sequence(scope, var) if w["line"] >= self.src.line(node)]
            if not layout:
                continue
            opcode = layout[0].get("resolved") if layout[0]["op"] == "UInt8" else None
            out.append({"opcode": opcode, "opcode_expression": layout[0]["value"], "line": self.src.line(node),
                        "function": function_name(self.src, node),
                        "size_expression": self.src.short(call_args(node)[0], 80) if call_args(node) else None,
                        "layout": layout[1:] if opcode is not None else layout,
                        "strings": nearby_strings(self.src, scope, 4)})
        return out

    # -- HTTP / WebSocket ------------------------------------------------------------
    def http_calls(self) -> list[dict]:
        out = []
        for node in self.src.walk():
            if node.type != "call_expression":
                continue
            obj, prop = callee_parts(self.src, node)
            args = call_args(node)
            if obj == "$" and prop in ("ajax", "get", "post", "getJSON") and args:
                entry = {"api": f"$.{prop}", "line": self.src.line(node), "function": function_name(self.src, node)}
                if args[0].type == "object":
                    for pair in args[0].named_children:
                        if pair.type != "pair":
                            continue
                        key = self.src.string_value(pair.child_by_field_name("key")) or \
                            self.src.text(pair.child_by_field_name("key"))
                        val = pair.child_by_field_name("value")
                        if key == "url":
                            entry[key] = self._url_text(val)
                        elif key in ("type", "method", "dataType", "contentType", "crossDomain"):
                            entry[key] = self.src.short(val, 140)
                        elif key == "data":
                            entry["data"] = self._payload_keys(val)
                        elif key == "xhrFields":
                            entry["xhrFields"] = self.src.short(val, 80)
                        elif key == "headers":
                            entry["headers"] = self.src.short(val, 120)
                else:
                    entry["url"] = self.src.short(args[0], 140)
                out.append(entry)
            elif obj == "fetch" and prop is None and args:
                target = args[0]
                if target.type == "new_expression" and call_args(target):
                    target = call_args(target)[0]  # fetch(new Request(url))
                entry = {"api": "fetch", "line": self.src.line(node), "function": function_name(self.src, node),
                         "url": self._url_text(target)}
                if len(args) > 1 and args[1].type == "object":
                    for pair in args[1].named_children:
                        if pair.type == "pair":
                            key = self.src.text(pair.child_by_field_name("key"))
                            val = pair.child_by_field_name("value")
                            entry[key] = self._payload_keys(val) if key == "body" else self.src.short(val, 120)
                out.append(entry)
        for entry in out:
            entry["url_resolved"] = self._resolve_url(entry.get("url"), entry["line"])
        return out

    def _local_value(self, ident):
        """Expression last assigned to a local identifier before its use, in the enclosing function."""
        name = self.src.text(ident)
        scope = enclosing(ident, FUNCTION_TYPES)
        best = None
        for sub in self.src.walk(scope) if scope is not None else ():
            if sub.start_byte >= ident.start_byte:
                continue
            if sub.type == "variable_declarator" and self.src.text(sub.child_by_field_name("name")) == name:
                best = sub.child_by_field_name("value") or best
            elif sub.type == "assignment_expression" and self.src.text(sub.child_by_field_name("left")) == name:
                best = sub.child_by_field_name("right") or best
        return best

    def _url_text(self, node) -> str:
        if node.type == "identifier":
            value = self._local_value(node)
            if value is not None:
                return self.src.short(value, 140)
        return self.src.short(node, 140)

    def _payload_keys(self, node, depth: int = 0):
        if node is None:
            return None
        if node.type == "identifier" and depth < 2:
            value = self._local_value(node)
            if value is not None:
                return self._payload_keys(value, depth + 1)
        if node.type == "object":
            return {"keys": [self.src.string_value(p.child_by_field_name("key")) or
                             self.src.text(p.child_by_field_name("key")) if p.type == "pair" else self.src.text(p)
                             for p in node.named_children if p.type in ("pair", "shorthand_property_identifier")]}
        if node.type == "call_expression" and "JSON.stringify" in self.src.text(node.child_by_field_name("function")):
            args = call_args(node)
            return {"json": self._payload_keys(args[0]) if args else None}
        return {"expression": self.src.short(node, 120)}

    def _resolve_url(self, expr: str | None, line: int) -> str | None:
        if not expr:
            return None
        value = expr
        for var, label in self.url_bases.items():
            value = re.sub(rf"\b{re.escape(var)}\b", "{" + label + "}", value)
        return value

    def find_url_bases(self) -> None:
        """Variables holding a server base URL (e.g. the account server)."""
        self.url_bases = {}
        for node in self.src.walk():
            if node.type != "variable_declarator":
                continue
            value = node.child_by_field_name("value")
            name = node.child_by_field_name("name")
            if value is None or name is None:
                continue
            text = self.src.text(value)
            if "devAccountServerUrl" in text:
                self.url_bases[self.src.text(name)] = "ACCOUNT_SERVER"
            elif value.type == "string" and re.match(r"https?://[\w.-]+/?$", self.src.string_value(value) or ""):
                self.url_bases.setdefault(self.src.text(name), self.src.string_value(value))

    def websockets(self) -> list[dict]:
        out = []
        for node in self.src.walk():
            if node.type != "new_expression":
                continue
            ctor = node.child_by_field_name("constructor")
            if ctor is None or self.src.text(ctor) != "WebSocket":
                continue
            args = call_args(node)
            entry = {"line": self.src.line(node), "function": function_name(self.src, node),
                     "url_expression": self.src.short(args[0], 100) if args else None}
            scope = enclosing(node, FUNCTION_TYPES)
            if args and args[0].type == "identifier" and scope is not None:
                var = self.src.text(args[0])
                for sub in self.src.walk(scope):
                    if sub.type == "variable_declarator" and self.src.text(sub.child_by_field_name("name")) == var:
                        entry["url_built_as"] = self.src.short(sub.child_by_field_name("value"), 140)
                    elif sub.type == "assignment_expression" and self.src.text(sub.child_by_field_name("left")) == var:
                        entry["url_built_as"] = self.src.short(sub.child_by_field_name("right"), 140)
            if scope is not None:
                handlers = sorted(set(re.findall(r"\.(onopen|onmessage|onclose|onerror)\s*=", self.src.text(scope))))
                entry["handlers"] = handlers
                entry["strings"] = nearby_strings(self.src, scope, 4)
            out.append(entry)
        return out

    def config_uses(self) -> dict:
        uses = defaultdict(list)
        for node in self.src.walk():
            if node.type == "member_expression":
                obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
                if obj is not None and prop is not None and self.src.text(obj) == "$config":
                    uses[self.src.text(prop)].append({"line": self.src.line(node),
                                                      "function": function_name(self.src, node),
                                                      "context": self.src.short(node.parent, 120)})
        return dict(sorted(uses.items()))

    def local_server_hooks(self) -> list[dict]:
        out = []
        for node in self.src.walk():
            if node.type != "call_expression":
                continue
            args = call_args(node)
            values = [self.src.string_value(a) for a in args if a.type == "string"]
            if any(v in ("localhost", "127.0.0.1") for v in values):
                fn = function_name(self.src, node)
                callers = []
                if fn and fn.startswith("_0x") and "." not in fn:
                    callers = [self.src.line(n) for n in self.src.walk() if n.type == "call_expression"
                               and self.src.text(n.child_by_field_name("function")) == fn]
                out.append({"line": self.src.line(node), "call": self.src.short(node, 120), "function": fn,
                            "function_called_at_lines": callers})
        return out


def build_inventory(client: Path) -> dict:
    src = Source(client)
    ex = Extractor(src)
    ex.collect_constants()
    ex.find_codec_classes()
    ex.collect_classes()
    ex.find_url_bases()
    classes = ex.describe_classes()
    dispatchers = ex.find_dispatchers()
    raw = ex.raw_sends()
    config = rb.extract_config(src.tree, src.data)

    # opcode overview
    opcodes: dict[int, dict] = {}

    def slot(code):
        return opcodes.setdefault(code, {"opcode": code, "client_to_server": [], "server_to_client": []})

    for cls in classes:
        if not cls["type"]:
            continue  # base classes (static type = 0)
        if "client_to_server" in cls:
            slot(cls["type"])["client_to_server"].append({"class": cls["class"], "line": cls["line"]})
        if "server_to_client" in cls:
            slot(cls["type"])["server_to_client"].append({
                "class": cls["class"], "line": cls["line"],
                "fields": [f["name"] for f in cls["server_to_client"]["fields"]]})
    for dispatcher in dispatchers:
        if dispatcher.get("role") != "message dispatcher":
            continue  # sub-switch values are not top-level opcodes
        for case in dispatcher["cases"]:
            if isinstance(case["opcode"], int):
                entry = slot(case["opcode"])
                if not any(h.get("dispatch_line") == case["line"] for h in entry["server_to_client"]):
                    entry["server_to_client"].append({"dispatch_line": case["line"],
                                                      "handler_classes": case["handler_classes"]})
    for send in raw:
        if isinstance(send["opcode"], int):
            slot(send["opcode"])["client_to_server"].append({"function": send["function"], "line": send["line"],
                                                             "layout": [w["op"] for w in send["layout"]]})
    names = defaultdict(set)
    for dispatcher in dispatchers:
        if dispatcher.get("role") != "message dispatcher":
            continue
        for case in dispatcher["cases"]:
            if isinstance(case["opcode"], int) and re.fullmatch(r"[\w$]+", case["case"]) and \
                    not case["case"].isdigit():
                names[case["opcode"]].add(case["case"])
    for send in raw:
        if isinstance(send["opcode"], int) and re.fullmatch(r"[\w$]+", send["opcode_expression"] or ""):
            names[send["opcode"]].add(send["opcode_expression"])
    for code, entry in opcodes.items():
        entry["constant_names"] = sorted(names.get(code, ()))

    regions = []
    if isinstance(config, dict):
        for server in config.get("gameServers") or []:
            if isinstance(server, dict):
                regions.append(server)
    return {
        "generated_at": rb.fm.now_iso(), "extractor_version": VERSION,
        "client": {"path": str(client), "sha256": rb.fm.sha256_bytes(src.data), "bytes": len(src.data),
                   "lines": src.data.count(b"\n")},
        "wire_format": {
            "byte_order": "big-endian (DataView setUint16/getUint16 with littleEndian=false)",
            "writer": sorted(ex.writer_names), "reader": sorted(ex.reader_names),
            "notes": [
                "Client->server strings: UInt16 byte length, then UTF-8 bytes (unescape(encodeURIComponent)).",
                "Server->client strings: UInt16 length, then that many bytes; the client DROPS the last byte "
                "(the length includes a trailing terminator), then UTF-8-decodes the rest.",
                "readBitGroup: bytes are read until one has bit 0 clear; bits 1-7 of each byte carry booleans / "
                "small ints (getBool, getIntWithXBits).",
                "Every packet starts with a UInt8 message type (opcode).",
            ],
        },
        "config": config,
        "regions": regions,
        "local_server_hooks": ex.local_server_hooks(),
        "config_uses": ex.config_uses(),
        "opcodes": [opcodes[k] for k in sorted(opcodes)],
        "message_classes": classes,
        "dispatchers": dispatchers,
        "raw_sends": raw,
        "websockets": ex.websockets(),
        "http": ex.http_calls(),
        "url_bases": ex.url_bases,
    }


def _md(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(inv: dict) -> str:
    """Opcode reference generated from the inventory (no hand-written names)."""
    classes = {c["type"]: c for c in inv["message_classes"] if c["type"]}
    main = [d for d in inv["dispatchers"] if d.get("role") == "message dispatcher"]
    subs = [d for d in inv["dispatchers"] if d.get("role") != "message dispatcher"]
    raw = defaultdict(list)
    for send in inv["raw_sends"]:
        if isinstance(send["opcode"], int):
            raw[send["opcode"]].append(send)
    cases = defaultdict(list)
    for dispatcher in main:
        for case in dispatcher["cases"]:
            if isinstance(case["opcode"], int):
                cases[case["opcode"]].append(case)

    def layout(ops):
        return ", ".join(w["op"] + ("?" if "conditional" in w.get("flags", []) else "") +
                         ("[]" if "in_loop" in w.get("flags", []) else "") for w in ops) or "(opcode only)"

    lines = [
        "# mope.io client protocol — opcode reference",
        "",
        f"Generated by `extract_client_protocol.py` {inv['extractor_version']} from `{Path(inv['client']['path']).name}` "
        f"(sha256 `{inv['client']['sha256'][:16]}…`, {inv['client']['lines']} lines). Line numbers refer to that file.",
        "Names come only from the code (returned field names, log strings); obfuscated identifiers are kept.",
        "`?` = written/read conditionally, `[]` = inside a loop. Every packet starts with the UInt8 opcode, "
        "integers are big-endian.",
        "",
        "| Opcode | Client → server (layout after the opcode) | Server → client (fields / reads) | Evidence |",
        "|---:|---|---|---|",
    ]
    for entry in inv["opcodes"]:
        code = entry["opcode"]
        c2s, s2c, evidence = [], [], []
        cls = classes.get(code)
        if cls and "client_to_server" in cls:
            c2s.append(f"`{cls['class']}.send` L{cls['client_to_server']['line']}: "
                       f"{layout(cls['client_to_server']['layout'])}")
        for send in raw.get(code, []):
            c2s.append(f"`{send['function']}` L{send['line']}: {layout(send['layout'])}")
            evidence += send["strings"][:2]
        if cls and "server_to_client" in cls:
            fields = cls["server_to_client"]["fields"]
            if fields:
                s2c.append(f"`{cls['class']}.handle` L{cls['server_to_client']['line']}: " +
                           ", ".join(f"{f['name']}:{f['type'] or 'derived'}" for f in fields))
            else:
                s2c.append(f"`{cls['class']}.handle` L{cls['server_to_client']['line']}: "
                           f"{layout(cls['server_to_client']['reads'])}")
        for case in cases.get(code, []):
            desc = f"case L{case['line']}"
            if case["handler_classes"]:
                desc += " → " + ", ".join(f"`{c}`" for c in case["handler_classes"])
            if case["direct_reads"]:
                desc += ": " + layout(case["direct_reads"])
            if not (cls and "server_to_client" in cls and not case["direct_reads"]):
                s2c.append(desc)
            evidence += case["strings"][:2]
        evidence = list(dict.fromkeys(e for e in evidence if e.strip()))[:3]
        lines.append(f"| {code} | {_md('<br>'.join(c2s)) or ''} | {_md('<br>'.join(s2c)) or ''} | "
                     f"{_md('; '.join(evidence))} |")
    for dispatcher in subs:
        where = (f"inside opcode {dispatcher['nested_in_opcode']}" if dispatcher.get("nested_in_opcode") is not None
                 else f"`{dispatcher['function']}`")
        lines += ["", f"## Sub-switch {where} (L{dispatcher['line']})", "",
                  "| Value | Reads | Evidence |", "|---:|---|---|"]
        for case in dispatcher["cases"]:
            lines.append(f"| {case['opcode'] if case['opcode'] is not None else _md(case['case'])} | "
                         f"{_md(layout(case['direct_reads']) if case['direct_reads'] else '')} | "
                         f"{_md('; '.join(case['strings'][:2]))} |")
    lines += ["", "## HTTP endpoints", "", "| Line | Method | URL | Payload keys |", "|---:|---|---|---|"]
    for call in inv["http"]:
        payload = call.get("data") or call.get("body") or {}
        keys = (payload.get("json") or {}).get("keys") if isinstance(payload.get("json"), dict) else \
            payload.get("keys")
        method = (call.get("type") or call.get("method") or "GET").strip("'\"")
        lines.append(f"| {call['line']} | {method} | `{_md(call.get('url_resolved') or call.get('url'))}` | "
                     f"{_md(', '.join(keys or []))} |")
    lines += ["", "## WebSocket endpoints", ""]
    for sock in inv["websockets"]:
        lines.append(f"* L{sock['line']} `{sock['function']}`: `new WebSocket({_md(sock.get('url_built_as') or sock['url_expression'])})`")
    return "\n".join(lines) + "\n"


def find_client(project: Path) -> Path:
    builds = sorted((project / "working" / "client-js").glob("*/client.js"))
    if not builds:
        raise SystemExit(f"error: no rebuilt client under {project}/working/client-js; run rebuild_client_js.py "
                         f"or pass --client")
    return builds[-1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inventory the networking surface of a mope.io client.js.")
    parser.add_argument("--project", default=str(SCRIPT_DIR), help="project root holding working/client-js/")
    parser.add_argument("--client", help="client.js to analyse (default: newest rebuild in the project)")
    parser.add_argument("--out-dir", help="where to write the JSON (default: <project>/working/phase2)")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    client = Path(args.client).resolve() if args.client else find_client(project)
    out_dir = Path(args.out_dir).resolve() if args.out_dir else project / "working" / "phase2"
    inventory = build_inventory(client)
    rb.fm.write_json(out_dir / "protocol_inventory.json", inventory)
    rb.fm.write_json(out_dir / "regions.json", {"source": inventory["client"], "regions": inventory["regions"],
                                                "local_server_hooks": inventory["local_server_hooks"]})
    (out_dir / "OPCODES.md").write_text(render_markdown(inventory), encoding="utf-8")
    c2s = sum(1 for o in inventory["opcodes"] if o["client_to_server"])
    s2c = sum(1 for o in inventory["opcodes"] if o["server_to_client"])
    print(f"client      : {client}")
    print(f"opcodes     : {len(inventory['opcodes'])} ({c2s} client->server, {s2c} server->client)")
    print(f"classes     : {len(inventory['message_classes'])} message classes")
    print(f"dispatch    : {sum(len(d['cases']) for d in inventory['dispatchers'])} cases in "
          f"{len(inventory['dispatchers'])} dispatcher(s)")
    print(f"raw sends   : {len(inventory['raw_sends'])}")
    print(f"websockets  : {len(inventory['websockets'])}   http calls: {len(inventory['http'])}")
    print(f"regions     : {len(inventory['regions'])}   local hooks: {len(inventory['local_server_hooks'])}")
    print(f"written to  : {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
