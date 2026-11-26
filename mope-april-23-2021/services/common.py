"""
Shared pieces for the local mope.io stack (Phase 3): configuration, logging, the binary
packet codec (byte-compatible with the archived client's reader/writer) and small HTTP
helpers for service-to-service calls.

Wire format (from phase2/README.md, task 12):
  * every packet starts with a UInt8 opcode; integers are big-endian
  * client -> server strings: UInt16 byte length + UTF-8 bytes
  * server -> client strings: UInt16 length + bytes; the client DROPS the last byte, so the
    server sends len(utf8) + 1 and one trailing filler byte
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "local_stack.json"
TOKEN_ENV = "LOCAL_STACK_TOKEN"
BACKEND_KEY_HEADER = "X-Backend-Key"

# Opcodes used by the Phase 3 servers (see phase2/OPCODES.md).
OP_JOIN = 1                # C->S  canvasW, canvasH, innerWidth (UInt16 x3), sessionId (String)
OP_SERVER_INFO = 2         # S->C  sessionId, gameVersion, gameMode, season, serverId, isAliveInGame, staticObjs
OP_CONNECTED = 3           # S->C  (no payload) client shows the menu
OP_LEAVE = 11              # C->S  clean leave / S->C disconnect(code, reason, flag)
OP_NOTIFICATION = 16       # S->C  String, shown as a success toast
OP_CAPTCHA = 64            # C->S  Turnstile token
OP_PLAY = 113              # C->S  width, height (UInt16), nickname (String)
OP_PING = 255              # both  ping / pong on the /ping socket


class ConfigError(Exception):
    pass


# =============================================================================
# Configuration
# =============================================================================

def load_stack_config(path: str | Path | None = None) -> dict:
    path = Path(path).resolve() if path else DEFAULT_CONFIG
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"stack config not found: {path}") from exc
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    root = path.parent.parent if path.parent.name == "config" else path.parent
    config["_path"] = str(path)
    config["_root"] = str(root)
    from services import settings as settings_mod  # local import: settings has no stack dependencies
    values, sources, setting_errors = settings_mod.load_settings(root)
    config["settings"], config["settings_sources"] = values, sources
    if values["SANDBOX_REGION_ENABLED"]:
        # The client's own (unused) hook: addServer('LOCAL', 'localhost', 'SANDBOX').
        config["regions"].append({"id": "localhost", "name": "LOCAL", "region": "SANDBOX", "gm": 0,
                                  "port": values["SANDBOX_REGION_PORT"], "enabled": True,
                                  "origin": "client-sandbox-hook"})
    if values["REMOTE_BACKEND_URL"]:
        # Remote mode: only the web gateway and the WebSocket relay run on this machine.
        config["remote"] = {"url": values["REMOTE_BACKEND_URL"], "key": values["REMOTE_BACKEND_KEY"],
                            "relay_port": int(values["REMOTE_RELAY_PORT"])}
    errors = setting_errors + validate_stack_config(config)
    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
    return config


def validate_stack_config(config: dict) -> list[str]:
    errors = []
    for key in ("public_host", "bind_host", "client_root", "web", "master", "regions", "game"):
        if key not in config:
            errors.append(f"missing key: {key}")
    if errors:
        return errors
    ports = {}
    for name in ("web", "master", "account"):
        if name in config and config[name].get("port"):
            ports.setdefault(int(config[name]["port"]), []).append(name)
    if config.get("remote"):
        ports.setdefault(int(config["remote"]["relay_port"]), []).append("relay")
        # regions are remote: their local ports are unused
        for users in ports.values():
            users[:] = [u for u in users if u in ("web", "master", "account", "relay")]
    ids = set()
    for region in config["regions"]:
        for key in ("id", "name", "region", "port"):
            if key not in region:
                errors.append(f"region entry missing {key}: {region}")
        if region.get("id") in ids:
            errors.append(f"duplicate region id {region.get('id')}")
        ids.add(region.get("id"))
        if region.get("port"):
            ports.setdefault(int(region["port"]), []).append(region.get("id"))
    for port, users in ports.items():
        if len(users) > 1:
            errors.append(f"port {port} is used by more than one service: {', '.join(map(str, users))}")
        if not 1 <= port <= 65535:
            errors.append(f"port out of range: {port}")
    return errors


def project_path(config: dict, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(config["_root"]) / path


def region_by_id(config: dict, region_id: str) -> dict:
    for region in config["regions"]:
        if region["id"] == region_id:
            return region
    raise ConfigError(f"unknown region id {region_id!r}")


def web_origin(config: dict) -> str:
    port = int(config["web"]["port"])
    return f"http://{config['public_host']}" + ("" if port == 80 else f":{port}")


def region_public_url(config: dict, region: dict) -> str:
    """The `url` the client puts after ws(s):// (it appends /ping for the ping socket).

    local (default):  host:port, one port per region
    remote mode:      host:<relay port>/r/<id>, the local relay forwards to the remote backend
    path routing:     host/r/<id>, the hosted backend's single public port (cloud.py)
    """
    if config.get("remote"):
        return f"{config['public_host']}:{int(config['remote']['relay_port'])}/r/{region['id']}"
    if config.get("region_routing") == "path":
        return f"{config['public_host']}/r/{region['id']}"
    return f"{config['public_host']}:{int(region['port'])}"


def master_url(config: dict) -> str:
    if config.get("remote"):
        return config["remote"]["url"]
    return f"http://127.0.0.1:{int(config['master']['port'])}"


def account_url(config: dict) -> str | None:
    if config.get("remote"):
        return config["remote"]["url"]
    account = config.get("account", {})
    return f"http://127.0.0.1:{int(account['port'])}" if account.get("enabled") else None


def remote_headers(config: dict) -> dict:
    """Headers for requests to a remote backend (its access key, when one is set)."""
    remote = config.get("remote") or {}
    return {BACKEND_KEY_HEADER: remote["key"]} if remote.get("key") else {}


def run_dir(config: dict) -> Path:
    path = project_path(config, config.get("run_dir", "run"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def stack_token(config: dict, create: bool = False) -> str:
    """Shared secret for the master's internal (registration) endpoints."""
    if os.environ.get(TOKEN_ENV):
        return os.environ[TOKEN_ENV]
    path = run_dir(config) / "local_stack_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if not create:
        raise ConfigError(f"no stack token: set {TOKEN_ENV} or start the stack with localstack.py")
    token = secrets.token_hex(16)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


