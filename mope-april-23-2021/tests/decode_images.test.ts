/** Offline tests for decode_images.ts (run: bun test tests/). Fixtures are generated in the tests. */

import { describe, expect, test } from "bun:test";
import { deflateSync } from "node:zlib";
import { crc32, decodeImage, decodePng, pngPasses } from "../imagetools/formats.ts";
import { analyseClient, categorise, templateRegex } from "../imagetools/client_refs.ts";
import { animalFor, bestTemplate, coverage, splitArchivedUrl, uniqueTemplates } from "../imagetools/catalog.ts";
import { digestMatches, isImageCapture, sha1Base32, surtHost } from "../imagetools/archives.ts";
import { dechunk, parseWarcRecord } from "../imagetools/warc.ts";
import { safeRelative } from "../decode_images.ts";

// -- fixtures -------------------------------------------------------------------------------------
function chunk(type: string, body: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + body.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, body.length);
  out.set(new TextEncoder().encode(type), 4);
  out.set(body, 8);
  view.setUint32(8 + body.length, crc32(out, 4, 8 + body.length));
  return out;
}

function png(width: number, height: number, opts: { interlace?: number; colorType?: number; corruptCrc?: boolean; dropEnd?: boolean; rawExtra?: number } = {}) {
  const colorType = opts.colorType ?? 6;
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType]!;
  const ihdr = new Uint8Array(13);
  const v = new DataView(ihdr.buffer);
  v.setUint32(0, width);
  v.setUint32(4, height);
  ihdr[8] = 8;
  ihdr[9] = colorType;
  ihdr[12] = opts.interlace ?? 0;
  let size = 0;
  for (const [w, h] of pngPasses(width, height, opts.interlace ?? 0)) size += h * (1 + w * channels);
  const raw = new Uint8Array(size + (opts.rawExtra ?? 0));   // filter 0 rows of zero pixels
  const parts = [Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr),
    chunk("IDAT", new Uint8Array(deflateSync(raw)))];
  if (!opts.dropEnd) parts.push(chunk("IEND", new Uint8Array()));
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  if (opts.corruptCrc) out[8 + 8 + 13] ^= 0xff;
  return out;
}

const bytes = (...values: number[]) => Uint8Array.from(values);

// -- formats ----------------------------------------------------------------------------------------
describe("image decoding", () => {
  test("PNG decodes fully, with dimensions", () => {
    const info = decodePng(png(5, 3));
    expect(info.ok).toBe(true);
    expect([info.width, info.height]).toEqual([5, 3]);
    expect(info.details.decodedBytes).toBe(3 * (1 + 5 * 4));
  });

  test("interlaced PNG sizes follow Adam7", () => {
    expect(pngPasses(1, 1, 1)).toEqual([[1, 1]]);
    expect(pngPasses(8, 8, 1).reduce((n, [w, h]) => n + w * h, 0)).toBe(64);
    expect(decodePng(png(9, 7, { interlace: 1, colorType: 2 })).ok).toBe(true);
  });

  test("PNG problems are reported", () => {
    expect(decodePng(png(4, 4, { corruptCrc: true })).problems.join()).toContain("CRC mismatch in IHDR");
    expect(decodePng(png(4, 4, { dropEnd: true })).problems.join()).toContain("no IEND");
    expect(decodePng(png(4, 4, { rawExtra: 3 })).problems.join()).toContain("IHDR implies");
    const truncated = png(4, 4).subarray(0, 40);
    expect(decodePng(truncated).ok).toBe(false);
  });

  test("JPEG header and EOI", () => {
    const sof = [0xff, 0xc0, 0x00, 0x11, 0x08, 0x00, 0x20, 0x00, 0x40, 0x03, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1];
    const sos = [0xff, 0xda, 0x00, 0x0c, 0x03, 1, 0, 2, 0x11, 3, 0x11, 0, 0x3f, 0];
    const ok = decodeImage(bytes(0xff, 0xd8, ...sof, ...sos, 0x12, 0x34, 0xff, 0xd9));
    expect(ok.format).toBe("jpeg");
    expect([ok.width, ok.height, ok.ok]).toEqual([64, 32, true]);
    expect(decodeImage(bytes(0xff, 0xd8, ...sof, ...sos, 0x12)).problems.join()).toContain("EOI");
  });

  test("GIF frames and trailer", () => {
    const gif = bytes(...new TextEncoder().encode("GIF89a"), 2, 0, 3, 0, 0, 0, 0,
      0x2c, 0, 0, 0, 0, 2, 0, 3, 0, 0, 2, 2, 0x4c, 0x01, 0, 0x3b);
    const info = decodeImage(gif);
    expect([info.format, info.width, info.height, info.frames, info.ok]).toEqual(["gif", 2, 3, 1, true]);
    expect(decodeImage(gif.subarray(0, gif.length - 1)).ok).toBe(false);
  });

  test("WebP VP8L and SVG and ICO", () => {
    const w = 300 - 1, h = 200 - 1;
    const bits = w | (h << 14);
    const webp = bytes(...new TextEncoder().encode("RIFF"), 13, 0, 0, 0, ...new TextEncoder().encode("WEBPVP8L"),
      5, 0, 0, 0, 0x2f, bits & 255, (bits >> 8) & 255, (bits >> 16) & 255, (bits >>> 24) & 255);
    const info = decodeImage(webp);
    expect([info.format, info.width, info.height]).toEqual(["webp", 300, 200]);
    const svg = decodeImage(new TextEncoder().encode('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 12"><path d="M0 0"/></svg>'));
    expect([svg.format, svg.width, svg.height, svg.ok]).toEqual(["svg", 24, 12, true]);
    const ico = bytes(0, 0, 1, 0, 1, 0, 16, 16, 0, 0, 1, 0, 32, 0, 4, 0, 0, 0, 22, 0, 0, 0, 1, 2, 3, 4);
    expect(decodeImage(ico).format).toBe("ico");
  });

  test("HTML saved under an image URL is rejected", () => {
    const info = decodeImage(new TextEncoder().encode("<!DOCTYPE html><html><body>404</body></html>"));
    expect(info.format).toBe("html");
    expect(info.ok).toBe(false);
  });
});

