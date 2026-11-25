"""Offline tests for rebuild_client_js.py.

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rebuild_client_js as rb  # noqa: E402

HAVE_TOOLS = not rb._MISSING


class StringTests(unittest.TestCase):
    def test_decode_escapes(self):
        self.assertEqual(rb.decode_js_string(r"'\x6e\x61\u006d\u{65}'"), "name")
        self.assertEqual(rb.decode_js_string(r"'\101\0'"), "A\0")
        self.assertEqual(rb.decode_js_string("'a\\\nb'"), "ab")  # line continuation
        self.assertEqual(rb.decode_js_string(r"'\uD83D\uDE00'"), "\U0001F600")
        self.assertEqual(rb.decode_js_string(r"'\q\''"), "q'")

    def test_encode(self):
        self.assertEqual(rb.encode_js_string("it's \"x\"\n", "'"), "'it\\'s \"x\"\\n'")
        self.assertEqual(rb.encode_js_string("\U0001F600", "'"), "'\\ud83d\\ude00'")
        self.assertEqual(rb.encode_js_string("é", "'", ascii_only=False), "'é'")
        self.assertEqual(rb.encode_js_string("\ud800", "'", ascii_only=False), "'\\ud800'")  # lone surrogate

    def test_numbers(self):
        self.assertEqual(rb.js_number("0x128"), 296.0)
        self.assertEqual(rb.js_number("296"), 296.0)
        self.assertEqual(rb.js_number("1_000"), 1000.0)
        self.assertEqual(rb.js_number("0b101"), 5.0)


@unittest.skipUnless(HAVE_TOOLS, "tree-sitter / jsbeautifier not installed")
class RebuildTests(unittest.TestCase):
    SOURCE = (b"function f(){'use strict';var o={'\\x61':0x10,'b-c':1,\"q\":'\\x22x'};"
              b"return o['\\x61']+o?.['b']+1['toString']+0x10['toString']()+(![])+typeof![]+!![]+[1,2][0]"
              b"+a[/*c*/'x']+`t${o['k']}`+/['a']/.test('z')}")

    def test_deobfuscate(self):
        out, stats = rb.deobfuscate(self.SOURCE, rb.make_parser())
        text = out.decode()
        for expected in ("{a:16,'b-c':1,q:'\"x'}", "o.a+o?.b", "1['toString']", "16['toString']()",
                         "(false)", "typeof false", "+true+", "'use strict'", "a[/*c*/'x']", "`t${o.k}`",
                         "/['a']/.test"):
            self.assertIn(expected, text)
        self.assertEqual(stats["boolean_idioms"], 3)

    def test_rebuild_is_verified(self):
        pretty, details = rb.rebuild_source(self.SOURCE)
        self.assertTrue(details["verification"]["rebuilt_vs_original"]["equivalent"])
        self.assertIn("\n", pretty)

    def test_verification_detects_changes(self):
        parser = rb.make_parser()
        original = b"var a = 0x10 + b['c'];"
        changed = b"var a = 17 + b.c;"
        result = rb.compare_trees(parser.parse(original), original, parser.parse(changed), changed)
        self.assertFalse(result["equivalent"])
        same = b"var a=16+b.c;"
        self.assertTrue(rb.compare_trees(parser.parse(original), original, parser.parse(same), same)["equivalent"])

    def test_extract_config(self):
        src = b"$config={'\\x67ameVersion':0x128,isBeta:![],servers:[{name:'A 1',port:-1}],f:x()};"
        parser = rb.make_parser()
        config = rb.extract_config(parser.parse(src), src)
        self.assertEqual(config["gameVersion"], 296)
        self.assertIs(config["isBeta"], False)
        self.assertEqual(config["servers"], [{"name": "A 1", "port": -1}])
        self.assertEqual(config["f"], {"$expression": "x()"})

    def test_rejects_unparseable_rewrite_overlap(self):
        with self.assertRaises(rb.RebuildError):
            rb.apply_edits(b"abcdef", [(0, 3, "x"), (2, 4, "y")])


if __name__ == "__main__":
    unittest.main()
