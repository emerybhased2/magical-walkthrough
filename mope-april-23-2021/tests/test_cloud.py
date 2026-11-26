"""Offline tests for hosting the backend: region URL modes, remote settings, and the edge.

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common, settings  # noqa: E402

try:
    import aiohttp  # noqa: F401
    from aiohttp.test_utils import TestClient, TestServer
    from services import edge
except ImportError:  # pragma: no cover
    edge = None

PROJECT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("test-cloud")


def config(**overrides) -> dict:
    base = {"public_host": "local.mope.io", "bind_host": "127.0.0.1", "web": {"port": 8080},
            "master": {"port": 8090}, "account": {"port": 8091, "enabled": True},
            "regions": [{"id": "r1", "name": "R1", "region": "Land", "port": 7001}]}
    base.update(overrides)
    return base


class UrlModeTests(unittest.TestCase):
    def test_local_default_is_host_port(self):
        self.assertEqual(common.region_public_url(config(), config()["regions"][0]), "local.mope.io:7001")

    def test_path_routing_for_hosted_backend(self):
        cfg = config(public_host="x.sevalla.app", region_routing="path")
        self.assertEqual(common.region_public_url(cfg, {"id": "r1"}), "x.sevalla.app/r/r1")

    def test_remote_mode_uses_local_relay(self):
        cfg = config(remote={"url": "https://x.sevalla.app", "key": "k", "relay_port": 7000})
        self.assertEqual(common.region_public_url(cfg, {"id": "r1"}), "local.mope.io:7000/r/r1")
        self.assertEqual(common.master_url(cfg), "https://x.sevalla.app")
        self.assertEqual(common.account_url(cfg), "https://x.sevalla.app")
        self.assertEqual(common.remote_headers(cfg), {common.BACKEND_KEY_HEADER: "k"})

    def test_no_key_no_header(self):
        cfg = config(remote={"url": "https://x", "key": "", "relay_port": 7000})
        self.assertEqual(common.remote_headers(cfg), {})


class RemoteSettingTests(unittest.TestCase):
    def load(self, url):
        env = {"REMOTE_BACKEND_URL": url}
        return settings.load_settings(PROJECT, environ=env, env_file=PROJECT / "missing.env")

    def test_trailing_slash_normalised(self):
        values, _, errors = self.load("https://x.sevalla.app/")
        self.assertEqual(errors, [])
        self.assertEqual(values["REMOTE_BACKEND_URL"], "https://x.sevalla.app")

    def test_path_or_bad_scheme_rejected(self):
        for bad in ("https://x.sevalla.app/api", "ftp://x", "x.sevalla.app"):
            _, _, errors = self.load(bad)
            self.assertTrue(any("REMOTE_BACKEND_URL" in e for e in errors), bad)


@unittest.skipIf(edge is None, "aiohttp not installed")
class EdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from websockets.asyncio.server import serve

        async def echo(ws):
            async for message in ws:
                await ws.send(message)

        self.region = await serve(echo, "127.0.0.1", 0)
        port = self.region.sockets[0].getsockname()[1] if hasattr(self.region, "sockets") else \
            list(self.region.server.sockets)[0].getsockname()[1]
        cfg = config(regions=[{"id": "r1", "name": "R1", "region": "Land", "port": port}])
        with mock.patch.dict(os.environ, {edge.KEY_ENV: "secret-key"}):
            self.edge = edge.Edge(cfg, "cloud", LOG)
        self.client = TestClient(TestServer(self.edge.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.region.close()
        await self.region.wait_closed()

    async def test_health_needs_no_key(self):
        resp = await self.client.get("/__edge/health")
        self.assertEqual(resp.status, 200)
        self.assertTrue((await resp.json())["access_key_required"])

    async def test_key_required(self):
        resp = await self.client.get("/servers/get")
        self.assertEqual(resp.status, 401)

    async def test_internal_master_endpoints_not_forwarded(self):
        resp = await self.client.post("/master/register", headers={common.BACKEND_KEY_HEADER: "secret-key"})
        self.assertEqual(resp.status, 404)

    async def test_websocket_piped_to_region(self):
        ws = await self.client.ws_connect("/r/r1/ping", headers={common.BACKEND_KEY_HEADER: "secret-key"})
        await ws.send_bytes(bytes([255]))
        msg = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual(msg.data, bytes([255]))
        await ws.close()

    async def test_unknown_region_404(self):
        resp = await self.client.get("/r/nope", headers={common.BACKEND_KEY_HEADER: "secret-key"})
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
