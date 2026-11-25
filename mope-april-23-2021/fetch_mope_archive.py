#!/usr/bin/env python3
"""
fetch_mope_archive.py -- Phase 1 snapshot fetcher for the Mope.io April 23, 2021
local archive project.

The script acquires and analyses the historical mope.io client.  It does not
rebuild the JavaScript client and it does not implement any server; it prepares
the source material and the inventories the later phases depend on.

Archive sources
---------------
By design this project does NOT use the Internet Archive / Wayback Machine.
Captures come from independent web archives, configured in config.json:

  arquivo      Arquivo.pt, the Portuguese national web archive.  pywb CDX API
               plus raw "id_" replay, which returns the original bytes.
  commoncrawl  Common Crawl.  CDX index (index.commoncrawl.org, falling back to
               a direct binary search of cluster.idx on data.commoncrawl.org)
               plus raw WARC records fetched with HTTP range requests.
  vefsafn      Icelandic Web Archive (vefsafn.is).  pywb CDX API; its replay is
               behind a CAPTCHA, so it is used for discovery only.

Any other pywb / OpenWayback-compatible archive can be added as a "pywb"
provider.  Wayback Machine hosts are rejected by the configuration validator.

Historical fidelity rules
-------------------------
* The target date (default 2021-04-23) never changes silently.
* The anchor is a homepage capture from the target date.  If none exists the
  script stops and lists the nearest captures; an off-date anchor needs an
  explicit --timestamp plus --allow-off-date, and is reported everywhere.
* Resources prefer captures from the anchor's day at or before the anchor,
  then (same_day_fallback) later the same day, then -- only with
  --allow-off-date -- the nearest capture within max_date_distance_days.
* Archive redirects to a different capture are refused, and Memento-Datetime /
  WARC-Date headers are checked against the requested timestamp.
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures as cf
import copy
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import stat
import sys
import threading
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

_MISSING_DEPS: list[str] = []
try:
    import requests
except ImportError:  # pragma: no cover - reported by check_dependencies()
    requests = None
    _MISSING_DEPS.append("requests")
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None
    _MISSING_DEPS.append("beautifulsoup4")
try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None
    _MISSING_DEPS.append("tqdm")


SCRIPT_VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
UTC = timezone.utc
TS_FORMAT = "%Y%m%d%H%M%S"

# Archive hosts this project refuses to use (Internet Archive / Wayback Machine).
EXCLUDED_ARCHIVE_HOSTS = (
    "archive.org",
    "web.archive.org",
    "wayback.archive.org",
    "archive-it.org",
    "wayback.archive-it.org",
)

MANIFEST_FILES = (
    "capture_manifest.json",
    "download_manifest.json",
    "missing_resources.json",
    "endpoint_candidates.json",
    "region_candidates.json",
    "archive_report.json",
)

DEFAULT_CONFIG: dict = {
    "project": "mope-april-23-2021",
    "domain": "mope.io",
    "snapshot_date": "2021-04-23",
    "snapshot_timezone": "UTC",
    "anchor_timestamp": None,
    "output_directory": "archive/original",
    "workers": 4,
    "request_timeout": 30,
    "max_retries": 3,
    "resume_downloads": True,
    "calculate_sha256": True,
    "discover_subdomains": True,
    "discover_external_assets": True,
    "same_day_fallback": True,
    "preserve_original_files": True,
    "allow_off_date": False,
    "max_date_distance_days": 60,
    "discovery_window_days": 60,
    "rate_limit_seconds": 0.5,
    "retry_backoff_seconds": 2.0,
    "max_crawl_depth": 4,
    "max_candidates_per_resource": 4,
    "include_target_date_captures": True,
    "homepage_paths": ["/", "/index.html"],
    "essential_paths": ["/", "/client.js"],
    "analysis_exclude_patterns": ["jquery", "adblockdet", "dfp.js", "pubfig", "/cdn-cgi/", "platform.js"],
    "user_agent": "mope-archive-fetcher/1.0 (historical preservation research; python-requests)",
    "providers": [
        {
            "name": "arquivo",
            "type": "pywb",
            "label": "Arquivo.pt",
            "enabled": True,
            "priority": 1,
            "cdx_url": "https://arquivo.pt/wayback/cdx",
            "replay_url": "https://arquivo.pt/wayback",
            "replay": True,
            "cdx_limit": 50000,
        },
        {
            "name": "commoncrawl",
            "type": "commoncrawl",
            "label": "Common Crawl",
            "enabled": True,
            "priority": 2,
            "index_url": "https://index.commoncrawl.org",
            "data_url": "https://data.commoncrawl.org",
            "index_mode": "auto",
            "collections": ["CC-MAIN-2021-10", "CC-MAIN-2021-17", "CC-MAIN-2021-21"],
            "lookup_external": False,
            "replay": True,
        },
        {
            "name": "vefsafn",
            "type": "pywb",
            "label": "Icelandic Web Archive (vefsafn.is)",
            "enabled": True,
            "priority": 3,
            "cdx_url": "https://vefsafn.is/is/cdx",
            "replay_url": "https://vefsafn.is/is",
            "replay": False,
            "cdx_limit": 50000,
        },
    ],
}


# =============================================================================
# Errors
# =============================================================================


class FetcherError(Exception):
    """Fatal pipeline error shown to the user."""

    exit_code = 1


class ConfigError(FetcherError):
    exit_code = 2


class SelectionError(FetcherError):
    """No acceptable anchor capture (e.g. nothing archived on the target date)."""

    exit_code = 3


class FetchError(Exception):
    """A single archive request failed."""


class TimestampMismatch(FetchError):
    """The archive answered with a different capture than the one requested."""


class RevisitUnresolved(FetchError):
    """A revisit record whose original payload could not be located."""


class PayloadTruncated(FetchError):
    """The archive stored only part of the response body."""


# =============================================================================
# Small helpers
# =============================================================================


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(ts: str) -> datetime:
    """Parse a (possibly partial) 14-digit archive timestamp as UTC."""
    digits = re.sub(r"\D", "", str(ts))[:14]
    if len(digits) < 4:
        raise ValueError(f"invalid archive timestamp: {ts!r}")
    digits = digits + "19700101000000"[len(digits):]
    return datetime.strptime(digits, TS_FORMAT).replace(tzinfo=UTC)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FORMAT)


def ts_iso(ts: str) -> str:
    return parse_ts(ts).isoformat().replace("+00:00", "Z")


def ts_human(ts: str) -> str:
    return parse_ts(ts).strftime("%Y-%m-%d %H:%M:%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def decode_text(body: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("latin-1")


def normalize_url(url: str) -> str | None:
    """Canonical absolute http(s) URL without fragment, or None."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if (port is None or default_port) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def match_key(url: str, keep_query: bool = True) -> str:
    """Scheme- and www-insensitive comparison key for archived URLs."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    key = host + (parts.path or "/")
    if keep_query and parts.query:
        key += "?" + parts.query
    return key


def is_same_site(host: str, domain: str) -> bool:
    host = (host or "").lower()
    return host == domain or host.endswith("." + domain)


def is_excluded_archive(host: str) -> bool:
    return any(is_same_site(host, excluded) for excluded in EXCLUDED_ARCHIVE_HOSTS)


def surt_host(host: str) -> str:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return ",".join(reversed(host.split(".")))


def surt_url(url: str) -> str:
    """Approximate SURT key as used by Common Crawl's CDX index."""
    parts = urlsplit(url.lower())
    key = surt_host(parts.hostname or "")
    if parts.port and parts.port not in (80, 443):
        key += f":{parts.port}"
    key += ")" + (parts.path or "/")
    if parts.query:
        key += "?" + "&".join(sorted(parts.query.split("&")))
    return key


KIND_BY_EXT = {
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".js": "js", ".mjs": "js",
    ".css": "css",
    ".json": "json", ".map": "json",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".svg": "image",
    ".webp": "image", ".bmp": "image", ".ico": "image", ".cur": "image",
    ".mp3": "audio", ".ogg": "audio", ".oga": "audio", ".wav": "audio", ".m4a": "audio",
    ".aac": "audio", ".flac": "audio",
    ".mp4": "video", ".webm": "video",
    ".woff": "font", ".woff2": "font", ".ttf": "font", ".otf": "font", ".eot": "font",
    ".txt": "text", ".xml": "xml", ".wasm": "wasm",
}


def kind_for(url: str, mime: str | None = None) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime and mime not in ("warc/revisit", "unk", "-", "application/octet-stream", "application/http"):
        if "html" in mime:
            return "html"
        if "css" in mime:
            return "css"
        if "javascript" in mime or "ecmascript" in mime:
            return "js"
        if "json" in mime:
            return "json"
        for prefix, kind in (("image/", "image"), ("audio/", "audio"), ("video/", "video"), ("font/", "font")):
            if mime.startswith(prefix):
                return kind
        if "woff" in mime or "opentype" in mime or "truetype" in mime:
            return "font"
    path = urlsplit(url).path or "/"
    if path.endswith("/"):
        return "html"
    return KIND_BY_EXT.get(PurePosixPath(path).suffix.lower(), "other")


# =============================================================================
# Target date window
# =============================================================================


class TargetWindow:
    """One calendar day in a configured timezone, expressed as a UTC interval."""

    def __init__(self, day: date, tz_name: str = "UTC"):
        self.day = day
        self.tz_name = tz_name
        self.tz = resolve_timezone(tz_name)
        start_local = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        nxt = day + timedelta(days=1)
        end_local = datetime(nxt.year, nxt.month, nxt.day, tzinfo=self.tz)
        self.start = start_local.astimezone(UTC)
        self.end = end_local.astimezone(UTC)

    @classmethod
    def for_datetime(cls, dt: datetime, tz_name: str) -> "TargetWindow":
        return cls(dt.astimezone(resolve_timezone(tz_name)).date(), tz_name)

    def contains(self, dt: datetime) -> bool:
        return self.start <= dt < self.end

    def signed_deviation_days(self, dt: datetime) -> float:
        """0.0 inside the day; otherwise signed distance to the day (never rounds to 0)."""
        if dt < self.start:
            return min(-round((self.start - dt).total_seconds() / 86400, 3), -0.001)
        if dt >= self.end:
            return max(round((dt - self.end).total_seconds() / 86400, 3), 0.001)
        return 0.0

    def describe(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "timezone": self.tz_name,
            "utc_window": [self.start.isoformat().replace("+00:00", "Z"), self.end.isoformat().replace("+00:00", "Z")],
        }


def resolve_timezone(tz_name: str):
    if not tz_name or tz_name.upper() == "UTC":
        return UTC
    if ZoneInfo is None:
        raise ConfigError("Timezones other than UTC need Python 3.9+ (zoneinfo).")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown snapshot_timezone {tz_name!r}") from exc


# =============================================================================
# Captures and selection policy
# =============================================================================


@dataclass
class Capture:
    provider: str
    url: str
    timestamp: str
    status: str = "-"
    mime: str = ""
    digest: str = ""
    length: str = ""
    revisit: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def dt(self) -> datetime:
        return parse_ts(self.timestamp)

    @property
    def ident(self) -> str:
        return f"{self.provider}|{self.timestamp}|{self.url}"

    @property
    def fetchable(self) -> bool:
        return self.status == "200" or self.revisit

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "url": self.url, "timestamp": self.timestamp,
            "status": self.status, "mime": self.mime, "digest": self.digest,
            "length": self.length, "revisit": self.revisit, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Capture":
        return cls(
            provider=data["provider"], url=data["url"], timestamp=data["timestamp"],
            status=str(data.get("status") or "-"), mime=data.get("mime") or "",
            digest=data.get("digest") or "", length=str(data.get("length") or ""),
            revisit=bool(data.get("revisit")), meta=data.get("meta") or {},
        )


@dataclass
class Candidate:
    capture: Capture
    tier: str
    distance_seconds: float
    query_mismatch: int = 0


class SelectionPolicy:
    """Ranks captures of one resource relative to the anchor capture."""

    TIERS = ("anchor", "same_day_at_or_before_anchor", "same_day_after_anchor", "off_date_nearest")

    def __init__(self, anchor_dt: datetime, target: TargetWindow, same_day_fallback: bool,
                 allow_off_date: bool, max_distance: timedelta, priorities: dict, replayable: set):
        self.anchor_dt = anchor_dt
        self.target = target
        self.day = TargetWindow.for_datetime(anchor_dt, target.tz_name)
        self.same_day_fallback = same_day_fallback
        self.allow_off_date = allow_off_date
        self.max_distance = max_distance
        self.priorities = priorities
        self.replayable = replayable

    def classify(self, cap: Capture) -> str | None:
        dt = cap.dt
        if self.day.contains(dt):
            if dt <= self.anchor_dt:
                return "same_day_at_or_before_anchor"
            return "same_day_after_anchor" if self.same_day_fallback else None
        if self.allow_off_date and abs(dt - self.anchor_dt) <= self.max_distance:
            return "off_date_nearest"
        return None

    def candidates(self, captures, url: str) -> list[Candidate]:
        wanted_query = urlsplit(url).query
        out, seen = [], set()
        for cap in captures:
            if cap.ident in seen or cap.provider not in self.replayable or not cap.fetchable:
                continue
            tier = self.classify(cap)
            if tier is None:
                continue
            seen.add(cap.ident)
            out.append(Candidate(
                capture=cap, tier=tier,
                distance_seconds=abs((cap.dt - self.anchor_dt).total_seconds()),
                query_mismatch=0 if urlsplit(cap.url).query == wanted_query else 1,
            ))
        out.sort(key=lambda c: (self.TIERS.index(c.tier), c.query_mismatch, c.distance_seconds,
                                self.priorities.get(c.capture.provider, 99)))
        return out

    def nearest(self, captures) -> Capture | None:
        usable = [c for c in captures if c.fetchable]
        if not usable:
            return None
        return min(usable, key=lambda c: abs((c.dt - self.anchor_dt).total_seconds()))

    def describe(self) -> dict:
        return {
            "anchor_day": self.day.describe(),
            "same_day_fallback": self.same_day_fallback,
            "allow_off_date": self.allow_off_date,
            "max_date_distance_days": self.max_distance.total_seconds() / 86400 if self.allow_off_date else 0,
            "tier_order": list(self.TIERS),
        }


