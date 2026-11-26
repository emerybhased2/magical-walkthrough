"""
Centralised settings for the local stack (Phase 4, tasks 25 and 28).

Values come from, in increasing priority:
  1. the defaults below
  2. the project's .env file (KEY=value lines; # comments)
  3. the process environment

Every variable is typed, documented and validated; load_settings() reports *all* problems at
once so services refuse to start with a clear list instead of failing later. `.env.example`
documents the same variables and is generated from this table (python localstack.py env-example).

Evidence for the client-facing and developer controls (see phase4/README.md):
  CLIENT_ENV / CLIENT_TESTING_MODE / CLIENT_BETA_MODE   -> $config.env / isTestingMode / isBetaMode
  DEV_TEST_ACCOUNT_SECRET                                -> the client's ?testAccountSecret=&user= hook,
                                                            which POSTs /auth/login/<secret>
  DEV_ADMIN_ENDPOINTS_ENABLED                            -> the client's /addCoins and /resetAccount calls
  SANDBOX_REGION_*                                       -> the client's unused addServer('LOCAL',
                                                            'localhost', 'SANDBOX') hook
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Setting:
    name: str
    kind: str            # bool | int | str | path | choice
    default: object
    group: str
    help: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple = ()
    secret: bool = False


SETTINGS: tuple[Setting, ...] = (
    # -- account server ------------------------------------------------------------
    Setting("ACCOUNT_DB_PATH", "path", "data/accounts.sqlite3", "Account server",
            "SQLite database for accounts, sessions, purchases and wardrobes (created and migrated on start)."),
    Setting("ACCOUNT_REGISTRATION_ENABLED", "bool", True, "Account server",
            "Allow new local accounts to be created from the local login page."),
    Setting("ACCOUNT_LOGIN_ENABLED", "bool", True, "Account server",
            "Allow logins (local accounts and, if enabled, test accounts)."),
    Setting("ACCOUNT_USERNAME_MIN_LENGTH", "int", 3, "Account server",
            "Shortest allowed local username.", 1, 15),
    Setting("ACCOUNT_USERNAME_MAX_LENGTH", "int", 15, "Account server",
            "Longest allowed local username (the client's name box holds 15 characters).", 1, 15),
    Setting("ACCOUNT_PASSWORD_MIN_LENGTH", "int", 8, "Account server",
            "Shortest allowed password.", 4, 128),
    Setting("ACCOUNT_SESSION_TTL_DAYS", "int", 30, "Account server",
            "How long a login token (the client's login_passwordToken) stays valid.", 1, 3650),
    Setting("ACCOUNT_LOGIN_MAX_FAILURES", "int", 10, "Account server",
            "Failed logins per username before it is locked for ACCOUNT_LOGIN_LOCKOUT_MINUTES.", 1, 1000),
    Setting("ACCOUNT_LOGIN_LOCKOUT_MINUTES", "int", 15, "Account server",
            "Lockout duration after too many failed logins.", 1, 1440),
    Setting("ACCOUNT_STARTING_COINS", "int", 0, "Account server",
            "Coins given to a new account.", 0, 10 ** 9),
    Setting("ACCOUNT_STARTING_GEMS", "int", 0, "Account server",
            "Gems given to a new account.", 0, 10 ** 9),
    Setting("ACCOUNT_INFINITE_CURRENCY", "bool", True, "Account server",
            "Every account has unlimited coins and gems: the client is shown 999999999 of each and purchases are "
            "recorded without charging. The stored balances are kept (false restores them)."),
    Setting("ACCOUNT_SHOP_ENABLED", "bool", True, "Account server",
            "Serve the shop endpoints (/shop/get, /shop/purchases, /shop/buy, /shop/wardrobe/save)."),
    Setting("ACCOUNT_SHOP_CATALOG", "path", "config/shop_catalog.json", "Account server",
            "Shop catalogue served by /shop/get (the archived catalogue was server-side and is not recoverable)."),
    Setting("SHOP_LIVE_SKINS_DIR", "path", "snapshots/2025-12-10/live-skins", "Account server",
            "Output of import_live_skins.rb. If it exists, its catalog.json items are added to /shop/get and the "
            "web gateway serves its skin pictures. Empty to turn off."),
    # -- client-facing ($config overlay) ------------------------------------------------
    Setting("CLIENT_ENV", "choice", "production", "Client",
            "$config.env. 'development' enables the client's [DEV] error handler and shows updates of every env; "
            "'development'/'staging' also switch its payment widget to the payment provider's sandbox.",
            choices=("production", "staging", "development")),
    Setting("CLIENT_TESTING_MODE", "bool", False, "Client",
            "$config.isTestingMode: verbose client logging of account requests."),
    Setting("CLIENT_BETA_MODE", "bool", False, "Client",
            "$config.isBetaMode: server-full thresholds 900/800 instead of 400/500."),
    Setting("WEB_UPDATES_FILE", "path", "config/updates.json", "Client",
            "JSON served as /updates.json for the client's Updates panel."),
    # -- developer --------------------------------------------------------------------
    Setting("DEV_TEST_ACCOUNTS_ENABLED", "bool", False, "Developer",
            "Enable the client's test-account hook: http://localhost:8080/?testAccountSecret=<secret>&user=<1-2 chars>."),
    Setting("DEV_TEST_ACCOUNT_SECRET", "str", "", "Developer",
            "Secret for the test-account hook (8-64 of A-Z a-z 0-9 _ -). Required when test accounts are enabled.",
            secret=True),
    Setting("DEV_ADMIN_ENDPOINTS_ENABLED", "bool", False, "Developer",
            "Enable /addCoins and /resetAccount (called by the client in testing builds). Local use only."),
    Setting("DEV_ADD_COINS_AMOUNT", "int", 1000, "Developer",
            "Coins added by one /addCoins call.", 1, 10 ** 7),
    # -- sandbox ----------------------------------------------------------------------
    Setting("SANDBOX_REGION_ENABLED", "bool", False, "Sandbox",
            "Add the client's built-in sandbox server: name 'LOCAL', id 'localhost', region 'SANDBOX'."),
    Setting("SANDBOX_REGION_PORT", "int", 7099, "Sandbox",
            "Port of the sandbox region server.", 1024, 65535),
    # -- remote backend ---------------------------------------------------------------
    Setting("REMOTE_BACKEND_URL", "str", "", "Remote backend",
            "Base URL of a hosted backend (cloud.py, e.g. https://<app>.sevalla.app). When set, `localstack.py up` runs "
            "only the web gateway and a local WebSocket relay; accounts, the master and the regions are the remote ones."),
    Setting("REMOTE_BACKEND_KEY", "str", "", "Remote backend",
            "Access key the hosted backend asks for (its BACKEND_ACCESS_KEY). Leave empty if it has none.",
            secret=True),
    Setting("REMOTE_RELAY_PORT", "int", 7000, "Remote backend",
            "Local port of the WebSocket relay that carries game traffic to the remote backend.", 1024, 65535),
)

BY_NAME = {s.name: s for s in SETTINGS}
TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off", ""}
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class SettingsError(Exception):
    pass


def parse_env_file(path: Path) -> tuple[dict, list[str]]:
    values, errors = {}, []
    if not path.exists():
        return values, errors
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            errors.append(f"{path.name}:{number}: expected KEY=value, got {raw!r}")
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values, errors


def _convert(setting: Setting, raw) -> tuple[object, str | None]:
    if not isinstance(raw, str):
        return raw, None
    if setting.kind == "bool":
        lowered = raw.strip().lower()
        if lowered in TRUE:
            return True, None
        if lowered in FALSE:
            return False, None
        return None, f"{setting.name} must be true/false, got {raw!r}"
    if setting.kind == "int":
        try:
            value = int(raw.strip())
        except ValueError:
            return None, f"{setting.name} must be a whole number, got {raw!r}"
        if setting.minimum is not None and value < setting.minimum:
            return None, f"{setting.name} must be >= {setting.minimum}, got {value}"
        if setting.maximum is not None and value > setting.maximum:
            return None, f"{setting.name} must be <= {setting.maximum}, got {value}"
        return value, None
    if setting.kind == "choice":
        if raw not in setting.choices:
            return None, f"{setting.name} must be one of {', '.join(setting.choices)}, got {raw!r}"
        return raw, None
    return raw, None


def load_settings(root: Path, environ: dict | None = None, env_file: Path | None = None) -> tuple[dict, dict, list[str]]:
    """Return (values, sources, errors). Values are typed; sources say where each came from."""
    environ = os.environ if environ is None else environ
    env_path = env_file or (root / ".env")
    file_values, errors = parse_env_file(env_path)
    values, sources = {}, {}
    for key in sorted(set(file_values) - set(BY_NAME)):
        errors.append(f"{env_path.name}: unknown setting {key} (typo?)")
    for setting in SETTINGS:
        raw, source = setting.default, "default"
        if setting.name in file_values:
            raw, source = file_values[setting.name], env_path.name
        if setting.name in environ:
            raw, source = environ[setting.name], "environment"
        value, error = _convert(setting, raw)
        if error:
            errors.append(f"{error} (from {source})")
            value = setting.default
        if setting.kind == "path" and value:
            path = Path(value)
            value = str(path if path.is_absolute() else root / path)
        values[setting.name] = value
        sources[setting.name] = source
    # cross-field rules
    if values["ACCOUNT_USERNAME_MIN_LENGTH"] > values["ACCOUNT_USERNAME_MAX_LENGTH"]:
        errors.append("ACCOUNT_USERNAME_MIN_LENGTH cannot be larger than ACCOUNT_USERNAME_MAX_LENGTH")
    if values["DEV_TEST_ACCOUNTS_ENABLED"]:
        if not SECRET_RE.match(values["DEV_TEST_ACCOUNT_SECRET"] or ""):
            errors.append("DEV_TEST_ACCOUNTS_ENABLED=true needs DEV_TEST_ACCOUNT_SECRET of 8-64 characters "
                          "(A-Z a-z 0-9 _ -)")
        elif values["DEV_TEST_ACCOUNT_SECRET"].lower() in {"local", "google", "facebook", "apple", "discord"}:
            errors.append("DEV_TEST_ACCOUNT_SECRET cannot be a login provider name")
    remote = values["REMOTE_BACKEND_URL"]
    if remote:
        parts = urlsplit(remote)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") \
                or parts.query or parts.fragment:
            errors.append(f"REMOTE_BACKEND_URL must look like https://host[:port] with no path, got {remote!r}")
        else:
            values["REMOTE_BACKEND_URL"] = f"{parts.scheme}://{parts.netloc}"
    for key in ("ACCOUNT_SHOP_CATALOG", "WEB_UPDATES_FILE"):
        if not Path(values[key]).is_file():
            errors.append(f"{key}: file not found: {values[key]}")
    return values, sources, errors


def example_file() -> str:
    lines = ["# Local mope.io stack settings. Copy to .env and edit; every line is optional.",
             "# Generated from services/settings.py -- run `python localstack.py check-config` to validate.", ""]
    group = None
    for setting in SETTINGS:
        if setting.group != group:
            group = setting.group
            lines += ["", f"# ---- {group} " + "-" * max(4, 60 - len(group))]
        bounds = ""
        if setting.minimum is not None or setting.maximum is not None:
            bounds = f" [{setting.minimum}..{setting.maximum}]"
        if setting.choices:
            bounds = f" [{' | '.join(setting.choices)}]"
        lines.append(f"# {setting.help}{bounds}")
        default = setting.default
        if isinstance(default, bool):
            default = "true" if default else "false"
        lines.append(f"{setting.name}={default}")
    return "\n".join(lines).strip() + "\n"


def describe(values: dict, sources: dict) -> list[tuple[str, str, str]]:
    rows = []
    for setting in SETTINGS:
        value = values[setting.name]
        shown = ("(set)" if value else "(empty)") if setting.secret else str(value)
        rows.append((setting.name, shown, sources[setting.name]))
    return rows
