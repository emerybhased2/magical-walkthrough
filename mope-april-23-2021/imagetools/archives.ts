/**
 * Non-Wayback web archives: discovery of archived image captures and raw downloads.
 *
 *   arquivo      Arquivo.pt pywb CDX (matchType=domain) + `id_` raw replay. Redirects to another
 *                capture are refused and Memento-Datetime must equal the requested timestamp.
 *   commoncrawl  CDX index server per crawl; when it is unreachable, a binary search of the
 *                crawl's cluster.idx with HTTP range requests (as fetch_mope_archive.py does).
 *                Records are read from the WARC files with range requests; revisit records are
 *                resolved to the response with the same payload digest.
 *   vefsafn      vefsafn.is pywb CDX, discovery only (its replay sits behind a CAPTCHA).
 *
 * The Internet Archive / Wayback Machine is deliberately not used (project rule).
 */

import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";
import { IMAGE_EXT } from "./client_refs.ts";
import { parseWarcRecord } from "./warc.ts";

export type Capture = {
  provider: string;
  url: string;
  timestamp: string;          // yyyyMMddHHmmss
  status: string;
  mime: string;
  digest: string;             // sha1 base32 of the payload, as the archive's index gives it
  length: number | null;
  downloadable: boolean;
  revisit: boolean;
  meta: Record<string, string | number>;
};

export type Fetched = { bytes: Uint8Array; capture: Capture; resolvedFrom?: string; digestVerified: boolean | null; note?: string };

export type Log = (message: string) => void;

const USER_AGENT = "mope-local-archive/1.0 (image catalogue; local preservation)";

export class FetchError extends Error {}

export class Http {
  private next = new Map<string, number>();
  /** Slower pacing for hosts that throttle (Common Crawl's data server answers 503 "slow down"). */
  readonly hostDelayMs = new Map<string, number>([["data.commoncrawl.org", 1000], ["index.commoncrawl.org", 1000]]);
  constructor(private delayMs = 300, private timeoutMs = 60000, private retries = 5) {}

  /** Requests to one host are spaced out; the slot is reserved before waiting so parallel workers queue. */
  private async slot(host: string) {
    const now = Date.now();
    const at = Math.max(now, this.next.get(host) ?? 0);
    this.next.set(host, at + (this.hostDelayMs.get(host) ?? this.delayMs));
    if (at > now) await Bun.sleep(at - now);
  }