# =============================================================================
# SQLite download tracking
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS captures (
    provider TEXT, url TEXT, timestamp TEXT, status TEXT, mime TEXT, digest TEXT,
    length TEXT, revisit INTEGER, on_target_date INTEGER, meta_json TEXT,
    PRIMARY KEY (provider, url, timestamp)
);
CREATE TABLE IF NOT EXISTS lookup_cache (cache_key TEXT PRIMARY KEY, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS resources (
    url TEXT PRIMARY KEY, kind TEXT, relation TEXT, discovered_from TEXT, depth INTEGER,
    external INTEGER, speculative INTEGER, optional INTEGER, anchor TEXT, run_id INTEGER,
    provider TEXT, capture_timestamp TEXT, capture_url TEXT, capture_status TEXT,
    selection_tier TEXT, distance_seconds REAL, deviation_days REAL, query_mismatch INTEGER,
    local_path TEXT, status TEXT, error TEXT, notes TEXT, http_status INTEGER, mime TEXT,
    size INTEGER, sha256 TEXT, attempts INTEGER, nearest_alternative TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_resources_path ON resources(local_path);
CREATE INDEX IF NOT EXISTS idx_resources_run ON resources(run_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
    command TEXT, anchor TEXT, summary TEXT
);
"""

RESOURCE_COLUMNS = (
    "url", "kind", "relation", "discovered_from", "depth", "external", "speculative", "optional",
    "anchor", "run_id", "provider", "capture_timestamp", "capture_url", "capture_status",
    "selection_tier", "distance_seconds", "deviation_days", "query_mismatch", "local_path",
    "status", "error", "notes", "http_status", "mime", "size", "sha256", "attempts",
    "nearest_alternative", "updated_at",
)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params=()) -> None:
        with self.lock:
            self.conn.execute(sql, params)

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_meta(self, key: str, value) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value, default=str)))

    # -- captures -----------------------------------------------------------
    def replace_captures(self, captures: list[Capture], target: TargetWindow) -> None:
        rows = [(c.provider, c.url, c.timestamp, c.status, c.mime, c.digest, c.length, int(c.revisit),
                 int(target.contains(c.dt)), json.dumps(c.meta)) for c in captures]
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("DELETE FROM captures")
                self.conn.executemany("INSERT OR REPLACE INTO captures VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def load_captures(self) -> list[Capture]:
        return [
            Capture(provider=r["provider"], url=r["url"], timestamp=r["timestamp"], status=r["status"],
                    mime=r["mime"], digest=r["digest"], length=r["length"], revisit=bool(r["revisit"]),
                    meta=json.loads(r["meta_json"] or "{}"))
            for r in self.query("SELECT * FROM captures ORDER BY timestamp")
        ]

    # -- lookup cache -------------------------------------------------------
    def cache_get(self, key: str):
        rows = self.query("SELECT payload FROM lookup_cache WHERE cache_key = ?", (key,))
        return json.loads(rows[0]["payload"]) if rows else None

    def cache_put(self, key: str, payload) -> None:
        self.execute("INSERT OR REPLACE INTO lookup_cache VALUES (?, ?, ?)", (key, now_iso(), json.dumps(payload)))

    def clear_cache(self) -> None:
        self.execute("DELETE FROM lookup_cache")

    # -- resources ----------------------------------------------------------
    def upsert_resource(self, record: dict) -> None:
        record = dict(record, updated_at=now_iso())
        cols = [c for c in RESOURCE_COLUMNS if c in record]
        values = []
        for col in cols:
            value = record[col]
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif isinstance(value, bool):
                value = int(value)
            values.append(value)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "url")
        sql = (f"INSERT INTO resources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
               f"ON CONFLICT(url) DO UPDATE SET {updates}")
        self.execute(sql, values)

    def get_resource(self, url: str) -> dict | None:
        rows = self.query("SELECT * FROM resources WHERE url = ?", (url,))
        return rows[0] if rows else None

    def resources(self, run_id: int | None = None) -> list[dict]:
        if run_id is None:
            return self.query("SELECT * FROM resources ORDER BY url")
        return self.query("SELECT * FROM resources WHERE run_id = ? ORDER BY url", (run_id,))

    def path_owner(self, local_path: str) -> str | None:
        rows = self.query("SELECT url FROM resources WHERE local_path = ? LIMIT 1", (local_path,))
        return rows[0]["url"] if rows else None

    def clear_resources(self) -> None:
        self.execute("DELETE FROM resources")

    def start_run(self, command: str, anchor: str | None) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO runs(started_at, command, anchor) VALUES (?, ?, ?)",
                                    (now_iso(), command, anchor))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                     (now_iso(), json.dumps(summary, default=str), run_id))


# =============================================================================
# HTTP with retries, timeouts and per-host rate limiting
# =============================================================================


class HostRateLimiter:
    def __init__(self, interval: float):
        self.interval = max(0.0, interval)
        self.lock = threading.Lock()
        self.next_slot: dict[str, float] = {}

    def wait(self, host: str) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot.get(host, 0.0))
            self.next_slot[host] = slot + self.interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class HttpClient:
    def __init__(self, config: dict, log: logging.Logger):
        self.timeout = float(config["request_timeout"])
        self.retries = int(config["max_retries"])
        self.backoff = float(config["retry_backoff_seconds"])
        self.user_agent = config["user_agent"]
        self.limiter = HostRateLimiter(float(config["rate_limit_seconds"]))
        self.log = log
        self._local = threading.local()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})
            self._local.session = session
        return session

    def request(self, method: str, url: str, *, headers=None, allow_redirects=True, timeout=None, retries=None):
        retries = self.retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        host = host_of(url)
        last_error = "unknown error"
        for attempt in range(retries + 1):
            self.limiter.wait(host)
            retry_after = None
            try:
                response = self._session().request(method, url, headers=headers, timeout=timeout,
                                                   allow_redirects=allow_redirects)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last_error = f"HTTP {response.status_code}"
                header = response.headers.get("Retry-After", "")
                retry_after = float(header) if header.isdigit() else None
            if attempt < retries:
                delay = retry_after if retry_after is not None else self.backoff * (2 ** attempt)
                delay = min(delay, 120.0) + random.uniform(0, 0.5)
                self.log.debug("Retrying %s in %.1fs (%s)", url, delay, last_error)
                time.sleep(delay)
        raise FetchError(f"{last_error} after {retries + 1} attempt(s)")

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs):
        return self.request("HEAD", url, **kwargs)


# =============================================================================
# Archive providers
# =============================================================================


@dataclass
class Payload:
    body: bytes
    http_status: int
    content_type: str
    memento_timestamp: str
    fetched_from: str
    notes: list = field(default_factory=list)


def decode_content_encoding(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return body
    if encoding in ("gzip", "x-gzip"):
        return zlib.decompress(body, 16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    if encoding == "br":
        try:
            import brotli  # type: ignore
        except ImportError as exc:
            raise FetchError("payload is brotli-encoded; install the 'brotli' package") from exc
        return brotli.decompress(body)
    raise FetchError(f"unsupported Content-Encoding {encoding!r}")


def dechunk(data: bytes) -> bytes:
    out, pos = bytearray(), 0
    while pos < len(data):
        line_end = data.find(b"\r\n", pos)
        if line_end < 0:
            break
        size_text = data[pos:line_end].split(b";")[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            return data  # not actually chunked
        if size == 0:
            break
        start = line_end + 2
        out += data[start:start + size]
        pos = start + size + 2
    return bytes(out)


def parse_headers(block: bytes) -> dict:
    headers = {}
    for line in block.split(b"\r\n"):
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def parse_warc_record(record: bytes) -> tuple[dict, int | None, dict, bytes]:
    """Split one uncompressed WARC record into (warc headers, http status, http headers, body)."""
    head, _, rest = record.partition(b"\r\n\r\n")
    first, _, header_block = head.partition(b"\r\n")
    if not first.startswith(b"WARC/"):
        raise FetchError("not a WARC record")
    warc_headers = parse_headers(header_block)
    try:
        content_length = int(warc_headers.get("content-length", len(rest)))
    except ValueError:
        content_length = len(rest)
    block = rest[:content_length]
    if warc_headers.get("warc-type") not in ("response", "revisit") or not block.startswith(b"HTTP/"):
        return warc_headers, None, {}, b""
    http_head, _, body = block.partition(b"\r\n\r\n")
    status_line, _, http_header_block = http_head.partition(b"\r\n")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError) as exc:
        raise FetchError(f"malformed HTTP status line {status_line[:60]!r}") from exc
    return warc_headers, status, parse_headers(http_header_block), body


class ArchiveProvider:
    type_name = "base"

    def __init__(self, cfg: dict, http: HttpClient, db: Database, log: logging.Logger, target_domain: str):
        self.cfg = cfg
        self.name = cfg["name"]
        self.label = cfg.get("label", self.name)
        self.priority = int(cfg.get("priority", 100))
        self.can_replay = bool(cfg.get("replay", True))
        self.http = http
        self.db = db
        self.log = log
        self.target_domain = target_domain
        self._coverage: dict[str, tuple[datetime, datetime]] = {}

    # Coverage: discovery returned *every* capture for a domain in a time window.
    def mark_covered(self, domain: str, start: datetime, end: datetime) -> None:
        self._coverage[domain] = (start, end)

    def covers(self, url: str, start: datetime, end: datetime) -> bool:
        host = host_of(url)
        return any(is_same_site(host, dom) and s <= start and e >= end for dom, (s, e) in self._coverage.items())

    def discover(self, domain: str, start: datetime, end: datetime) -> list[Capture]:
        raise NotImplementedError

    def lookup(self, url: str, start: datetime, end: datetime) -> list[Capture]:
        raise NotImplementedError

    def fetch(self, cap: Capture) -> Payload:
        raise NotImplementedError

    def human_link(self, cap: Capture) -> str:
        return cap.url

    def register_known(self, captures: list[Capture]) -> None:
        """Hook for providers that need previously discovered captures (e.g. revisit resolution)."""

    def cached_lookup(self, url: str, start: datetime, end: datetime) -> list[Capture]:
        key = f"lookup|{self.name}|{url}|{fmt_ts(start)}|{fmt_ts(end)}"
        cached = self.db.cache_get(key)
        if cached is not None:
            return [Capture.from_dict(d) for d in cached]
        captures = self.lookup(url, start, end)
        self.db.cache_put(key, [c.to_dict() for c in captures])
        return captures

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "type": self.type_name,
                "priority": self.priority, "replay_supported": self.can_replay}


class PywbProvider(ArchiveProvider):
    """Any pywb / OpenWayback-style archive with a CDX API and raw (id_) replay."""

    type_name = "pywb"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cdx_url = self.cfg["cdx_url"].rstrip("/")
        self.replay_url = self.cfg.get("replay_url", "").rstrip("/")
        self.cdx_limit = int(self.cfg.get("cdx_limit", 50000))
        if self.can_replay and not self.replay_url:
            raise ConfigError(f"provider {self.name}: replay_url is required when replay is enabled")

    def _cdx(self, params: dict) -> list[dict]:
        query = {"output": "json", **params}
        response = self.http.get(f"{self.cdx_url}?{urlencode(query)}")
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise FetchError(f"CDX query returned HTTP {response.status_code}")
        text = response.text.strip()
        if not text:
            return []
        if text.startswith("["):
            data = json.loads(text)
            if data and isinstance(data[0], list):
                header = data[0]
                return [dict(zip(header, row)) for row in data[1:]]
            return data
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                rows.append(json.loads(line))
        return rows

    def _capture(self, row: dict) -> Capture:
        mime = row.get("mime") or row.get("mimetype") or ""
        return Capture(
            provider=self.name,
            url=row.get("url") or row.get("original") or "",
            timestamp=str(row["timestamp"]),
            status=str(row.get("status") or row.get("statuscode") or "-"),
            mime=mime,
            digest=row.get("digest") or "",
            length=str(row.get("length") or ""),
            revisit=(mime == "warc/revisit"),
            meta={k: row[k] for k in ("urlkey", "filename", "offset", "collection", "redirect", "source")
                  if row.get(k) not in (None, "", "-")},
        )

    def discover(self, domain, start, end):
        rows = self._cdx({"url": domain, "matchType": "domain", "from": fmt_ts(start), "to": fmt_ts(end),
                          "limit": self.cdx_limit})
        if len(rows) < self.cdx_limit:
            self.mark_covered(domain, start, end)
        else:
            self.log.warning("%s: discovery hit the CDX limit (%d rows); per-URL lookups will be used",
                             self.label, self.cdx_limit)
        return [self._capture(r) for r in rows if r.get("timestamp")]

    def lookup(self, url, start, end):
        base = strip_query(url)
        exact_only = (urlsplit(base).path or "/") == "/"
        params = {"url": url if exact_only else base, "matchType": "exact" if exact_only else "prefix",
                  "from": fmt_ts(start), "to": fmt_ts(end), "limit": 5000}
        wanted = match_key(url, keep_query=False)
        return [self._capture(r) for r in self._cdx(params)
                if r.get("timestamp") and match_key(r.get("url") or r.get("original") or "", keep_query=False) == wanted]

    def human_link(self, cap):
        return f"{self.replay_url or self.cdx_url}/{cap.timestamp}/{cap.url}"

    def fetch(self, cap):
        if not self.can_replay:
            raise FetchError(f"{self.label} is configured as discovery-only (no replay)")
        replay = f"{self.replay_url}/{cap.timestamp}id_/{cap.url}"
        response = self.http.get(replay, allow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            match = re.search(r"/(\d{14})(?:[a-z]{2}_)?/", location)
            if match and match.group(1) != cap.timestamp:
                raise TimestampMismatch(
                    f"archive redirected {cap.timestamp} -> {match.group(1)}; refused (different capture)")
            raise FetchError(f"replay answered HTTP {response.status_code} (Location: {location or '-'})")
        if response.status_code != 200:
            raise FetchError(f"replay answered HTTP {response.status_code}")
        notes = []
        memento = response.headers.get("Memento-Datetime")
        got = None
        if memento:
            try:
                got = fmt_ts(parsedate_to_datetime(memento))
            except (TypeError, ValueError):
                got = None
        if got and got != cap.timestamp:
            raise TimestampMismatch(f"archive served capture {got} instead of {cap.timestamp}; refused")
        if not got:
            notes.append("Memento-Datetime header missing; capture timestamp not verified by the archive")
        body = response.content
        encoding = response.headers.get("Content-Encoding", "")
        if encoding.strip().lower() == "br":
            body = decode_content_encoding(body, "br")
        return Payload(body=body, http_status=200, content_type=response.headers.get("Content-Type", ""),
                       memento_timestamp=got or cap.timestamp, fetched_from=replay, notes=notes)


class CommonCrawlProvider(ArchiveProvider):
    """Common Crawl: CDX index + raw WARC records fetched with range requests."""

    type_name = "commoncrawl"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index_url = self.cfg.get("index_url", "https://index.commoncrawl.org").rstrip("/")
        self.data_url = self.cfg.get("data_url", "https://data.commoncrawl.org").rstrip("/")
        self.collections = list(self.cfg.get("collections") or [])
        self.index_mode = self.cfg.get("index_mode", "auto")
        self.lookup_external = bool(self.cfg.get("lookup_external", False))
        if self.index_mode not in ("auto", "server", "cluster"):
            raise ConfigError(f"provider {self.name}: index_mode must be auto, server or cluster")
        if not self.collections:
            raise ConfigError(f"provider {self.name}: list at least one crawl in 'collections'")
        self._server_ok: bool | None = None
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()
        self._by_digest: dict[tuple[str, str], Capture] = {}
        self._by_ts: dict[tuple[str, str], Capture] = {}

    # -- raw range access ---------------------------------------------------
    def _range(self, url: str, start: int, length: int) -> bytes:
        response = self.http.get(url, headers={"Range": f"bytes={start}-{start + length - 1}",
                                               "Accept-Encoding": "identity"})
        if response.status_code == 200 and start > 0:
            raise FetchError("server ignored the Range header")
        if response.status_code not in (200, 206):
            raise FetchError(f"HTTP {response.status_code} for range request")
        return response.content

    def _size(self, url: str) -> int:
        with self._lock:
            if url in self._sizes:
                return self._sizes[url]
        response = self.http.head(url, headers={"Accept-Encoding": "identity"})
        if response.status_code != 200:
            raise FetchError(f"HTTP {response.status_code} for {url}")
        size = int(response.headers["Content-Length"])
        with self._lock:
            self._sizes[url] = size
        return size

    # -- index access -------------------------------------------------------
    def _key_at(self, idx_url: str, pos: int, total: int) -> bytes | None:
        """Key of the first complete cluster.idx line starting after byte `pos`."""
        chunk = self._range(idx_url, pos, min(16384, total - pos))
        newline = chunk.find(b"\n")
        if newline < 0:
            return None
        rest = chunk[newline + 1:]
        if not rest:
            return None
        return rest.split(b"\n", 1)[0].split(b"\t", 1)[0]

    def _cluster_entries(self, idx_url: str, lo: bytes, hi: bytes) -> list[tuple[str, int, int]]:
        """Binary-search cluster.idx (sorted, ~150 MB) with range requests.

        Returns the CDX blocks (file, offset, length) that may contain SURT keys
        in [lo, hi).
        """
        total = self._size(idx_url)
        lo_pos, hi_pos = 0, total
        while hi_pos - lo_pos > 65536:
            mid = (lo_pos + hi_pos) // 2
            key = self._key_at(idx_url, mid, total)
            if key is None or key >= lo:
                hi_pos = mid
            else:
                lo_pos = mid
        entries: list[tuple[str, int, int]] = []
        previous = None
        started = False
        pos, buffer, skip_partial = lo_pos, b"", lo_pos > 0
        while pos < total:
            chunk = self._range(idx_url, pos, min(131072, total - pos))
            pos += len(chunk)
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop() if pos < total else b""
            if skip_partial:
                lines = lines[1:]
                skip_partial = False
            done = False
            for line in lines:
                fields = line.split(b"\t")
                if len(fields) < 4:
                    continue
                key = fields[0]
                entry = (fields[1].decode(), int(fields[2]), int(fields[3]))
                if key < lo:
                    previous = entry
                    continue
                if not started:
                    started = True
                    if previous is not None:
                        entries.append(previous)
                if key >= hi:
                    done = True
                    break
                entries.append(entry)
            if done:
                break
        if not started and previous is not None:
            entries.append(previous)
        return entries

    def _cluster_query(self, coll: str, lo: bytes, hi: bytes) -> list[dict]:
        base = f"{self.data_url}/cc-index/collections/{coll}/indexes"
        rows = []
        for filename, offset, length in self._cluster_entries(f"{base}/cluster.idx", lo, hi):
            raw = self._range(f"{base}/{filename}", offset, length)
            try:
                text = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
            except zlib.error as exc:
                raise FetchError(f"corrupt CDX block {filename}@{offset}: {exc}") from exc
            for line in text.split(b"\n"):
                parts = line.split(b" ", 2)
                if len(parts) < 3 or not (lo <= parts[0] < hi):
                    continue
                row = json.loads(parts[2])
                row.setdefault("timestamp", parts[1].decode())
                row.setdefault("urlkey", parts[0].decode())
                rows.append(row)
        return rows

    def _server_query(self, coll: str, params: dict) -> list[dict]:
        base = f"{self.index_url}/{coll}-index"
        rows = []
        response = self.http.get(f"{base}?{urlencode({**params, 'showNumPages': 'true'})}", retries=1,
                                 timeout=min(self.http.timeout, 30))
        pages = 1
        if response.status_code == 200:
            try:
                pages = max(1, int(json.loads(response.text).get("pages", 1)))
            except (ValueError, AttributeError):
                pages = 1
        elif response.status_code != 404:
            raise FetchError(f"index server returned HTTP {response.status_code}")
        for page in range(pages):
            response = self.http.get(f"{base}?{urlencode({**params, 'output': 'json', 'page': page})}",
                                     retries=1, timeout=min(self.http.timeout, 60))
            if response.status_code == 404 and "No Captures" in response.text:
                return rows
            if response.status_code != 200:
                raise FetchError(f"index server returned HTTP {response.status_code}")
            rows.extend(json.loads(line) for line in response.text.splitlines() if line.strip().startswith("{"))
        return rows

    def _query(self, coll: str, lo: bytes, hi: bytes, server_params: dict) -> list[dict]:
        if self.index_mode in ("auto", "server") and self._server_ok is not False:
            try:
                rows = self._server_query(coll, server_params)
                self._server_ok = True
                return rows
            except FetchError as exc:
                if self.index_mode == "server":
                    raise
                self._server_ok = False
                self.log.info("Common Crawl index server unavailable (%s); using direct cluster.idx "
                              "search on %s", exc, host_of(self.data_url))
        return self._cluster_query(coll, lo, hi)

    def _domain_rows(self, coll: str, domain: str) -> list[dict]:
        key = f"cc-domain|{coll}|{domain}"
        cached = self.db.cache_get(key)
        if cached is not None:
            return cached
        surt = surt_host(domain)
        rows = self._query(coll, (surt + ")").encode(), (surt + "-").encode(),
                           {"url": domain, "matchType": "domain"})
        rows = [r for r in rows if is_same_site(host_of(r.get("url", "")), domain)]
        self.db.cache_put(key, rows)
        return rows

    def _capture(self, row: dict, coll: str) -> Capture:
        mime = row.get("mime") or ""
        return Capture(
            provider=self.name, url=row["url"], timestamp=str(row["timestamp"]),
            status=str(row.get("status") or "-"), mime=mime, digest=row.get("digest") or "",
            length=str(row.get("length") or ""), revisit=(mime == "warc/revisit"),
            meta={k: v for k, v in {
                "collection": coll, "filename": row.get("filename"), "offset": row.get("offset"),
                "length": row.get("length"), "mime_detected": row.get("mime-detected"),
                "truncated": row.get("truncated"), "urlkey": row.get("urlkey"),
            }.items() if v not in (None, "")},
        )

    def register_known(self, captures: list[Capture]) -> None:
        self._register([c for c in captures if c.provider == self.name])

    def _register(self, captures: list[Capture]) -> None:
        with self._lock:
            for cap in captures:
                if not cap.revisit and cap.status == "200":
                    self._by_ts.setdefault((match_key(cap.url), cap.timestamp), cap)
                    if cap.digest:
                        self._by_digest.setdefault((match_key(cap.url), cap.digest), cap)

    def discover(self, domain, start, end):
        out, errors = [], []
        for coll in self.collections:
            try:
                rows = self._domain_rows(coll, domain)
            except (FetchError, ValueError) as exc:
                errors.append(f"{coll}: {exc}")
                continue
            for row in rows:
                cap = self._capture(row, coll)
                if start <= cap.dt <= end:
                    out.append(cap)
        if errors and len(errors) == len(self.collections):
            raise FetchError("; ".join(errors))
        for err in errors:
            self.log.warning("%s: %s", self.label, err)
        if not errors:
            self.mark_covered(domain, start, end)
        self._register(out)
        return out

    def lookup(self, url, start, end):
        host = host_of(url)
        wanted = match_key(url, keep_query=False)
        out = []
        for coll in self.collections:
            if is_same_site(host, self.target_domain):
                rows = self._domain_rows(coll, self.target_domain)
            elif self.lookup_external:
                surt = surt_url(strip_query(url))
                rows = self._query(coll, surt.encode(), (surt + "@").encode(),
                                   {"url": strip_query(url), "matchType": "prefix"})
            else:
                return []
            for row in rows:
                if match_key(row.get("url", ""), keep_query=False) != wanted:
                    continue
                cap = self._capture(row, coll)
                if start <= cap.dt <= end:
                    out.append(cap)
        self._register(out)
        return out

    def human_link(self, cap):
        return (f"{self.data_url}/{cap.meta.get('filename')} "
                f"(offset {cap.meta.get('offset')}, length {cap.meta.get('length')})")

    def _read_record(self, cap: Capture) -> tuple[dict, int | None, dict, bytes, str]:
        try:
            filename = cap.meta["filename"]
            offset, length = int(cap.meta["offset"]), int(cap.meta["length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FetchError("capture has no WARC location") from exc
        raw = self._range(f"{self.data_url}/{filename}", offset, length)
        try:
            record = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        except zlib.error as exc:
            raise FetchError(f"corrupt WARC member: {exc}") from exc
        warc_headers, status, http_headers, body = parse_warc_record(record)
        warc_date = warc_headers.get("warc-date", "")
        try:
            got = fmt_ts(datetime.fromisoformat(warc_date.replace("Z", "+00:00")))
        except ValueError:
            got = None
        if got and got != cap.timestamp:
            raise TimestampMismatch(f"WARC-Date {warc_date} does not match capture {cap.timestamp}")
        return warc_headers, status, http_headers, body, f"{self.data_url}/{filename}#{offset}"

    def _resolve_revisit(self, cap: Capture, notes: list) -> Capture:
        """Find the capture whose payload a revisit record stands for.

        1. same URL + same payload digest, or
        2. the record's WARC-Refers-To-Target-URI / WARC-Refers-To-Date (e.g. a
           'server-not-modified' revisit: the site answered 304 for unchanged content).
        """
        key = match_key(cap.url)
        with self._lock:
            hit = self._by_digest.get((key, cap.digest)) if cap.digest else None
        if hit is not None:
            notes.append(f"identical-payload revisit; payload taken from capture {hit.timestamp} (same digest)")
            return hit
        warc, _, http_headers, _, _ = self._read_record(cap)
        if warc.get("warc-type") != "revisit":
            raise FetchError(f"expected a revisit record, found {warc.get('warc-type')!r}")
        ref_date = warc.get("warc-refers-to-date", "")
        ref_uri = warc.get("warc-refers-to-target-uri") or cap.url
        try:
            ref_ts = fmt_ts(datetime.fromisoformat(ref_date.replace("Z", "+00:00")))
        except ValueError:
            raise RevisitUnresolved("revisit record names no usable WARC-Refers-To-Date") from None
        with self._lock:
            hit = self._by_ts.get((match_key(ref_uri), ref_ts))
        if hit is None:
            raise RevisitUnresolved(f"revisit refers to {ref_uri} captured {ref_date}, which is not in the "
                                    f"configured Common Crawl collections")
        profile = warc.get("warc-profile", "").rstrip("/").rsplit("/", 1)[-1] or "revisit"
        last_modified = http_headers.get("last-modified")
        notes.append(f"{profile} revisit: on {ts_iso(cap.timestamp)} the server reported this content unchanged"
                     + (f" (Last-Modified {last_modified})" if last_modified else "")
                     + f"; payload taken from referenced capture {hit.timestamp}")
        return hit

    def fetch(self, cap):
        notes = []
        source = self._resolve_revisit(cap, notes) if cap.revisit else cap
        if source.meta.get("truncated"):
            raise PayloadTruncated(f"Common Crawl truncated this payload ({source.meta['truncated']})")
        warc_headers, status, http_headers, body, location = self._read_record(source)
        if warc_headers.get("warc-type") == "revisit":
            raise RevisitUnresolved("referenced WARC record is itself a revisit")
        if warc_headers.get("warc-truncated"):
            raise PayloadTruncated(f"WARC-Truncated: {warc_headers['warc-truncated']}")
        if "chunked" in http_headers.get("transfer-encoding", "").lower():
            body = dechunk(body)
        body = decode_content_encoding(body, http_headers.get("content-encoding", ""))
        return Payload(body=body, http_status=status or 0, content_type=http_headers.get("content-type", ""),
                       memento_timestamp=cap.timestamp, fetched_from=location, notes=notes)


PROVIDER_TYPES = {"pywb": PywbProvider, "commoncrawl": CommonCrawlProvider}


# =============================================================================
# Reference extraction (HTML / CSS / JS)
# =============================================================================

ASSET_EXTS = ("png|jpe?g|gif|svg|webp|bmp|ico|cur|mp3|ogg|oga|wav|m4a|aac|flac|webm|mp4|woff2?|ttf|otf|eot|"
              "json|css|js|mjs|txt|xml|html?|wasm|atlas|fnt|plist")
JS_ASSET_LITERAL_RE = re.compile(
    r"""(["'`])((?:https?:)?(?://)?[A-Za-z0-9_\-./%~:@]*?[A-Za-z0-9_\-%~]\.(?:EXTS)(?:\?[^"'`\s<>\\]*)?)\1"""
    .replace("EXTS", ASSET_EXTS), re.I)
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+?)\1\s*\)""", re.I)
CSS_IMPORT_RE = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.I)
SKIP_SCHEMES = ("data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "chrome:", "file:", "#")


@dataclass
class Ref:
    url: str
    relation: str
    speculative: bool = False


def _resolve_ref(base: str, raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip("'\"")
    if not raw or raw.lower().startswith(SKIP_SCHEMES) or len(raw) > 2048:
        return None
    if any(token in raw for token in ("${", "{{", "\n", " + ")):
        return None
    return normalize_url(urljoin(base, raw))


def extract_css_refs(css_url: str, text: str, relation: str = "css-url") -> list[Ref]:
    refs = []
    for regex in (CSS_URL_RE, CSS_IMPORT_RE):
        for match in regex.finditer(text):
            url = _resolve_ref(css_url, match.group(2))
            if url:
                refs.append(Ref(url, "css-import" if regex is CSS_IMPORT_RE else relation))
    return refs


def extract_js_refs(document_base: str, text: str) -> list[Ref]:
    """String literals that look like asset paths (speculative: JS may build them differently)."""
    refs = []
    for match in JS_ASSET_LITERAL_RE.finditer(text):
        url = _resolve_ref(document_base, match.group(2))
        if url:
            refs.append(Ref(url, "js-literal", speculative=True))
    return refs


def extract_html_refs(page_url: str, html: str) -> list[Ref]:
    soup = BeautifulSoup(html, "html.parser")
    base = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(page_url, base_tag["href"])
    refs: list[Ref] = []

    def add(raw, relation, speculative=False):
        url = _resolve_ref(base, raw)
        if url:
            refs.append(Ref(url, relation, speculative))

    for tag in soup.find_all("script", src=True):
        add(tag["src"], "script")
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        if "stylesheet" in rel:
            add(tag["href"], "stylesheet")
        elif any(word in rel for word in ("icon", "manifest", "preload", "prefetch", "apple-touch", "mask-icon")):
            add(tag["href"], "link-asset")
    for tag in soup.find_all(["img", "source", "audio", "video", "embed", "track", "input", "iframe", "frame"]):
        if tag.get("src"):
            add(tag["src"], "iframe" if tag.name in ("iframe", "frame") else tag.name)
        if tag.get("poster"):
            add(tag["poster"], "poster")
        for candidate in (tag.get("srcset") or "").split(","):
            if candidate.strip():
                add(candidate.strip().split()[0], "srcset")
    for tag in soup.find_all("object", data=True):
        add(tag["data"], "object")
    for tag in soup.find_all("meta", content=True):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        if prop in ("og:image", "twitter:image", "msapplication-tileimage"):
            add(tag["content"], "meta-image", speculative=True)
    for tag in soup.find_all(style=True):
        refs.extend(extract_css_refs(base, tag["style"], "inline-style"))
    for tag in soup.find_all("style"):
        refs.extend(extract_css_refs(base, tag.get_text(), "style-block"))
    for tag in soup.find_all("script", src=False):
        refs.extend(extract_js_refs(base, tag.get_text()))
    for tag in soup.find_all("a", href=True):
        add(tag["href"], "hyperlink")
    return refs


def extract_refs(url: str, kind: str, body: bytes, document_base: str) -> list[Ref]:
    text = decode_text(body)
    if kind == "html":
        return extract_html_refs(url, text)
    if kind == "css":
        return extract_css_refs(url, text)
    if kind in ("js", "json"):
        return extract_js_refs(document_base, text)
    return []


# =============================================================================
# Project paths
# =============================================================================

UNSAFE_SEGMENT_RE = re.compile(r'[\x00-\x1f<>:"\\|?*]')


def sanitize_segment(segment: str) -> str:
    segment = UNSAFE_SEGMENT_RE.sub("_", segment).strip(" ")
    if segment in ("", ".", ".."):
        return "_"
    if len(segment) > 120:
        suffix = PurePosixPath(segment).suffix[:12]
        segment = segment[:80] + "-" + hashlib.sha1(segment.encode()).hexdigest()[:10] + suffix
    return segment


class ProjectPaths:
    def __init__(self, root: Path, config: dict):
        self.root = root
        self.original = (root / config["output_directory"]).resolve()
        self.archive = root / "archive"
        self.manifests = self.archive / "manifests"
        self.database = self.archive / "database" / "downloads.sqlite3"
        self.working = root / "working"
        self.logs = root / "logs"

    def ensure(self) -> None:
        for directory in (self.original, self.manifests, self.database.parent, self.logs):
            directory.mkdir(parents=True, exist_ok=True)

    def manifest(self, name: str) -> Path:
        return self.manifests / name

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def abs(self, rel: str) -> Path:
        path = Path(rel)
        return path if path.is_absolute() else self.root / path

    def local_path_for(self, url: str, kind: str, domain: str) -> Path:
        parts = urlsplit(url)
        host = (parts.hostname or "unknown-host").lower()
        if parts.port and parts.port not in (80, 443):
            host = f"{host}_{parts.port}"
        base = self.original / host if is_same_site(parts.hostname or "", domain) else \
            self.original / "external-hosts" / host
        raw_path = unquote(parts.path or "/")
        if raw_path.endswith("/"):
            raw_path += "index.html"
        segments = [sanitize_segment(s) for s in raw_path.split("/") if s not in ("", ".", "..")]
        if not segments:
            segments = ["index.html"]
        if kind == "html" and "." not in segments[-1]:
            segments[-1] += ".html"
        path = (base.joinpath(*segments)).resolve()
        if self.original not in path.parents:
            raise FetchError(f"refusing unsafe local path for {url}")
        return path


def with_hash_suffix(path: Path, url: str) -> Path:
    tag = hashlib.sha1(url.encode()).hexdigest()[:8]
    return path.with_name(f"{path.stem}-{tag}{path.suffix}")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.part-{threading.get_ident()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# =============================================================================
# Logging
# =============================================================================


class TqdmLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            message = self.format(record)
            if tqdm is not None:
                tqdm.write(message, file=sys.stderr)
            else:
                print(message, file=sys.stderr)
        except Exception:  # pragma: no cover
            self.handleError(record)


def setup_logging(logs_dir: Path, verbose: bool) -> tuple[logging.Logger, logging.Logger]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("mope")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    fetch_handler = logging.FileHandler(logs_dir / "fetch.log", encoding="utf-8")
    fetch_handler.setLevel(logging.DEBUG)
    fetch_handler.setFormatter(file_fmt)
    error_handler = logging.FileHandler(logs_dir / "errors.log", encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(file_fmt)
    console = TqdmLoggingHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s" if verbose else "%(message)s"))
    for handler in (fetch_handler, error_handler, console):
        log.addHandler(handler)

    vlog = logging.getLogger("mope.validation")
    vlog.setLevel(logging.DEBUG)
    for handler in list(vlog.handlers):
        vlog.removeHandler(handler)
    validation_handler = logging.FileHandler(logs_dir / "validation.log", encoding="utf-8")
    validation_handler.setFormatter(file_fmt)
    vlog.addHandler(validation_handler)
    vlog.propagate = True
    return log, vlog


# =============================================================================
# Configuration
# =============================================================================


def load_config(path: Path) -> tuple[dict, bool]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        return config, False
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    config.update(user)
    return config, True


def apply_cli_overrides(config: dict, args) -> None:
    if args.date:
        config["snapshot_date"] = args.date
    if args.domain:
        config["domain"] = args.domain
    if args.workers:
        config["workers"] = args.workers
    if args.timezone:
        config["snapshot_timezone"] = args.timezone
    if args.timestamp:
        config["anchor_timestamp"] = args.timestamp
    if args.allow_off_date:
        config["allow_off_date"] = True
    if args.max_date_distance is not None:
        config["max_date_distance_days"] = args.max_date_distance
    if args.no_same_day_fallback:
        config["same_day_fallback"] = False
    if args.no_external:
        config["discover_external_assets"] = False
    if args.provider:
        wanted = {p.strip() for item in args.provider for p in item.split(",") if p.strip()}
        known = {p["name"] for p in config["providers"]}
        unknown = wanted - known
        if unknown:
            raise ConfigError(f"unknown provider(s): {', '.join(sorted(unknown))} (known: {', '.join(sorted(known))})")
        for provider in config["providers"]:
            provider["enabled"] = provider["name"] in wanted


def validate_config(config: dict) -> list[str]:
    try:
        return _validate_config(config)
    except (KeyError, TypeError, ValueError) as exc:
        return [f"malformed configuration value: {exc}"]


def _validate_config(config: dict) -> list[str]:
    errors = []
    try:
        date.fromisoformat(str(config["snapshot_date"]))
    except ValueError:
        errors.append(f"snapshot_date must be YYYY-MM-DD, got {config['snapshot_date']!r}")
    domain = str(config.get("domain") or "").lower().strip()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        errors.append(f"domain looks invalid: {config.get('domain')!r}")
    if not 1 <= int(config["workers"]) <= 32:
        errors.append("workers must be between 1 and 32")
    if float(config["request_timeout"]) <= 0:
        errors.append("request_timeout must be positive")
    if int(config["max_retries"]) < 0:
        errors.append("max_retries cannot be negative")
    if float(config["max_date_distance_days"]) < 0 or float(config["discovery_window_days"]) < 0:
        errors.append("max_date_distance_days and discovery_window_days cannot be negative")
    anchor = config.get("anchor_timestamp")
    if anchor not in (None, "") and not re.fullmatch(r"\d{8,14}", str(anchor)):
        errors.append(f"anchor_timestamp must be 8-14 digits (YYYYMMDDhhmmss), got {anchor!r}")
    names = set()
    enabled_replay = False
    for provider in config.get("providers", []):
        name = provider.get("name")
        if not name or name in names:
            errors.append(f"provider names must be unique and non-empty ({name!r})")
        names.add(name)
        if provider.get("type") not in PROVIDER_TYPES:
            errors.append(f"provider {name}: type must be one of {', '.join(PROVIDER_TYPES)}")
        for key in ("cdx_url", "replay_url", "index_url", "data_url"):
            if provider.get(key) and is_excluded_archive(host_of(provider[key])):
                errors.append(f"provider {name}: {provider[key]} is an Internet Archive / Wayback Machine "
                              f"endpoint, which this project excludes by design")
        if provider.get("enabled", True) and provider.get("replay", True):
            enabled_replay = True
    if not enabled_replay:
        errors.append("enable at least one provider with replay support (needed to download files)")
    return errors


# =============================================================================
# Crawler: resource discovery + downloading
# =============================================================================


@dataclass
class QueueItem:
    url: str
    kind: str
    relation: str
    discovered_from: str
    depth: int
    speculative: bool = False
    external: bool = False
    optional: bool = False
    forced: Capture | None = None


class Crawler:
    def __init__(self, pipeline: "Pipeline", anchor: Capture, policy: SelectionPolicy, resume: bool, run_id: int):
        self.p = pipeline
        self.cfg = pipeline.config
        self.db = pipeline.db
        self.log = pipeline.log
        self.domain = pipeline.domain
        self.anchor = anchor
        self.policy = policy
        self.resume = resume
        self.run_id = run_id
        self.document_base = normalize_url(anchor.url)
        self.home_key = match_key(self.document_base)
        self.items: dict[str, QueueItem] = {}
        self.lock = threading.Lock()
        self.path_lock = threading.Lock()
        self.reserved: dict[str, str] = {}
        self.external_links: set[str] = set()
        self.skipped_archive_refs: set[str] = set()
        self.stats = defaultdict(int)
        self.lookup_window = pipeline.lookup_window(anchor.dt)
        self.index_by_path: dict[str, list[Capture]] = defaultdict(list)
        self.index_by_url: dict[str, list[Capture]] = defaultdict(list)
        for cap in pipeline.captures:
            self.index_by_path[match_key(cap.url, keep_query=False)].append(cap)
            self.index_by_url[match_key(cap.url)].append(cap)

    # -- queue --------------------------------------------------------------
    def enqueue(self, item: QueueItem) -> bool:
        key = match_key(item.url)
        with self.lock:
            existing = self.items.get(key)
            if existing is not None:
                if existing.speculative and not item.speculative:
                    existing.speculative = False
                    existing.relation = item.relation
                    existing.discovered_from = item.discovered_from
                if existing.optional and not item.optional:
                    existing.optional = False
                return False
            if is_excluded_archive(host_of(item.url)):
                self.skipped_archive_refs.add(item.url)
                return False
            if item.depth > int(self.cfg["max_crawl_depth"]):
                return False
            if item.external and not self.cfg["discover_external_assets"]:
                self.items[key] = item
                self.db.upsert_resource(self._base_record(item) | {
                    "status": "skipped_external", "error": "discover_external_assets is disabled"})
                return False
            self.items[key] = item
            return True

    def _base_record(self, item: QueueItem) -> dict:
        return {
            "url": item.url, "kind": item.kind, "relation": item.relation,
            "discovered_from": item.discovered_from, "depth": item.depth, "external": item.external,
            "speculative": item.speculative, "optional": item.optional,
            "anchor": self.anchor.timestamp, "run_id": self.run_id,
        }

    def seeds(self) -> list[QueueItem]:
        items = [QueueItem(url=self.document_base, kind="html", relation="homepage", discovered_from="anchor",
                           depth=0, forced=self.anchor)]
        if not self.cfg["include_target_date_captures"]:
            return items
        for key, caps in sorted(self.index_by_url.items()):
            if key == self.home_key:
                continue
            same_day = [c for c in self.policy.candidates(caps, caps[0].url)
                        if c.tier in ("same_day_at_or_before_anchor", "same_day_after_anchor")]
            if not same_day or not is_same_site(host_of(same_day[0].capture.url), self.domain):
                continue
            url = normalize_url(same_day[0].capture.url)
            if url:
                items.append(QueueItem(url=url, kind=kind_for(url, same_day[0].capture.mime),
                                       relation="archive-index", discovered_from="capture-index", depth=1))
        return items

    # -- resolution ---------------------------------------------------------
    def resolve(self, url: str) -> tuple[list[Candidate], Capture | None]:
        caps = list(self.index_by_path.get(match_key(url, keep_query=False), ()))
        seen = {c.ident for c in caps}
        lo, hi = self.lookup_window
        for provider in self.p.providers.values():
            if not provider.can_replay or provider.covers(url, lo, hi):
                continue
            try:
                extra = provider.cached_lookup(url, lo, hi)
            except (FetchError, ValueError) as exc:
                self.log.debug("%s lookup failed for %s: %s", provider.name, url, exc)
                continue
            for cap in extra:
                if cap.ident not in seen:
                    seen.add(cap.ident)
                    caps.append(cap)
        candidates = self.policy.candidates(caps, url)
        return candidates, (None if candidates else self.policy.nearest(caps))

    def allocate_path(self, url: str, kind: str) -> tuple[Path, str]:
        with self.path_lock:
            existing = self.db.get_resource(url)
            if existing and existing.get("local_path"):
                path = self.p.paths.abs(existing["local_path"])
                return path, existing["local_path"]
            path = self.p.paths.local_path_for(url, kind, self.domain)
            rel = self.p.paths.rel(path)
            owner = self.reserved.get(rel) or self.db.path_owner(rel)
            if owner and owner != url:
                path = with_hash_suffix(path, url)
                rel = self.p.paths.rel(path)
            self.reserved[rel] = url
            return path, rel

    def _nearest_info(self, cap: Capture | None) -> dict | None:
        if cap is None:
            return None
        return {"provider": cap.provider, "timestamp": cap.timestamp, "iso": ts_iso(cap.timestamp),
                "url": cap.url, "status": cap.status,
                "deviation_from_target_days": self.p.target.signed_deviation_days(cap.dt),
                "replay_supported": self.p.providers[cap.provider].can_replay
                if cap.provider in self.p.providers else False}

    # -- processing ---------------------------------------------------------
    def process(self, item: QueueItem) -> list[QueueItem]:
        base = self._base_record(item)
        existing = self.db.get_resource(item.url)
        if (self.resume and existing and existing.get("status") == "ok"
                and existing.get("anchor") == self.anchor.timestamp and existing.get("local_path")):
            path = self.p.paths.abs(existing["local_path"])
            if path.is_file() and (not self.cfg["calculate_sha256"] or not existing.get("sha256")
                                   or sha256_file(path) == existing["sha256"]):
                self.db.upsert_resource({k: v for k, v in base.items() if k != "kind"} | {"status": "ok"})
                self.stats["reused"] += 1
                return self.children(item, path.read_bytes(), existing.get("mime"))
        if item.forced is not None:
            candidates, nearest = [Candidate(item.forced, "anchor", 0.0)], None
        else:
            candidates, nearest = self.resolve(item.url)
        if not candidates:
            status = "off_date_blocked" if nearest else "not_archived"
            if nearest is not None:
                error = (f"no capture allowed by the date policy; nearest is {nearest.provider} "
                         f"{ts_human(nearest.timestamp)} UTC")
            else:
                error = "no capture found in the configured archives"
            self.db.upsert_resource(base | {"status": status, "error": error,
                                            "nearest_alternative": self._nearest_info(nearest)})
            self.stats[status] += 1
            return []

        errors = []
        max_candidates = int(self.cfg["max_candidates_per_resource"])
        for attempt, cand in enumerate(candidates[:max_candidates], start=1):
            cap = cand.capture
            provider = self.p.providers[cap.provider]
            try:
                payload = provider.fetch(cap)
            except FetchError as exc:
                errors.append(f"{provider.name}@{cap.timestamp}: {exc}")
                self.log.debug("Fetch failed for %s via %s@%s: %s", item.url, provider.name, cap.timestamp, exc)
                continue
            except Exception as exc:  # unexpected parser errors etc.
                errors.append(f"{provider.name}@{cap.timestamp}: unexpected {type(exc).__name__}: {exc}")
                self.log.debug("Unexpected error for %s", item.url, exc_info=True)
                continue
            if payload.http_status != 200:
                errors.append(f"{provider.name}@{cap.timestamp}: archived HTTP status {payload.http_status}")
                continue
            capture_mime = cap.mime if cap.mime not in ("", "warc/revisit", "unk", "-") else ""
            mime = capture_mime or payload.content_type.split(";")[0].strip() or \
                (mimetypes.guess_type(urlsplit(item.url).path)[0] or "")
            kind = kind_for(item.url, mime)
            path, rel = self.allocate_path(item.url, kind)
            try:
                write_atomic(path, payload.body)
            except (FileExistsError, NotADirectoryError, IsADirectoryError):
                path = self.p.paths.original / "_path-conflicts" / \
                    f"{hashlib.sha1(item.url.encode()).hexdigest()[:12]}-{path.name}"
                rel = self.p.paths.rel(path)
                write_atomic(path, payload.body)
            digest = sha256_bytes(payload.body) if self.cfg["calculate_sha256"] else None
            self.db.upsert_resource(base | {
                "kind": kind, "provider": cap.provider, "capture_timestamp": cap.timestamp,
                "capture_url": cap.url, "capture_status": cap.status, "selection_tier": cand.tier,
                "distance_seconds": cand.distance_seconds,
                "deviation_days": self.p.target.signed_deviation_days(cap.dt),
                "query_mismatch": cand.query_mismatch, "local_path": rel, "status": "ok",
                "error": "; ".join(errors) or None, "notes": "; ".join(payload.notes) or None,
                "http_status": payload.http_status, "mime": mime, "size": len(payload.body),
                "sha256": digest, "attempts": attempt, "nearest_alternative": None,
            })
            self.stats["downloaded"] += 1
            return self.children(item, payload.body, mime)
        self.db.upsert_resource(base | {"status": "failed", "error": "; ".join(errors) or "all candidates failed",
                                        "attempts": min(len(candidates), max_candidates)})
        self.stats["failed"] += 1
        return []

    def children(self, item: QueueItem, body: bytes, mime: str | None) -> list[QueueItem]:
        kind = kind_for(item.url, mime)
        if item.depth >= int(self.cfg["max_crawl_depth"]) or kind not in ("html", "css", "js", "json"):
            return []
        try:
            refs = extract_refs(item.url, kind, body, self.document_base)
        except Exception as exc:  # malformed markup should never stop the crawl
            self.log.warning("Could not parse references in %s: %s", item.url, exc)
            return []
        out = []
        for ref in refs:
            host = host_of(ref.url)
            same = is_same_site(host, self.domain)
            if not self.cfg["discover_subdomains"] and same and host not in (self.domain, "www." + self.domain):
                continue
            if ref.relation == "hyperlink":
                ref_kind = kind_for(ref.url)
                if same and item.depth == 0 and ref_kind in ("html", "text", "json", "xml") \
                        and match_key(ref.url) != self.home_key:
                    out.append(QueueItem(url=ref.url, kind=ref_kind, relation="hyperlink",
                                         discovered_from=item.url, depth=item.depth + 1, optional=True))
                elif not same:
                    self.external_links.add(ref.url)
                continue
            out.append(QueueItem(url=ref.url, kind=kind_for(ref.url), relation=ref.relation,
                                 discovered_from=item.url, depth=item.depth + 1,
                                 speculative=ref.speculative, external=not same))
        return out

    # -- main loop ----------------------------------------------------------
    def run(self) -> dict:
        workers = int(self.cfg["workers"])
        pending: dict[cf.Future, QueueItem] = {}
        bar = tqdm(total=0, unit="file", desc="Downloading", disable=None, dynamic_ncols=True) if tqdm else None
        executor = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch")
        try:
            def submit(item: QueueItem) -> None:
                pending[executor.submit(self.process, item)] = item
                if bar is not None:
                    bar.total += 1
                    bar.refresh()

            for seed in self.seeds():
                if self.enqueue(seed):
                    submit(seed)
            while pending:
                done, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                for future in done:
                    item = pending.pop(future)
                    try:
                        children = future.result()
                    except Exception as exc:
                        self.log.error("Unhandled error processing %s: %s", item.url, exc, exc_info=True)
                        self.db.upsert_resource(self._base_record(item) | {"status": "failed", "error": str(exc)})
                        self.stats["failed"] += 1
                        children = []
                    if bar is not None:
                        bar.update(1)
                    for child in children:
                        if self.enqueue(child):
                            submit(child)
        except KeyboardInterrupt:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            if bar is not None:
                bar.close()
            executor.shutdown(wait=True)
        self.stats["queued"] = len(self.items)
        return dict(self.stats)


# =============================================================================
# Client analysis
# =============================================================================

WS_URL_RE = re.compile(r"""wss?://[^\s'"`<>\\)]+""", re.I)
HTTP_URL_RE = re.compile(r"""https?://[^\s'"`<>\\)]+""", re.I)
IP_RE = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?::\d{2,5})?(?![\d.])")
HOST_PORT_RE = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?::(\d{2,5}))\b", re.I)
WEBSOCKET_CTOR_RE = re.compile(r"new\s+WebSocket\s*\(")
NETCALL_RE = re.compile(r"""(?:\$\.(?:ajax|get|post|getJSON)\s*\(|\bfetch\s*\(|new\s+XMLHttpRequest|\.open\s*\(\s*["'](?:GET|POST|PUT|DELETE)["'])""")
STRING_LITERAL_RE = re.compile(r"""(["'])((?:\\.|(?!\1)[^\\\n]){1,300}?)\1""")
COMMON_TLDS = {"io", "com", "net", "org", "gg", "xyz", "me", "co", "us", "eu", "uk", "de", "fr", "ru", "br", "au",
               "ca", "jp", "info", "biz", "app", "dev", "online", "space", "games", "fun", "cloud", "tv", "pt", "is"}
