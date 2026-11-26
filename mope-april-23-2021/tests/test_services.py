"""Offline tests for the Phase 3 stack (codec, master registry, gateway rewrites).

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common  # noqa: E402
from services.master_server import Registry  # noqa: E402

LOG = logging.getLogger("test-services")


def stack_config(**overrides) -> dict:
    config = {
        "public_host": "local.mope.io", "bind_host": "127.0.0.1", "client_root": "x",
        "web": {"port": 8080}, "master": {"port": 8090, "heartbeat_interval_seconds": 2,
                                          "heartbeat_timeout_seconds": 6},
        "game": {"version": 296},
        "regions": [{"id": "r1", "name": "Region 1", "region": "Land", "gm": 0, "port": 7001},
                    {"id": "r2", "name": "Region 2", "region": "Land", "gm": 2, "port": 7002}],
        "_root": "/tmp",
    }
    config.update(overrides)
    return config


class CodecTests(unittest.TestCase):
    def test_join_matches_captured_client_bytes(self):
        # captured from the archived client in phase2 (data/task8_probe.json)
        self.assertEqual(common.build_join().hex(), "0105000320050000000000")
        self.assertEqual(common.parse_join(bytes.fromhex("0105000320050000000000")),
                         {"canvas_width": 1280, "canvas_height": 800, "inner_width": 1280, "session_id": ""})

    def test_play_matches_captured_client_bytes(self):
        captured = bytes.fromhex("71050003200007" + b"mope.io".hex())
        self.assertEqual(common.parse_play(captured), {"width": 1280, "height": 800, "nickname": "mope.io"})
        self.assertEqual(common.build_play(), captured)

    def test_server_strings_have_the_trailing_byte_the_client_drops(self):
        data = common.PacketWriter().string_s2c("héllo").bytes()
        self.assertEqual(int.from_bytes(data[:2], "big"), len("héllo".encode()) + 1)
        self.assertEqual(common.PacketReader(data).string_s2c(), "héllo")
        self.assertEqual(common.PacketReader(common.PacketWriter().string_s2c("").bytes()).string_s2c(), "")

    def test_server_info_layout(self):
        reader = common.PacketReader(common.msg_server_info("abc", 296, 2, 1, "r1", alive=True))
        self.assertEqual([reader.u8(), reader.string_s2c(), reader.u16(), reader.u8(), reader.u8(),
                          reader.string_s2c(), reader.u8(), reader.u16()], [2, "abc", 296, 2, 1, "r1", 1, 0])
        self.assertEqual(reader.remaining, 0)

    def test_disconnect_layout(self):
        reader = common.PacketReader(common.msg_disconnect("bye", code=0))
        self.assertEqual([reader.u8(), reader.u8(), reader.string_s2c(), reader.u8()], [11, 0, "bye", 0])

    def test_short_packets_raise(self):
        with self.assertRaises(ValueError):
            common.parse_join(b"\x01\x00")


class ConfigTests(unittest.TestCase):
    def test_duplicate_ports_and_ids(self):
        config = stack_config()
        config["regions"].append({"id": "r1", "name": "dup", "region": "x", "port": 8080})
        errors = common.validate_stack_config(config)
        self.assertTrue(any("duplicate region id" in e for e in errors))
        self.assertTrue(any("port 8080" in e for e in errors))

    def test_public_urls(self):
        config = stack_config()
        self.assertEqual(common.region_public_url(config, config["regions"][0]), "local.mope.io:7001")
        self.assertEqual(common.web_origin(config), "http://local.mope.io:8080")

    def test_repo_config_is_valid(self):
        config = common.load_stack_config()
        archived = [r for r in config["regions"] if r["origin"] == "archived-2025-12-10"]
        added = [r for r in config["regions"] if r["origin"] == "local-addition"]
        self.assertEqual(len(archived), 9)
        self.assertEqual(archived[0]["id"], "prod-lnd-us-ny-1")
        self.assertEqual(len(added), 11)
        self.assertTrue(all(r["id"].startswith("local-") for r in added))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(stack_config(), LOG)

    def status(self, region_id):
        return next(r for r in self.registry.snapshot() if r["id"] == region_id)["status"]

    def test_lifecycle(self):
        self.assertEqual(self.status("r1"), "unknown")
        code, _ = self.registry.upsert({"id": "r1", "port": 7001, "pid": 1}, heartbeat=False)
        self.assertEqual(code, 200)
        self.assertEqual(self.status("r1"), "online")
        self.registry.upsert({"id": "r1", "port": 7001, "players": 4}, heartbeat=True)
        self.assertEqual(self.registry.client_stats(), [{"id": "r1", "totalPlayerCount": 4}])
        self.registry.regions["r1"]["last_heartbeat"] = time.time() - 60
        self.assertEqual(self.status("r1"), "offline")
        self.assertEqual(self.registry.client_stats(), [])
        self.registry.upsert({"id": "r1", "port": 7001, "players": 1}, heartbeat=True)
        self.assertEqual(self.status("r1"), "online")
        self.registry.deregister({"id": "r1", "reason": "test"})
        self.assertEqual(self.status("r1"), "offline")

    def test_rejects_unknown_and_wrong_port(self):
        self.assertEqual(self.registry.upsert({"id": "nope", "port": 1}, heartbeat=True)[0], 404)
        self.assertEqual(self.registry.upsert({"id": "r1", "port": 9999}, heartbeat=True)[0], 409)

    def test_client_config(self):
        overlay = self.registry.client_config()
        self.assertEqual([s["url"] for s in overlay["gameServers"]], ["local.mope.io:7001", "local.mope.io:7002"])
        self.assertEqual(overlay["gameServers"][1]["gm"], 2)
        self.assertEqual(overlay["devAccountServerUrl"], "http://local.mope.io:8080")
        self.assertIs(overlay["useDevAccountServer"], True)


try:
    from services.web_gateway import ClientConfigRewriter, Gateway
    import rebuild_client_js as rb
    HAVE_TREE_SITTER = not rb._MISSING
except ImportError:  # pragma: no cover
    HAVE_TREE_SITTER = False


@unittest.skipUnless(HAVE_TREE_SITTER, "tree-sitter not installed")
class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_rewrite_keeps_the_rest_of_the_file(self):
        path = self.root / "client.js"
        path.write_text("$config={gameVersion:0x128,isBetaMode:![],gameServers:[{id:'a',url:'prod.example'}]};"
                        "const a0_0xab04ee={run(){}};a0_0xab04ee.run();", encoding="utf-8")
        rewriter = ClientConfigRewriter(path, LOG)
        out = rewriter.render({"gameServers": [{"id": "a", "url": "local.mope.io:7001"}],
                               "useDevAccountServer": True}).decode()
        head, rest = out.split(";", 1)
        merged = json.loads(head[len("$config="):])
        self.assertEqual(merged["gameVersion"], 296)
        self.assertIs(merged["isBetaMode"], False)
        self.assertEqual(merged["gameServers"][0]["url"], "local.mope.io:7001")
        self.assertEqual(rest, "const a0_0xab04ee={run(){}};a0_0xab04ee.run();")

    def test_config_rewrite_refuses_non_literals(self):
        path = self.root / "client.js"
        path.write_text("$config={a:compute()};", encoding="utf-8")
        with self.assertRaises(ValueError):
            ClientConfigRewriter(path, LOG).render({})

    def test_static_paths_stay_inside_the_root(self):
        (self.root / "index.html").write_text("<html></html>")
        (self.root / "img").mkdir()
        (self.root / "img" / "a.png").write_bytes(b"x")
        gateway = Gateway(stack_config(client_root=str(self.root)), LOG)
        self.assertEqual(gateway.static_path("/img/a.png"), (self.root / "img" / "a.png").resolve())
        self.assertEqual(gateway.static_path("/"), (self.root / "index.html").resolve())
        self.assertIsNone(gateway.static_path("/../../etc/passwd"))
        self.assertIsNone(gateway.static_path("/missing.js"))

    def test_asset_fallbacks_come_after_the_snapshot(self):
        (self.root / "index.html").write_text("<html></html>")
        (self.root / "img").mkdir()
        (self.root / "img" / "a.png").write_bytes(b"snapshot")
        extra = self.root.parent / f"{self.root.name}-fallback"
        (extra / "img").mkdir(parents=True)
        (extra / "img" / "a.png").write_bytes(b"fallback")
        (extra / "img" / "b.png").write_bytes(b"fallback")
        try:
            config = stack_config(client_root=str(self.root),
                                  web={"port": 8080, "asset_fallbacks": [str(extra), str(extra / "missing-dir")]})
            gateway = Gateway(config, LOG)
            self.assertEqual(gateway.static_path("/img/a.png"), (self.root / "img" / "a.png").resolve())
            self.assertEqual(gateway.static_path("/img/b.png"), (extra / "img" / "b.png").resolve())
            self.assertTrue(gateway.source_of(gateway.static_path("/img/b.png")).startswith("fallback:"))
            self.assertEqual(gateway.source_of(gateway.static_path("/img/a.png")), "snapshot")
            self.assertIsNone(gateway.static_path(f"/../{extra.name}/img/b.png"))
            self.assertIsNone(gateway.static_path("/img/c.png"))
        finally:
            import shutil
            shutil.rmtree(extra)

    def test_index_gets_the_domain_shim_first_and_the_local_scripts(self):
        (self.root / "index.html").write_text("<html><head><meta charset=utf-8></head><body></body></html>")
        gateway = Gateway(stack_config(client_root=str(self.root)), LOG)
        gateway.account = "http://127.0.0.1:8091"
        page, notes = gateway.rewrite_index((self.root / "index.html").read_bytes())
        head = page.index(b"<head>")
        self.assertEqual(page.index(b"/__local/domain_lock_local.js"), head + len(b"<head>\n") + len(b'<script src="'))
        self.assertLess(page.index(b"/__local/shop_buy_all.js"), page.index(b"</body>"))
        self.assertIn("domain-lock shim (1)", notes)
        gateway.rewrites["domain_lock_shim"] = False
        self.assertNotIn(b"domain_lock_local", gateway.rewrite_index(b"<html><head></head></html>")[0])

    def test_domain_shim_property_is_enumerable(self):
        # the client's guard finds document.domain with for...in; a non-enumerable own property hides it
        shim = (Path(__file__).resolve().parents[1] / "shims" / "domain_lock_local.js").read_text()
        self.assertIn("enumerable: true", shim)
        self.assertIn("localhost.mope.io", shim)


if __name__ == "__main__":
    unittest.main()
