"""Offline tests for fetch_mope_archive.py (no network access needed).

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fetch_mope_archive as fm  # noqa: E402

UTC = timezone.utc
LOG = logging.getLogger("test")


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class FakeHttp:
    """Serves byte ranges and canned responses from memory."""

    timeout = 30

    def __init__(self, files=None, routes=None):
        self.files = files or {}
        self.routes = routes or {}
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        data = self.files.get(url)
        if data is None:
            return FakeResponse(404)
        rng = (headers or {}).get("Range")
        if rng:
            start, end = (int(x) for x in rng.split("=")[1].split("-"))
            return FakeResponse(206, data[start:end + 1])
        return FakeResponse(200, data)

    def head(self, url, headers=None, **kwargs):
        data = self.files.get(url)
        if data is None:
            return FakeResponse(404)
        return FakeResponse(200, b"", {"Content-Length": str(len(data))})


class FakeDb:
    def __init__(self):
        self.cache = {}

    def cache_get(self, key):
        return self.cache.get(key)

    def cache_put(self, key, value):
        self.cache[key] = value


class HelperTests(unittest.TestCase):
    def test_parse_partial_timestamp(self):
        self.assertEqual(fm.parse_ts("20210423"), datetime(2021, 4, 23, tzinfo=UTC))
        self.assertEqual(fm.fmt_ts(fm.parse_ts("20210423123456")), "20210423123456")

    def test_match_key_ignores_scheme_and_www(self):
        self.assertEqual(fm.match_key("http://www.mope.io/a.js?x=1"), fm.match_key("https://mope.io/a.js?x=1"))
        self.assertEqual(fm.match_key("https://mope.io/a.js?x=1", keep_query=False), "mope.io/a.js")

    def test_surt(self):
        self.assertEqual(fm.surt_host("www.mope.io"), "io,mope")
        self.assertEqual(fm.surt_url("https://beta.mope.io/Client.js?b=2&a=1"), "io,mope,beta)/client.js?a=1&b=2")

    def test_excluded_archives(self):
        self.assertTrue(fm.is_excluded_archive("web.archive.org"))
        self.assertFalse(fm.is_excluded_archive("arquivo.pt"))

    def test_config_rejects_wayback(self):
        config = json.loads(json.dumps(fm.DEFAULT_CONFIG))
        config["providers"].append({"name": "ia", "type": "pywb", "cdx_url": "https://web.archive.org/cdx/search/cdx",
                                    "replay_url": "https://web.archive.org/web"})
        errors = fm.validate_config(config)
        self.assertTrue(any("Wayback" in e for e in errors), errors)

    def test_default_config_is_valid(self):
        self.assertEqual(fm.validate_config(json.loads(json.dumps(fm.DEFAULT_CONFIG))), [])

    def test_target_window_timezone(self):
        window = fm.TargetWindow(date(2021, 4, 23), "America/New_York")
        self.assertEqual(window.start, datetime(2021, 4, 23, 4, tzinfo=UTC))
        self.assertTrue(window.contains(datetime(2021, 4, 24, 3, 59, tzinfo=UTC)))
        self.assertFalse(window.contains(datetime(2021, 4, 24, 4, tzinfo=UTC)))

    def test_signed_deviation(self):
        window = fm.TargetWindow(date(2021, 4, 23))
        self.assertEqual(window.signed_deviation_days(datetime(2021, 4, 23, 12, tzinfo=UTC)), 0.0)
        self.assertAlmostEqual(window.signed_deviation_days(datetime(2021, 4, 22, tzinfo=UTC)), -1.0)
        self.assertAlmostEqual(window.signed_deviation_days(datetime(2021, 4, 25, tzinfo=UTC)), 1.0)
        self.assertGreater(window.signed_deviation_days(datetime(2021, 4, 24, tzinfo=UTC)), 0.0)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.target = fm.TargetWindow(date(2021, 4, 23))
        self.anchor = datetime(2021, 4, 23, 12, tzinfo=UTC)

    def policy(self, **kw):
        defaults = dict(anchor_dt=self.anchor, target=self.target, same_day_fallback=True, allow_off_date=False,
                        max_distance=timedelta(days=30), priorities={"a": 1, "b": 2}, replayable={"a", "b"})
        defaults.update(kw)
        return fm.SelectionPolicy(**defaults)

    def cap(self, ts, provider="a", status="200", url="https://mope.io/x.js"):
        return fm.Capture(provider=provider, url=url, timestamp=ts, status=status)

    def test_tier_order(self):
        caps = [self.cap("20210423180000"), self.cap("20210423060000"), self.cap("20210422120000")]
        result = self.policy().candidates(caps, "https://mope.io/x.js")
        self.assertEqual([c.capture.timestamp for c in result], ["20210423060000", "20210423180000"])
        self.assertEqual(result[0].tier, "same_day_at_or_before_anchor")

    def test_strict_blocks_off_date(self):
        policy = self.policy()
        caps = [self.cap("20210310000000")]
        self.assertEqual(policy.candidates(caps, "https://mope.io/x.js"), [])
        self.assertEqual(policy.nearest(caps).timestamp, "20210310000000")

    def test_no_same_day_fallback(self):
        caps = [self.cap("20210423180000")]
        self.assertEqual(self.policy(same_day_fallback=False).candidates(caps, "https://mope.io/x.js"), [])

    def test_off_date_needs_opt_in_and_distance(self):
        caps = [self.cap("20210410000000"), self.cap("20210101000000")]
        result = self.policy(allow_off_date=True).candidates(caps, "https://mope.io/x.js")
        self.assertEqual([c.capture.timestamp for c in result], ["20210410000000"])
        self.assertEqual(result[0].tier, "off_date_nearest")

    def test_skips_non_replay_and_non_200(self):
        caps = [self.cap("20210423100000", provider="c"), self.cap("20210423100000", status="301")]
        self.assertEqual(self.policy().candidates(caps, "https://mope.io/x.js"), [])

    def test_exact_query_preferred(self):
        caps = [self.cap("20210423110000", url="https://mope.io/x.js?v=1"),
                self.cap("20210423100000", url="https://mope.io/x.js?v=2")]
        result = self.policy().candidates(caps, "https://mope.io/x.js?v=2")
        self.assertEqual(result[0].capture.url, "https://mope.io/x.js?v=2")


class ExtractionTests(unittest.TestCase):
    HTML = """<html><head><link rel="stylesheet" href="main.css?v=live.257"><link rel="icon" href="favicon.ico">
    <script src="jquery_3_3_1.js"></script></head><body style="background:url('img/bg.png')">
    <img src="/assets/s/1/skins/mouse.png" srcset="a.png 1x, b.png 2x"><a href="privacy/index.html">p</a>
    <a href="https://www.reddit.com/r/mopeio/">r</a>
    <script>var s=document.createElement('script'); s.src = 'client.js?x=' + (+new Date());</script>
    </body></html>"""

    def test_html_refs(self):
        refs = {r.url: r for r in fm.extract_html_refs("https://mope.io/", self.HTML)}
        self.assertEqual(refs["https://mope.io/main.css?v=live.257"].relation, "stylesheet")
        self.assertEqual(refs["https://mope.io/jquery_3_3_1.js"].relation, "script")
        self.assertIn("https://mope.io/assets/s/1/skins/mouse.png", refs)
        self.assertIn("https://mope.io/b.png", refs)
        self.assertIn("https://mope.io/img/bg.png", refs)
        self.assertEqual(refs["https://mope.io/privacy/index.html"].relation, "hyperlink")
        client = refs["https://mope.io/client.js?x="]
        self.assertTrue(client.speculative)

    def test_css_refs(self):
        css = "@import 'base.css'; .a{background:url(../img/x.png)} .b{src:url(\"data:font/woff;base64,AA\")}"
        urls = [r.url for r in fm.extract_css_refs("https://mope.io/css/main.css", css)]
        self.assertEqual(urls, ["https://mope.io/img/x.png", "https://mope.io/css/base.css"])

    def test_js_literals(self):
        js = 'var a="img/sprites/fox.png",b=\'audio/eat.mp3\',c="not a path",d="x"+".png";'
        urls = sorted(r.url for r in fm.extract_js_refs("https://mope.io/", js))
        self.assertEqual(urls, ["https://mope.io/audio/eat.mp3", "https://mope.io/img/sprites/fox.png"])


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = dict(fm.DEFAULT_CONFIG)
        self.paths = fm.ProjectPaths(Path(self.tmp.name), config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mapping(self):
        p = self.paths.local_path_for("https://mope.io/", "html", "mope.io")
        self.assertEqual(self.paths.rel(p), "archive/original/mope.io/index.html")
        p = self.paths.local_path_for("https://mope.io/client.js?x=123", "js", "mope.io")
        self.assertEqual(self.paths.rel(p), "archive/original/mope.io/client.js")
        p = self.paths.local_path_for("https://apis.google.com/js/platform.js", "js", "mope.io")
        self.assertEqual(self.paths.rel(p), "archive/original/external-hosts/apis.google.com/js/platform.js")

    def test_traversal_is_contained(self):
        p = self.paths.local_path_for("https://mope.io/%2e%2e/%2e%2e/etc/passwd", "other", "mope.io")
        self.assertIn(self.paths.original, p.parents)


def gz_member(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as handle:
        handle.write(data)
    return buf.getvalue()


class CommonCrawlTests(unittest.TestCase):
    DATA = "https://data.example"

    def build_index(self):
        """Synthetic collection: 400 CDX blocks, mope.io captures inside one of them."""
        base = f"{self.DATA}/cc-index/collections/CC-TEST/indexes"
        keys = [f"com,example{i:04d})/ 20210410000000" for i in range(200)]
        keys += ["io,mope)/ 20210417072725", "io,mope)/robots.txt 20210417072725"]
        keys += [f"org,zz{i:04d})/ 20210410000000" for i in range(200)]
        keys.sort()
        cdx = bytearray()
        cluster_lines = []
        block_size = 3
        for start in range(0, len(keys), block_size):
            lines = []
            for key in keys[start:start + block_size]:
                surt, ts = key.split(" ")
                host = ".".join(reversed(surt.split(")")[0].split(",")))
                path = surt.split(")", 1)[1]
                lines.append(f"{surt} {ts} " + json.dumps({"url": f"https://{host}{path}", "mime": "text/html",
                                                           "status": "200", "digest": "D", "length": "10",
                                                           "offset": "0", "filename": "x.warc.gz"}))
            member = gz_member(("\n".join(lines) + "\n").encode())
            cluster_lines.append(f"{keys[start]}\tcdx-00000.gz\t{len(cdx)}\t{len(member)}\t{start // 3 + 1}")
            cdx += member
        files = {f"{base}/cluster.idx": ("\n".join(cluster_lines) + "\n").encode(),
                 f"{base}/cdx-00000.gz": bytes(cdx)}
        return files

    def provider(self, files):
        cfg = {"name": "cc", "type": "commoncrawl", "data_url": self.DATA, "index_url": "https://idx.example",
               "index_mode": "cluster", "collections": ["CC-TEST"]}
        return fm.CommonCrawlProvider(cfg, FakeHttp(files), FakeDb(), LOG, "mope.io")

    def test_cluster_binary_search_finds_domain(self):
        provider = self.provider(self.build_index())
        caps = provider.discover("mope.io", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 12, 31, tzinfo=UTC))
        self.assertEqual(sorted(c.url for c in caps), ["https://mope.io/", "https://mope.io/robots.txt"])

    def test_warc_fetch_and_timestamp_check(self):
        body = b"User-agent: *\n"
        http = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Encoding: gzip\r\n\r\n"
                + gzip.compress(body))
        warc = (b"WARC/1.0\r\nWARC-Type: response\r\nWARC-Date: 2021-04-17T07:27:25Z\r\n"
                + f"Content-Length: {len(http)}\r\n\r\n".encode() + http + b"\r\n\r\n")
        member = gz_member(warc)
        files = {f"{self.DATA}/crawl/x.warc.gz": b"PAD" + member}
        provider = self.provider(files)
        cap = fm.Capture(provider="cc", url="https://mope.io/robots.txt", timestamp="20210417072725", status="200",
                         meta={"filename": "crawl/x.warc.gz", "offset": 3, "length": len(member)})
        payload = provider.fetch(cap)
        self.assertEqual(payload.body, body)
        cap.timestamp = "20210417000000"
        with self.assertRaises(fm.TimestampMismatch):
            provider.fetch(cap)

    @staticmethod
    def warc_member(warc_headers: str, http: bytes) -> bytes:
        return gz_member(f"WARC/1.0\r\n{warc_headers}Content-Length: {len(http)}\r\n\r\n".encode()
                         + http + b"\r\n\r\n")

    def revisit_fixture(self, refers_to="2021-03-06T07:04:42Z"):
        revisit = self.warc_member(
            "WARC-Type: revisit\r\nWARC-Date: 2021-04-17T07:27:25Z\r\n"
            "WARC-Refers-To-Target-URI: https://mope.io/\r\n"
            f"WARC-Refers-To-Date: {refers_to}\r\n"
            "WARC-Profile: http://netpreserve.org/warc/1.1/revisit/server-not-modified\r\n",
            b"HTTP/1.1 304 Not Modified\r\nLast-Modified: Fri, 05 Mar 2021 11:49:49 GMT\r\n\r\n")
        original = self.warc_member("WARC-Type: response\r\nWARC-Date: 2021-03-06T07:04:42Z\r\n",
                                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html>march</html>")
        files = {f"{self.DATA}/crawl/a.warc.gz": revisit, f"{self.DATA}/crawl/b.warc.gz": original}
        provider = self.provider(files)
        provider.register_known([fm.Capture(provider="cc", url="https://mope.io/", timestamp="20210306070442",
                                            status="200", meta={"filename": "crawl/b.warc.gz", "offset": 0,
                                                                "length": len(original)})])
        cap = fm.Capture(provider="cc", url="https://mope.io/", timestamp="20210417072725", status="304",
                         mime="warc/revisit", revisit=True,
                         meta={"filename": "crawl/a.warc.gz", "offset": 0, "length": len(revisit)})
        return provider, cap

    def test_server_not_modified_revisit_resolves(self):
        provider, cap = self.revisit_fixture()
        payload = provider.fetch(cap)
        self.assertEqual(payload.body, b"<html>march</html>")
        self.assertIn("server-not-modified", payload.notes[0])
        self.assertIn("20210306070442", payload.notes[0])

    def test_unresolved_revisit(self):
        provider, cap = self.revisit_fixture(refers_to="2021-01-01T00:00:00Z")
        with self.assertRaises(fm.RevisitUnresolved):
            provider.fetch(cap)

    def test_dechunk(self):
        self.assertEqual(fm.dechunk(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"), b"Wikipedia")


class PywbTests(unittest.TestCase):
    def provider(self, routes):
        cfg = {"name": "pt", "type": "pywb", "cdx_url": "https://a.example/cdx", "replay_url": "https://a.example/wb"}
        return fm.PywbProvider(cfg, FakeHttp(routes=routes), FakeDb(), LOG, "mope.io")

    def test_refuses_redirect_to_other_capture(self):
        url = "https://a.example/wb/20210423000000id_/https://mope.io/"
        provider = self.provider({url: FakeResponse(307, headers={
            "Location": "https://a.example/wb/20210604001244id_/https://mope.io/"})})
        cap = fm.Capture(provider="pt", url="https://mope.io/", timestamp="20210423000000", status="200")
        with self.assertRaises(fm.TimestampMismatch):
            provider.fetch(cap)

    def test_refuses_memento_mismatch(self):
        url = "https://a.example/wb/20210310091713id_/https://mope.io/"
        provider = self.provider({url: FakeResponse(200, b"<html>", {
            "Memento-Datetime": "Thu, 11 Mar 2021 09:17:13 GMT"})})
        cap = fm.Capture(provider="pt", url="https://mope.io/", timestamp="20210310091713", status="200")
        with self.assertRaises(fm.TimestampMismatch):
            provider.fetch(cap)

    def test_accepts_matching_capture(self):
        url = "https://a.example/wb/20210310091713id_/https://mope.io/"
        provider = self.provider({url: FakeResponse(200, b"<html>", {
            "Memento-Datetime": "Wed, 10 Mar 2021 09:17:13 GMT", "Content-Type": "text/html"})})
        cap = fm.Capture(provider="pt", url="https://mope.io/", timestamp="20210310091713", status="200")
        self.assertEqual(provider.fetch(cap).body, b"<html>")

    def test_cdx_formats(self):
        ndjson = b'{"timestamp": "20210310091713", "url": "https://mope.io/", "status": "200", "mime": "text/html"}\n'
        array = json.dumps([["timestamp", "original", "statuscode", "mimetype"],
                            ["20210310091713", "https://mope.io/", "200", "text/html"]]).encode()
        for body in (ndjson, array):
            provider = self.provider({})
            provider.http.get = lambda url, **kw: FakeResponse(200, body)
            caps = provider.discover("mope.io", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 12, 1, tzinfo=UTC))
            self.assertEqual((caps[0].url, caps[0].status), ("https://mope.io/", "200"))


class AnalyzerTests(unittest.TestCase):
    def test_decode_js_escapes(self):
        obfuscated = "a['\\x6e\\x61\\x6d\\x65']='\\x22x';" * 20
        decoded = fm.decode_js_escapes(obfuscated, threshold=10)
        self.assertIn("a['name']='\\x22x';", decoded)  # quote stays escaped
        self.assertIsNone(fm.decode_js_escapes("plain code", threshold=10))

    def test_region_and_endpoint_detection(self):
        js = ('var servers=[{name:"USA 1",ip:"usa1.mope.io",port:443},{name:"Europe 2",ip:"10.0.0.2",port:7020}];'
              'var ws=new WebSocket("wss://"+s.ip+":"+s.port);$.ajax({url:"/api/login.php"});'
              'if(isDevMode){debugDraw=true}')
        analyzer = fm.ClientAnalyzer.__new__(fm.ClientAnalyzer)
        analyzer.domain = "mope.io"
        analyzer.findings = fm.FindingSet()
        analyzer.regions = {}
        analyzer._scan(js, "client.js")
        analyzer._hostname_regions()
        names = {r["name"]: r for r in analyzer.regions.values()}
        self.assertEqual(names["USA 1"]["confidence"], "high")
        self.assertEqual(names["Europe 2"]["properties"]["port"], ["7020"])
        self.assertTrue(analyzer.findings.category("websocket_constructors"))
        self.assertIn("/api/login.php", [e["value"] for e in analyzer.findings.category("endpoint_paths")])
        self.assertTrue(analyzer.findings.category("developer"))
        self.assertIn("10.0.0.2", [e["value"] for e in analyzer.findings.category("ip_addresses")])


if __name__ == "__main__":
    unittest.main()