KEYWORD_PATTERNS = {
    "account_server_references": r"log_?in|log_?out|register|sign_?up|sign_?in|account|password|passwd|auth|token|"
                                 r"session|oauth|gapi|googleuser|google_?signin|facebook|fblogin|username|user_?id|"
                                 r"mopecoin|coins|purchase|premium|verify|captcha",
    "master_server_references": r"server_?list|getservers?|master|region|serverselect|lobby|matchmak|"
                                r"player_?count|players_?online|server_?info|game_?server|ping",
    "sandbox": r"sandbox|private_?server|custom_?server|freeplay|test_?mode|test_?server",
    "developer": r"dev_?mode|is_?dev|developer|debug|admin|moderator|god_?mode|cheat|localhost|127\.0\.0\.1|"
                 r"show_?fps|staging|beta\.mope",
}
KEYWORD_RES = {
    cat: re.compile(r"(?<![A-Za-z0-9_$])[A-Za-z0-9_$]{0,24}(?:%s)[A-Za-z0-9_$]{0,24}" % pat, re.I)
    for cat, pat in KEYWORD_PATTERNS.items()
}
REGION_WORDS = (r"north\s*america|south\s*america|us\s*(?:east|west|central)|usa|america|europe|asia|brazil|"
                r"australia|oceania|russia|india|japan|singapore|germany|france|united\s*kingdom|london|canada|"
                r"turkey|korea|china|mexico|poland|netherlands|frankfurt|amsterdam|dallas|miami|seattle|virginia|"
                r"new\s*york|chicago|los\s*angeles|sydney|tokyo|hong\s*kong|sandbox|beta|test|dev|private|local")
