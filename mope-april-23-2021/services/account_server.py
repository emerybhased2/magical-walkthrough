#!/usr/bin/env python3
"""
account_server.py -- Phase 4 (tasks 21-22): the local account server.

Implements the account API the archived 2025 client calls (phase2/README.md, task 13), with
the exact response envelopes its code reads. The web gateway forwards these paths to it,
so the client sees them on its own origin (its $config.devAccountServerUrl).

  POST /auth/login/<provider>      {accessToken, platform, sessionId, serverId}
        provider == DEV_TEST_ACCOUNT_SECRET -> the client's test-account hook (developer setting)
        google / facebook / apple / discord -> refused: social sign-in cannot work offline
  POST /auth/@me                   {userId, token, isRefresh}  -> {success, data:{coins, gems, hasVip, ...}}
        ACCOUNT_INFINITE_CURRENCY (default on): coins and gems are shown as 999999999 and nothing is charged
  POST /auth/logout                {userId, sessionId, serverId}
  GET  /shop/get                   -> {items, settings}          (catalogue: ACCOUNT_SHOP_CATALOG)
  POST /shop/purchases             -> {success, data:{settings, purchases:[itemId], wardrobe}}
  POST /shop/buy                   {shopItem}                    -> {success, reason}
  POST /shop/buyAll                {userId, token}               -> {success, bought, skins, coins, gems}
                                   local addition: every skin not owned yet, in one all-or-nothing order
  POST /shop/wardrobe/save         {data}                        -> {success, data}
  GET  /playerSettings_update?userId&passwordToken&itemId -> JSON *text* (the client JSON.parse()s it)
  GET  /addCoins, /resetAccount    developer endpoints (DEV_ADMIN_ENDPOINTS_ENABLED)
  /xsolla/*                        real-money payments: not reconstructed (501)

Local accounts (the replacement for social sign-in):
  GET  /auth/local                 login / registration page
  POST /auth/local/login           {username, password} -> {success, data:{userId, token, userName, avatar}}
  POST /auth/local/register        {username, password} -> same
The page stores the result under the client's own localStorage keys (login_userId,
login_passwordToken, login_name, login_profilePicURL, login_socialNetworkName) and returns to
the game; the unmodified client then verifies it with /auth/@me.
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common  # noqa: E402
from services.account_store import AccountStore, StoreError, client_account_data  # noqa: E402

VERSION = "1.0.0"
SOCIAL_PROVIDERS = {"google", "facebook", "apple", "discord"}
TEST_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
LOGIN_PAGE = Path(__file__).resolve().parent / "static" / "local_login.html"


class AccountService:
    def __init__(self, config: dict, log, store: AccountStore | None = None):
        self.config = config
        self.settings = config["settings"]
        self.log = log
        self.store = store or AccountStore(self.settings["ACCOUNT_DB_PATH"], self.settings)
        self._catalog = (None, None)
        live = self.settings.get("SHOP_LIVE_SKINS_DIR") or ""
        self.live_catalog = Path(live) / "catalog.json" if live else None

    # -- helpers ------------------------------------------------------------------------
    def catalog(self) -> dict:
        """ACCOUNT_SHOP_CATALOG plus, when present, the live skins from import_live_skins.rb."""
        path = Path(self.settings["ACCOUNT_SHOP_CATALOG"])
        live = self.live_catalog if self.live_catalog and self.live_catalog.is_file() else None
        key = (path.stat().st_mtime, live.stat().st_mtime if live else None)
        if self._catalog[0] != key:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = list(data.get("items", []))
            if live:
                try:
                    known = {str(i.get("id")) for i in items}
                    extra = [i for i in json.loads(live.read_text(encoding="utf-8")).get("items", [])
                             if str(i.get("id")) not in known]
                    items += extra
                    self.log.info("shop: %d live skins added from %s", len(extra), live)
                except (OSError, ValueError) as exc:
                    self.log.error("shop: cannot read %s (%s); serving the base catalogue only", live, exc)
            self._catalog = (key, {"items": items, "settings": data.get("settings", [])})
        return self._catalog[1]

    @property
    def infinite(self) -> bool:
        return bool(self.settings.get("ACCOUNT_INFINITE_CURRENCY"))

    def authed(self, body: dict) -> dict:
        user = self.store.check_token(body.get("userId"), body.get("token") or body.get("passwordToken"))
        if user is None:
            raise PermissionError("Your login has expired; please log in again.")
        return user

    def login_result(self, user: dict, provider: str) -> dict:
        if user["banned"]:
            return {"success": False, "error": "This account is banned.", "coins": "BANNED"}
        token = self.store.issue_token(user["id"])
        self.log.info("login ok: %s (%s, %s)", user["username"], user["id"], provider)
        return {"success": True, "data": {**client_account_data(user, self.infinite), "token": token}}

    # -- endpoints --------------------------------------------------------------------------
    def social_or_test_login(self, provider: str, body: dict) -> tuple[int, dict]:
        if not self.settings["ACCOUNT_LOGIN_ENABLED"]:
            return 403, {"success": False, "error": "Logins are disabled on this server."}
        secret = self.settings["DEV_TEST_ACCOUNT_SECRET"]
        if self.settings["DEV_TEST_ACCOUNTS_ENABLED"] and secret and hmac.compare_digest(provider, secret):
            user_name = str(body.get("accessToken") or "")
            if not TEST_USER_RE.match(user_name):
                return 400, {"success": False, "error": "Invalid test user name."}
            user_id = secret + user_name  # the client compares its stored userId with secret + user
            try:
                user = self.store.get_user(user_id) or self.store.create_user(f"test-{user_name}", None, "test", user_id)
            except StoreError as exc:
                return 400, {"success": False, "error": str(exc)}
            return 200, self.login_result(user, "test")
        if provider.lower() in SOCIAL_PROVIDERS:
            return 501, {"success": False, "error": f"{provider} sign-in is not available on the local server - "
                                                    f"use 'Login with a local account' ({common.web_origin(self.config)}/auth/local)"}
        return 404, {"success": False, "error": "Unknown login provider."}

    def local_login(self, body: dict, register: bool) -> tuple[int, dict]:
        if not self.settings["ACCOUNT_LOGIN_ENABLED"]:
            return 403, {"success": False, "error": "Logins are disabled on this server."}
        username, password = str(body.get("username", "")), str(body.get("password", ""))
        try:
            if register:
                if not self.settings["ACCOUNT_REGISTRATION_ENABLED"]:
                    return 403, {"success": False, "error": "Registration is disabled on this server."}
                user = self.store.create_user(username, password, "local")
                self.log.info("registered %s (%s)", user["username"], user["id"])
            else:
                user = self.store.check_password_login(username, password)
        except StoreError as exc:
            return 400, {"success": False, "error": str(exc)}
        result = self.login_result(user, "local")
        return (200 if result["success"] else 403), result

    def me(self, body: dict) -> tuple[int, dict]:
        try:
            user = self.authed(body)
        except PermissionError as exc:
            return 401, {"success": False, "error": str(exc)}
        data = client_account_data(user, self.infinite)
        return 200, {"success": True, "data": data, "coins": data["coins"]}

    def logout(self, body: dict) -> tuple[int, dict]:
        # The client sends only userId here; tokens are not revoked without proof of ownership.
        if body.get("token"):
            self.store.revoke_token(str(body["token"]))
        self.log.info("logout: %s", body.get("userId"))
        return 200, {"success": True}

    def purchases(self, body: dict) -> tuple[int, dict]:
        user = self.authed(body)
        return 200, {"success": True, "data": {"settings": self.store.player_settings(user["id"]),
                                               "purchases": self.store.purchases(user["id"]),
                                               "wardrobe": self.store.wardrobe(user["id"])}}

    @staticmethod
    def charged_price(item: dict) -> int:
        return int((item.get("salePrice") if item.get("saleType") else item.get("price")) or 0)

    def buy(self, body: dict) -> tuple[int, dict]:
        user = self.authed(body)
        item_id = str(body.get("shopItem", ""))
        item = next((i for i in self.catalog()["items"] if str(i.get("id")) == item_id), None)
        if item is None:
            return 404, {"success": False, "error": "That item is not in the local shop catalogue."}
        price = self.charged_price(item)
        try:
            self.store.buy(user["id"], item_id, price, int(item.get("currencyType", 0)), charge=not self.infinite)
        except StoreError as exc:
            return 400, {"success": False, "error": str(exc)}
        self.log.info("purchase: %s bought %s for %s", user["id"], item_id, price)
        return 200, {"success": True, "reason": "purchased"}

    def buy_all(self, body: dict) -> tuple[int, dict]:
        """Buy every skin (shopCategory 0) of the catalogue the account does not own yet, in one order."""
        user = self.authed(body)
        skins = [i for i in self.catalog()["items"]
                 if i.get("shopCategory", 0) == 0 and i.get("canBuy", True) and int(i.get("currencyType", 0)) in (0, 1)]
        order = [(str(i["id"]), self.charged_price(i), int(i.get("currencyType", 0))) for i in skins]
        try:
            bought = self.store.buy_many(user["id"], order, charge=not self.infinite)
        except StoreError as exc:
            return 400, {"success": False, "error": str(exc)}
        self.log.info("purchase: %s bought all skins (%d new of %d)", user["id"], bought, len(order))
        data = client_account_data(self.store.get_user(user["id"]), self.infinite)
        return 200, {"success": True, "reason": "purchased", "bought": bought, "skins": len(order),
                     "coins": data["coins"], "gems": data["gems"]}

    def wardrobe_save(self, body: dict) -> tuple[int, dict]:
        user = self.authed(body)
        try:
            self.store.save_wardrobe(user["id"], body.get("data"))
        except StoreError as exc:
            return 400, {"success": False, "error": str(exc)}
        return 200, {"success": True, "data": self.store.wardrobe(user["id"])}

    def query_user(self, query: dict) -> dict:
        return self.authed({"userId": query.get("userId", [None])[0],
                            "passwordToken": query.get("passwordToken", [None])[0]})

    def player_settings_update(self, query: dict) -> tuple[int, dict]:
        user = self.query_user(query)
        self.store.set_player_setting(user["id"], query.get("itemId", ["none"])[0])
        return 200, {"success": True, "reason": "ok"}

    def add_coins(self, query: dict) -> tuple[int, dict]:
        if not self.settings["DEV_ADMIN_ENDPOINTS_ENABLED"]:
            return 403, {"success": False, "error": "Developer endpoints are disabled."}
        user = self.query_user(query)
        amount = self.settings["DEV_ADD_COINS_AMOUNT"]
        updated = self.store.update_user(user["id"], coins=user["coins"] + amount)
        self.log.info("dev: added %d coins to %s", amount, user["id"])
        return 200, {"success": True, "coins": updated["coins"]}

    def reset_account(self, query: dict) -> tuple[int, dict]:
        if not self.settings["DEV_ADMIN_ENDPOINTS_ENABLED"]:
            return 403, {"success": False, "error": "Developer endpoints are disabled."}
        user = self.query_user(query)
        self.store.reset_account(user["id"])
        self.log.info("dev: reset account %s", user["id"])
        return 200, {"success": True}


def make_handler(service: AccountService):
    log = service.log

    class Handler(BaseHTTPRequestHandler):
        server_version = f"mope-local-account/{VERSION}"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload, ctype: str = "application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 256_000:
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _dispatch(self, method: str):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            try:
                if method == "GET":
                    if path == "/auth/local":
                        return self._send(200, LOGIN_PAGE.read_bytes(), "text/html; charset=utf-8")
                    if path == "/auth/health":
                        return self._send(200, {"ok": True, "service": "account", "version": VERSION,
                                                "schema_version": service.store.schema_version()})
                    if path == "/shop/get":
                        if not service.settings["ACCOUNT_SHOP_ENABLED"]:
                            return self._send(503, {"success": False, "error": "The shop is disabled."})
                        return self._send(200, service.catalog())
                    if path == "/playerSettings_update":
                        status, payload = service.player_settings_update(query)
                        return self._send(status, json.dumps(payload).encode(), "text/plain; charset=utf-8")
                    if path == "/addCoins":
                        return self._send(*service.add_coins(query))
                    if path == "/resetAccount":
                        return self._send(*service.reset_account(query))
                    if path in ("/xsolla/getPackets", "/xsolla/getVip"):
                        # Read on every page load: answer "nothing for sale" quietly (the client only shows an
                        # error toast for non-2xx answers). Paying itself (POST) still explains it is unavailable.
                        return self._send(200, {"success": False, "data": None,
                                                "error": "Real-money payments are not available on the local server."})
                    if path.startswith("/xsolla/"):
                        return self._send(501, {"success": False,
                                                "error": "Real-money payments are not available on the local server."})
                    return self._send(404, {"success": False, "error": "not found"})
                body = self._body()
                if path.startswith("/auth/login/"):
                    return self._send(*service.social_or_test_login(path[len("/auth/login/"):], body))
                routes = {
                    "/auth/local/login": lambda: service.local_login(body, register=False),
                    "/auth/local/register": lambda: service.local_login(body, register=True),
                    "/auth/@me": lambda: service.me(body),
                    "/auth/logout": lambda: service.logout(body),
                    "/shop/purchases": lambda: service.purchases(body),
                    "/shop/buy": lambda: service.buy(body),
                    "/shop/buyAll": lambda: service.buy_all(body),
                    "/shop/wardrobe/save": lambda: service.wardrobe_save(body),
                }
                if path.startswith("/xsolla/"):
                    return self._send(501, {"success": False,
                                            "error": "Real-money payments are not available on the local server."})
                if path.startswith("/shop/") and not service.settings["ACCOUNT_SHOP_ENABLED"]:
                    return self._send(503, {"success": False, "error": "The shop is disabled."})
                handler = routes.get(path)
                if handler is None:
                    return self._send(404, {"success": False, "error": "not found"})
                return self._send(*handler())
            except PermissionError as exc:
                return self._send(401, {"success": False, "error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"success": False, "error": f"bad request: {exc}"})
            except Exception:  # noqa: BLE001 - never leak a traceback to the client
                log.exception("error handling %s %s", method, path)
                return self._send(500, {"success": False, "error": "internal error"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local mope.io account server")
    parser.add_argument("--config", help="stack config (default config/local_stack.json)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    config = common.load_stack_config(args.config)
    log = common.setup_logging(config, "account", args.verbose)
    service = AccountService(config, log)
    s = config["settings"]
    bind, port = config["bind_host"], int(config["account"]["port"])
    server = ThreadingHTTPServer((bind, port), make_handler(service))
    server.daemon_threads = True
    log.info("account server %s on http://%s:%d (db %s, schema v%d; registration %s; test accounts %s; "
             "dev endpoints %s; shop %s)", VERSION, bind, port, s["ACCOUNT_DB_PATH"], service.store.schema_version(),
             "on" if s["ACCOUNT_REGISTRATION_ENABLED"] else "off", "on" if s["DEV_TEST_ACCOUNTS_ENABLED"] else "off",
             "on" if s["DEV_ADMIN_ENDPOINTS_ENABLED"] else "off", "on" if s["ACCOUNT_SHOP_ENABLED"] else "off")

    def shutdown(signum, frame):
        log.info("account server stopping (signal %s)", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    started = time.time()
    server.serve_forever()
    server.server_close()
    log.info("account server stopped after %.0fs", time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
