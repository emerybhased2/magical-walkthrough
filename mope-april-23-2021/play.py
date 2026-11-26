#!/usr/bin/env python3
"""
play.py -- one command to play the archived 2025 mope.io client against a hosted backend.

    python play.py               # check everything, set up what is missing, start, open the browser
    python play.py --check       # only report what is ready and what is missing

Steps (each is skipped when already done):
  1. Python 3.10+
  2. .env with REMOTE_BACKEND_URL / REMOTE_BACKEND_KEY (copies a downloaded mope.env if it finds one,
     or use --env PATH)
  3. Python packages (requirements-play.txt)
  4. hosts entry 127.0.0.1 local.mope.io (--add-hosts writes it when run as administrator / sudo)
  5. the 2025-12-10 client snapshot (downloaded once from Arquivo.pt, ~1-3 minutes)
  6. the backend answers with your key
  7. `localstack.py up` (local web gateway + WebSocket relay), then opens http://local.mope.io:8080/
Ctrl-C stops everything.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
HOST = "local.mope.io"
SNAPSHOT = PROJECT / "snapshots" / "2025-12-10"
CLIENT = SNAPSHOT / "working" / "client" / "mope.io"
FETCH = [sys.executable, "fetch_mope_archive.py", "--date", "2025-12-10", "--output", "snapshots/2025-12-10",
         "--provider", "arquivo", "--all"]
MODULES = {"requests": "requests", "bs4": "beautifulsoup4", "tqdm": "tqdm", "jsbeautifier": "jsbeautifier",
           "tree_sitter": "tree-sitter", "tree_sitter_javascript": "tree-sitter-javascript",
           "websockets": "websockets", "aiohttp": "aiohttp"}
OK, NO, FIX = "  [ok]  ", "  [!!]  ", "  [..]  "


def say(mark: str, text: str) -> None:
    print(f"{mark}{text}", flush=True)


def hosts_file() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def hosts_help() -> str:
    if platform.system() == "Windows":
        return (f"open Notepad as Administrator, open {hosts_file()}, add the line `127.0.0.1 {HOST}`, save; "
                "or run this script from an Administrator terminal with --add-hosts")
    return f"run: echo '127.0.0.1 {HOST}' | sudo tee -a /etc/hosts   (or: sudo {sys.executable} play.py --add-hosts)"


# -- steps ------------------------------------------------------------------------------------

def check_python() -> bool:
    if sys.version_info < (3, 10):
        say(NO, f"Python {platform.python_version()} is too old; install Python 3.10 or newer")
        return False
    say(OK, f"Python {platform.python_version()}")
    return True


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def check_env(given: str | None, fix: bool) -> dict | None:
    env = PROJECT / ".env"
    if given:
        source = Path(given).expanduser().resolve()
        if not source.is_file():
            say(NO, f"--env: {source} not found")
            return None
        if source != env:
            if env.exists():
                shutil.copy2(env, env.with_name(".env.before-play"))
                say(FIX, "kept your previous .env as .env.before-play")
            shutil.copy2(source, env)
            say(FIX, f"copied {source.name} to .env")
    if not env.exists() and fix:
        home = Path.home()
        for candidate in (PROJECT / "mope.env", home / "Downloads" / "mope.env", home / "mope.env",
                          home / "Desktop" / "mope.env"):
            if candidate.is_file():
                shutil.copy2(candidate, env)
                say(FIX, f"copied {candidate} to .env")
                break
    if not env.exists():
        say(NO, f".env not found: put the mope.env file you were given in {PROJECT} (or pass --env PATH)")
        return None
    values = read_env(env)
    if not values.get("REMOTE_BACKEND_URL"):
        say(NO, ".env has no REMOTE_BACKEND_URL (use the mope.env file you were given)")
        return None
    say(OK, f".env -> backend {values['REMOTE_BACKEND_URL']}"
            + ("" if values.get("REMOTE_BACKEND_KEY") else " (no access key set)"))
    return values


def check_packages(fix: bool) -> bool:
    missing = [pkg for mod, pkg in MODULES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        say(OK, "Python packages")
        return True
    if not fix:
        say(NO, f"missing Python packages: {', '.join(missing)}")
        return False
    say(FIX, f"installing {', '.join(missing)} ...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements-play.txt"], cwd=PROJECT)
    if result.returncode != 0:
        say(NO, "pip install failed (see above); try: python -m pip install -r requirements-play.txt")
        return False
    importlib.invalidate_caches()
    still = [pkg for mod, pkg in MODULES.items() if importlib.util.find_spec(mod) is None]
    if still:
        say(NO, f"still missing after install: {', '.join(still)} (restart the script)")
        return False
    say(OK, "Python packages installed")
    return True


def resolves_local() -> bool:
    try:
        return socket.gethostbyname(HOST).startswith("127.")
    except OSError:
        return False


def check_hosts(add: bool) -> bool:
    if resolves_local():
        say(OK, f"hosts entry: {HOST} -> 127.0.0.1")
        return True
    if add:
        try:
            with open(hosts_file(), "a", encoding="utf-8") as fh:
                fh.write(f"\n127.0.0.1 {HOST}\n")
            time.sleep(0.5)
        except OSError as exc:
            say(NO, f"cannot write {hosts_file()} ({exc.__class__.__name__}); {hosts_help()}")
            return False
        if resolves_local():
            say(OK, f"hosts entry added: {HOST} -> 127.0.0.1")
            return True
        say(NO, f"added the hosts entry but {HOST} still does not resolve to 127.0.0.1 "
                "(a VPN or secure-DNS browser setting can override it)")
        return False
    say(NO, f"{HOST} does not point to this computer; {hosts_help()}")
    return False


def check_client(fix: bool) -> bool:
    if (CLIENT / "index.html").is_file() and (CLIENT / "client.js").is_file():
        say(OK, "2025-12-10 client snapshot")
        return True
    if not fix:
        say(NO, "2025-12-10 client snapshot not downloaded yet")
        return False
    say(FIX, "downloading the 2025-12-10 client snapshot from Arquivo.pt (once; takes a few minutes) ...")
    result = subprocess.run(FETCH, cwd=PROJECT)
    if (CLIENT / "index.html").is_file() and (CLIENT / "client.js").is_file():
        say(OK, "2025-12-10 client snapshot downloaded")
        return True
    say(NO, f"snapshot download failed (exit {result.returncode}); see logs/ under {SNAPSHOT}, "
            "then run play.py again (finished files are reused)")
    return False


def opener():
    built = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    built.addheaders = [("User-Agent", "mope-local-stack/1.0")]  # the host blocks "Python-urllib"
    return built


def check_backend(env: dict) -> bool:
    url = env["REMOTE_BACKEND_URL"].rstrip("/")
    client = opener()
    try:
        with client.open(url + "/__edge/health", timeout=20) as resp:
            health = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        say(NO, f"backend {url} is not answering ({exc}); it may be starting up, try again in a minute")
        return False
    request = urllib.request.Request(url + "/servers/get", headers={"X-Backend-Key": env.get("REMOTE_BACKEND_KEY", "")})
    try:
        with client.open(request, timeout=20) as resp:
            online = len(json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            say(NO, "the backend rejected REMOTE_BACKEND_KEY; ask for the current mope.env")
        else:
            say(NO, f"backend error {exc.code} on /servers/get")
        return False
    except (OSError, ValueError) as exc:
        say(NO, f"backend region list failed ({exc})")
        return False
    say(OK, f"backend up: {online}/{health.get('regions', '?')} regions online")
    return True


# -- run ------------------------------------------------------------------------------------------

def wait_for_gateway(timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    local = opener()
    while time.time() < deadline:
        try:
            with local.open("http://127.0.0.1:8080/__local/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Play the archived 2025 mope.io client against the hosted backend")
    parser.add_argument("--check", action="store_true", help="only report; change nothing")
    parser.add_argument("--env", help="path to the mope.env file you were given (copied to .env)")
    parser.add_argument("--add-hosts", action="store_true", help=f"add 127.0.0.1 {HOST} to the hosts file "
                                                                 "(needs administrator / sudo)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    os.chdir(PROJECT)
    for name in ("SIGTERM", "SIGHUP"):  # killed or terminal window closed: clean up like Ctrl-C
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    fix = not args.check
    print("mope.io player setup\n")

    ready = check_python()
    env = check_env(args.env, fix) if ready else None
    ready = ready and env is not None
    ready = check_packages(fix) and ready
    ready = check_hosts(args.add_hosts and fix) and ready
    ready = check_client(fix and ready) and ready
    ready = (check_backend(env) if env else False) and ready
    if not ready:
        print("\nNot ready yet: fix the [!!] lines above, then run play.py again.")
        return 1
    if args.check:
        print("\nEverything is ready. Run `python play.py` to play.")
        return 0

    # Services left over from an earlier run that was killed would otherwise be reused as-is.
    subprocess.run([sys.executable, "localstack.py", "stop"], cwd=PROJECT, stdout=subprocess.DEVNULL)
    print("\nstarting the local web gateway and relay (Ctrl-C to stop) ...\n")
    stack = subprocess.Popen([sys.executable, "localstack.py", "up"], cwd=PROJECT)
    try:
        if wait_for_gateway():
            url = f"http://{HOST}:8080/"
            print(f"\n  >>> play at {url}\n")
            if not args.no_browser:
                webbrowser.open(url)
        elif stack.poll() is None:
            print("\nthe web gateway did not come up within 30 s; see logs/services/web.log")
        return stack.wait()
    except KeyboardInterrupt:
        # Ctrl-C reaches localstack.py through the console; a kill or closed window does not, so pass it on.
        if os.name != "nt" and stack.poll() is None:
            stack.send_signal(signal.SIGINT)
        try:
            stack.wait(timeout=20)
        except subprocess.TimeoutExpired:
            stack.terminate()
            stack.wait(timeout=10)
        return 130
    finally:
        # The services run in their own sessions, so make sure none outlive this launcher.
        subprocess.run([sys.executable, "localstack.py", "stop"], cwd=PROJECT, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