REGION_LITERAL_RE = re.compile(rf"^\s*(?:{REGION_WORDS})(?:[\s#\-_]*\d{{1,2}})?(?:\s*\([^)]*\))?\s*$", re.I)
REGION_CODE_RE = re.compile(r"^(?:US|NA|EU|SA|AS|AU|OC|BR|RU|IN|JP|SG|UK|DE|FR|CA|TR|KR|CN|MX|PL|NL|USE|USW|EUW|EUE)"
                            r"(?:[\s#\-_]?\d{1,2})?$")
SERVER_CONTEXT_RE = re.compile(r"server|region|\bip\b|host|port|wss?://|socket", re.I)
REGION_KEY_RE = re.compile(r"""(?:^|[{,\s])["']?(?:name|region|label|title|serverName|server_name|loc|location)["']?\s*:""", re.I)
ADDRESS_KEY_RE = re.compile(r"""(?:^|[{,\s])["']?(?:ip|host|hostname|address|addr|domain|url|port|ws|socket|endpoint)["']?\s*:""", re.I)
OBJECT_RE = re.compile(r"\{[^{}]{0,600}\}")
ARRAY_RE = re.compile(r"\[[^\[\]]{0,400}\]")
PROP_RE = re.compile(r"""["']?([A-Za-z_$][\w$]*)["']?\s*:\s*(?:(["'])(.*?)\2|(\d+(?:\.\d+)?))""")
REGION_HOST_LABEL_RE = re.compile(r"^(?:server\d+|s\d+|game\d*|(?:us|na|eu|as|sa|au|br|ru|in|jp|sg|uk|de|fr|ca)"
                                  r"(?:[-_]?\w{0,8})?\d{0,2})$", re.I)
