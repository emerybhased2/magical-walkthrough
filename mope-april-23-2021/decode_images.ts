#!/usr/bin/env bun
/**
 * decode_images.ts -- catalogue, fetch and decode every mope.io image the archived client uses
 * (animals, food, game objects, abilities, disasters, shop, UI ...), from non-Wayback archives.
 *
 *   1. catalogue  parse the readable client (rebuild_client_js.py output) and decode every image
 *                 path it can build: helpers, constants, ternaries, path helpers, the animal table
 *   2. discover   list archived image captures of mope.io and its subdomains on Arquivo.pt,
 *                 Common Crawl and vefsafn.is (discovery only)
 *   3. download   fetch the raw bytes of every distinct version (payload digest) of every image,
 *                 refusing archive redirects to other captures and checking the payload digest
 *   4. decode     identify the real format from the bytes and decode it fully (PNG: CRCs and the
 *                 inflated image data; JPEG/GIF/WebP/SVG/ICO structure); HTML error pages saved
 *                 under image URLs are rejected
 *   5. report     manifests by category and animal, plus coverage against the client's references
 *
 * Usage:
 *   bun decode_images.ts                         # everything, default paths
 *   bun decode_images.ts --catalog-only          # step 1 only (offline)
 *   bun decode_images.ts --providers arquivo     # skip Common Crawl / vefsafn
 *   bun decode_images.ts --cc-from 2019 --cc-to 2025 --versions nearest --refresh
 *
 * Output (git-ignored): archive/images/
 *   files/<host>/<path>                 the version nearest the target date, original bytes
 *   versions/<host>/<path>/<ts>-<provider>-<digest><ext>   every distinct archived version
 *   rejected/...                        archived responses that are not images (kept for audit)
 *   manifests/*.json                    client references, animals, captures, images, coverage
 *   cache/                              archive index responses (reused unless --refresh)
 * The Wayback Machine is not used (project rule).
 */

import { mkdir, chmod } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { analyseClient, type ClientCatalog } from "./imagetools/client_refs.ts";
import { CommonCrawlProvider, Http, PywbProvider, type Capture, type Provider } from "./imagetools/archives.ts";
import { animalFor, bestTemplate, categoryFor, coverage, splitArchivedUrl, uniqueTemplates } from "./imagetools/catalog.ts";
import { decodeImage, EXTENSIONS, type ImageInfo } from "./imagetools/formats.ts";

const VERSION = "1.0.0";
const PROJECT = import.meta.dir;
const DEFAULT_CLIENT = "snapshots/2025-12-10/working/client-js/20251210042757_arquivo/client.js";

type Options = {
  client: string;
  output: string;
  domain: string;
  date: string;
  providers: string[];
  ccFrom: number;
  ccTo: number;
  versions: "all" | "nearest";
  workers: number;
  refresh: boolean;
  catalogOnly: boolean;
};

function parseArgs(argv: string[]): Options {
  const config = (() => {
    try {
      return JSON.parse(require("node:fs").readFileSync(join(PROJECT, "config.json"), "utf8"));
    } catch {
      return {};
    }
  })();
  const options: Options = {
    client: DEFAULT_CLIENT, output: "archive/images", domain: config.domain ?? "mope.io",
    date: config.snapshot_date ?? "2021-04-23", providers: ["arquivo", "commoncrawl", "vefsafn"],
    ccFrom: 2016, ccTo: new Date().getUTCFullYear(), versions: "all", workers: 4, refresh: false, catalogOnly: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--client") options.client = next();
    else if (a === "--output") options.output = next();
    else if (a === "--domain") options.domain = next();
    else if (a === "--date") options.date = next();
    else if (a === "--providers") options.providers = next().split(",").map((s) => s.trim()).filter(Boolean);
    else if (a === "--cc-from") options.ccFrom = Number(next());
    else if (a === "--cc-to") options.ccTo = Number(next());
    else if (a === "--versions") options.versions = next() === "nearest" ? "nearest" : "all";
    else if (a === "--workers") options.workers = Math.max(1, Number(next()));
    else if (a === "--refresh") options.refresh = true;
    else if (a === "--catalog-only") options.catalogOnly = true;
    else if (a === "-h" || a === "--help") {
      console.log(require("node:fs").readFileSync(import.meta.path, "utf8").split("*/")[0]);
      process.exit(0);
    } else throw new Error(`unknown argument ${a}`);
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(options.date)) throw new Error("--date must be YYYY-MM-DD");
  for (const p of options.providers) {
    if (!["arquivo", "commoncrawl", "vefsafn"].includes(p)) throw new Error(`unknown provider ${p} (Wayback is not used)`);
  }
  return options;
}