// -- client analysis ------------------------------------------------------------------------------------
const CLIENT = `
var _0xa = 5, _0xb = 'peacock', _0xflag = 2;
var loader = function (p) { var i = new Image(); var q = '?v=' + 1; i.src = p + q; return i; };
var _0xtable = function (t) { var o = {}; switch (t) {
  case _0xa: o.aniName = 'Deer', o.skinName = 'deer'; break;
  case 6: o.aniName = 'Mole', o.skinName = 'mole'; break; } return o; };
function Base() {}
Base.prototype.getSpeciesPath = function (n) { return n + '/' + this.animalSpecies; };
Base.prototype.getWing = function (n, i, s) { var p = 'skins/' + (s ? this.getSpeciesPath(n) + '/' : n + '/'); return p += n + '_wing' + i + '.png', p; };
function Bird() {}
Bird.prototype = Object.create(Base.prototype);
Bird.prototype.draw = function () {
  this.getSkinImageObj(this.getWing(_0xb, 1, true));
  this.getSkinImageObj('img/coconut' + (this.isEdibleOutlined() ? '_e' : '') + '.png');
  var local = 'gameobj/' + this.oType + '/0.png';
  this.getSkinImageObj(local);
  loader('img/sound_on.png');
  this.getImageObjForPath('img/dirt/' + this.eff + '/1.png');
};
_0xflag = 3;
`;

describe("client image references", () => {
  const catalog = analyseClient(CLIENT, "fixture.js");
  const byTemplate = new Map(catalog.references.map((r) => [r.template, r]));

  test("loaders, constants and the animal table", () => {
    expect(catalog.loaders).toEqual(["loader"]);
    expect(catalog.animals.map((a) => [a.id, a.aniName, a.skinName])).toEqual([[5, "Deer", "deer"], [6, "Mole", "mole"]]);
  });

  test("helpers are inlined through the prototype chain", () => {
    const wing = byTemplate.get("skins/peacock/{animalSpecies}/peacock_wing1.png");
    expect(wing?.via).toBe("getSkinImageObj");
    expect(wing?.base).toBe("season");
    expect(wing?.category).toBe("animals");
  });

  test("ternaries expand to alternatives and mark edible variants", () => {
    const food = [...byTemplate.values()].find((r) => r.template.startsWith("img/coconut"));
    expect(food?.alternatives.sort()).toEqual(["img/coconut.png", "img/coconut_e.png"]);
    expect(food?.category).toBe("food");
  });

  test("single-definition locals are resolved; loader paths are root-relative", () => {
    expect(byTemplate.get("gameobj/{oType}/0.png")?.via).toBe("getSkinImageObj");
    expect(byTemplate.get("img/sound_on.png")?.base).toBe("root");
    expect(byTemplate.get("img/dirt/{eff}/1.png")?.category).toBe("world-and-effects");
  });

  test("reassigned variables are not constants", () => {
    expect([...byTemplate.keys()].some((t) => t.includes("2") && t.includes("flag"))).toBe(false);
  });
});

