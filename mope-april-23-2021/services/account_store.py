"""
Account storage for the local account server (Phase 4, task 23).

SQLite with versioned migrations (schema_version table). Passwords are hashed with scrypt
and a per-user salt; login tokens (the client's `login_passwordToken`) are random and only
their SHA-256 is stored, with an expiry. One connection per operation keeps the store safe
to use from the threaded HTTP server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        username_key TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        password_hash TEXT,
        avatar TEXT NOT NULL DEFAULT '',
        coins INTEGER NOT NULL DEFAULT 0,
        gems INTEGER NOT NULL DEFAULT 0,
        level INTEGER NOT NULL DEFAULT 0,
        xp INTEGER NOT NULL DEFAULT 0,
        has_vip INTEGER NOT NULL DEFAULT 0,
        has_auto_renew INTEGER NOT NULL DEFAULT 0,
        banned INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_login_at REAL
    );
    CREATE TABLE tokens (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_used_at REAL,
        revoked INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_tokens_user ON tokens(user_id);
    CREATE TABLE purchases (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL,
        price INTEGER NOT NULL,
        currency_type INTEGER NOT NULL,
        purchased_at REAL NOT NULL,
        PRIMARY KEY (user_id, item_id)
    );
    CREATE TABLE wardrobe (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        data TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE player_settings (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE login_failures (
        key TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        first_at REAL NOT NULL
    );
    """),
]