ASSET_SUFFIX_RE = re.compile(r"\.(?:%s)(?:\?|$)" % ASSET_EXTS, re.I)
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


JS_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")


def decode_js_escapes(text: str, threshold: int = 50) -> str | None:
    """Decode \\xNN / \\uNNNN escapes (common in obfuscated builds); None if there are few.

    Characters that would break string-literal boundaries stay escaped.
    """
    if len(JS_ESCAPE_RE.findall(text[:2_000_000])) < threshold:
        return None

    def repl(match):
        char = chr(int(match.group(1) or match.group(2), 16))
        return match.group(0) if char in "\"'`\\\n\r" else char

    return JS_ESCAPE_RE.sub(repl, text)


class TextLocator:
    def __init__(self, text: str):
        self.text = text
        self.newlines = [i for i, ch in enumerate(text) if ch == "\n"]

    def line_col(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.newlines, offset - 1)
        start = self.newlines[line - 1] + 1 if line else 0
        return line + 1, offset - start + 1

    def context(self, start: int, end: int, radius: int = 80) -> str:
        snippet = self.text[max(0, start - radius):min(len(self.text), end + radius)]
        return re.sub(r"\s+", " ", snippet).strip()


class FindingSet:
    MAX_OCCURRENCES = 5

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def add(self, category: str, value: str, source: str, loc: TextLocator, start: int, end: int, **extra):
        key = (category, value.lower() if category in KEYWORD_PATTERNS else value)
        entry = self.items.get(key)
        if entry is None:
            entry = {"value": value, "count": 0, "files": [], "occurrences": [], **extra}
            self.items[key] = entry
        entry["count"] += 1
        if source not in entry["files"]:
            entry["files"].append(source)
        if len(entry["occurrences"]) < self.MAX_OCCURRENCES:
            line, col = loc.line_col(start)
            entry["occurrences"].append({"file": source, "line": line, "column": col, "offset": start,
                                         "context": loc.context(start, end)})

    def category(self, name: str, limit: int = 500) -> list[dict]:
        rows = [v for (cat, _), v in self.items.items() if cat == name]
        rows.sort(key=lambda r: (-r["count"], r["value"]))
        return rows[:limit]