# =============================================================================
# Logging
# =============================================================================

def setup_logging(config: dict, service: str, verbose: bool = False) -> logging.Logger:
    logs = project_path(config, config.get("logs_dir", "logs")) / "services"
    logs.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"stack.{service}")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    fmt = logging.Formatter(f"%(asctime)s %(levelname)-7s [{service}] %(message)s")
    # Central log folder with rotation: logs/services/<service>.log (+ .1 ... .5), 5 MB each.
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(logs / f"{service}.log", maxBytes=5 * 1024 * 1024, backupCount=5,
                                       encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(console)
    return log


# =============================================================================
# Binary codec (mirrors the client's _0x2293a6 writer / _0x4dbf94 reader)
# =============================================================================

class PacketWriter:
    def __init__(self, opcode: int | None = None):
        self.buf = bytearray()
        if opcode is not None:
            self.u8(opcode)

    def u8(self, value: int) -> "PacketWriter":
        self.buf += struct.pack(">B", value & 0xFF)
        return self

    def u16(self, value: int) -> "PacketWriter":
        self.buf += struct.pack(">H", value & 0xFFFF)
        return self

    def i16(self, value: int) -> "PacketWriter":
        self.buf += struct.pack(">h", value)
        return self

    def u32(self, value: int) -> "PacketWriter":
        self.buf += struct.pack(">I", value & 0xFFFFFFFF)
        return self

    def string_s2c(self, text: str) -> "PacketWriter":
        """Server -> client string: length counts one trailing byte that the client drops."""
        data = text.encode("utf-8")
        self.u16(len(data) + 1)
        self.buf += data + b"\x00"
        return self

    def string_c2s(self, text: str) -> "PacketWriter":
        """Client -> server string (used by tests that play the client's role)."""
        data = text.encode("utf-8")
        self.u16(len(data))
        self.buf += data
        return self

    def bytes(self) -> bytes:
        return bytes(self.buf)


class PacketReader:
    def __init__(self, data: bytes):
        self.data = bytes(data)
        self.offset = 0

    def _take(self, size: int) -> bytes:
        if self.offset + size > len(self.data):
            raise ValueError(f"packet too short: need {size} bytes at offset {self.offset}, have {len(self.data)}")
        chunk = self.data[self.offset:self.offset + size]
        self.offset += size
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self._take(2))[0]

    def i16(self) -> int:
        return struct.unpack(">h", self._take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def string_c2s(self) -> str:
        length = self.u16()
        return self._take(length).decode("utf-8", "replace")

    def string_s2c(self) -> str:
        """Exactly what the client does: read `length` bytes, drop the last one."""
        length = self.u16()
        raw = self._take(length)
        return raw[:-1].decode("utf-8", "replace") if length else ""

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset


# -- server -> client messages -------------------------------------------------

def msg_server_info(session_id: str, game_version: int, game_mode: int, season: int, server_id: str,
                    alive: bool = False) -> bytes:
    return (PacketWriter(OP_SERVER_INFO).string_s2c(session_id).u16(game_version).u8(game_mode).u8(season)
            .string_s2c(server_id).u8(1 if alive else 0).u16(0).bytes())  # 0 static objects


def msg_connected() -> bytes:
    return bytes([OP_CONNECTED])


def msg_notification(text: str) -> bytes:
    return PacketWriter(OP_NOTIFICATION).string_s2c(text).bytes()


def msg_disconnect(reason: str, code: int = 0, reload_page: bool = False) -> bytes:
    """code 0 + reload False: client shows the reason and re-joins the best other server."""
    return PacketWriter(OP_LEAVE).u8(code).string_s2c(reason).u8(1 if reload_page else 0).bytes()


# -- client -> server messages -------------------------------------------------

def parse_join(data: bytes) -> dict:
    reader = PacketReader(data)
    if reader.u8() != OP_JOIN:
        raise ValueError("not a join packet")
    width, height, inner_width = reader.u16(), reader.u16(), reader.u16()
    session = reader.string_c2s() if reader.remaining >= 2 else ""
    return {"canvas_width": width, "canvas_height": height, "inner_width": inner_width,
            "session_id": session.strip("\x00")}


def parse_play(data: bytes) -> dict:
    reader = PacketReader(data)
    if reader.u8() != OP_PLAY:
        raise ValueError("not a play packet")
    width, height = reader.u16(), reader.u16()
    return {"width": width, "height": height, "nickname": reader.string_c2s()}


def build_join(width: int = 1280, height: int = 800, inner_width: int = 1280, session_id: str = "") -> bytes:
    """A join packet exactly as the archived client builds it (buffer 9 + 2 + len)."""
    data = PacketWriter(OP_JOIN).u16(width).u16(height).u16(inner_width).string_c2s(session_id).bytes()
    return data + b"\x00\x00"


def build_play(nickname: str = "mope.io", width: int = 1280, height: int = 800) -> bytes:
    return PacketWriter(OP_PLAY).u16(width).u16(height).string_c2s(nickname).bytes()


# =============================================================================
# Local HTTP helpers (never use the environment's proxy for loopback calls)
# =============================================================================

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method: str, url: str, payload: dict | None = None, token: str | None = None,
              timeout: float = 3.0) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Local-Stack-Token", token)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body) if body else None
        except ValueError:
            return exc.code, body.decode("utf-8", "replace")


def http_raw(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 5.0) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