class StoreError(Exception):
    """A user-facing account error (message is safe to show)."""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                                p=int(p), dklen=len(digest_hex) // 2)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountStore:
    USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

    def __init__(self, path: str | Path, settings: dict):
        self.path = Path(path)
        self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- schema -------------------------------------------------------------------------
    def schema_version(self) -> int:
        with self.db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            return int(row["v"] or 0)

    def migrate(self) -> list[int]:
        applied = []
        current = self.schema_version()
        with self.db() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            for version, sql in MIGRATIONS:
                if version > current:
                    conn.executescript("BEGIN;" + sql + f"INSERT INTO schema_version VALUES ({version}); COMMIT;")
                    applied.append(version)
        return applied

    # -- users --------------------------------------------------------------------------
    def _validate_username(self, username: str) -> str:
        username = (username or "").strip()
        low, high = self.settings["ACCOUNT_USERNAME_MIN_LENGTH"], self.settings["ACCOUNT_USERNAME_MAX_LENGTH"]
        if not low <= len(username) <= high:
            raise StoreError(f"Usernames must be {low}-{high} characters.")
        if not self.USERNAME_RE.match(username):
            raise StoreError("Usernames may use letters, digits, _ . and - only.")
        return username

    def create_user(self, username: str, password: str | None, provider: str = "local",
                    user_id: str | None = None) -> dict:
        if provider == "local":
            username = self._validate_username(username)
            if password is None or len(password) < self.settings["ACCOUNT_PASSWORD_MIN_LENGTH"]:
                raise StoreError(f"Passwords must be at least {self.settings['ACCOUNT_PASSWORD_MIN_LENGTH']} characters.")
        user_id = user_id or f"local_{secrets.token_hex(8)}"
        # local names are unique per provider; other providers (test accounts) are unique per user id
        username_key = f"{provider}:{username.lower()}" if provider == "local" else f"{provider}:{user_id}"
        now = time.time()
        try:
            with self.db() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, username_key, provider, password_hash, coins, gems, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, username, username_key, provider,
                     hash_password(password) if password is not None else None,
                     self.settings["ACCOUNT_STARTING_COINS"], self.settings["ACCOUNT_STARTING_GEMS"], now))
        except sqlite3.IntegrityError as exc:
            raise StoreError("That username is already taken.") from exc
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        with self.db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def find_user(self, username: str, provider: str = "local") -> dict | None:
        with self.db() as conn:
            row = conn.execute("SELECT * FROM users WHERE username_key = ?",
                               (f"{provider}:{(username or '').strip().lower()}",)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self.db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at")]

    def update_user(self, user_id: str, **fields) -> dict:
        allowed = {"coins", "gems", "level", "xp", "has_vip", "has_auto_renew", "banned", "avatar", "password_hash"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {', '.join(sorted(bad))}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            with self.db() as conn:
                conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*fields.values(), user_id))
        return self.get_user(user_id)

    def delete_user(self, user_id: str) -> bool:
        with self.db() as conn:
            return conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0

    # -- login throttling ------------------------------------------------------------------
    def _locked(self, key: str) -> bool:
        window = self.settings["ACCOUNT_LOGIN_LOCKOUT_MINUTES"] * 60
        with self.db() as conn:
            row = conn.execute("SELECT * FROM login_failures WHERE key = ?", (key,)).fetchone()
            if row and time.time() - row["first_at"] > window:
                conn.execute("DELETE FROM login_failures WHERE key = ?", (key,))
                return False
        return bool(row and row["count"] >= self.settings["ACCOUNT_LOGIN_MAX_FAILURES"])

    def _fail(self, key: str) -> None:
        with self.db() as conn:
            conn.execute("INSERT INTO login_failures (key, count, first_at) VALUES (?, 1, ?) "
                         "ON CONFLICT(key) DO UPDATE SET count = count + 1", (key, time.time()))

    def check_password_login(self, username: str, password: str) -> dict:
        key = f"user:{(username or '').strip().lower()}"
        if self._locked(key):
            raise StoreError("Too many failed logins; try again later.")
        user = self.find_user(username)
        if user is None or not verify_password(password or "", user["password_hash"]):
            self._fail(key)
            raise StoreError("Wrong username or password.")
        with self.db() as conn:
            conn.execute("DELETE FROM login_failures WHERE key = ?", (key,))
        return user

    # -- tokens -----------------------------------------------------------------------------
    def issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        ttl = self.settings["ACCOUNT_SESSION_TTL_DAYS"] * 86400
        with self.db() as conn:
            conn.execute("INSERT INTO tokens (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                         (token_hash(token), user_id, now, now + ttl))
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))
        return token

    def check_token(self, user_id: str | None, token: str | None) -> dict | None:
        if not user_id or not token:
            return None
        now = time.time()
        with self.db() as conn:
            row = conn.execute("SELECT * FROM tokens WHERE token_hash = ?", (token_hash(str(token)),)).fetchone()
            if row is None or row["user_id"] != user_id or row["revoked"] or row["expires_at"] < now:
                return None
            conn.execute("UPDATE tokens SET last_used_at = ? WHERE token_hash = ?", (now, row["token_hash"]))
        return self.get_user(user_id)

    def revoke_token(self, token: str) -> None:
        with self.db() as conn:
            conn.execute("UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (token_hash(token),))

    def revoke_user_tokens(self, user_id: str) -> int:
        with self.db() as conn:
            return conn.execute("UPDATE tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0",
                                (user_id,)).rowcount

    # -- shop ---------------------------------------------------------------------------------
    def purchases(self, user_id: str) -> list[str]:
        with self.db() as conn:
            return [r["item_id"] for r in conn.execute(
                "SELECT item_id FROM purchases WHERE user_id = ? ORDER BY purchased_at", (user_id,))]

    def buy(self, user_id: str, item_id: str, price: int, currency_type: int) -> dict:
        column = {0: "coins", 1: "gems"}.get(int(currency_type))
        if column is None:
            raise StoreError("This item cannot be bought with coins or gems on the local server.")
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute("SELECT 1 FROM purchases WHERE user_id = ? AND item_id = ?",
                                 (user_id, item_id)).fetchone()
            if owned:
                raise StoreError("You already own this item.")
            balance = conn.execute(f"SELECT {column} FROM users WHERE id = ?", (user_id,)).fetchone()[0]
            if balance < price:
                raise StoreError(f"Not enough {column} ({balance}/{price}).")
            conn.execute(f"UPDATE users SET {column} = {column} - ? WHERE id = ?", (price, user_id))
            conn.execute("INSERT INTO purchases VALUES (?, ?, ?, ?, ?)",
                         (user_id, item_id, price, int(currency_type), time.time()))
        return self.get_user(user_id)

    def grant_item(self, user_id: str, item_id: str) -> None:
        with self.db() as conn:
            conn.execute("INSERT OR IGNORE INTO purchases VALUES (?, ?, 0, 0, ?)", (user_id, item_id, time.time()))

    def wardrobe(self, user_id: str):
        with self.db() as conn:
            row = conn.execute("SELECT data FROM wardrobe WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def save_wardrobe(self, user_id: str, data) -> None:
        payload = json.dumps(data)
        if len(payload) > 64_000:
            raise StoreError("Wardrobe data is too large.")
        with self.db() as conn:
            conn.execute("INSERT INTO wardrobe VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET data = excluded.data,"
                         " updated_at = excluded.updated_at", (user_id, payload, time.time()))

    def set_player_setting(self, user_id: str, item_id: str) -> None:
        with self.db() as conn:
            conn.execute("INSERT INTO player_settings VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                         "item_id = excluded.item_id, updated_at = excluded.updated_at", (user_id, item_id, time.time()))

    def player_settings(self, user_id: str) -> list[str]:
        with self.db() as conn:
            row = conn.execute("SELECT item_id FROM player_settings WHERE user_id = ?", (user_id,)).fetchone()
        return [row["item_id"]] if row else []

    def reset_account(self, user_id: str) -> dict:
        with self.db() as conn:
            conn.execute("DELETE FROM purchases WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM wardrobe WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM player_settings WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE users SET coins = ?, gems = ?, level = 0, xp = 0 WHERE id = ?",
                         (self.settings["ACCOUNT_STARTING_COINS"], self.settings["ACCOUNT_STARTING_GEMS"], user_id))
        return self.get_user(user_id)


def client_account_data(user: dict) -> dict:
    """The account object the client reads (coins, gems, hasVip, hasAutoRenew; login fields)."""
    return {
        "userId": user["id"], "userName": user["username"], "avatar": user["avatar"] or "",
        "coins": "BANNED" if user["banned"] else user["coins"], "gems": user["gems"],
        "level": user["level"], "xp": user["xp"], "xpNextLvl": 0,
        "hasVip": bool(user["has_vip"]), "hasAutoRenew": bool(user["has_auto_renew"]),
        "socialNetwork": user["provider"],
    }