class ClientAnalyzer:
    def __init__(self, pipeline: "Pipeline"):
        self.p = pipeline
        self.domain = pipeline.domain
        self.findings = FindingSet()
        self.regions: dict[str, dict] = {}

    def _sources(self, resources: list[dict]) -> tuple[dict | None, list[dict]]:
        excludes = [p.lower() for p in self.p.config["analysis_exclude_patterns"]]
        ok = [r for r in resources if r["status"] == "ok" and r.get("local_path")]
        client = None
        for res in ok:
            path = urlsplit(res["url"]).path
            if PurePosixPath(path).name.lower() == "client.js" and is_same_site(host_of(res["url"]), self.domain):
                if client is None or host_of(res["url"]) == self.domain:
                    client = res
        if client is None:
            for res in ok:
                name = PurePosixPath(urlsplit(res["url"]).path).name.lower()
                if res["kind"] == "js" and "client" in name and is_same_site(host_of(res["url"]), self.domain):
                    client = res
                    break
        sources = [r for r in ok if r["kind"] in ("js", "html", "json")
                   and is_same_site(host_of(r["url"]), self.domain)
                   and not any(pat in r["url"].lower() for pat in excludes)]
        if client is not None and client not in sources:
            sources.insert(0, client)
        return client, sources

    def _add_region(self, name: str, confidence: str, source_kind: str, source: str, loc: TextLocator,
                    start: int, end: int, props: dict | None = None) -> None:
        props = props or {}
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            return
        key = name.lower()
        entry = self.regions.setdefault(key, {"name": name, "confidence": confidence, "source_kinds": [],
                                              "properties": {}, "evidence": []})
        if CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK[entry["confidence"]]:
            entry["confidence"] = confidence
        if source_kind not in entry["source_kinds"]:
            entry["source_kinds"].append(source_kind)
        for k, v in props.items():
            values = entry["properties"].setdefault(k, [])
            if v not in values:
                values.append(v)
        if len(entry["evidence"]) < 8:
            line, col = loc.line_col(start)
            entry["evidence"].append({"file": source, "line": line, "column": col,
                                      "kind": source_kind, "context": loc.context(start, end)})

    def _scan(self, text: str, source: str) -> None:
        loc = TextLocator(text)
        add = self.findings.add
        for match in WS_URL_RE.finditer(text):
            add("websocket_urls", match.group(0).rstrip(".,;"), source, loc, match.start(), match.end())
        for match in HTTP_URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;")
            host = host_of(url)
            add("http_urls", url, source, loc, match.start(), match.end(), host=host,
                same_site=is_same_site(host, self.domain), looks_like_asset=bool(ASSET_SUFFIX_RE.search(url)))
        for match in IP_RE.finditer(text):
            add("ip_addresses", match.group(0), source, loc, match.start(), match.end())
        for match in HOST_PORT_RE.finditer(text):
            host, port = match.group(1).lower(), match.group(2)
            if host.rsplit(".", 1)[-1] in COMMON_TLDS or is_same_site(host, self.domain):
                add("host_ports", f"{host}:{port}", source, loc, match.start(), match.end())
        for match in WEBSOCKET_CTOR_RE.finditer(text):
            snippet = re.sub(r"\s+", " ", text[match.start():match.end() + 160])
            add("websocket_constructors", snippet, source, loc, match.start(), match.end() + 160)
        for match in NETCALL_RE.finditer(text):
            snippet = re.sub(r"\s+", " ", text[match.start():match.end() + 140])
            add("network_calls", snippet, source, loc, match.start(), match.end() + 140)
        for category, regex in KEYWORD_RES.items():
            for match in regex.finditer(text):
                add(category, match.group(0), source, loc, match.start(), match.end())
        for match in STRING_LITERAL_RE.finditer(text):
            value = match.group(2)
            if self._looks_like_endpoint(value):
                add("endpoint_paths", value, source, loc, match.start(), match.end())
            if REGION_LITERAL_RE.match(value) or REGION_CODE_RE.match(value):
                window = text[max(0, match.start() - 250):match.end() + 250]
                confidence = "medium" if SERVER_CONTEXT_RE.search(window) else "low"
                self._add_region(value, confidence, "string-literal", source, loc, match.start(), match.end())
        for regex, kind in ((OBJECT_RE, "object-literal"), (ARRAY_RE, "array-literal")):
            for match in regex.finditer(text):
                body = match.group(0)
                if kind == "object-literal" and REGION_KEY_RE.search(body) and ADDRESS_KEY_RE.search(body):
                    props = {}
                    for prop in PROP_RE.finditer(body):
                        props[prop.group(1)] = prop.group(3) if prop.group(3) is not None else prop.group(4)
                    name = next((props[k] for k in props if k.lower() in (
                        "name", "region", "label", "title", "servername", "server_name", "loc", "location")), None)
                    if name:
                        self._add_region(str(name), "high", kind, source, loc, match.start(), match.end(),
                                         {k: v for k, v in props.items() if v is not None})
                elif kind == "array-literal":
                    literals = [m.group(2) for m in STRING_LITERAL_RE.finditer(body)]
                    names = [s for s in literals if REGION_LITERAL_RE.match(s) or REGION_CODE_RE.match(s)]
                    addresses = [s for s in literals if WS_URL_RE.match(s) or IP_RE.fullmatch(s.split(":")[0] or "")
                                 or (is_same_site(s.split(":")[0].lower(), self.domain) and "." in s)]
                    for name in names:
                        if addresses:
                            self._add_region(name, "high", kind, source, loc, match.start(), match.end(),
                                             {"address": addresses[0]})

    def _looks_like_endpoint(self, value: str) -> bool:
        if " " in value or len(value) < 2 or (ASSET_SUFFIX_RE.search(value) and not value.endswith(".json")):
            return False
        lower = value.lower()
        return (lower.startswith("/") and len(value) > 1 and re.fullmatch(r"/[\w\-./?=&%]+", value) is not None) \
            or ".php" in lower or "/api/" in lower or lower.startswith("api/")

    def _hostname_regions(self) -> None:
        hosts = set()
        for category in ("http_urls", "websocket_urls", "host_ports"):
            for entry in self.findings.category(category, limit=10000):
                host = host_of(entry["value"]) if "://" in entry["value"] else entry["value"].split(":")[0]
                if is_same_site(host, self.domain) and host not in (self.domain, "www." + self.domain):
                    hosts.add((host, entry["occurrences"][0]["file"] if entry["occurrences"] else ""))
        for host, source in sorted(hosts):
            label = host[: -len(self.domain) - 1].split(".")[0]
            if REGION_HOST_LABEL_RE.match(label) or label in ("beta", "sandbox", "test", "dev"):
                entry = self.regions.setdefault(label.lower(), {"name": label, "confidence": "medium",
                                                                "source_kinds": [], "properties": {},
                                                                "evidence": []})
                if "hostname" not in entry["source_kinds"]:
                    entry["source_kinds"].append("hostname")
                entry["properties"].setdefault("host", [])
                if host not in entry["properties"]["host"]:
                    entry["properties"]["host"].append(host)
                if len(entry["evidence"]) < 8:
                    entry["evidence"].append({"file": source, "kind": "hostname", "context": host})

    def _html_options(self, html: str, source: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for select in soup.find_all("select"):
            ident = select.get("id") or select.get("name") or ""
            if not re.search(r"server|region", ident, re.I):
                continue
            for option in select.find_all("option"):
                out.append({"select": ident, "text": option.get_text(" ", strip=True),
                            "value": option.get("value"), "file": source})
        return out

    def run(self, resources: list[dict]) -> dict:
        client, sources = self._sources(resources)
        notes = []
        html_options = []
        scanned = []
        for res in sources:
            path = self.p.paths.abs(res["local_path"])
            if not path.is_file():
                continue
            text = decode_text(path.read_bytes())
            label = res["local_path"]
            scanned.append({"url": res["url"], "local_path": label, "kind": res["kind"], "size": res.get("size"),
                            "is_client_js": client is not None and res["url"] == client["url"]})
            self._scan(text, label)
            decoded = decode_js_escapes(text)
            if decoded is not None:
                self._scan(decoded, f"{label} [escape-decoded]")
                scanned[-1]["escape_decoded_pass"] = True
                notes.append(f"{label} uses hex/unicode-escaped strings (obfuscated); it was also scanned with "
                             f"escapes decoded (findings marked [escape-decoded], offsets refer to that view).")
            if res["kind"] == "html":
                html_options.extend(self._html_options(text, label))
        self._hostname_regions()
        if client is None:
            notes.append("client.js was NOT recovered from the configured archives for the selected date; "
                         "endpoint and region inventories below come only from the other files and are "
                         "incomplete. Supply a verified client.js capture before Phase 2.")
        if html_options and all("finding server" in o["text"].lower() for o in html_options):
            notes.append("The server <select> in index.html only contains a placeholder; the region list is "
                         "populated at runtime (client.js / master server), so regions must come from client.js.")
        client_info = None
        if client is not None:
            client_info = {"url": client["url"], "local_path": client["local_path"], "sha256": client.get("sha256"),
                           "size": client.get("size"), "capture_timestamp": client.get("capture_timestamp"),
                           "provider": client.get("provider")}
        generated = now_iso()
        endpoint_report = {
            "generated_at": generated, "client_js": client_info, "sources_scanned": scanned,
            "websocket_urls": self.findings.category("websocket_urls"),
            "websocket_constructors": self.findings.category("websocket_constructors"),
            "http_urls": self.findings.category("http_urls"),
            "host_ports": self.findings.category("host_ports"),
            "ip_addresses": self.findings.category("ip_addresses"),
            "network_calls": self.findings.category("network_calls"),
            "endpoint_paths": self.findings.category("endpoint_paths"),
            "account_server_references": self.findings.category("account_server_references"),
            "master_server_references": self.findings.category("master_server_references"),
            "notes": notes,
        }
        regions = sorted(self.regions.values(), key=lambda r: (-CONFIDENCE_RANK[r["confidence"]], r["name"].lower()))
        region_report = {
            "generated_at": generated, "client_js": client_info, "sources_scanned": [s["local_path"] for s in scanned],
            "html_server_select_options": html_options, "candidates": regions,
            "confidence_levels": {
                "high": "object/array literal pairing a region name with an address, host or port",
                "medium": "region-like literal near server/region/host/port code, or a region-like subdomain",
                "low": "region-like string literal without server context",
            },
            "notes": notes,
        }
        config_report = {
            "generated_at": generated, "client_js": client_info,
            "sandbox": self.findings.category("sandbox"),
            "developer": self.findings.category("developer"),
            "notes": notes + ["Keyword hits are leads for Phase 4 (tasks 26-27), not confirmed configuration."],
        }
        write_json(self.p.paths.manifest("endpoint_candidates.json"), endpoint_report)
        write_json(self.p.paths.manifest("region_candidates.json"), region_report)
        write_json(self.p.paths.manifest("config_candidates.json"), config_report)
        endpoint_total = sum(len(endpoint_report[k]) for k in (
            "websocket_urls", "http_urls", "host_ports", "ip_addresses", "endpoint_paths"))
        summary = {
            "client_js_found": client is not None, "client_js": client_info, "files_scanned": len(scanned),
            "regions_discovered": len(regions),
            "regions_high_confidence": sum(1 for r in regions if r["confidence"] == "high"),
            "endpoints_discovered": endpoint_total,
            "websocket_urls": len(endpoint_report["websocket_urls"]),
            "account_references": len(endpoint_report["account_server_references"]),
            "master_server_references": len(endpoint_report["master_server_references"]),
            "sandbox_references": len(config_report["sandbox"]),
            "developer_references": len(config_report["developer"]),
            "notes": notes,
        }
        return summary


# =============================================================================
# Pipeline
# =============================================================================


class Pipeline:
    def __init__(self, args):
        self.args = args
        config_path = Path(args.config).resolve() if args.config else SCRIPT_DIR / "config.json"
        self.config, config_found = load_config(config_path)
        apply_cli_overrides(self.config, args)
        errors = validate_config(self.config)
        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))
        self.config_path = config_path
        self.domain = self.config["domain"].lower().strip()
        root = Path(args.output).resolve() if args.output else (config_path.parent if config_found else SCRIPT_DIR)
        self.paths = ProjectPaths(root, self.config)

        # Stage 1 -- initialization
        self.paths.ensure()
        self.log, self.vlog = setup_logging(self.paths.logs, args.verbose)
        self.target = TargetWindow(date.fromisoformat(str(self.config["snapshot_date"])),
                                   str(self.config["snapshot_timezone"]))
        self.db = Database(self.paths.database)
        if args.refresh_cache:
            self.db.clear_cache()
        self.http = HttpClient(self.config, self.log)
        self.providers: dict[str, ArchiveProvider] = {}
        for cfg in self.config["providers"]:
            if cfg.get("enabled", True):
                self.providers[cfg["name"]] = PROVIDER_TYPES[cfg["type"]](cfg, self.http, self.db, self.log,
                                                                          self.domain)
        self.captures: list[Capture] = []
        self.anchor: Capture | None = None
        self.selection: dict | None = None
        self.download_stats: dict | None = None
        self.analysis: dict | None = None
        self.validation: dict | None = None
        self.run_id: int | None = self.db.get_meta("last_download_run")

        self.log.info("Mope.io archive fetcher %s", SCRIPT_VERSION)
        self.log.info("Config: %s%s", config_path, "" if config_found else " (not found; using defaults)")
        self.log.info("Project root: %s", self.paths.root)
        self.log.info("Target date: %s (%s) -> UTC %s .. %s", self.target.day, self.target.tz_name,
                      self.target.start.isoformat(), self.target.end.isoformat())
        self.log.info("Archive sources: %s  (Internet Archive / Wayback Machine excluded by design)",
                      ", ".join(f"{p.label}{'' if p.can_replay else ' [discovery only]'}"
                                for p in self.providers.values()))
        self.log.debug("Python %s, requests %s, sqlite %s", sys.version.split()[0],
                       getattr(requests, "__version__", "?"), sqlite3.sqlite_version)

    # -- shared helpers -----------------------------------------------------
    def discovery_window(self) -> tuple[datetime, datetime]:
        days = float(self.config["discovery_window_days"])
        return self.target.start - timedelta(days=days), self.target.end + timedelta(days=days)

    def lookup_window(self, anchor_dt: datetime) -> tuple[datetime, datetime]:
        lo, hi = self.discovery_window()
        if self.config["allow_off_date"]:
            distance = timedelta(days=float(self.config["max_date_distance_days"]))
            lo, hi = min(lo, anchor_dt - distance), max(hi, anchor_dt + distance)
        return lo, hi

    def is_homepage(self, url: str) -> bool:
        host = host_of(url)
        if host not in (self.domain, "www." + self.domain):
            return False
        path = urlsplit(url).path or "/"
        return path in self.config["homepage_paths"]

    def homepage_captures(self) -> list[Capture]:
        return sorted((c for c in self.captures if self.is_homepage(c.url) and c.fetchable
                       and ("html" in c.mime or c.revisit or not c.mime)), key=lambda c: c.timestamp)

    def ensure_captures(self) -> None:
        if self.captures:
            return
        discovery = self.db.get_meta("discovery") or {}
        if discovery and (discovery.get("domain") != self.domain
                          or discovery.get("target_date") != self.target.day.isoformat()):
            self.log.info("Stored discovery was for %s on %s; discovering again.",
                          discovery.get("domain"), discovery.get("target_date"))
            self.stage_discover()
            return
        self.captures = self.db.load_captures()
        if not self.captures:
            self.log.info("No capture manifest yet; running discovery first.")
            self.stage_discover()
            return
        window = discovery.get("window")
        if window:
            start, end = datetime.fromisoformat(window[0]), datetime.fromisoformat(window[1])
            for info in discovery.get("providers", []):
                provider = self.providers.get(info.get("name"))
                if provider is not None and info.get("complete"):
                    provider.mark_covered(self.domain, start, end)
        for provider in self.providers.values():
            provider.register_known(self.captures)

    def priorities(self) -> dict:
        return {name: p.priority for name, p in self.providers.items()}

    def make_policy(self, anchor: Capture) -> SelectionPolicy:
        return SelectionPolicy(
            anchor_dt=anchor.dt, target=self.target,
            same_day_fallback=bool(self.config["same_day_fallback"]),
            allow_off_date=bool(self.config["allow_off_date"]),
            max_distance=timedelta(days=float(self.config["max_date_distance_days"])),
            priorities=self.priorities(),
            replayable={n for n, p in self.providers.items() if p.can_replay},
        )

    # -- Stage 2: discovery -------------------------------------------------
    def stage_discover(self) -> None:
        self.log.info("Stage 2 - Snapshot discovery")
        start, end = self.discovery_window()
        captures: dict[str, Capture] = {}
        provider_info = []
        for provider in self.providers.values():
            info = provider.describe() | {"queried": True, "error": None, "captures": 0, "on_target_date": 0}
            self.log.info("  querying %s for %s and subdomains (%s .. %s)", provider.label, self.domain,
                          start.date(), end.date())
            try:
                found = provider.discover(self.domain, start, end)
            except (FetchError, ValueError) as exc:
                info["error"] = str(exc)
                self.log.warning("  %s discovery failed: %s", provider.label, exc)
                provider_info.append(info)
                continue
            if not self.config["discover_subdomains"]:
                found = [c for c in found if host_of(c.url) in (self.domain, "www." + self.domain)]
            for cap in found:
                captures[cap.ident] = cap
            info["captures"] = len(found)
            info["on_target_date"] = sum(1 for c in found if self.target.contains(c.dt))
            info["complete"] = provider.covers(f"https://{self.domain}/", start, end)
            provider_info.append(info)
            self.log.info("  %s: %d captures in window, %d on %s", provider.label, len(found),
                          info["on_target_date"], self.target.day)
        if all(info["error"] for info in provider_info):
            raise FetcherError("Discovery failed for every archive source; check network access and logs/errors.log")
        self.captures = sorted(captures.values(), key=lambda c: (c.timestamp, c.url))
        self.db.replace_captures(self.captures, self.target)
        self.db.set_meta("discovery", {"at": now_iso(), "domain": self.domain,
                                       "target_date": self.target.day.isoformat(),
                                       "window": [start.isoformat(), end.isoformat()],
                                       "providers": provider_info})
        self.write_capture_manifest(provider_info, start, end)

    def write_capture_manifest(self, provider_info: list, start: datetime, end: datetime) -> None:
        homepage = self.homepage_captures()
        on_date_home = [c for c in homepage if self.target.contains(c.dt)]
        before = [c for c in homepage if c.dt < self.target.start]
        after = [c for c in homepage if c.dt >= self.target.end]
        hosts = defaultdict(int)
        for cap in self.captures:
            hosts[host_of(cap.url)] += 1

        def brief(cap):
            return None if cap is None else {"provider": cap.provider, "timestamp": cap.timestamp,
                                             "iso": ts_iso(cap.timestamp), "url": cap.url,
                                             "deviation_days": self.target.signed_deviation_days(cap.dt)}

        manifest = {
            "generated_at": now_iso(), "script_version": SCRIPT_VERSION, "domain": self.domain,
            "target": self.target.describe(),
            "discovery_window": {"from": start.isoformat(), "to": end.isoformat(),
                                 "days_each_side": self.config["discovery_window_days"]},
            "excluded_archives": list(EXCLUDED_ARCHIVE_HOSTS),
            "providers": provider_info,
            "summary": {
                "total_captures": len(self.captures),
                "on_target_date": sum(1 for c in self.captures if self.target.contains(c.dt)),
                "distinct_urls": len({match_key(c.url) for c in self.captures}),
                "hosts": dict(sorted(hosts.items(), key=lambda kv: -kv[1])),
                "homepage_captures_in_window": len(homepage),
                "homepage_captures_on_target_date": len(on_date_home),
                "nearest_homepage_before_target": brief(before[-1] if before else None),
                "nearest_homepage_after_target": brief(after[0] if after else None),
            },
            "captures": [c.to_dict() | {"iso": ts_iso(c.timestamp), "on_target_date": self.target.contains(c.dt),
                                        "deviation_days": self.target.signed_deviation_days(c.dt)}
                         for c in self.captures],
        }
        write_json(self.paths.manifest("capture_manifest.json"), manifest)
        self.log.info("  saved %s (%d captures)", self.paths.rel(self.paths.manifest("capture_manifest.json")),
                      len(self.captures))

    # -- Stage 3: listing and selection -------------------------------------
    def stage_list_captures(self) -> None:
        self.ensure_captures()
        homepage = self.homepage_captures()
        print()
        print(f"Target date : {self.target.day} ({self.target.tz_name})")
        print(f"Domain      : {self.domain}")
        print("Sources     : " + ", ".join(f"{p.label}{'' if p.can_replay else ' [discovery only]'}"
                                           for p in self.providers.values()))
        print("Excluded    : Internet Archive / Wayback Machine (by design)")
        print()
        print(f"{'provider':<14}{'captures':>10}{'on ' + str(self.target.day):>16}{'homepage on date':>18}")
        for name, provider in self.providers.items():
            caps = [c for c in self.captures if c.provider == name]
            on = sum(1 for c in caps if self.target.contains(c.dt))
            home_on = sum(1 for c in homepage if c.provider == name and self.target.contains(c.dt))
            print(f"{name:<14}{len(caps):>10}{on:>16}{home_on:>18}")
        print()
        if not homepage:
            print("No homepage captures were found in the discovery window.")
            return
        print(f"Homepage captures within +/-{self.config['discovery_window_days']} days:")
        print(f"  {'timestamp':<16}{'UTC':<21}{'provider':<13}{'status':<8}{'vs target':>11}  replay")
        for cap in homepage:
            dev = self.target.signed_deviation_days(cap.dt)
            marker = "  ON DATE" if self.target.contains(cap.dt) else f"{dev:+9.1f} d"
            replay = "yes" if self.providers[cap.provider].can_replay else "no"
            status = cap.status + ("*" if cap.revisit else "")
            print(f"  {cap.timestamp:<16}{ts_human(cap.timestamp):<21}{cap.provider:<13}{status:<8}{marker:>11}  {replay}")
        if any(c.revisit for c in homepage):
            print("  (* revisit record: content identical to an earlier capture)")
        on_date = [c for c in homepage if self.target.contains(c.dt)]
        if not on_date:
            print()
            print(f"No homepage capture exists on {self.target.day} in the configured archives.")
            print("To build the archive from a different capture, choose it explicitly, e.g.:")
            replayable = [c for c in homepage if self.providers[c.provider].can_replay]
            options = []
            for pool in (replayable, [c for c in replayable if not c.revisit]):
                if pool:
                    best = min(pool, key=lambda c: abs(self.target.signed_deviation_days(c.dt)))
                    if best not in options:
                        options.append(best)
            for cap in options:
                kind = "revisit" if cap.revisit else "full capture"
                print(f"  python fetch_mope_archive.py --timestamp {cap.timestamp} --allow-off-date --all"
                      f"   # {cap.provider}, {kind}, {self.target.signed_deviation_days(cap.dt):+.1f} days")
        print()

    def select_anchor(self) -> Capture:
        self.ensure_captures()
        explicit = str(self.config.get("anchor_timestamp") or "").strip()
        allow_off = bool(self.config["allow_off_date"])
        stored = self.db.get_meta("selection")
        homepage = self.homepage_captures()
        replayable = [c for c in homepage if self.providers.get(c.provider) and self.providers[c.provider].can_replay]

        if not explicit and stored and not self.args.select:
            match = [c for c in replayable if c.timestamp == stored["anchor_timestamp"]
                     and c.provider == stored["provider"]]
            if match and (self.target.contains(match[0].dt) or allow_off):
                return self._accept_anchor(match[0], explicit=stored.get("explicit", False), reused=True)

        if explicit:
            matches = [c for c in replayable if c.timestamp.startswith(explicit)]
            if not matches:
                day = TargetWindow.for_datetime(parse_ts(explicit), "UTC")
                for provider in self.providers.values():
                    if not provider.can_replay:
                        continue
                    try:
                        found = provider.cached_lookup(f"https://{self.domain}/", day.start, day.end)
                    except FetchError as exc:
                        self.log.debug("anchor lookup failed on %s: %s", provider.name, exc)
                        continue
                    matches += [c for c in found if c.timestamp.startswith(explicit) and c.fetchable]
            if not matches:
                raise SelectionError(f"No replayable homepage capture matches timestamp {explicit}. "
                                     f"Run --list-captures to see the available ones.")
            matches.sort(key=lambda c: (c.revisit, self.providers[c.provider].priority, -int(c.timestamp)))
            anchor = matches[0]
            if not self.target.contains(anchor.dt) and not allow_off:
                dev = self.target.signed_deviation_days(anchor.dt)
                raise SelectionError(
                    f"Capture {anchor.timestamp} is {dev:+.1f} days from the target date {self.target.day}. "
                    f"Off-date anchors must be requested explicitly with --allow-off-date.")
            return self._accept_anchor(anchor, explicit=True)

        on_date = [c for c in replayable if self.target.contains(c.dt)]
        if on_date:
            on_date.sort(key=lambda c: (c.revisit, self.providers[c.provider].priority, -int(c.timestamp)))
            return self._accept_anchor(on_date[0], explicit=False)

        if allow_off:
            limit = timedelta(days=float(self.config["max_date_distance_days"]))
            mid = self.target.start + (self.target.end - self.target.start) / 2
            near = [c for c in replayable if abs(c.dt - mid) <= limit]
            if near:
                near.sort(key=lambda c: (abs(self.target.signed_deviation_days(c.dt)), c.revisit,
                                         self.providers[c.provider].priority))
                return self._accept_anchor(near[0], explicit=False)

        lines = [f"No replayable homepage capture of {self.domain} exists on {self.target.day} "
                 f"in: {', '.join(p.label for p in self.providers.values())}."]
        before = [c for c in homepage if c.dt < self.target.start]
        after = [c for c in homepage if c.dt >= self.target.end]
        for label, cap in (("Nearest before", before[-1] if before else None),
                           ("Nearest after", after[0] if after else None)):
            if cap is not None:
                lines.append(f"  {label}: {cap.timestamp} ({ts_human(cap.timestamp)} UTC) on {cap.provider}, "
                             f"{self.target.signed_deviation_days(cap.dt):+.1f} days"
                             f"{'' if self.providers[cap.provider].can_replay else ' [discovery only]'}")
        lines.append("The target date is never substituted silently. To use another capture, pass it explicitly:")
        lines.append("  --timestamp <YYYYMMDDhhmmss> --allow-off-date   (see --list-captures)")
        raise SelectionError("\n".join(lines))

    def _accept_anchor(self, anchor: Capture, explicit: bool, reused: bool = False) -> Capture:
        on_date = self.target.contains(anchor.dt)
        deviation = self.target.signed_deviation_days(anchor.dt)
        provider = self.providers[anchor.provider]
        selection = {
            "anchor_timestamp": anchor.timestamp, "anchor_iso": ts_iso(anchor.timestamp),
            "provider": anchor.provider, "provider_label": provider.label, "url": anchor.url,
            "status": anchor.status, "mime": anchor.mime, "revisit": anchor.revisit,
            "archive_link": provider.human_link(anchor), "target": self.target.describe(),
            "on_target_date": on_date, "deviation_days": deviation, "explicit": explicit,
            "selected_at": now_iso(), "policy": self.make_policy(anchor).describe(),
            "homepage_captures_on_target_date": sum(1 for c in self.homepage_captures()
                                                    if self.target.contains(c.dt)),
        }
        previous = self.db.get_meta("selection")
        if previous and previous.get("anchor_timestamp") == anchor.timestamp:
            selection["selected_at"] = previous.get("selected_at", selection["selected_at"])
        self.db.set_meta("selection", selection)
        write_json(self.paths.manifest("selection.json"), selection)
        self.anchor, self.selection = anchor, selection
        verb = "Reusing" if reused else "Selected"
        self.log.info("%s anchor: %s (%s UTC) from %s", verb, anchor.timestamp, ts_human(anchor.timestamp),
                      provider.label)
        if not on_date:
            self.log.warning("ANCHOR IS NOT FROM %s: it is %+.1f days away. Every report records this "
                             "deviation; the historical target date is unchanged.", self.target.day, deviation)
        return anchor

    def stage_select(self) -> None:
        self.log.info("Stage 3 - Snapshot selection")
        self.select_anchor()

    # -- Stages 4 & 5: resource discovery and download ------------------------
    def stage_download(self, resume: bool) -> None:
        self.log.info("Stage 4/5 - Resource discovery and download%s", " (resume)" if resume else "")
        if self.anchor is None:
            self.select_anchor()
        anchor = self.anchor
        previous_anchor = self.db.get_meta("download_anchor")
        if previous_anchor and previous_anchor != anchor.timestamp:
            self.supersede_original(previous_anchor, anchor.timestamp)
            resume = False
        policy = self.make_policy(anchor)
        run_id = self.db.start_run("download", anchor.timestamp)
        crawler = Crawler(self, anchor, policy, resume=resume, run_id=run_id)
        stats = crawler.run()
        stats["external_links"] = len(crawler.external_links)
        stats["skipped_archive_refs"] = len(crawler.skipped_archive_refs)
        self.db.finish_run(run_id, stats)
        self.db.set_meta("download_anchor", anchor.timestamp)
        self.db.set_meta("last_download_run", run_id)
        self.db.set_meta("external_links", sorted(crawler.external_links))
        self.run_id = run_id
        self.download_stats = stats
        self.write_download_manifests()
        self.log.info("  queued %d, downloaded %d, reused %d, failed %d, not archived %d, blocked by date "
                      "policy %d", stats.get("queued", 0), stats.get("downloaded", 0), stats.get("reused", 0),
                      stats.get("failed", 0), stats.get("not_archived", 0), stats.get("off_date_blocked", 0))

    def supersede_original(self, old_anchor: str, new_anchor: str) -> None:
        """Move the previous anchor's files aside instead of mixing two snapshots."""
        original = self.paths.original
        if original.exists() and any(original.iterdir()):
            stamp = datetime.now(UTC).strftime(TS_FORMAT)
            dest = self.paths.archive / "superseded" / f"original-{old_anchor}-{stamp}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(dest))
            original.mkdir(parents=True, exist_ok=True)
            baseline = self.paths.manifest("baseline.json")
            if baseline.exists():
                shutil.move(str(baseline), str(dest / "_baseline.json"))
            self.log.warning("Anchor changed (%s -> %s): previous files moved to %s", old_anchor, new_anchor,
                             self.paths.rel(dest))
        else:
            self.log.info("Anchor changed (%s -> %s); download records reset.", old_anchor, new_anchor)
        self.db.clear_resources()

    def current_resources(self) -> list[dict]:
        if self.run_id is None:
            return []
        return self.db.resources(self.run_id)

    @staticmethod
    def duplicate_groups(resources: list[dict]) -> dict[str, list[str]]:
        groups = defaultdict(list)
        for res in resources:
            if res["status"] == "ok" and res.get("sha256"):
                groups[res["sha256"]].append(res["url"])
        return {sha: urls for sha, urls in groups.items() if len(urls) > 1}

    def write_download_manifests(self) -> None:
        resources = self.current_resources()
        duplicates = self.duplicate_groups(resources)
        duplicate_of = {}
        for urls in duplicates.values():
            for url in sorted(urls)[1:]:
                duplicate_of[url] = sorted(urls)[0]

        essential_keys = {match_key(f"https://{self.domain}{p}", keep_query=False)
                          for p in self.config["essential_paths"]}

        def row(res):
            nearest = json.loads(res["nearest_alternative"]) if res.get("nearest_alternative") else None
            return {
                "url": res["url"], "status": res["status"], "kind": res["kind"], "relation": res["relation"],
                "essential": match_key(res["url"], keep_query=False) in essential_keys,
                "discovered_from": res["discovered_from"], "depth": res["depth"],
                "external": bool(res["external"]), "speculative": bool(res["speculative"]),
                "optional": bool(res["optional"]), "local_path": res.get("local_path"),
                "provider": res.get("provider"), "capture_timestamp": res.get("capture_timestamp"),
                "capture_iso": ts_iso(res["capture_timestamp"]) if res.get("capture_timestamp") else None,
                "capture_url": res.get("capture_url"), "selection_tier": res.get("selection_tier"),
                "deviation_from_target_days": res.get("deviation_days"),
                "seconds_from_anchor": res.get("distance_seconds"),
                "query_string_differs": bool(res.get("query_mismatch")), "mime": res.get("mime"),
                "size": res.get("size"), "sha256": res.get("sha256"),
                "duplicate_of": duplicate_of.get(res["url"]), "attempts": res.get("attempts"),
                "notes": res.get("notes"), "error": res.get("error"), "nearest_alternative": nearest,
            }

        rows = [row(r) for r in resources]
        ok = [r for r in rows if r["status"] == "ok"]
        totals = {
            "discovered": len(rows), "downloaded": len(ok),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "not_archived": sum(1 for r in rows if r["status"] == "not_archived"),
            "off_date_blocked": sum(1 for r in rows if r["status"] == "off_date_blocked"),
            "skipped_external": sum(1 for r in rows if r["status"] == "skipped_external"),
            "duplicates": sum(len(v) - 1 for v in duplicates.values()),
            "bytes": sum(r["size"] or 0 for r in ok),
            "off_target_date": sum(1 for r in ok if r["deviation_from_target_days"]),
        }
        write_json(self.paths.manifest("download_manifest.json"), {
            "generated_at": now_iso(), "run_id": self.run_id, "selection": self.db.get_meta("selection"),
            "output_directory": self.paths.rel(self.paths.original), "totals": totals,
            "duplicate_groups": duplicates, "resources": rows,
        })
        missing = [r for r in rows if r["status"] != "ok"]
        write_json(self.paths.manifest("missing_resources.json"), {
            "generated_at": now_iso(), "run_id": self.run_id,
            "explanation": {
                "not_archived": "no capture of this URL exists in the configured archives (within the lookup window)",
                "off_date_blocked": "captures exist, but none is allowed by the date policy (see nearest_alternative)",
                "failed": "allowed captures exist but every download attempt failed (see error)",
                "skipped_external": "external dependency not fetched because discover_external_assets is off",
            },
            "required": [r for r in missing if r["essential"] or
                         (not r["speculative"] and not r["optional"] and not r["external"])],
            "external_dependencies": [r for r in missing if r["external"] and not r["speculative"]],
            "optional_pages": [r for r in missing if r["optional"] and not r["essential"]],
            "speculative": [r for r in missing if r["speculative"] and not r["essential"]],
            "external_links_not_followed": self.db.get_meta("external_links", []),
        })

    # -- Stage 6: analysis --------------------------------------------------
    def stage_analyze(self) -> None:
        self.log.info("Stage 6 - Client analysis")
        resources = self.current_resources()
        if not any(r["status"] == "ok" for r in resources):
            raise FetcherError("Nothing has been downloaded yet; run --download first.")
        self.analysis = ClientAnalyzer(self).run(resources)
        self.db.set_meta("analysis", self.analysis)
        if self.analysis["client_js_found"]:
            self.log.info("  client.js: %s", self.analysis["client_js"]["local_path"])
        else:
            self.log.warning("  client.js was not recovered; analysis covers %d other file(s) only",
                             self.analysis["files_scanned"])
        self.log.info("  region candidates: %d (high confidence %d); endpoint candidates: %d",
                      self.analysis["regions_discovered"], self.analysis["regions_high_confidence"],
                      self.analysis["endpoints_discovered"])

    # -- Stage 7: validation ------------------------------------------------
    def stage_validate(self) -> dict:
        self.log.info("Stage 7 - Validation")
        vlog = self.vlog
        resources = self.current_resources()
        if not resources:
            raise FetcherError("No download records; run --download first.")
        by_key = {match_key(r["url"]): r for r in resources}
        by_path = defaultdict(list)
        for res in resources:
            by_path[match_key(res["url"], keep_query=False)].append(res)
        selection = self.db.get_meta("selection") or {}
        home_url = normalize_url(selection.get("url", f"https://{self.domain}/"))

        # 1. essential client files
        essential_paths = list(dict.fromkeys(self.config["essential_paths"]))
        for res in resources:
            if (res["discovered_from"] == home_url and res["relation"] in ("script", "stylesheet")
                    and not res["external"] and not res["speculative"]):
                path = urlsplit(res["url"]).path or "/"
                if path not in essential_paths:
                    essential_paths.append(path)
        essential = []
        for path in essential_paths:
            key = match_key(f"https://{self.domain}{path}", keep_query=False)
            matches = [r for r in by_path.get(key, []) if r["status"] == "ok"] or by_path.get(key, [])
            res = matches[0] if matches else None
            present = bool(res and res["status"] == "ok" and self.paths.abs(res["local_path"]).is_file())
            essential.append({"path": path, "present": present, "url": res["url"] if res else None,
                              "status": res["status"] if res else "not_discovered",
                              "local_path": res.get("local_path") if res else None})
            (vlog.info if present else vlog.error)("essential %-28s %s", path, "OK" if present else
                                                   f"MISSING ({res['status'] if res else 'not discovered'})")

        # 2. integrity (existence, size, SHA-256)
        integrity_failures = []
        checked = 0
        for res in resources:
            if res["status"] != "ok":
                continue
            checked += 1
            path = self.paths.abs(res["local_path"])
            problem = None
            if not path.is_file():
                problem = "file missing"
            elif res.get("size") is not None and path.stat().st_size != res["size"]:
                problem = f"size {path.stat().st_size} != recorded {res['size']}"
            elif self.config["calculate_sha256"] and res.get("sha256") and sha256_file(path) != res["sha256"]:
                problem = "sha256 mismatch"
            if problem:
                integrity_failures.append({"url": res["url"], "local_path": res["local_path"], "problem": problem})
                vlog.error("integrity %s: %s", res["local_path"], problem)

        # 3. local references from downloaded HTML/CSS
        unresolved = []
        for res in resources:
            if res["status"] != "ok" or res["kind"] not in ("html", "css"):
                continue
            path = self.paths.abs(res["local_path"])
            if not path.is_file():
                continue
            refs = extract_refs(res["url"], res["kind"], path.read_bytes(), home_url)
            for ref in refs:
                if ref.speculative or ref.relation == "hyperlink" or not is_same_site(host_of(ref.url), self.domain):
                    continue
                target = by_key.get(match_key(ref.url)) or next(iter(by_path.get(
                    match_key(ref.url, keep_query=False), [])), None)
                if target is None or target["status"] != "ok":
                    unresolved.append({"from": res["local_path"], "reference": ref.url, "relation": ref.relation,
                                       "status": target["status"] if target else "not_discovered"})
        for item in unresolved:
            vlog.warning("unresolved local reference %s -> %s (%s)", item["from"], item["reference"], item["status"])

        # 4. missing dependencies
        missing_required = [r["url"] for r in resources if r["status"] != "ok" and not r["speculative"]
                            and not r["optional"] and not r["external"]]
        missing_external = [r["url"] for r in resources if r["status"] != "ok" and r["external"]
                            and not r["speculative"]]

        # 5. historical baseline (original archive must remain unmodified)
        baseline = read_json(self.paths.manifest("baseline.json"))
        modified = []
        if baseline:
            for rel, digest in baseline.get("files", {}).items():
                path = self.paths.abs(rel)
                if not path.is_file():
                    modified.append({"local_path": rel, "problem": "missing"})
                elif sha256_file(path) != digest:
                    modified.append({"local_path": rel, "problem": "modified since baseline"})
            for item in modified:
                vlog.error("baseline %s: %s", item["local_path"], item["problem"])

        essential_ok = all(e["present"] for e in essential)
        valid = essential_ok and not integrity_failures and not modified
        retryable = sum(1 for r in resources if r["status"] == "failed")
        self.validation = {
            "validated_at": now_iso(), "archive_valid": valid, "essential_files_ok": essential_ok,
            "essential_files": essential, "files_checked": checked,
            "integrity_failures": integrity_failures, "unresolved_local_references": unresolved,
            "missing_required_resources": missing_required, "missing_external_dependencies": missing_external,
            "baseline_present": bool(baseline), "baseline_modifications": modified,
            "retryable_failures": retryable,
        }
        self.db.set_meta("validation", self.validation)
        vlog.info("validation result: %s (essential ok=%s, integrity failures=%d, unresolved refs=%d, "
                  "baseline modifications=%d)", "VALID" if valid else "INVALID", essential_ok,
                  len(integrity_failures), len(unresolved), len(modified))
        self.log.info("  archive %s: essential files %s, %d integrity failures, %d unresolved local references",
                      "VALID" if valid else "INVALID", "ok" if essential_ok else "MISSING",
                      len(integrity_failures), len(unresolved))
        return self.validation

    # -- Stage 8: reports ---------------------------------------------------
    def stage_report(self) -> dict:
        self.log.info("Stage 8 - Report generation")
        if self.run_id is not None:
            self.write_download_manifests()
            if self.analysis is None:
                self.analysis = self.db.get_meta("analysis")
                if self.analysis is None and any(r["status"] == "ok" for r in self.current_resources()):
                    self.stage_analyze()
            if self.validation is None:
                self.validation = self.db.get_meta("validation") or self.stage_validate()
        selection = self.db.get_meta("selection")
        discovery = self.db.get_meta("discovery") or {}
        download = read_json(self.paths.manifest("download_manifest.json"), {}) or {}
        totals = download.get("totals", {})
        resources = download.get("resources", [])
        ok = [r for r in resources if r["status"] == "ok"]
        deviations = [abs(r["deviation_from_target_days"] or 0) for r in ok]
        anchor_on_date = bool(selection and selection.get("on_target_date"))
        if not selection:
            statement = "No anchor selected yet."
        elif anchor_on_date and not any(deviations):
            statement = f"Every file comes from a capture dated {self.target.day}."
        else:
            statement = (f"NOT a pure {self.target.day} snapshot: anchor deviation "
                         f"{selection.get('deviation_days', 0):+.1f} days; {sum(1 for d in deviations if d)} file(s) "
                         f"come from other dates (max {max(deviations or [0]):.1f} days).")
        report = {
            "project": self.config["project"], "script_version": SCRIPT_VERSION, "generated_at": now_iso(),
            "domain": self.domain, "target": self.target.describe(),
            "archive_sources": {
                "used": [p.describe() for p in self.providers.values()],
                "excluded": list(EXCLUDED_ARCHIVE_HOSTS),
                "note": "The Internet Archive / Wayback Machine is deliberately not used by this project.",
            },
            "discovery": {
                "at": discovery.get("at"), "window": discovery.get("window"),
                "providers": discovery.get("providers"),
                "total_captures": len(self.captures) or len(self.db.load_captures()),
            },
            "selection": selection,
            "historical_fidelity": {
                "anchor_on_target_date": anchor_on_date,
                "anchor_deviation_days": selection.get("deviation_days") if selection else None,
                "files_from_other_dates": sum(1 for d in deviations if d),
                "max_file_deviation_days": max(deviations or [0]),
                "statement": statement,
            },
            "downloads": totals,
            "client_analysis": self.analysis,
            "validation": self.validation,
            "baseline": read_json(self.paths.manifest("baseline.json"), {}).get("created_at")
            if self.paths.manifest("baseline.json").exists() else None,
            "manifests": {name: self.paths.rel(self.paths.manifest(name)) for name in MANIFEST_FILES
                          + ("config_candidates.json", "selection.json")
                          if self.paths.manifest(name).exists() or name == "archive_report.json"},
            "next_steps": self.next_steps(selection, totals),
        }
        write_json(self.paths.manifest("archive_report.json"), report)
        self.log.info("  wrote %s", self.paths.rel(self.paths.manifest("archive_report.json")))
        return report

    def next_steps(self, selection, totals) -> list[str]:
        steps = []
        if not selection:
            steps.append("Select an anchor capture (see --list-captures).")
        elif not selection.get("on_target_date"):
            steps.append(f"The anchor is not from {self.target.day}. Look for an on-date capture in other "
                         f"non-Wayback archives (add a pywb provider) before treating this as the baseline.")
        if self.analysis and not self.analysis.get("client_js_found"):
            steps.append("client.js is missing: add an archive that holds the 2021 client, or import a verified "
                         "copy, before starting Phase 2 (tasks 8-13).")
        if totals.get("failed"):
            steps.append("Re-run with --resume to retry failed downloads.")
        if self.validation and self.validation.get("archive_valid"):
            steps.append("Proceed to Phase 2: serve working/client locally and reverse-engineer client.js.")
        return steps

    # -- Stage 9: working copy ----------------------------------------------
    def stage_working_copy(self, force: bool) -> bool:
        self.log.info("Stage 9 - Working copy")
        validation = self.validation or self.db.get_meta("validation")
        if validation is None:
            validation = self.stage_validate()
        if not validation.get("archive_valid") and not force:
            self.log.warning("  archive is not valid; working copy NOT created (use --working-copy to force).")
            return False
        resources = [r for r in self.current_resources() if r["status"] == "ok"]
        if not resources:
            raise FetcherError("Nothing has been downloaded yet; run --download before --working-copy.")
        selection = self.db.get_meta("selection") or {}
        files = {}
        for res in resources:
            path = self.paths.abs(res["local_path"])
            if path.is_file():
                files[res["local_path"]] = res.get("sha256") or sha256_file(path)
        baseline = {
            "historical_baseline": True, "created_at": now_iso(), "target": self.target.describe(),
            "anchor": selection, "archive_valid": validation.get("archive_valid"), "forced": force and
            not validation.get("archive_valid"), "file_count": len(files), "files": files,
        }
        write_json(self.paths.manifest("baseline.json"), baseline)
        if self.config["preserve_original_files"]:
            for rel in files:
                path = self.paths.abs(rel)
                path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

        client_dir = self.paths.working / "client"
        if client_dir.exists() and self.args.refresh_working:
            shutil.rmtree(client_dir)
        if client_dir.exists():
            self.log.info("  %s already exists; left untouched (use --refresh-working to recreate).",
                          self.paths.rel(client_dir))
        else:
            shutil.copytree(self.paths.original, client_dir,
                            ignore=shutil.ignore_patterns("*.part-*"), copy_function=shutil.copy)
            for path in client_dir.rglob("*"):
                if path.is_file():
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
            self.log.info("  copied the original archive to %s", self.paths.rel(client_dir))
        readmes = {
            "master-server": "Phase 3 (tasks 14-17, 24): rebuilt master server. Inputs: "
                             "archive/manifests/region_candidates.json and endpoint_candidates.json.",
            "account-server": "Phase 4 (tasks 21-25): rebuilt account server. Inputs: account_server_references "
                              "in archive/manifests/endpoint_candidates.json.",
            "regions": "Phase 3 (task 15): one local gameplay-server configuration per discovered region. "
                       "Inputs: archive/manifests/region_candidates.json.",
            "sandbox": "Phase 4 (task 26): sandbox configuration reconstructed from evidence in "
                       "archive/manifests/config_candidates.json.",
            "developer": "Phase 4 (task 27): developer configuration reconstructed from evidence in "
                         "archive/manifests/config_candidates.json. Keep privileged settings local.",
        }
        for name, text in readmes.items():
            directory = self.paths.working / name
            directory.mkdir(parents=True, exist_ok=True)
            readme = directory / "README.md"
            if not readme.exists():
                readme.write_text(f"# {name}\n\n{text}\n\nCreated by fetch_mope_archive.py {SCRIPT_VERSION}. "
                                  f"Baseline anchor: {selection.get('anchor_timestamp')} "
                                  f"({'on' if selection.get('on_target_date') else 'NOT on'} "
                                  f"{self.target.day}).\n", encoding="utf-8")
        self.log.info("  historical baseline recorded in %s (%d files)",
                      self.paths.rel(self.paths.manifest("baseline.json")), len(files))
        return True

    # -- Stage 10: completion -----------------------------------------------
    def print_summary(self, report: dict) -> None:
        selection = report.get("selection") or {}
        downloads = report.get("downloads") or {}
        analysis = report.get("client_analysis") or {}
        validation = report.get("validation") or {}
        fidelity = report.get("historical_fidelity") or {}
        anchor = selection.get("anchor_timestamp")
        lines = [
            "",
            "=" * 72,
            f" Mope.io archive -- target {self.target.day}",
            "=" * 72,
            f" Selected snapshot      : {anchor or '-'}"
            + (f" ({ts_human(anchor)} UTC, {selection.get('provider_label')})" if anchor else ""),
            f" On target date         : {'yes' if fidelity.get('anchor_on_target_date') else 'NO'}"
            + (f" ({selection.get('deviation_days'):+.1f} days)" if anchor and not
               fidelity.get('anchor_on_target_date') else ""),
            f" Discovered resources   : {downloads.get('discovered', 0)}",
            f" Downloaded resources   : {downloads.get('downloaded', 0)}",
            f" Failed downloads       : {downloads.get('failed', 0)}"
            f"  (not archived {downloads.get('not_archived', 0)}, "
            f"blocked by date policy {downloads.get('off_date_blocked', 0)})",
            f" Duplicate resources    : {downloads.get('duplicates', 0)}",
            f" client.js recovered    : {'yes' if analysis.get('client_js_found') else 'NO'}",
            f" Discovered regions     : {analysis.get('regions_discovered', 0)}"
            f" (high confidence {analysis.get('regions_high_confidence', 0)})",
            f" Discovered endpoints   : {analysis.get('endpoints_discovered', 0)}",
            f" Archive validation     : {'VALID' if validation.get('archive_valid') else 'INVALID'}"
            f" ({validation.get('files_checked', 0)} files checked)",
            f" Fidelity               : {fidelity.get('statement', '-')}",
            f" Output directory       : {self.paths.original}",
            f" Reports                : {self.paths.manifests}",
        ]
        for step in report.get("next_steps", []):
            lines.append(f" Next: {step}")
        lines.append("=" * 72)
        print("\n".join(lines))


