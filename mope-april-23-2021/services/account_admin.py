#!/usr/bin/env python3
"""
account_admin.py -- local administration of the account database (developer-only controls).

Works directly on ACCOUNT_DB_PATH, so it only runs on the machine that hosts the stack.

  python services/account_admin.py list
  python services/account_admin.py create <username> [--password P]
  python services/account_admin.py set-password <username> [--password P]
  python services/account_admin.py coins <username> <+N|-N|N>      (also: gems)
  python services/account_admin.py ban|unban <username>
  python services/account_admin.py grant <username> <itemId>
  python services/account_admin.py logout-all <username>
  python services/account_admin.py delete <username>
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common  # noqa: E402
from services.account_store import AccountStore, StoreError, hash_password  # noqa: E402


def find(store: AccountStore, name: str) -> dict:
    user = store.find_user(name) or store.get_user(name)
    if user is None:
        raise SystemExit(f"error: no account {name!r}")
    return user


def amount(current: int, change: str) -> int:
    value = int(change)
    return max(0, current + value) if change[:1] in "+-" else max(0, value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Administer local mope.io accounts.")
    parser.add_argument("--config", help="stack config (default config/local_stack.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("create", "set-password"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password")
    for name in ("coins", "gems"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("change", help="N, +N or -N")
    for name in ("ban", "unban", "logout-all", "delete"):
        sub.add_parser(name).add_argument("username")
    p = sub.add_parser("grant")
    p.add_argument("username")
    p.add_argument("item")
    args = parser.parse_args(argv)

    config = common.load_stack_config(args.config)
    store = AccountStore(config["settings"]["ACCOUNT_DB_PATH"], config["settings"])
    try:
        if args.command == "list":
            print(f"{'username':<16} {'id':<26} {'provider':<8} {'coins':>8} {'gems':>6} {'banned':>6}  last login")
            for u in store.list_users():
                last = datetime.fromtimestamp(u["last_login_at"]).strftime("%Y-%m-%d %H:%M") if u["last_login_at"] else "-"
                print(f"{u['username']:<16} {u['id']:<26} {u['provider']:<8} {u['coins']:>8} {u['gems']:>6} "
                      f"{'yes' if u['banned'] else 'no':>6}  {last}")
        elif args.command == "create":
            password = args.password or getpass.getpass("password: ")
            user = store.create_user(args.username, password)
            print(f"created {user['username']} ({user['id']})")
        elif args.command == "set-password":
            user = find(store, args.username)
            password = args.password or getpass.getpass("new password: ")
            if len(password) < config["settings"]["ACCOUNT_PASSWORD_MIN_LENGTH"]:
                raise StoreError("password too short")
            store.update_user(user["id"], password_hash=hash_password(password))
            store.revoke_user_tokens(user["id"])
            print(f"password changed for {user['username']}; existing logins revoked")
        elif args.command in ("coins", "gems"):
            user = find(store, args.username)
            updated = store.update_user(user["id"], **{args.command: amount(user[args.command], args.change)})
            print(f"{user['username']}: {args.command} {user[args.command]} -> {updated[args.command]}")
        elif args.command in ("ban", "unban"):
            user = find(store, args.username)
            store.update_user(user["id"], banned=1 if args.command == "ban" else 0)
            if args.command == "ban":
                store.revoke_user_tokens(user["id"])
            print(f"{user['username']}: {args.command}ned" if args.command == "ban" else f"{user['username']}: unbanned")
        elif args.command == "grant":
            user = find(store, args.username)
            store.grant_item(user["id"], args.item)
            print(f"{user['username']}: granted {args.item}")
        elif args.command == "logout-all":
            user = find(store, args.username)
            print(f"{user['username']}: revoked {store.revoke_user_tokens(user['id'])} login(s)")
        elif args.command == "delete":
            user = find(store, args.username)
            store.delete_user(user["id"])
            print(f"deleted {user['username']}")
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