// -- catalogue and coverage ------------------------------------------------------------------------------
describe("catalogue", () => {
  test("archived URLs split into layout, season and relative path", () => {
    const a = splitArchivedUrl("http://www.mope.io/assets/s/1/skins/land/mouse/0/mouse.png?v=296");
    expect([a.host, a.layout, a.season, a.rel]).toEqual(["mope.io", "season", "1", "skins/land/mouse/0/mouse.png"]);
    expect(splitArchivedUrl("https://mope.io/skins/bear.png").layout).toBe("root");
    expect(splitArchivedUrl("https://mope.io/assets/t/4/skins/fox.png").theme).toBe("4");
  });

  test("generic templates do not claim files", () => {
    expect(templateRegex("{getSkinImagePath()}.png")).toBeNull();
    expect(templateRegex("img/{x}.png")).toBeNull();
    expect(templateRegex("assets/s/{season}/{path()}")).toBeNull();
    expect(templateRegex("skins/land/peacock/{animalSpecies}/peacock_wing1.png")!.test("skins/land/peacock/3/peacock_wing1.png")).toBe(true);
  });

  test("best template, animal and coverage", () => {
    const catalog = analyseClient(CLIENT);
    const templates = uniqueTemplates(catalog.references);
    const path = splitArchivedUrl("https://mope.io/assets/s/1/img/coconut_e.png");
    expect(bestTemplate(path.rel, path.path, templates)?.category).toBe("food");
    expect(animalFor("skins/arctic/deer/2/deer_head.png", catalog.animals)?.aniName).toBe("Deer");
    expect(animalFor("skins/mole.png", catalog.animals)?.aniName).toBe("Mole");
    const cov = coverage(templates, [{ path, category: "food" }]);
    const food = cov.byCategory.find((c) => c.category === "food")!;
    expect([food.concretePaths, food.concretePathsArchived, food.archivedFiles]).toEqual([2, 1, 1]);
  });

  test("categories for archive-only paths", () => {
    const base = { via: "archive", base: "root", edibleVariant: false };
    expect(categorise("cdn-cgi/images/x.png", base)).toBe("cloudflare");
    expect(categorise("shop/left.png", base)).toBe("shop-ui");
    expect(categorise("img/update/promo3.png", base)).toBe("promo");
    expect(categorise("img/logo.png", base)).toBe("ui");
    expect(categorise("skins/bear.png", base)).toBe("animals");
  });

  test("storage paths cannot leave the output folder", () => {
    expect(safeRelative("mope.io/../../etc/passwd")).toBe("mope.io/etc/passwd");
    expect(safeRelative("mope.io/a:b?.png")).toBe("mope.io/a_b_.png");
  });
});

// -- archives and WARC ---------------------------------------------------------------------------------------
describe("archives", () => {
  test("sha1 base32 digests", () => {
    const data = new TextEncoder().encode("abc");
    expect(sha1Base32(data)).toBe("VGMT4NSHA2AWVOR6EVYXQUGCNSONBWE5");
    expect(digestMatches(data, "sha1:VGMT4NSHA2AWVOR6EVYXQUGCNSONBWE5")).toBe(true);
    expect(digestMatches(data, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")).toBe(false);
    expect(digestMatches(data, "-")).toBeNull();
  });

  test("image capture filter and SURT host", () => {
    expect(isImageCapture("https://mope.io/a/b.PNG?v=1", "text/html")).toBe(true);
    expect(isImageCapture("https://mope.io/x", "image/webp")).toBe(true);
    expect(isImageCapture("https://mope.io/client.js", "application/javascript")).toBe(false);
    expect(surtHost("www.mope.io")).toBe("io,mope");
  });

  test("WARC response record with chunked payload", () => {
    const body = "4\r\nPNG!\r\n0\r\n\r\n";
    const http = `HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nTransfer-Encoding: chunked\r\n\r\n${body}`;
    const warc = `WARC/1.0\r\nWARC-Type: response\r\nWARC-Date: 2021-04-17T07:27:25Z\r\nContent-Length: ${http.length}\r\n\r\n${http}\r\n\r\n`;
    const record = parseWarcRecord(new TextEncoder().encode(warc));
    expect(record.warcType).toBe("response");
    expect(record.httpStatus).toBe(200);
    expect(new TextDecoder().decode(record.body)).toBe("PNG!");
    expect(new TextDecoder().decode(dechunk(new TextEncoder().encode("3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")))).toBe("abcde");
  });
});