const log = (message: string) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${message}`);

function timestampMs(ts: string): number {
  const p = ts.padEnd(14, "0");
  return Date.UTC(+p.slice(0, 4), +p.slice(4, 6) - 1, +p.slice(6, 8), +p.slice(8, 10), +p.slice(10, 12), +p.slice(12, 14));
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

/** A storage path that stays inside the output folder whatever the archived URL contains. */
export function safeRelative(key: string): string {
  const parts = key.split("/").filter((s) => s && s !== "." && s !== "..").map((s) => s.replace(/[\u0000-\u001f<>:"\\|?*]/g, "_"));
  return parts.join("/") || "_";
}

async function pool<T>(items: T[], workers: number, task: (item: T, index: number) => Promise<void>) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(workers, items.length) }, async () => {
    while (next < items.length) {
      const index = next++;
      await task(items[index], index);
    }
  }));
}

type Version = {
  provider: string;
  timestamp: string;
  capturedUrl: string;
  digest: string;
  resolvedFromRevisit?: string;
  digestVerified: boolean | null;
  sha256: string;
  bytes: number;
  decoded: ImageInfo;
  storedAt: string;
  note?: string;
};

type ImageEntry = {
  key: string;
  url: string;
  layout: string;
  season: string | null;
  theme: string | null;
  rel: string;
  category: string;
  animal: { id: number; name: string } | null;
  clientTemplate: string | null;
  clientLines: number[];
  primary: Version | null;
  versions: Version[];
  failures: Array<{ provider: string; timestamp: string; error: string }>;
};

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const out = resolve(PROJECT, options.output);
  const manifests = join(out, "manifests");
  const cacheDir = join(out, "cache");
  await mkdir(manifests, { recursive: true });
  await mkdir(cacheDir, { recursive: true });
  const started = Date.now();

  // 1. catalogue --------------------------------------------------------------------------------
  const clientPath = resolve(PROJECT, options.client);
  if (!(await Bun.file(clientPath).exists())) {
    throw new Error(`readable client not found: ${options.client} (run rebuild_client_js.py first)`);
  }
  log(`catalogue: analysing ${options.client}`);
  const catalog: ClientCatalog = analyseClient(await Bun.file(clientPath).text(), options.client);
  const templates = uniqueTemplates(catalog.references);
  const byCat: Record<string, number> = {};
  for (const t of templates) byCat[t.category] = (byCat[t.category] ?? 0) + 1;
  log(`catalogue: ${catalog.references.length} image references (${templates.length} distinct templates), ` +
      `${catalog.animals.length} animals, loaders ${catalog.loaders.join(", ")}`);
  await Bun.write(join(manifests, "client_references.json"), JSON.stringify({
    generated: new Date().toISOString(), tool: `decode_images.ts ${VERSION}`, client: options.client,
    constants: catalog.constants, classes: catalog.classes, loaders: catalog.loaders,
    templatesByCategory: byCat,
    templates: templates.map(({ regexes, literalChars, ...t }) => t),
  }, null, 1));
  await Bun.write(join(manifests, "animals.json"), JSON.stringify(catalog.animals, null, 1));
  if (options.catalogOnly) {
    printCategories("client templates", byCat);
    return;
  }

  // 2. discover -----------------------------------------------------------------------------------
  const http = new Http(350, 90000, 3);
  const providers: Provider[] = [];
  if (options.providers.includes("arquivo")) {
    providers.push(new PywbProvider("arquivo", "https://arquivo.pt/wayback/cdx", "https://arquivo.pt/wayback", http, log));
  }
  if (options.providers.includes("commoncrawl")) {
    let collections: string[] = [];
    try {
      collections = await CommonCrawlProvider.collectionsBetween(http, options.ccFrom, options.ccTo);
    } catch (error) {
      log(`commoncrawl: crawl list unavailable (${(error as Error).message}); skipped`);
    }
    if (collections.length) providers.push(new CommonCrawlProvider(http, log, collections));
  }
  if (options.providers.includes("vefsafn")) {
    providers.push(new PywbProvider("vefsafn", "https://vefsafn.is/is/cdx", null, http, log));
  }
  const captures: Capture[] = [];
  for (const provider of providers) {
    try {
      const found = await provider.discover(options.domain, cacheDir, options.refresh);
      log(`${provider.name}: ${found.length} image captures`);
      captures.push(...found);
    } catch (error) {
      log(`${provider.name}: discovery failed (${(error as Error).message})`);
    }
  }
  await Bun.write(join(manifests, "captures.json"), JSON.stringify(captures, null, 1));

  // group by storage key; keep downloadable, successful (or revisit) captures
  const target = Date.parse(`${options.date}T12:00:00Z`);
  const priority = new Map(providers.map((p, i) => [p.name, i]));
  const groups = new Map<string, Capture[]>();
  const discoveryOnly = new Map<string, Capture[]>();
  for (const c of captures) {
    let key: string;
    try {
      key = splitArchivedUrl(c.url).key;
    } catch {
      continue;
    }
    if (!c.downloadable) {
      (discoveryOnly.get(key) ?? discoveryOnly.set(key, []).get(key)!).push(c);
      continue;
    }
    if (c.status !== "200" && !c.revisit) continue;
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(c);
  }
  log(`discover: ${groups.size} distinct image URLs downloadable, ${[...discoveryOnly.keys()].filter((k) => !groups.has(k)).length} known only from discovery-only archives`);

  // 3 + 4. download and decode ---------------------------------------------------------------------
  const entries: ImageEntry[] = [];
  const keys = [...groups.keys()].sort();
  let done = 0;
  const manifestPath = join(manifests, "images.json");
  const previous = new Map<string, Version>();
  if (!options.refresh && (await Bun.file(manifestPath).exists())) {
    for (const e of (await Bun.file(manifestPath).json()).images as ImageEntry[]) {
      for (const v of e.versions) previous.set(`${v.provider}|${v.timestamp}|${e.key}`, v);
    }
  }
  await pool(keys, options.workers, async (key) => {
    const list = groups.get(key)!;
    const path = splitArchivedUrl(list[0].url);
    const template = bestTemplate(path.rel, path.path, templates);
    const category = categoryFor(path, template);
    const animal = category === "animals" || category === "shop-skins" || category === "abilities" ? animalFor(path.rel, catalog.animals) : null;
    // one capture per distinct payload digest: nearest the target date, then provider priority
    const byDigest = new Map<string, Capture[]>();
    for (const c of list) (byDigest.get(c.digest || `${c.provider}${c.timestamp}`) ?? byDigest.set(c.digest || `${c.provider}${c.timestamp}`, []).get(c.digest || `${c.provider}${c.timestamp}`)!).push(c);
    const rank = (c: Capture) => [c.revisit ? 1 : 0, Math.abs(timestampMs(c.timestamp) - target), priority.get(c.provider) ?? 9] as const;
    const cmp = (a: Capture, b: Capture) => {
      const ra = rank(a), rb = rank(b);
      return ra[0] - rb[0] || ra[1] - rb[1] || ra[2] - rb[2];
    };
    let chosen = [...byDigest.values()].map((cs) => cs.sort(cmp));
    chosen.sort((a, b) => cmp(a[0], b[0]));
    if (options.versions === "nearest") chosen = chosen.slice(0, 1);
    const entry: ImageEntry = {
      key, url: list[0].url, layout: path.layout, season: path.season, theme: path.theme, rel: path.rel, category,
      animal: animal ? { id: animal.id, name: animal.aniName } : null,
      clientTemplate: template?.template ?? null, clientLines: template?.lines.slice(0, 20) ?? [],
      primary: null, versions: [], failures: [],
    };
    for (const candidates of chosen) {
      let stored: Version | null = null;
      for (const capture of candidates) {
        const cacheKey = `${capture.provider}|${capture.timestamp}|${key}`;
        const known = previous.get(cacheKey);
        if (known && (await Bun.file(join(out, known.storedAt)).exists())) {
          stored = known;
          break;
        }
        const provider = providers.find((p) => p.name === capture.provider)!;
        try {
          const fetched = await provider.fetch(capture, captures);
          if (fetched.digestVerified === false) throw new Error("payload digest does not match the archive index");
          const decoded = decodeImage(fetched.bytes);
          const digest8 = (capture.digest || sha256(fetched.bytes)).slice(0, 8).toLowerCase();
          const ext = EXTENSIONS[decoded.format] ?? (path.path.match(/\.[a-z0-9]+$/i)?.[0] ?? ".bin");
          const folder = decoded.format === "html" || decoded.format === "unknown" ? "rejected" : "versions";
          const storedAt = `${folder}/${safeRelative(key)}/${fetched.capture.timestamp}-${capture.provider}-${digest8}${ext}`;
          await writeReadOnly(join(out, storedAt), fetched.bytes);
          stored = {
            provider: capture.provider, timestamp: fetched.capture.timestamp, capturedUrl: fetched.capture.url,
            digest: fetched.capture.digest, resolvedFromRevisit: fetched.resolvedFrom, digestVerified: fetched.digestVerified,
            sha256: sha256(fetched.bytes), bytes: fetched.bytes.length, decoded, storedAt, note: fetched.note,
          };
          break;
        } catch (error) {
          entry.failures.push({ provider: capture.provider, timestamp: capture.timestamp, error: (error as Error).message });
        }
      }
      if (stored) entry.versions.push(stored);
    }
    const images = entry.versions.filter((v) => !v.storedAt.startsWith("rejected/"));
    entry.primary = images.sort((a, b) => Math.abs(timestampMs(a.timestamp) - target) - Math.abs(timestampMs(b.timestamp) - target))[0] ?? null;
    if (entry.primary) {
      const primaryPath = join(out, "files", safeRelative(key));
      await writeReadOnly(primaryPath, new Uint8Array(await Bun.file(join(out, entry.primary.storedAt)).arrayBuffer()));
    }
    entries.push(entry);
    done++;
    if (done % 25 === 0 || done === keys.length) log(`download: ${done}/${keys.length} URLs`);
  });
  entries.sort((a, b) => a.key.localeCompare(b.key));

  // 5. report ----------------------------------------------------------------------------------------
  const good = entries.filter((e) => e.primary);
  const cov = coverage(templates, good.map((e) => ({ path: splitArchivedUrl(e.url), category: e.category })));
  const versions = entries.flatMap((e) => e.versions);
  const summary = {
    generated: new Date().toISOString(),
    tool: `decode_images.ts ${VERSION}`,
    seconds: Math.round((Date.now() - started) / 1000),
    targetDate: options.date,
    providers: providers.map((p) => p.name),
    client: options.client,
    clientReferences: catalog.references.length,
    clientTemplates: templates.length,
    animalsInClientTable: catalog.animals.length,
    imageCaptures: captures.length,
    distinctUrls: entries.length,
    urlsWithImage: good.length,
    versionsStored: versions.filter((v) => v.storedAt.startsWith("versions/")).length,
    versionsDecodedOk: versions.filter((v) => v.decoded.ok).length,
    rejectedNotImages: versions.filter((v) => v.storedAt.startsWith("rejected/")).length,
    digestVerified: versions.filter((v) => v.digestVerified === true).length,
    failedUrls: entries.filter((e) => !e.versions.length).length,
    discoveryOnlyUrls: [...discoveryOnly.keys()].filter((k) => !groups.has(k)),
    byCategory: Object.fromEntries(Object.entries(groupCount(good, (e) => e.category)).sort()),
    byAnimal: Object.fromEntries(Object.entries(groupCount(good.filter((e) => e.animal), (e) => e.animal!.name)).sort()),
    byFormat: groupCount(versions, (v) => v.decoded.format),
    coverage: cov.byCategory,
  };
  await Bun.write(manifestPath, JSON.stringify({ ...summary, images: entries }, null, 1));
  await Bun.write(join(manifests, "coverage.json"), JSON.stringify({
    generated: summary.generated, byCategory: cov.byCategory,
    concretePaths: cov.concrete, templates: cov.templateMatches,
  }, null, 1));
  await Bun.write(join(manifests, "report.json"), JSON.stringify(summary, null, 1));

  console.log("");
  console.log(`client references: ${summary.clientReferences} (${summary.clientTemplates} templates), animals: ${summary.animalsInClientTable}`);
  console.log(`archived image captures: ${summary.imageCaptures} -> ${summary.distinctUrls} URLs, ${summary.urlsWithImage} with a decodable image`);
  console.log(`versions stored: ${summary.versionsStored} (${summary.versionsDecodedOk} decode cleanly, ${summary.digestVerified} digest-verified), rejected: ${summary.rejectedNotImages}, failed URLs: ${summary.failedUrls}`);
  printCategories("archived images", summary.byCategory);
  console.log("\ncoverage of the client's references:");
  for (const c of cov.byCategory) {
    console.log(`  ${c.category.padEnd(18)} templates ${String(c.templatesWithArchivedFiles).padStart(4)}/${String(c.templates).padEnd(4)}` +
                `  exact paths ${String(c.concretePathsArchived).padStart(4)}/${String(c.concretePaths).padEnd(4)}  files ${c.archivedFiles}`);
  }
  console.log(`\noutput: ${out}`);
}

function groupCount<T>(items: T[], key: (item: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const item of items) out[key(item)] = (out[key(item)] ?? 0) + 1;
  return out;
}

function printCategories(title: string, counts: Record<string, number>) {
  console.log(`${title} by category:`);
  for (const [k, v] of Object.entries(counts).sort((a, b) => b[1] - a[1])) console.log(`  ${k.padEnd(18)} ${v}`);
}

async function writeReadOnly(path: string, bytes: Uint8Array) {
  await mkdir(dirname(path), { recursive: true });
  const file = Bun.file(path);
  if (await file.exists()) {
    if (sha256(new Uint8Array(await file.arrayBuffer())) === sha256(bytes)) return;
    await chmod(path, 0o644);
  }
  await Bun.write(path, bytes);
  await chmod(path, 0o444);
}

if (import.meta.main) {
  main().catch((error) => {
    console.error(`error: ${(error as Error).message}`);
    process.exit(1);
  });
}
