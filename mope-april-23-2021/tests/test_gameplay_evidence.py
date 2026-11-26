"""Offline tests for extract_gameplay_evidence.py (synthetic client in the obfuscator's style)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import extract_gameplay_evidence as ege
    import rebuild_client_js as rb
    HAVE_TREE_SITTER = not rb._MISSING
except ImportError:  # pragma: no cover
    HAVE_TREE_SITTER = False

CLIENT = r"""
var _0xT = 2, _0xP = 131, _0xF = 9, _0xMOUSE = 1, _0xHDR = 1, _0xHDR2 = 3;
var _0xreg = { setCustomClassForGameObjType: function(a, b, c) {} };
function _0xBase(t) {}
_0xBase.prototype = {
  readCustomData_onNewlyVisible: function(r) { this.oType == _0xF && (this.innerRad = r.readUInt16()); },
  readUpdatedProperty: function(p, r) { p == 4 && (this.hp = r.readUInt8()); }
};
function _0xAnimal(a, b) { _0xAnimal.superClass.call(this, _0xT); }
var _0x244a35 = _0xAnimal, _0x3c3358 = _0xBase;
_0x244a35.prototype = Object.create(_0x3c3358.prototype), _0x244a35.superClass = _0x3c3358,
_0xAnimal.prototype.readCustomData_onNewlyVisible = function(r) {
  _0xAnimal.superClass.prototype.readCustomData_onNewlyVisible.call(this, r);
  this.nickName = r.readString(), this.species = r.readUInt16();
};
function _0xMouse() {}
var _0x244a35 = _0xMouse, _0x3c3358 = _0xAnimal;
_0x244a35.prototype = Object.create(_0x3c3358.prototype), _0x244a35.superClass = _0x3c3358;
function _0xPumpkin() {}
var _0x244a35 = _0xPumpkin, _0x3c3358 = _0xBase;
_0x244a35.prototype = Object.create(_0x3c3358.prototype),
_0xPumpkin.prototype.readCustomData_onNewlyVisible = function(r) { this.isRolling = r.readUInt8() == 1, this.pumpkinType = r.readUInt8(); };
_0xreg.setCustomClassForGameObjType(_0xMouse, _0xT, _0xMOUSE);
_0xreg.setCustomClassForGameObjType(_0xPumpkin, _0xP);
var _0xtable = function(t) { var o = {}; switch (t) {
  case _0xMOUSE: o.aniName = 'Mouse', o.skinName = 'mouse', o.aniCol = '#9BA9B9'; break; } return o; };
var _0xheader = function(f, r) {
  if (f == _0xHDR) zoom = r.readUInt16() / 1000;
  else if (f == 2) a = r.readUInt32();
  else if (f == _0xHDR2) { x = r.readUInt16(); y = r.readUInt16(); }
  else if (f == 4) b = r.readUInt8();
  else if (f == 5) c = r.readUInt8();
  else if (f == 6) d = r.readString();
};
"""


@unittest.skipUnless(HAVE_TREE_SITTER, "tree-sitter not installed")
class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "client.js"
        self.path.write_text(CLIENT, encoding="utf-8")
        self.ev = ege.Evidence(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_aliases_resolve_by_position(self):
        self.assertEqual(self.ev.chain("_0xMouse"), ["_0xMouse", "_0xAnimal", "_0xBase"])
        self.assertEqual(self.ev.chain("_0xPumpkin"), ["_0xPumpkin", "_0xBase"])

    def test_registry_and_effective_readers(self):
        registry = {(r["object_type"], r["subtype"]): r["class"] for r in self.ev.registry()}
        self.assertEqual(registry, {(2, 1): "_0xMouse", (131, None): "_0xPumpkin"})
        mouse = ege.flatten(self.ev.reader_of("_0xMouse", "readCustomData_onNewlyVisible"))
        self.assertEqual([r["op"] for r in mouse], ["UInt16", "String", "UInt16"])   # base (conditional) + animal
        self.assertIn("conditional", mouse[0].get("flags", []))
        pumpkin = ege.flatten(self.ev.reader_of("_0xPumpkin", "readCustomData_onNewlyVisible"))
        self.assertEqual([r["op"] for r in pumpkin], ["UInt8", "UInt8"])
        self.assertEqual(self.ev.reader_of("_0xMouse", "readUpdatedProperty")["property_ids"], [4])

    def test_tables_and_field_chains(self):
        self.assertEqual(self.ev.switch_tables(("aniName", "skinName", "aniCol"), "aniName"),
                         [{"id": 1, "line": self.ev.switch_tables(("aniName",), "aniName")[0]["line"],
                           "aniName": "Mouse", "skinName": "mouse", "aniCol": "#9BA9B9"}])
        chains = ege.field_chains(self.ev)
        self.assertEqual(len(chains), 1)
        self.assertEqual({c["id"]: c["reads"] for c in chains[0]["cases"]},
                         {1: ["UInt16"], 2: ["UInt32"], 3: ["UInt16", "UInt16"], 4: ["UInt8"], 5: ["UInt8"], 6: ["String"]})


if __name__ == "__main__":
    unittest.main()
