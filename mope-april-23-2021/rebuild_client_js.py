#!/usr/bin/env python3
"""
rebuild_client_js.py -- rebuild a readable mope.io client.js from a real archived copy.

The April 23, 2021 client.js is not held by any archive this project uses (the
Wayback Machine is excluded by design). This script therefore works from the
client.js captures that ARE available -- by default the newest one -- and writes a
prettified, de-obfuscated version that is verified to be equivalent to the original:

  1. Source: fetch the original bytes from a non-Wayback archive (Arquivo.pt raw
     replay; the archive's Memento-Datetime is checked), or use --input FILE.
  2. De-obfuscate with edits guided by the JavaScript syntax tree (tree-sitter):
         '\\x6e\\x61\\x6d\\x65'  -> 'name'      string escapes decoded
         obj['name']           -> obj.name     (valid identifier names only)
         {'name': 1}           -> {name: 1}
         0x128                 -> 296
         ![]  /  !![]          -> false / true
         static['type'] / ['send']() -> static type / send()
  3. Beautify (jsbeautifier).
  4. Verify: the rebuilt file's syntax tree must equal the original's, allowing only
     the rewrites listed above; `node --check` is run as well when node is installed.

The original is kept byte-for-byte (read-only) under archive/client-js/ and the
rebuild is written to working/client-js/; both directories are git-ignored.

Examples:
  python rebuild_client_js.py --list
  python rebuild_client_js.py                         # newest archived client.js
  python rebuild_client_js.py --timestamp 20180421014457
  python rebuild_client_js.py --closest-to-target     # capture nearest 2021-04-23
  python rebuild_client_js.py --all-captures
  python rebuild_client_js.py --input some/client.js
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
import shutil
import stat
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_mope_archive as fm  # noqa: E402  (shared archive providers / HTTP client)

_MISSING: list[str] = list(fm._MISSING_DEPS)
try:
    import jsbeautifier
except ImportError:  # pragma: no cover - reported by check_dependencies()
    jsbeautifier = None
    _MISSING.append("jsbeautifier")
try:
    import tree_sitter_javascript
    from tree_sitter import Language, Parser
except ImportError:  # pragma: no cover
    tree_sitter_javascript = None
    _MISSING.append("tree-sitter tree-sitter-javascript")

VERSION = "1.0.0"
UTC = timezone.utc
CLIENT_URL = "https://mope.io/client.js"

IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")
ESCAPE_RE = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[0-3][0-7]{0,2}|[4-7][0-7]?|\r\n|[\s\S])")
SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
REVERSE_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f", "\v": "\\v"}
LINE_CONTINUATIONS = {"\n", "\r", "\r\n", "\u2028", "\u2029"}

log = logging.getLogger("mope.rebuild")


class RebuildError(Exception):
    """Fatal error shown to the user."""


# =============================================================================
# JavaScript string literals
# =============================================================================


def _unescape(match: re.Match) -> str:
    esc = match.group(1)
    if esc in LINE_CONTINUATIONS:
        return ""
    head = esc[0]
    if head == "u":
        return chr(int(esc[2:-1] if esc[1] == "{" else esc[1:], 16))
    if head == "x":
        return chr(int(esc[1:], 16))
    if head in "01234567":
        return "\0" if esc == "0" else chr(int(esc, 8))
    return SIMPLE_ESCAPES.get(head, head)


def decode_js_string(raw: str) -> str:
    """Value of a quoted JS string literal (including its quotes)."""
    value = ESCAPE_RE.sub(_unescape, raw[1:-1])
    # merge \uD83D\uDE00-style surrogate pairs into real characters; lone surrogates stay
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")


def encode_js_string(value: str, quote: str = "'", ascii_only: bool = True) -> str:
    out = [quote]
    for ch in value:
        cp = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == quote:
            out.append("\\" + quote)
        elif ch in REVERSE_ESCAPES:
            out.append(REVERSE_ESCAPES[ch])
        elif cp < 0x20 or cp == 0x7F:
            out.append(f"\\x{cp:02x}")
        elif cp < 0x80:
            out.append(ch)
        elif (ascii_only or 0xD800 <= cp <= 0xDFFF or cp in (0x2028, 0x2029, 0xFEFF)
              or not ch.isprintable()):
            if cp > 0xFFFF:
                cp -= 0x10000
                out.append(f"\\u{0xD800 + (cp >> 10):04x}\\u{0xDC00 + (cp & 0x3FF):04x}")
            else:
                out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append(quote)
    return "".join(out)


def js_number(text: str):
    """Numeric value of a JS number literal (as JS would see it)."""
    t = text.replace("_", "").lower()
    if t.endswith("n"):
        return ("bigint", int(t[:-1], 0) if t[:2] in ("0x", "0o", "0b") else int(t[:-1]))
    if t.startswith("0x"):
        return float(int(t, 16))
    if t.startswith("0o"):
        return float(int(t[2:], 8))
    if t.startswith("0b"):
        return float(int(t[2:], 2))
    if re.fullmatch(r"0[0-7]+", t):  # legacy octal
        return float(int(t, 8))
    return float(t)


# =============================================================================
# Syntax-tree helpers
# =============================================================================


def make_parser():
    return Parser(Language(tree_sitter_javascript.language()))


def node_text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "surrogatepass")


def same_node(a, b) -> bool:
    return a is not None and b is not None and a.start_byte == b.start_byte and a.end_byte == b.end_byte \
        and a.type == b.type


def bool_idiom(node):
    """True/False for `!![]` / `![]` (any depth of `!`), else None."""
    if node.type != "unary_expression":
        return None
    operator = node.child_by_field_name("operator")
    argument = node.child_by_field_name("argument")
    if operator is None or operator.type != "!" or argument is None:
        return None
    if argument.type == "array" and argument.named_child_count == 0:
        return False
    inner = bool_idiom(argument)
    return None if inner is None else not inner


def static_member(node, data: bytes):
    """(property name, optional?) for a.b / a['b'] style access, else (None, None)."""
    optional = any(child.type == "optional_chain" for child in node.children)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        if prop is not None and prop.type == "property_identifier":
            return node_text(prop, data), optional
        return None, None
    if node.type == "subscript_expression":
        index = node.child_by_field_name("index")
        if index is not None and index.type == "string":
            name = decode_js_string(node_text(index, data))
            if IDENT_RE.match(name):
                return name, optional
    return None, None


# Names whose computed and plain forms mean different things:
# `['constructor']()` is an ordinary method, `constructor()` is the class constructor;
# `static prototype` and plain `constructor` fields are syntax errors; `['__proto__']: x`
# defines a property while `__proto__: x` sets the prototype.
COMPUTED_NAME_EXCLUSIONS = {"constructor", "prototype", "__proto__"}


def static_computed_name(node, data: bytes) -> str | None:
    """'name' for a class member / object key written as ['name'], when unquoting is safe."""
    if node.type != "computed_property_name" or node.parent is None or \
            node.parent.type not in ("method_definition", "field_definition", "pair"):
        return None
    inner = [child for child in node.named_children]
    if len(inner) != 1 or inner[0].type != "string":
        return None
    name = decode_js_string(node_text(inner[0], data))
    if IDENT_RE.match(name) and name not in COMPUTED_NAME_EXCLUSIONS:
        return name
    return None


def is_directive_candidate(node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "expression_statement"


def has_errors(tree) -> bool:
    return tree.root_node.has_error


def first_error(tree, data: bytes) -> str:
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            start = node.start_byte
            return f"byte {start}: {data[max(0, start - 60):start + 60]!r}"
        stack.extend(node.children)
    return "unknown position"


# =============================================================================
# De-obfuscation
# =============================================================================


def deobfuscate(data: bytes, parser, ascii_only: bool = True) -> tuple[bytes, Counter]:
    tree = parser.parse(data)
    if has_errors(tree):
        raise RebuildError(f"the original does not parse cleanly ({first_error(tree, data)})")
    edits: list[tuple[int, int, str]] = []
    stats: Counter = Counter()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = node.type
        if kind == "string":
            if not is_directive_candidate(node):
                raw = node_text(node, data)
                new = encode_js_string(decode_js_string(raw), raw[0], ascii_only)
                if new != raw:
                    edits.append((node.start_byte, node.end_byte, new))
                    stats["strings_rewritten"] += 1
                    delta = raw.count("\\") - new.count("\\")
                    if delta > 0:
                        stats["escapes_removed"] += delta
                    elif delta < 0:  # e.g. a literal non-ASCII character escaped for ASCII-safe output
                        stats["escapes_added"] += -delta
            continue
        if kind == "number":
            text = node_text(node, data)
            parent = node.parent
            is_member_object = parent is not None and parent.type == "member_expression" and \
                same_node(parent.child_by_field_name("object"), node)
            if HEX_RE.match(text) and not is_member_object:
                edits.append((node.start_byte, node.end_byte, str(int(text, 16))))
                stats["hex_numbers"] += 1
            continue
        if kind == "unary_expression":
            value = bool_idiom(node)
            if value is not None:
                edits.append((node.start_byte, node.end_byte, "true" if value else "false"))
                stats["boolean_idioms"] += 1
                continue
        if kind == "subscript_expression":
            obj = node.child_by_field_name("object")
            name, optional = static_member(node, data)
            if name is not None and obj is not None and obj.type != "number":
                start_child = next((c for c in node.children if c.type in ("optional_chain", "[")), None)
                if start_child is not None and node.children[-1].type == "]" and \
                        not any(c.type == "comment" for c in node.children):
                    edits.append((start_child.start_byte, node.end_byte, ("?." if optional else ".") + name))
                    stats["member_accesses"] += 1
                    stack.append(obj)
                    continue
        if kind == "pair":
            key = node.child_by_field_name("key")
            if key is not None and key.type == "string":
                name = decode_js_string(node_text(key, data))
                if IDENT_RE.match(name):
                    edits.append((key.start_byte, key.end_byte, name))
                    stats["object_keys"] += 1
                    stack.extend(reversed([c for c in node.children if not same_node(c, key)]))
                    continue
        if kind == "computed_property_name":
            name = static_computed_name(node, data)
            if name is not None:
                edits.append((node.start_byte, node.end_byte, name))
                stats["computed_member_names"] += 1
                continue
        stack.extend(reversed(node.children))
    return apply_edits(data, edits), stats


def _ident_byte(value: int) -> bool:
    return chr(value).isalnum() or value in (0x24, 0x5F) or value >= 0x80


def apply_edits(data: bytes, edits: list[tuple[int, int, str]]) -> bytes:
    """Apply non-overlapping byte-range edits, keeping adjacent tokens separated."""
    edits.sort()
    out = bytearray()
    pos = 0
    last_was_edit = False

    def append(chunk: bytes, is_edit: bool) -> None:
        nonlocal last_was_edit
        if chunk:
            if out and (is_edit or last_was_edit) and _ident_byte(out[-1]) and _ident_byte(chunk[0]):
                out.extend(b" ")
            out.extend(chunk)
            last_was_edit = is_edit

    for start, end, replacement in edits:
        if start < pos:
            raise RebuildError(f"internal error: overlapping edits at byte {start}")
        append(data[pos:start], False)
        append(replacement.encode("utf-8"), True)
        pos = end
    append(data[pos:], False)
    return bytes(out)


def beautify(source: str, indent: int) -> str:
    options = jsbeautifier.default_options()
    options.indent_size = indent
    options.indent_char = " "
    options.preserve_newlines = True
    options.max_preserve_newlines = 2
    options.end_with_newline = True
    options.wrap_line_length = 0
    options.e4x = False
    options.unescape_strings = False
    return jsbeautifier.beautify(source, options)


# =============================================================================
# Verification: equivalent syntax trees modulo the rewrites above
# =============================================================================


def canonical_stream(tree, data: bytes):
    """Yield (token, byte offset) describing the program, normalising exactly the
    differences this script introduces (escapes, a['b'] vs a.b, quoted keys,
    number spelling, ![]/!![] vs false/true) and ignoring comments/whitespace."""
    stack: list = [tree.root_node]
    while stack:
        item = stack.pop()
        if isinstance(item, tuple):
            yield item
            continue
        node = item
        kind = node.type
        pos = node.start_byte
        if kind == "comment":
            continue
        if kind == "string":
            yield ("str", decode_js_string(node_text(node, data))), pos
            continue
        if kind == "number":
            yield ("num", js_number(node_text(node, data))), pos
            continue
        if kind in ("true", "false"):
            yield ("bool", kind == "true"), pos
            continue
        if kind == "unary_expression":
            value = bool_idiom(node)
            if value is not None:
                yield ("bool", value), pos
                continue
        if kind in ("member_expression", "subscript_expression"):
            name, optional = static_member(node, data)
            if name is not None:
                yield ("member", optional), pos
                stack.append((("prop", name), pos))
                stack.append(node.child_by_field_name("object"))
                continue
        if kind == "computed_property_name":
            name = static_computed_name(node, data)
            if name is not None:
                yield ("property_identifier", name), pos
                continue
        if kind == "pair":
            key = node.child_by_field_name("key")
            computed = static_computed_name(key, data) if key is not None else None
            if key is not None and (key.type in ("string", "property_identifier") or computed is not None):
                if computed is not None:
                    name = computed
                elif key.type == "string":
                    name = decode_js_string(node_text(key, data))
                else:
                    name = node_text(key, data)
                yield ("pair",), pos
                value = node.child_by_field_name("value")
                if value is not None:
                    stack.append(value)
                stack.append((("key", name), key.start_byte))
                continue
        if node.child_count == 0:
            yield (kind, node_text(node, data)), pos
            continue
        yield (kind,), pos
        stack.extend(reversed(node.children))


def compare_trees(orig_tree, orig: bytes, new_tree, new: bytes) -> dict:
    count = 0
    for a, b in itertools.zip_longest(canonical_stream(orig_tree, orig), canonical_stream(new_tree, new)):
        if a is None or b is None or a[0] != b[0]:
            def show(entry, data):
                if entry is None:
                    return {"token": None}
                token, offset = entry
                return {"token": repr(token)[:200], "byte": offset,
                        "context": data[max(0, offset - 80):offset + 80].decode("utf-8", "replace")}
            return {"equivalent": False, "tokens_compared": count,
                    "original": show(a, orig), "rebuilt": show(b, new)}
        count += 1
    return {"equivalent": True, "tokens_compared": count}


def node_check(path: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        return {"status": "skipped", "reason": "node is not installed"}
    try:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "reason": str(exc)}
    return {"status": "ok" if result.returncode == 0 else "failed",
            "stderr": result.stderr.strip()[-2000:] or None}


# =============================================================================
# Extract the client's own config block ($config = {...}), when present
# =============================================================================


def literal_value(node, data: bytes, depth: int = 0):
    kind = node.type
    if kind == "string":
        return decode_js_string(node_text(node, data))
    if kind == "number":
        value = js_number(node_text(node, data))
        return int(value) if isinstance(value, float) and value.is_integer() else value
    if kind in ("true", "false"):
        return kind == "true"
    if kind in ("null", "undefined"):
        return None
    if kind == "unary_expression":
        value = bool_idiom(node)
        if value is not None:
            return value
        operator = node.child_by_field_name("operator")
        argument = node.child_by_field_name("argument")
        if operator is not None and operator.type == "-" and argument is not None and argument.type == "number":
            return -literal_value(argument, data)
    if depth < 6 and kind == "object":
        out = {}
        for pair in node.named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            value = pair.child_by_field_name("value")
            if key is None or value is None:
                continue
            name = decode_js_string(node_text(key, data)) if key.type == "string" else node_text(key, data)
            out[name] = literal_value(value, data, depth + 1)
        return out
    if depth < 6 and kind == "array":
        return [literal_value(child, data, depth + 1) for child in node.named_children if child.type != "comment"]
    text = node_text(node, data)
    return {"$expression": text if len(text) <= 160 else text[:157] + "..."}


def extract_config(tree, data: bytes, names=("$config",)) -> dict | None:
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None and right.type == "object" and node_text(left, data) in names:
                return literal_value(right, data)
        elif node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and value is not None and value.type == "object" and node_text(name, data) in names:
                return literal_value(value, data)
        stack.extend(reversed(node.children))
    return None


# =============================================================================
# Rebuild one client.js
# =============================================================================


def rebuild_source(original: bytes, *, deobfuscate_code: bool = True, indent: int = 2, ascii_only: bool = True,
                   verify: bool = True) -> tuple[str, dict]:
    """Return (prettified source, details). Raises RebuildError if verification fails."""
    parser = make_parser()
    details: dict = {"transforms": {}, "verification": {}}
    orig_tree = parser.parse(original)
    parseable = not has_errors(orig_tree)
    if deobfuscate_code and parseable:
        transformed, stats = deobfuscate(original, parser, ascii_only)
        details["transforms"] = dict(stats)
    else:
        transformed = original
        if deobfuscate_code:
            details["transforms"] = {"skipped": f"original not parseable by tree-sitter "
                                                f"({first_error(orig_tree, original)})"}
    if verify and parseable and transformed is not original:
        stage = compare_trees(orig_tree, original, parser.parse(transformed), transformed)
        details["verification"]["deobfuscated_vs_original"] = stage
        if not stage["equivalent"]:
            raise RebuildError("de-obfuscation changed the program structure; refusing to write it:\n"
                               + json.dumps(stage, indent=2))
    pretty = beautify(transformed.decode("utf-8", "surrogatepass"), indent)
    pretty_bytes = pretty.encode("utf-8", "surrogatepass")
    pretty_tree = parser.parse(pretty_bytes)
    if verify and parseable:
        if has_errors(pretty_tree):
            raise RebuildError(f"the beautified output does not parse ({first_error(pretty_tree, pretty_bytes)})")
        stage = compare_trees(orig_tree, original, pretty_tree, pretty_bytes)
        details["verification"]["rebuilt_vs_original"] = stage
        if not stage["equivalent"]:
            raise RebuildError("beautifying changed the program structure; refusing to write it:\n"
                               + json.dumps(stage, indent=2))
    elif verify:
        details["verification"]["rebuilt_vs_original"] = {"equivalent": None,
                                                          "reason": "original not parseable by tree-sitter"}
    details["client_config"] = extract_config(pretty_tree, pretty_bytes) if not has_errors(pretty_tree) else None
    details["lines"] = pretty.count("\n")
    return pretty, details


def header_comment(source: dict, details: dict, target_day: date, target_gap_days: float | None) -> str:
    transforms = details.get("transforms") or {}
    applied = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in transforms.items()) or "none"
    verification = details.get("verification", {}).get("rebuilt_vs_original", {})
    verified = {True: f"syntax tree equivalent to the original ({verification.get('tokens_compared')} tokens)",
                False: "NOT equivalent", None: "not verified"}[verification.get("equivalent")]
    lines = [
        "/*",
        " * mope.io client.js -- readable rebuild generated by rebuild_client_js.py " + VERSION,
        f" * Source   : {source['description']}",
        f" *            sha256 {source['sha256']} ({source['size']} bytes)",
    ]
    if target_gap_days:
        lines.append(f" * Date gap : {target_gap_days:+.1f} days from the project target {target_day} -- this is "
                     f"NOT the {target_day} client.")
    lines += [
        f" * Rewrites : {applied}",
        " *            (string escapes decoded, obj['x'] -> obj.x, quoted keys unquoted, hex -> decimal,",
        " *             ![] / !![] -> false / true), then beautified.",
        f" * Verified : {verified}; node --check: {details.get('node_check', {}).get('status', 'n/a')}",
        " * The original bytes are preserved under archive/client-js/.",
        " */",
        "",
    ]
    return "\n".join(lines)


# =============================================================================
# Sources (non-Wayback archives) and the CLI
# =============================================================================


class Project:
    def __init__(self, args):
        config_path = Path(args.config).resolve() if args.config else SCRIPT_DIR / "config.json"
        self.config, found = fm.load_config(config_path)
        errors = fm.validate_config(self.config)
        if errors:
            raise RebuildError("Invalid configuration:\n  - " + "\n  - ".join(errors))
        root = Path(args.output).resolve() if args.output else (config_path.parent if found else SCRIPT_DIR)
        self.paths = fm.ProjectPaths(root, self.config)
        self.paths.ensure()
        self.originals = self.paths.archive / "client-js"
        self.builds = self.paths.working / "client-js"
        self.target = fm.TargetWindow(date.fromisoformat(str(self.config["snapshot_date"])),
                                      str(self.config["snapshot_timezone"]))
        handler = logging.FileHandler(self.paths.logs / "rebuild_client_js.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(handler)
        self._db = None
        self._providers = None
        self.wanted_providers = {p.strip() for item in (args.provider or []) for p in item.split(",") if p.strip()}

    @property
    def providers(self) -> dict:
        if self._providers is None:
            self._db = fm.Database(self.paths.database)  # shares the fetcher's archive-lookup cache
            http = fm.HttpClient(self.config, log)
            domain = self.config["domain"]
            self._providers = {}
            for cfg in self.config["providers"]:
                if not cfg.get("enabled", True):
                    continue
                if self.wanted_providers and cfg["name"] not in self.wanted_providers:
                    continue
                self._providers[cfg["name"]] = fm.PROVIDER_TYPES[cfg["type"]](cfg, http, self._db, log, domain)
        return self._providers

    def close(self) -> None:
        if self._db is not None:
            self._db.close()

    def captures(self) -> list[fm.Capture]:
        start = datetime(1996, 1, 1, tzinfo=UTC)
        end = datetime.now(UTC)
        found: dict[str, fm.Capture] = {}
        for provider in self.providers.values():
            try:
                for cap in provider.lookup(CLIENT_URL, start, end):
                    if cap.fetchable:
                        found[cap.ident] = cap
            except (fm.FetchError, ValueError) as exc:
                log.warning("%s: client.js lookup failed: %s", provider.label, exc)
        return sorted(found.values(), key=lambda c: c.timestamp)

    def gap_days(self, cap: fm.Capture) -> float:
        return self.target.signed_deviation_days(cap.dt)


def save_original(project: Project, cap: fm.Capture, payload, provider) -> tuple[Path, dict]:
    folder = project.originals / f"{cap.timestamp}_{cap.provider}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "client.original.js"
    digest = fm.sha256_bytes(payload.body)
    if not (path.exists() and fm.sha256_file(path) == digest):
        fm.write_atomic(path, payload.body)
    path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    meta = {
        "provider": cap.provider, "provider_label": provider.label, "url": cap.url,
        "timestamp": cap.timestamp, "iso": fm.ts_iso(cap.timestamp), "archive_link": provider.human_link(cap),
        "fetched_from": payload.fetched_from, "notes": payload.notes, "sha256": digest,
        "size": len(payload.body), "content_type": payload.content_type, "saved_at": fm.now_iso(),
        "local_path": project.paths.rel(path),
        "description": f"{provider.label} capture {cap.timestamp} ({fm.ts_human(cap.timestamp)} UTC) of {cap.url}",
    }
    fm.write_json(folder / "capture.json", meta)
    return path, meta


def build_one(project: Project, original: bytes, source: dict, build_id: str, args,
              gap_days: float | None) -> dict:
    log.info("Rebuilding %s (%d bytes)...", source["description"], len(original))
    if original.lstrip()[:1] == b"<":
        raise RebuildError("the source looks like HTML, not JavaScript")
    pretty, details = rebuild_source(original, deobfuscate_code=not args.beautify_only, indent=args.indent,
                                     ascii_only=not args.utf8, verify=not args.no_verify)
    out_dir = project.builds / build_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "client.js"
    fm.write_atomic(out_path, pretty.encode("utf-8", "surrogatepass"))
    if not args.no_verify:
        details["node_check"] = node_check(out_path)
        if details["node_check"]["status"] == "failed":
            raise RebuildError(f"node --check rejected the rebuild: {details['node_check']['stderr']}")
    final = header_comment(source, details, project.target.day, gap_days) + pretty
    fm.write_atomic(out_path, final.encode("utf-8", "surrogatepass"))
    report = {
        "generated_at": fm.now_iso(), "script_version": VERSION, "build_id": build_id,
        "source": source, "target_date": project.target.day.isoformat(),
        "days_from_target": gap_days,
        "output": {"path": project.paths.rel(out_path), "sha256": fm.sha256_file(out_path),
                   "size": out_path.stat().st_size, "lines": final.count("\n")},
        "options": {"deobfuscate": not args.beautify_only, "indent": args.indent, "ascii_only": not args.utf8,
                    "verify": not args.no_verify},
        "transforms": details.get("transforms"), "verification": details.get("verification"),
        "node_check": details.get("node_check"), "client_config": details.get("client_config"),
        "tools": {"jsbeautifier": getattr(jsbeautifier, "__version__", "?"), "python": sys.version.split()[0]},
    }
    fm.write_json(out_dir / "rebuild_report.json", report)
    index_path = project.builds / "index.json"
    index = fm.read_json(index_path, {"builds": {}}) or {"builds": {}}
    index["builds"][build_id] = {k: report[k] for k in ("generated_at", "days_from_target")} | {
        "source": source["description"], "output": report["output"]["path"],
        "verified": (report["verification"] or {}).get("rebuilt_vs_original", {}).get("equivalent")}
    fm.write_json(index_path, index)
    return report


def print_report(report: dict) -> None:
    transforms = report.get("transforms") or {}
    verification = (report.get("verification") or {}).get("rebuilt_vs_original", {})
    config = report.get("client_config")
    print()
    print(f"  source      : {report['source']['description']}")
    if report.get("days_from_target") is not None:
        print(f"  date gap    : {report['days_from_target']:+.1f} days from {report['target_date']}")
    print(f"  output      : {report['output']['path']} ({report['output']['lines']} lines, "
          f"{report['output']['size']:,} bytes)")
    print("  rewrites    : " + (", ".join(f"{k}={v}" for k, v in transforms.items()) or "none"))
    print(f"  verified    : {verification.get('equivalent')} "
          f"({verification.get('tokens_compared', 0):,} syntax tokens compared); "
          f"node --check: {(report.get('node_check') or {}).get('status', 'n/a')}")
    if isinstance(config, dict):
        preview = ", ".join(f"{k}={json.dumps(v)[:40]}" for k, v in list(config.items())[:8])
        print(f"  $config     : {len(config)} keys ({preview}{', ...' if len(config) > 8 else ''})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild a readable mope.io client.js from a real archived copy "
                                                 "(non-Wayback archives) and verify it is equivalent.")
    src = parser.add_argument_group("source (default: newest archived client.js)")
    src.add_argument("--list", action="store_true", help="list archived client.js captures and exit")
    src.add_argument("--timestamp", help="use the capture with this timestamp (prefix allowed)")
    src.add_argument("--closest-to-target", action="store_true",
                     help="use the capture closest to the project target date (2021-04-23)")
    src.add_argument("--all-captures", action="store_true", help="rebuild every archived capture")
    src.add_argument("--input", help="rebuild a local client.js file instead of fetching one")
    src.add_argument("--provider", action="append", help="restrict archive providers (default: all enabled)")
    out = parser.add_argument_group("output")
    out.add_argument("--indent", type=int, default=2, help="indent width (default 2)")
    out.add_argument("--beautify-only", action="store_true", help="skip de-obfuscation, only beautify")
    out.add_argument("--utf8", action="store_true", help="keep non-ASCII characters literal in strings")
    out.add_argument("--no-verify", action="store_true", help="skip the equivalence and node checks")
    out.add_argument("--config", help="path to config.json (default: next to this script)")
    out.add_argument("--output", help="project root (default: the config file's directory)")
    out.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    log.setLevel(logging.DEBUG)
    project = None
    try:
        if _MISSING:
            raise RebuildError(f"missing Python packages: {', '.join(_MISSING)}\n"
                               f"Install them with: pip install -r requirements.txt")
        if not 1 <= args.indent <= 8:
            raise RebuildError("--indent must be between 1 and 8")
        project = Project(args)

        if args.input:
            path = Path(args.input).resolve()
            data = path.read_bytes()
            digest = fm.sha256_bytes(data)
            source = {"provider": "local", "local_path": str(path), "sha256": digest, "size": len(data),
                      "description": f"local file {path.name}"}
            report = build_one(project, data, source, f"local-{digest[:12]}", args, None)
            print_report(report)
            return 0

        log.info("Looking up archived client.js captures (%s)...",
                 ", ".join(p.label for p in project.providers.values()))
        captures = project.captures()
        replayable = [c for c in captures if project.providers[c.provider].can_replay]
        if args.list or not replayable:
            print()
            print(f"  {'timestamp':<16}{'UTC':<21}{'provider':<13}{'vs ' + str(project.target.day):>14}  url")
            for cap in captures:
                print(f"  {cap.timestamp:<16}{fm.ts_human(cap.timestamp):<21}{cap.provider:<13}"
                      f"{project.gap_days(cap):>+12.1f} d  {cap.url}"
                      f"{'' if project.providers[cap.provider].can_replay else '  [discovery only]'}")
            if not replayable:
                raise RebuildError("no downloadable client.js capture was found in the configured archives")
            return 0

        if args.all_captures:
            chosen = replayable
        elif args.timestamp:
            chosen = [c for c in replayable if c.timestamp.startswith(args.timestamp)][-1:]
            if not chosen:
                raise RebuildError(f"no client.js capture matches {args.timestamp}; see --list")
        elif args.closest_to_target:
            chosen = [min(replayable, key=lambda c: abs(project.gap_days(c)))]
        else:
            chosen = [replayable[-1]]

        failures = 0
        for cap in chosen:
            provider = project.providers[cap.provider]
            log.info("Fetching %s %s (%s UTC) from %s", cap.url, cap.timestamp, fm.ts_human(cap.timestamp),
                     provider.label)
            try:
                payload = provider.fetch(cap)
                _, source = save_original(project, cap, payload, provider)
                report = build_one(project, payload.body, source, f"{cap.timestamp}_{cap.provider}", args,
                                   project.gap_days(cap))
            except (fm.FetchError, RebuildError) as exc:
                failures += 1
                log.error("  %s: %s", cap.timestamp, exc)
                continue
            print_report(report)
        if failures:
            raise RebuildError(f"{failures} of {len(chosen)} rebuild(s) failed (see logs/rebuild_client_js.log)")
        print()
        print(f"Originals: {project.paths.rel(project.originals)}   Rebuilds: {project.paths.rel(project.builds)}")
        return 0
    except RebuildError as exc:
        log.error("error: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if project is not None:
            project.close()


if __name__ == "__main__":
    sys.exit(main())
