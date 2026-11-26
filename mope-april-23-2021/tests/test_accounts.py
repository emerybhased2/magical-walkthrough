"""Offline tests for Phase 4: settings, account storage and the account API.

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import settings as settings_mod  # noqa: E402
from services.account_server import AccountService, make_handler  # noqa: E402
from services.account_store import AccountStore, StoreError, hash_password, verify_password  # noqa: E402

LOG = logging.getLogger("test-accounts")
LOG.addHandler(logging.NullHandler())


def make_settings(root: Path, **overrides) -> dict:
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "shop_catalog.json").write_text(json.dumps({"items": [
        {"id": "skinA", "price": 50, "currencyType": 0, "data": {"shopCategory": 0, "itemType": 0}},
        {"id": "gemSkin", "price": 5, "currencyType": 1, "data": {"shopCategory": 0, "itemType": 0}},
        {"id": "saleSkin", "price": 100, "saleType": 1, "salePrice": 10, "currencyType": 0, "data": {}},
    ], "settings": [{"name": "x"}]}))
    (root / "config" / "updates.json").write_text("[]")
    environ = {k: str(v) for k, v in overrides.items()}
    values, _, errors = settings_mod.load_settings(root, environ=environ)
    if errors:
        raise AssertionError(errors)
    return values


class SettingsTests(unittest.TestCase):
    def test_defaults_env_file_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_settings(root)
            (root / ".env").write_text("# comment\nACCOUNT_STARTING_COINS=25\nCLIENT_ENV='staging'\n"
                                       "ACCOUNT_SESSION_TTL_DAYS=7 # inline comment\n")
            values, sources, errors = settings_mod.load_settings(root, environ={"ACCOUNT_STARTING_COINS": "40"})
            self.assertEqual(errors, [])
            self.assertEqual(values["ACCOUNT_STARTING_COINS"], 40)
            self.assertEqual(sources["ACCOUNT_STARTING_COINS"], "environment")
            self.assertEqual(values["CLIENT_ENV"], "staging")
            self.assertEqual(values["ACCOUNT_SESSION_TTL_DAYS"], 7)
            self.assertEqual(sources["ACCOUNT_PASSWORD_MIN_LENGTH"], "default")

    def test_all_errors_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_settings(root)
            (root / ".env").write_text("ACCOUNT_TYPO=1\nNOEQUALS\n")
            _, _, errors = settings_mod.load_settings(root, environ={
                "ACCOUNT_REGISTRATION_ENABLED": "maybe", "ACCOUNT_PASSWORD_MIN_LENGTH": "2",
                "CLIENT_ENV": "prod", "DEV_TEST_ACCOUNTS_ENABLED": "true", "DEV_TEST_ACCOUNT_SECRET": "short",
                "ACCOUNT_USERNAME_MIN_LENGTH": "12", "ACCOUNT_USERNAME_MAX_LENGTH": "5"})
            text = "\n".join(errors)
            for fragment in ("unknown setting ACCOUNT_TYPO", "expected KEY=value", "must be true/false",
                             "must be >= 4", "must be one of", "DEV_TEST_ACCOUNT_SECRET", "cannot be larger"):
                self.assertIn(fragment, text)

    def test_example_file_lists_every_setting(self):
        example = settings_mod.example_file()
        for setting in settings_mod.SETTINGS:
            self.assertIn(f"{setting.name}=", example)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tmp.name), ACCOUNT_STARTING_COINS=100, ACCOUNT_LOGIN_MAX_FAILURES=3)
        self.store = AccountStore(Path(self.tmp.name) / "db.sqlite3", self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def test_password_hashing(self):
        stored = hash_password("secret pass")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(verify_password("secret pass", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("x", None))

    def test_migrations_are_idempotent(self):
        self.assertEqual(self.store.schema_version(), 1)
        self.assertEqual(AccountStore(self.store.path, self.settings).migrate(), [])

    def test_users_and_validation(self):
        user = self.store.create_user("Alice", "password1")
        self.assertEqual(user["coins"], 100)
        with self.assertRaises(StoreError):
            self.store.create_user("alice", "password2")  # case-insensitive unique
        with self.assertRaises(StoreError):
            self.store.create_user("a", "password1")
        with self.assertRaises(StoreError):
            self.store.create_user("bob", "short")
        with self.assertRaises(StoreError):
            self.store.create_user("bad name!", "password1")

    def test_login_lockout(self):
        self.store.create_user("carol", "password1")
        for _ in range(3):
            with self.assertRaises(StoreError):
                self.store.check_password_login("carol", "nope")
        with self.assertRaisesRegex(StoreError, "Too many"):
            self.store.check_password_login("carol", "password1")

    def test_tokens(self):
        user = self.store.create_user("dave", "password1")
        token = self.store.issue_token(user["id"])
        self.assertEqual(self.store.check_token(user["id"], token)["id"], user["id"])
        self.assertIsNone(self.store.check_token("someone-else", token))
        with self.store.db() as conn:
            conn.execute("UPDATE tokens SET expires_at = ?", (time.time() - 1,))
        self.assertIsNone(self.store.check_token(user["id"], token))
        token2 = self.store.issue_token(user["id"])
        self.store.revoke_token(token2)
        self.assertIsNone(self.store.check_token(user["id"], token2))

    def test_buy(self):
        user = self.store.create_user("erin", "password1")
        self.store.buy(user["id"], "skinA", 60, 0)
        self.assertEqual(self.store.get_user(user["id"])["coins"], 40)
        with self.assertRaisesRegex(StoreError, "already own"):
            self.store.buy(user["id"], "skinA", 1, 0)
        with self.assertRaisesRegex(StoreError, "Not enough"):
            self.store.buy(user["id"], "big", 1000, 0)
        self.assertEqual(self.store.purchases(user["id"]), ["skinA"])

    def test_buy_many_is_all_or_nothing(self):
        user = self.store.create_user("gina", "password1")
        self.store.update_user(user["id"], coins=100, gems=5)
        with self.assertRaisesRegex(StoreError, "Not enough gems"):
            self.store.buy_many(user["id"], [("a", 50, 0), ("g", 6, 1)])
        self.assertEqual((self.store.purchases(user["id"]), self.store.get_user(user["id"])["coins"]), ([], 100))
        self.assertEqual(self.store.buy_many(user["id"], [("a", 50, 0), ("g", 5, 1), ("a", 50, 0)]), 2)
        self.assertEqual(self.store.buy_many(user["id"], [("a", 50, 0), ("b", 50, 0)]), 1)
        after = self.store.get_user(user["id"])
        self.assertEqual((after["coins"], after["gems"]), (0, 0))
        self.assertEqual(self.store.buy_many(user["id"], [("c", 10 ** 6, 0)], charge=False), 1)
        self.assertEqual(self.store.get_user(user["id"])["coins"], 0)


class ApiTests(unittest.TestCase):
    """The real HTTP handler on an ephemeral port."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        settings = make_settings(root, ACCOUNT_STARTING_COINS=100, DEV_TEST_ACCOUNTS_ENABLED="true",
                                 DEV_TEST_ACCOUNT_SECRET="sekret_123", DEV_ADMIN_ENDPOINTS_ENABLED="true",
                                 DEV_ADD_COINS_AMOUNT=7, ACCOUNT_INFINITE_CURRENCY="false")
        config = {"settings": settings, "public_host": "local.mope.io", "web": {"port": 8080}}
        self.service = AccountService(config, LOG, AccountStore(root / "db.sqlite3", settings))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path: str, body: dict | None = None):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None
                                         else None, headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, response.headers.get("Content-Type"), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), json.loads(exc.read())

    def login(self):
        status, _, body = self.call("/auth/local/register", {"username": "frank", "password": "password1"})
        self.assertEqual(status, 200)
        return body["data"]["userId"], body["data"]["token"]

    def test_login_me_logout(self):
        user_id, token = self.login()
        status, _, body = self.call("/auth/@me", {"userId": user_id, "token": token, "isRefresh": True})
        self.assertEqual((status, body["success"], body["data"]["coins"], body["coins"]), (200, True, 100, 100))
        self.assertEqual(self.call("/auth/@me", {"userId": user_id, "token": "nope"})[0], 401)
        status, _, body = self.call("/auth/local/login", {"username": "FRANK", "password": "password1"})
        self.assertTrue(body["success"])
        self.assertEqual(self.call("/auth/logout", {"userId": user_id})[2], {"success": True})

    def test_social_and_unknown_providers(self):
        status, _, body = self.call("/auth/login/google", {"accessToken": "x"})
        self.assertEqual(status, 501)
        self.assertIn("local account", body["error"])
        self.assertEqual(self.call("/auth/login/whatever", {"accessToken": "x"})[0], 404)

    def test_test_account_hook(self):
        status, _, body = self.call("/auth/login/sekret_123", {"accessToken": "ab", "platform": "web"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["userId"], "sekret_123ab")  # client compares with secret + user
        self.assertEqual(body["data"]["userName"], "test-ab")
        again = self.call("/auth/login/sekret_123", {"accessToken": "ab"})[2]
        self.assertEqual(again["data"]["userId"], "sekret_123ab")
        # a different secret with the same short user must not collide (regression)
        self.service.settings["DEV_TEST_ACCOUNT_SECRET"] = "other_secret_9"
        status, _, other = self.call("/auth/login/other_secret_9", {"accessToken": "ab"})
        self.assertEqual((status, other["data"]["userId"]), (200, "other_secret_9ab"))

    def test_buy_all_charges_the_whole_order(self):
        user_id, token = self.login()  # 100 coins, 0 gems: the gem skin makes the order unaffordable
        status, _, body = self.call("/shop/buyAll", {"userId": user_id, "token": token})
        self.assertEqual((status, body["success"]), (400, False))
        self.assertEqual(self.call("/shop/purchases", {"userId": user_id, "token": token})[2]["data"]["purchases"], [])
        self.assertEqual(self.call("/shop/buyAll", {"userId": user_id, "token": "nope"})[0], 401)

    def test_infinite_currency(self):
        self.service.settings["ACCOUNT_INFINITE_CURRENCY"] = True
        user_id, token = self.login()
        me = self.call("/auth/@me", {"userId": user_id, "token": token})[2]["data"]
        self.assertEqual((me["coins"], me["gems"]), (999_999_999, 999_999_999))
        status, _, body = self.call("/shop/buyAll", {"userId": user_id, "token": token})
        self.assertEqual((status, body["bought"], body["skins"]), (200, 3, 3))
        self.assertEqual(self.call("/shop/buyAll", {"userId": user_id, "token": token})[2]["bought"], 0)
        purchases = self.call("/shop/purchases", {"userId": user_id, "token": token})[2]["data"]["purchases"]
        self.assertEqual(sorted(purchases), ["gemSkin", "saleSkin", "skinA"])
        self.assertEqual(self.service.store.get_user(user_id)["coins"], 100)  # stored balance untouched

    def test_shop_flow(self):
        user_id, token = self.login()
        self.assertEqual(len(self.call("/shop/get")[2]["items"]), 3)
        status, _, body = self.call("/shop/buy", {"userId": user_id, "token": token, "shopItem": "saleSkin"})
        self.assertEqual((status, body), (200, {"success": True, "reason": "purchased"}))
        self.assertEqual(self.call("/shop/buy", {"userId": user_id, "token": token, "shopItem": "gemSkin"})[0], 400)
        self.assertEqual(self.call("/shop/buy", {"userId": user_id, "token": token, "shopItem": "nope"})[0], 404)
        self.call("/shop/wardrobe/save", {"userId": user_id, "token": token, "data": {"slot": 3}})
        body = self.call("/shop/purchases", {"userId": user_id, "token": token})[2]
        self.assertEqual(body["data"]["purchases"], ["saleSkin"])
        self.assertEqual(body["data"]["wardrobe"], {"slot": 3})
        me = self.call("/auth/@me", {"userId": user_id, "token": token})[2]
        self.assertEqual(me["data"]["coins"], 90)  # sale price charged

    def test_text_json_and_dev_endpoints(self):
        user_id, token = self.login()
        status, ctype, body = self.call(f"/playerSettings_update?userId={user_id}&passwordToken={token}&itemId=4")
        self.assertTrue(ctype.startswith("text/plain"))  # the client JSON.parse()s the text itself
        self.assertEqual(body, {"success": True, "reason": "ok"})
        self.assertEqual(self.call(f"/addCoins?userId={user_id}&passwordToken={token}")[2]["coins"], 107)
        self.assertEqual(self.call(f"/resetAccount?userId={user_id}&passwordToken={token}")[0], 200)
        self.assertEqual(self.call(f"/addCoins?userId={user_id}&passwordToken=bad")[0], 401)

    def test_banned_account(self):
        user_id, token = self.login()
        self.service.store.update_user(user_id, banned=1)
        body = self.call("/auth/@me", {"userId": user_id, "token": token, "isRefresh": True})[2]
        self.assertEqual(body["coins"], "BANNED")
        status, _, body = self.call("/auth/local/login", {"username": "frank", "password": "password1"})
        self.assertEqual((status, body["success"]), (403, False))


if __name__ == "__main__":
    unittest.main()
