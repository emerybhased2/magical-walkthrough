"""Offline tests for extract_client_protocol.py on a small synthetic client.

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rebuild_client_js as rb  # noqa: E402

HAVE_TOOLS = not rb._MISSING
if HAVE_TOOLS:
    import extract_client_protocol as ecp  # noqa: E402

CLIENT = r"""
$config = {gameVersion: 7, useDevAccountServer: false, devAccountServerUrl: '',
  gameServers: [{id: 'srv-a', name: 'Alpha 1', url: 'alpha.example', region: 'Alpha Land', gm: 0}]};
const accountBase = $config.useDevAccountServer ? $config.devAccountServerUrl : 'https://accounts.example';
const T_INFO = 2, T_EFFECT = 23, T_PING = 255;
function Writer(size) { this.writeUInt8 = function(v) {}; this.writeUInt16 = function(v) {}; this.writeString = function(s) {}; }
function Reader(view) { this.readUInt8 = function() {}; this.readUInt16 = function() {}; this.readString = function() {}; }
class Base { static type = 0; send() {} }
class Join extends Base {
  static type = 1;
  static send(ws, session) {
    const w = new Writer(9);
    w.writeUInt8(Join.type), w.writeUInt16(1280), w.writeUInt16(800);
    if (session) w.writeString(session);
    ws.send(w.dataView.buffer);
  }
}
class Info extends Base {
  static type = 2;
  static handle(r) {
    const sid = r.readString(), version = r.readUInt16();
    return {sessionId: sid, gameVersion: version};
  }
}
function onMessage(view) {
  var r = new Reader(view), t = r.readUInt8();
  switch (t) {
    case T_INFO: Info.handle(r); break;
    case 3: shown(); break;
    case 4: break;
    case 5: break;
    case 6: console.log('You spawned alive ingame!'); break;
    case 7: break;
    case 8: break;
    case T_EFFECT: {
      var kind = r.readUInt8();
      switch (kind) {
        case 1: note('Ouch! Your tail got bitten!'); break;
        case 2: break; case 3: break; case 4: break; case 5: break; case 6: break; case 7: break; case 8: break;
      }
    } break;
    case T_PING: break;
  }
}
function leave() { var w = new Writer(1); w.writeUInt8(11); sock.send(w.dataView.buffer); }
function stats() { $.ajax({url: accountBase + '/servers/get', type: 'GET'}); }
async function login(token) {
  const url = accountBase + '/auth/login/google';
  await fetch(url, {method: 'POST', body: JSON.stringify({accessToken: token, platform: 'web'})});
}
function connect(server) { var u = 'ws://' + server.url; sock = new WebSocket(u); sock.onmessage = onMessage; }
"""


@unittest.skipUnless(HAVE_TOOLS, "tree-sitter / jsbeautifier not installed")
class ExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "client.js"
        path.write_text(CLIENT, encoding="utf-8")
        cls.inv = ecp.build_inventory(path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_message_classes(self):
        classes = {c["type"]: c for c in self.inv["message_classes"]}
        join = classes[1]["client_to_server"]["layout"]
        self.assertEqual([w["op"] for w in join], ["UInt16", "UInt16", "String"])
        self.assertIn("conditional", join[2]["flags"])
        fields = classes[2]["server_to_client"]["fields"]
        self.assertEqual(fields, [{"name": "sessionId", "type": "String"}, {"name": "gameVersion", "type": "UInt16"}])

    def test_dispatcher_and_subswitch(self):
        roles = {d["role"]: d for d in self.inv["dispatchers"]}
        main = roles["message dispatcher"]
        case2 = next(c for c in main["cases"] if c["opcode"] == 2)
        self.assertEqual(case2["handler_classes"], ["Info"])
        self.assertEqual(roles["sub-switch"]["nested_in_opcode"], 23)
        opcodes = {o["opcode"] for o in self.inv["opcodes"]}
        self.assertIn(255, opcodes)
        self.assertNotIn(0, opcodes)  # base class

    def test_raw_send_http_ws_config(self):
        self.assertIn(11, [s["opcode"] for s in self.inv["raw_sends"]])
        urls = {h["url_resolved"] for h in self.inv["http"]}
        self.assertIn("{ACCOUNT_SERVER} + '/servers/get'", urls)
        self.assertIn("{ACCOUNT_SERVER} + '/auth/login/google'", urls)
        login = next(h for h in self.inv["http"] if "login" in h["url_resolved"])
        self.assertEqual(login["body"]["json"]["keys"], ["accessToken", "platform"])
        self.assertEqual(self.inv["websockets"][0]["url_built_as"], "'ws://' + server.url")
        self.assertEqual(self.inv["regions"][0]["name"], "Alpha 1")

    def test_markdown(self):
        text = ecp.render_markdown(self.inv)
        self.assertIn("| 1 | `Join.send`", text)
        self.assertIn("sessionId:String", text)
        self.assertIn("Sub-switch inside opcode 23", text)


if __name__ == "__main__":
    unittest.main()