# =============================================================================
# CLI
# =============================================================================


def check_dependencies() -> None:
    if sys.version_info < (3, 9):
        raise ConfigError("Python 3.9 or newer is required.")
    if _MISSING_DEPS:
        raise ConfigError(f"Missing Python packages: {', '.join(_MISSING_DEPS)}\n"
                          f"Install them with: pip install -r requirements.txt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch and analyse the April 23, 2021 mope.io client from non-Wayback web archives "
                    "(Arquivo.pt, Common Crawl, vefsafn.is, or any pywb archive you configure).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python fetch_mope_archive.py --discover --list-captures\n"
               "  python fetch_mope_archive.py --all\n"
               "  python fetch_mope_archive.py --timestamp 20210310091713 --allow-off-date --all\n"
               "  python fetch_mope_archive.py --resume --validate --report\n")
    stages = parser.add_argument_group("stages (run in pipeline order)")
    stages.add_argument("--discover", action="store_true", help="discover captures in the configured archives")
    stages.add_argument("--list-captures", action="store_true", help="display discovered homepage timestamps")
    stages.add_argument("--select", action="store_true", help="(re)select the anchor homepage capture")
    stages.add_argument("--download", action="store_true", help="discover resources and download them")
    stages.add_argument("--resume", action="store_true", help="resume an interrupted download, retrying failures")
    stages.add_argument("--analyze", action="store_true", help="analyse client.js and other client files")
    stages.add_argument("--validate", action="store_true", help="validate the downloaded archive")
    stages.add_argument("--report", action="store_true", help="(re)generate all JSON reports")
    stages.add_argument("--working-copy", action="store_true",
                        help="create working/ and the historical baseline (forces it even if validation fails)")
    stages.add_argument("--all", action="store_true", help="run the entire workflow")

    opts = parser.add_argument_group("options")
    opts.add_argument("--config", help="path to config.json (default: next to this script)")
    opts.add_argument("--output", help="project root directory (default: the config file's directory)")
    opts.add_argument("--date", help="target snapshot date YYYY-MM-DD (default from config: 2021-04-23)")
    opts.add_argument("--domain", help="domain to archive (default from config: mope.io)")
    opts.add_argument("--timezone", help="timezone that defines the target day (default UTC)")
    opts.add_argument("--timestamp", help="explicit anchor capture timestamp (YYYYMMDDhhmmss)")
    opts.add_argument("--workers", type=int, help="concurrent download workers")
    opts.add_argument("--provider", action="append",
                      help="restrict to these providers (repeatable or comma-separated)")
    opts.add_argument("--allow-off-date", action="store_true",
                      help="explicitly allow captures from other dates (always reported)")
    opts.add_argument("--max-date-distance", type=float,
                      help="with --allow-off-date: max days between a capture and the anchor")
    opts.add_argument("--no-same-day-fallback", action="store_true",
                      help="only accept same-day captures at or before the anchor")
    opts.add_argument("--no-external", action="store_true", help="do not fetch external-host dependencies")
    opts.add_argument("--refresh-cache", action="store_true", help="clear cached archive index lookups")
    opts.add_argument("--refresh-working", action="store_true", help="recreate working/client from the archive")
    opts.add_argument("-v", "--verbose", action="store_true", help="verbose console logging")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stage_flags = ("discover", "list_captures", "select", "download", "resume", "analyze", "validate", "report",
                   "working_copy", "all")
    if not any(getattr(args, flag) for flag in stage_flags):
        parser.print_help()
        return 0
    pipeline = None
    try:
        check_dependencies()
        pipeline = Pipeline(args)
        if args.all:
            pipeline.stage_discover()
            pipeline.stage_list_captures()
            pipeline.stage_select()
            pipeline.stage_download(resume=bool(pipeline.config["resume_downloads"]) or args.resume)
            pipeline.stage_analyze()
            validation = pipeline.stage_validate()
            if not validation["archive_valid"] and validation["retryable_failures"]:
                pipeline.log.info("Archive invalid with %d retryable failure(s); retrying once.",
                                  validation["retryable_failures"])
                pipeline.stage_download(resume=True)
                pipeline.stage_analyze()
                pipeline.stage_validate()
            report = pipeline.stage_report()
            if pipeline.stage_working_copy(force=args.working_copy):
                report = pipeline.stage_report()
            pipeline.print_summary(report)
            return 0
        if args.discover:
            pipeline.stage_discover()
        if args.list_captures:
            pipeline.stage_list_captures()
        if args.select:
            pipeline.stage_select()
        if args.download or args.resume:
            pipeline.stage_download(resume=args.resume or bool(pipeline.config["resume_downloads"]))
        if args.analyze:
            pipeline.stage_analyze()
        if args.validate:
            pipeline.stage_validate()
        if args.report:
            report = pipeline.stage_report()
            pipeline.print_summary(report)
        if args.working_copy:
            pipeline.stage_working_copy(force=True)
            pipeline.stage_report()
        return 0
    except FetcherError as exc:
        message = str(exc)
        if pipeline is not None:
            pipeline.log.error(message)
        else:
            print(f"error: {message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; continue with --resume.", file=sys.stderr)
        return 130
    finally:
        if pipeline is not None:
            pipeline.db.close()


if __name__ == "__main__":
    sys.exit(main())