  async request(url: string, init: RequestInit = {}): Promise<Response> {
    const host = new URL(url).host;
    let lastError: unknown;
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      await this.slot(host);
      try {
        const response = await fetch(url, {
          ...init,
          headers: { "User-Agent": USER_AGENT, "Accept-Encoding": "identity", ...(init.headers ?? {}) },
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (response.status === 429 || response.status >= 500) {
          lastError = new FetchError(`HTTP ${response.status} for ${url}`);
          await response.arrayBuffer().catch(() => undefined);
          const retryAfter = Number(response.headers.get("retry-after"));
          if (Number.isFinite(retryAfter) && retryAfter > 0) {
            await Bun.sleep(Math.min(retryAfter, 120) * 1000);
            continue;
          }
        } else {
          return response;
        }
      } catch (error) {
        lastError = error;
      }
      await Bun.sleep(Math.min(60000, 3000 * 2 ** attempt));
    }
    throw lastError instanceof Error ? lastError : new FetchError(String(lastError));
  }

  async bytes(url: string, init: RequestInit = {}): Promise<[Response, Uint8Array]> {
    const response = await this.request(url, init);
    return [response, new Uint8Array(await response.arrayBuffer())];
  }
}

export function isImageCapture(url: string, mime: string): boolean {
  const path = url.split(/[?#]/)[0];
  return mime.startsWith("image/") || IMAGE_EXT.test(path);
}

/** sha1 base32 (RFC 4648, as used by CDX digests). */
export function sha1Base32(bytes: Uint8Array): string {
  const digest = createHash("sha1").update(bytes).digest();
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0, value = 0, out = "";
  for (const byte of digest) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += alphabet[(value << (5 - bits)) & 31];
  return out;
}

export function digestMatches(bytes: Uint8Array, digest: string): boolean | null {
  const clean = digest.replace(/^sha1:/i, "").toUpperCase();
  if (!/^[A-Z2-7]{32}$/.test(clean)) return null;
  return sha1Base32(bytes) === clean;
}

function httpDateToTimestamp(value: string | null): string | null {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().replace(/[-:T]/g, "").slice(0, 14);
}

export function surtHost(domain: string): string {
  return domain.toLowerCase().replace(/^www\./, "").split(".").reverse().join(",");
}

export interface Provider {
  name: string;
  discover(domain: string, cacheDir: string, refresh: boolean): Promise<Capture[]>;
  fetch(capture: Capture, all: Capture[]): Promise<Fetched>;
}

async function cached<T>(path: string, refresh: boolean, produce: () => Promise<T>): Promise<T> {
  const file = Bun.file(path);
  if (!refresh && (await file.exists())) return (await file.json()).data as T;
  const data = await produce();
  await Bun.write(path, JSON.stringify({ written: new Date().toISOString(), data }, null, 1));
  return data;
}

// -- pywb (Arquivo.pt, vefsafn.is) ---------------------------------------------------------------

export class PywbProvider implements Provider {
  constructor(readonly name: string, private cdxUrl: string, private replayUrl: string | null, private http: Http,
              private log: Log) {}

  async discover(domain: string, cacheDir: string, refresh: boolean): Promise<Capture[]> {
    const rows = await cached(`${cacheDir}/${this.name}-${domain}.json`, refresh, async () => {
      const url = `${this.cdxUrl}?url=${encodeURIComponent(domain)}&matchType=domain&output=json&limit=500000`;
      const response = await this.http.request(url);
      if (response.status === 404) return [];
      if (response.status !== 200) throw new FetchError(`${this.name} CDX returned HTTP ${response.status}`);
      const text = await response.text();
      return text.split("\n").filter((l) => l.trim().startsWith("{")).map((l) => JSON.parse(l));
    });
    this.log(`${this.name}: ${rows.length} captures under ${domain}`);
    return rows.filter((r: any) => isImageCapture(r.url ?? "", r.mime ?? "")).map((r: any) => ({
      provider: this.name, url: r.url, timestamp: String(r.timestamp), status: String(r.status ?? "-"),
      mime: r.mime ?? "", digest: r.digest ?? "", length: r.length ? Number(r.length) : null,
      downloadable: this.replayUrl !== null, revisit: r.mime === "warc/revisit",
      meta: Object.fromEntries(Object.entries({ collection: r.collection, filename: r.filename }).filter(([, v]) => v)) as any,
    }));
  }

  async fetch(capture: Capture, all: Capture[]): Promise<Fetched> {
    if (!this.replayUrl) throw new FetchError(`${this.name} has no raw replay`);
    let target = capture;
    let resolvedFrom: string | undefined;
    if (capture.revisit) {
      const original = all.find((c) => c.provider === this.name && c.url === capture.url && c.digest === capture.digest && !c.revisit && c.status === "200");
      if (!original) throw new FetchError("revisit record without a matching original capture");
      target = original;
      resolvedFrom = capture.timestamp;
    }
    const url = `${this.replayUrl}/${target.timestamp}id_/${target.url}`;
    const [response, bytes] = await this.http.bytes(url, { redirect: "manual" });
    if (response.status >= 300 && response.status < 400) {
      throw new FetchError(`archive redirected to ${response.headers.get("location")} (another capture); refused`);
    }
    if (response.status !== 200) throw new FetchError(`replay returned HTTP ${response.status}`);
    const memento = httpDateToTimestamp(response.headers.get("memento-datetime"));
    if (memento && memento !== target.timestamp) {
      throw new FetchError(`replay served capture ${memento}, not ${target.timestamp}; refused`);
    }
    return { bytes, capture: target, resolvedFrom, digestVerified: digestMatches(bytes, target.digest) };
  }
}

// -- Common Crawl ----------------------------------------------------------------------------------

export class CommonCrawlProvider implements Provider {
  readonly name = "commoncrawl";
  private serverFailures = 0;   // consecutive index-server failures; after 3 only cluster.idx is used
  private sizes = new Map<string, number>();

  constructor(private http: Http, private log: Log, private collections: string[],
              private indexUrl = "https://index.commoncrawl.org", private dataUrl = "https://data.commoncrawl.org") {}

  static async collectionsBetween(http: Http, fromYear: number, toYear: number,
                                  indexUrl = "https://index.commoncrawl.org"): Promise<string[]> {
    const response = await http.request(`${indexUrl}/collinfo.json`);
    if (response.status !== 200) throw new FetchError(`collinfo.json returned HTTP ${response.status}`);
    const list = (await response.json()) as Array<{ id: string }>;
    return list.map((c) => c.id).filter((id) => {
      const year = Number(/CC-MAIN-(\d{4})/.exec(id)?.[1]);
      return year >= fromYear && year <= toYear;
    }).sort();
  }

  private async range(url: string, start: number, length: number): Promise<Uint8Array> {
    const [response, bytes] = await this.http.bytes(url, { headers: { Range: `bytes=${start}-${start + length - 1}` } });
    if (response.status === 200 && start > 0) throw new FetchError("server ignored the Range header");
    if (response.status !== 200 && response.status !== 206) throw new FetchError(`HTTP ${response.status} for range request`);
    return bytes;
  }

  private async size(url: string): Promise<number> {
    if (this.sizes.has(url)) return this.sizes.get(url)!;
    const response = await this.http.request(url, { method: "HEAD" });
    if (response.status !== 200) throw new FetchError(`HTTP ${response.status} for ${url}`);
    const size = Number(response.headers.get("content-length"));
    this.sizes.set(url, size);
    return size;
  }

  private async keyAt(idxUrl: string, pos: number, total: number): Promise<string | null> {
    const chunk = new TextDecoder().decode(await this.range(idxUrl, pos, Math.min(16384, total - pos)));
    const newline = chunk.indexOf("\n");
    if (newline < 0) return null;
    const rest = chunk.slice(newline + 1);
    return rest ? rest.split("\n", 1)[0].split("\t", 1)[0] : null;
  }

  /** CDX blocks (file, offset, length) of cluster.idx that may hold SURT keys in [lo, hi). */
  private async clusterEntries(idxUrl: string, lo: string, hi: string): Promise<Array<[string, number, number]>> {
    const total = await this.size(idxUrl);
    let loPos = 0, hiPos = total;
    while (hiPos - loPos > 65536) {
      const mid = Math.floor((loPos + hiPos) / 2);
      const key = await this.keyAt(idxUrl, mid, total);
      if (key === null || key >= lo) hiPos = mid; else loPos = mid;
    }
    const entries: Array<[string, number, number]> = [];
    let previous: [string, number, number] | null = null;
    let started = false, done = false;
    let pos = loPos, buffer = "", skipPartial = loPos > 0;
    while (pos < total && !done) {
      const chunk = await this.range(idxUrl, pos, Math.min(131072, total - pos));
      pos += chunk.length;
      buffer += new TextDecoder().decode(chunk);
      const lines = buffer.split("\n");
      buffer = pos < total ? lines.pop()! : "";
      if (skipPartial) {
        lines.shift();
        skipPartial = false;
      }
      for (const line of lines) {
        const fields = line.split("\t");
        if (fields.length < 4) continue;
        const entry: [string, number, number] = [fields[1], Number(fields[2]), Number(fields[3])];
        if (fields[0] < lo) {
          previous = entry;
          continue;
        }
        if (!started) {
          started = true;
          if (previous) entries.push(previous);
        }
        if (fields[0] >= hi) {
          done = true;
          break;
        }
        entries.push(entry);
      }
    }
    if (!started && previous) entries.push(previous);
    return entries;
  }

  private async clusterQuery(collection: string, lo: string, hi: string): Promise<any[]> {
    const base = `${this.dataUrl}/cc-index/collections/${collection}/indexes`;
    const rows: any[] = [];
    for (const [file, offset, length] of await this.clusterEntries(`${base}/cluster.idx`, lo, hi)) {
      const text = new TextDecoder().decode(gunzipSync(await this.range(`${base}/${file}`, offset, length)));
      for (const line of text.split("\n")) {
        const first = line.indexOf(" "), second = line.indexOf(" ", first + 1);
        if (first < 0 || second < 0) continue;
        const key = line.slice(0, first);
        if (key < lo || key >= hi) continue;
        rows.push({ urlkey: key, timestamp: line.slice(first + 1, second), ...JSON.parse(line.slice(second + 1)) });
      }
    }
    return rows;
  }

  private async serverQuery(collection: string, domain: string): Promise<any[]> {
    const base = `${this.indexUrl}/${collection}-index?url=${encodeURIComponent(domain)}&matchType=domain`;
    const pagesResponse = await this.http.request(`${base}&showNumPages=true`);
    if (pagesResponse.status === 404) return [];
    if (pagesResponse.status !== 200) throw new FetchError(`index server HTTP ${pagesResponse.status}`);
    const pages = Math.max(1, Number((await pagesResponse.json() as any).pages ?? 1));
    const rows: any[] = [];
    for (let page = 0; page < pages; page++) {
      const response = await this.http.request(`${base}&output=json&page=${page}`);
      const text = await response.text();
      if (response.status === 404 && text.includes("No Captures")) break;
      if (response.status !== 200) throw new FetchError(`index server HTTP ${response.status}`);
      rows.push(...text.split("\n").filter((l) => l.trim().startsWith("{")).map((l) => JSON.parse(l)));
    }
    return rows;
  }

  async discover(domain: string, cacheDir: string, refresh: boolean): Promise<Capture[]> {
    const surt = surtHost(domain);
    const out: Capture[] = [];
    let total = 0;
    for (const collection of this.collections) {
      let rows: any[];
      try {
        rows = await cached(`${cacheDir}/commoncrawl-${collection}-${domain}.json`, refresh, async () => {
          if (this.serverFailures < 3) {
            try {
              const r = await this.serverQuery(collection, domain);
              this.serverFailures = 0;
              return r;
            } catch (error) {
              this.serverFailures++;
              this.log(`commoncrawl ${collection}: index server failed (${(error as Error).message}); using cluster.idx range search` +
                       (this.serverFailures >= 3 ? " from now on" : ""));
            }
          }
          return this.clusterQuery(collection, `${surt})`, `${surt}-`);
        });
      } catch (error) {
        this.log(`commoncrawl ${collection}: skipped (${(error as Error).message})`);
        continue;
      }
      total += rows.length;
      for (const r of rows) {
        const mime = r.mime === "warc/revisit" ? "warc/revisit" : (r["mime-detected"] || r.mime || "");
        if (!isImageCapture(r.url ?? "", mime)) continue;
        out.push({
          provider: this.name, url: r.url, timestamp: String(r.timestamp), status: String(r.status ?? "-"), mime,
          digest: r.digest ?? "", length: r.length ? Number(r.length) : null, downloadable: true,
          revisit: mime === "warc/revisit",
          meta: { collection, filename: r.filename, offset: Number(r.offset), recordLength: Number(r.length) },
        });
      }
    }
    this.log(`commoncrawl: ${total} captures under ${domain} in ${this.collections.length} crawls`);
    return out;
  }

  async fetch(capture: Capture, all: Capture[]): Promise<Fetched> {
    let target = capture;
    let resolvedFrom: string | undefined;
    if (capture.revisit) {
      const original = all.find((c) => c.provider === this.name && c.digest === capture.digest && !c.revisit && c.status === "200");
      if (!original) throw new FetchError("revisit record without a matching original response in the index");
      target = original;
      resolvedFrom = capture.timestamp;
    }
    const raw = await this.range(`${this.dataUrl}/${target.meta.filename}`, Number(target.meta.offset), Number(target.meta.recordLength));
    const record = parseWarcRecord(gunzipSync(raw));
    if (record.warcType !== "response") throw new FetchError(`WARC record is a ${record.warcType}, not a response`);
    const date = record.headers["warc-date"]?.replace(/[-:TZ]/g, "").slice(0, 14);
    if (date && date !== target.timestamp) throw new FetchError(`WARC-Date ${date} does not match ${target.timestamp}`);
    if (record.httpStatus !== 200) throw new FetchError(`archived HTTP status ${record.httpStatus}`);
    const verified = digestMatches(record.payload, target.digest) ?? digestMatches(record.body, target.digest);
    return { bytes: record.body, capture: target, resolvedFrom, digestVerified: verified,
             note: record.notes.length ? record.notes.join("; ") : undefined };
  }
}
