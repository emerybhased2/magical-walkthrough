/**
 * Image decoding and integrity checks for archived files.
 *
 * Every format is parsed from its own structure, not trusted from a file extension or a
 * Content-Type header:
 *   PNG   signature, every chunk CRC, IHDR, IEND, and the full zlib stream of the IDAT data
 *         inflated and checked against the size IHDR implies (incl. Adam7 interlacing) and
 *         every row's filter byte (0..4). APNG frames are counted.
 *   JPEG  SOI, segment walk to the SOF header (size, components, progressive), EOI present
 *   GIF   header, logical screen, every block walked to the trailer, frame count
 *   WebP  RIFF size, VP8 / VP8L / VP8X headers
 *   SVG   XML text with an <svg> root; width/height/viewBox read
 *   ICO   directory entries
 * Archived "images" that are really HTML (soft 404s, error pages) are reported as such.
 */

import { inflateSync } from "node:zlib";

export type ImageInfo = {
  format: string;            // png, jpeg, gif, webp, svg, ico, html, unknown
  ok: boolean;               // decodes completely without integrity problems
  width?: number;
  height?: number;
  frames?: number;           // animated GIF / APNG
  details: Record<string, unknown>;
  problems: string[];
};

const PNG_SIGNATURE = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array, start = 0, end = data.length): number {
  let c = 0xffffffff;
  for (let i = start; i < end; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function u32be(b: Uint8Array, o: number): number {
  return ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0;
}
function u16be(b: Uint8Array, o: number): number {
  return (b[o] << 8) | b[o + 1];
}
function u16le(b: Uint8Array, o: number): number {
  return b[o] | (b[o + 1] << 8);
}
function u24le(b: Uint8Array, o: number): number {
  return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16);
}
function ascii(b: Uint8Array, start: number, length: number): string {
  return String.fromCharCode(...b.subarray(start, start + length));
}

const PNG_CHANNELS: Record<number, number> = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 };
const ADAM7 = [[0, 0, 8, 8], [4, 0, 8, 8], [0, 4, 4, 8], [2, 0, 4, 4], [0, 2, 2, 4], [1, 0, 2, 2], [0, 1, 1, 2]];

/** Sub-images (width, height) whose rows are stored in the IDAT stream. */
export function pngPasses(width: number, height: number, interlace: number): Array<[number, number]> {
  if (!interlace) return [[width, height]];
  return ADAM7.map(([x0, y0, dx, dy]) => [
    width > x0 ? Math.ceil((width - x0) / dx) : 0,
    height > y0 ? Math.ceil((height - y0) / dy) : 0,
  ] as [number, number]).filter(([w, h]) => w > 0 && h > 0);
}

export function decodePng(data: Uint8Array): ImageInfo {
  const info: ImageInfo = { format: "png", ok: false, details: {}, problems: [] };
  for (let i = 0; i < 8; i++) {
    if (data[i] !== PNG_SIGNATURE[i]) {
      info.problems.push("bad PNG signature");
      return info;
    }
  }
  let offset = 8;
  let ihdr: { width: number; height: number; bitDepth: number; colorType: number; interlace: number } | null = null;
  const idat: Uint8Array[] = [];
  let sawEnd = false;
  let frames = 0;
  const chunkTypes: string[] = [];
  while (offset + 12 <= data.length) {
    const length = u32be(data, offset);
    const type = ascii(data, offset + 4, 4);
    const end = offset + 12 + length;
    if (end > data.length) {
      info.problems.push(`chunk ${type} truncated`);
      break;
    }
    const stored = u32be(data, offset + 8 + length);
    if (crc32(data, offset + 4, offset + 8 + length) !== stored) info.problems.push(`CRC mismatch in ${type}`);
    const body = data.subarray(offset + 8, offset + 8 + length);
    if (!chunkTypes.includes(type)) chunkTypes.push(type);
    if (type === "IHDR") {
      ihdr = { width: u32be(body, 0), height: u32be(body, 4), bitDepth: body[8], colorType: body[9], interlace: body[12] };
    } else if (type === "IDAT") {
      idat.push(body);
    } else if (type === "acTL") {
      frames = u32be(body, 0);
    } else if (type === "IEND") {
      sawEnd = true;
      offset = end;
      break;
    }
    offset = end;
  }
  if (!ihdr) {
    info.problems.push("no IHDR chunk");
    return info;
  }
  info.width = ihdr.width;
  info.height = ihdr.height;
  info.details = { bitDepth: ihdr.bitDepth, colorType: ihdr.colorType, interlaced: !!ihdr.interlace, chunks: chunkTypes };
  if (frames) info.frames = frames;
  if (!sawEnd) info.problems.push("no IEND chunk (truncated file)");
  if (offset < data.length) info.details.trailingBytes = data.length - offset;
  const channels = PNG_CHANNELS[ihdr.colorType];
  if (!channels) {
    info.problems.push(`invalid colour type ${ihdr.colorType}`);
    return info;
  }
  if (!idat.length) {
    info.problems.push("no IDAT data");
    return info;
  }
  let raw: Uint8Array;
  try {
    const joined = new Uint8Array(idat.reduce((n, c) => n + c.length, 0));
    let p = 0;
    for (const c of idat) {
      joined.set(c, p);
      p += c.length;
    }
    raw = new Uint8Array(inflateSync(joined));
  } catch (error) {
    info.problems.push(`image data does not inflate: ${(error as Error).message}`);
    return info;
  }
  const bitsPerPixel = channels * ihdr.bitDepth;
  let expected = 0;
  let pos = 0;
  let badFilter = 0;
  for (const [w, h] of pngPasses(ihdr.width, ihdr.height, ihdr.interlace)) {
    const rowBytes = Math.ceil((w * bitsPerPixel) / 8);
    expected += h * (1 + rowBytes);
    for (let row = 0; row < h && pos < raw.length; row++) {
      if (raw[pos] > 4) badFilter++;
      pos += 1 + rowBytes;
    }
  }
  info.details.decodedBytes = raw.length;
  if (raw.length !== expected) info.problems.push(`decoded ${raw.length} bytes, IHDR implies ${expected}`);
  if (badFilter) info.problems.push(`${badFilter} rows with an invalid filter type`);
  info.ok = info.problems.length === 0;
  return info;
}

const SOF_MARKERS = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

export function decodeJpeg(data: Uint8Array): ImageInfo {
  const info: ImageInfo = { format: "jpeg", ok: false, details: {}, problems: [] };
  if (data[0] !== 0xff || data[1] !== 0xd8) {
    info.problems.push("no SOI marker");
    return info;
  }
  let offset = 2;
  while (offset + 4 <= data.length) {
    if (data[offset] !== 0xff) {
      info.problems.push(`expected a marker at byte ${offset}`);
      break;
    }
    const marker = data[offset + 1];
    if (marker === 0xff) {
      offset++;
      continue;
    }
    if (marker === 0xd8 || (marker >= 0xd0 && marker <= 0xd7) || marker === 0x01) {
      offset += 2;
      continue;
    }
    const length = u16be(data, offset + 2);
    if (SOF_MARKERS.has(marker)) {
      info.height = u16be(data, offset + 5);
      info.width = u16be(data, offset + 7);
      info.details = { components: data[offset + 9], precision: data[offset + 4], progressive: marker === 0xc2 || marker === 0xc6 };
    }
    if (marker === 0xda) break;   // start of scan: entropy-coded data follows
    offset += 2 + length;
  }
  if (info.width === undefined) info.problems.push("no SOF header");
  let end = data.length;
  while (end > 2 && (data[end - 1] === 0x00 || data[end - 1] === 0x0a || data[end - 1] === 0x0d)) end--;
  if (!(data[end - 2] === 0xff && data[end - 1] === 0xd9)) info.problems.push("no EOI marker (truncated file)");
  info.ok = info.problems.length === 0 && !!info.width && !!info.height;
  return info;
}

export function decodeGif(data: Uint8Array): ImageInfo {
  const info: ImageInfo = { format: "gif", ok: false, details: {}, problems: [] };
  const version = ascii(data, 0, 6);
  if (version !== "GIF87a" && version !== "GIF89a") {
    info.problems.push("bad GIF header");
    return info;
  }
  info.width = u16le(data, 6);
  info.height = u16le(data, 8);
  const packed = data[10];
  let offset = 13 + (packed & 0x80 ? 3 * (1 << ((packed & 7) + 1)) : 0);
  let frames = 0;
  let trailer = false;
  const skipSubBlocks = () => {
    while (offset < data.length && data[offset] !== 0) offset += data[offset] + 1;
    offset++;
  };
  while (offset < data.length) {
    const block = data[offset];
    if (block === 0x3b) {
      trailer = true;
      break;
    } else if (block === 0x21) {
      offset += 2;
      skipSubBlocks();
    } else if (block === 0x2c) {
      frames++;
      const local = data[offset + 9];
      offset += 10 + (local & 0x80 ? 3 * (1 << ((local & 7) + 1)) : 0);
      offset++;   // LZW minimum code size
      skipSubBlocks();
    } else {
      info.problems.push(`unknown GIF block 0x${block.toString(16)} at ${offset}`);
      break;
    }
  }
  if (!trailer) info.problems.push("no GIF trailer (truncated file)");
  if (!frames) info.problems.push("no image frames");
  info.frames = frames;
  info.details = { version };
  info.ok = info.problems.length === 0;
  return info;
}

export function decodeWebp(data: Uint8Array): ImageInfo {
  const info: ImageInfo = { format: "webp", ok: false, details: {}, problems: [] };
  if (ascii(data, 0, 4) !== "RIFF" || ascii(data, 8, 4) !== "WEBP") {
    info.problems.push("bad RIFF/WEBP header");
    return info;
  }
  const riffSize = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24);
  if (riffSize + 8 > data.length) info.problems.push("RIFF size exceeds the file (truncated)");
  const chunk = ascii(data, 12, 4);
  const body = 20;
  info.details = { chunk };
  if (chunk === "VP8 ") {
    if (data[body + 3] !== 0x9d || data[body + 4] !== 0x01 || data[body + 5] !== 0x2a) info.problems.push("bad VP8 start code");
    info.width = u16le(data, body + 6) & 0x3fff;
    info.height = u16le(data, body + 8) & 0x3fff;
  } else if (chunk === "VP8L") {
    if (data[body] !== 0x2f) info.problems.push("bad VP8L signature");
    const bits = data[body + 1] | (data[body + 2] << 8) | (data[body + 3] << 16) | (data[body + 4] << 24);
    info.width = (bits & 0x3fff) + 1;
    info.height = ((bits >>> 14) & 0x3fff) + 1;
  } else if (chunk === "VP8X") {
    info.width = u24le(data, body + 4) + 1;
    info.height = u24le(data, body + 7) + 1;
    info.details.animated = !!(data[body] & 0x02);
  } else {
    info.problems.push(`unknown WebP chunk ${chunk}`);
  }
  info.ok = info.problems.length === 0;
  return info;
}

function svgLength(value: string | undefined): number | undefined {
  if (!value) return undefined;
  const match = /^\s*([0-9.]+)\s*(px)?\s*$/.exec(value);
  return match ? Number(match[1]) : undefined;
}

export function decodeSvg(text: string): ImageInfo {
  const info: ImageInfo = { format: "svg", ok: false, details: {}, problems: [] };
  const root = /<svg\b([^>]*)>/i.exec(text);
  if (!root) {
    info.problems.push("no <svg> element");
    return info;
  }
  const attr = (name: string) => new RegExp(`\\s${name}\\s*=\\s*["']([^"']*)["']`, "i").exec(root[1])?.[1];
  const viewBox = attr("viewBox");
  info.width = svgLength(attr("width"));
  info.height = svgLength(attr("height"));
  if ((info.width === undefined || info.height === undefined) && viewBox) {
    const parts = viewBox.trim().split(/[\s,]+/).map(Number);
    if (parts.length === 4) {
      info.width ??= parts[2];
      info.height ??= parts[3];
    }
  }
  info.details = { viewBox: viewBox ?? null };
  if (!/<\/svg\s*>\s*$/i.test(text.trim())) info.problems.push("no closing </svg> (truncated file)");
  info.ok = info.problems.length === 0;
  return info;
}

export function decodeIco(data: Uint8Array): ImageInfo {
  const info: ImageInfo = { format: "ico", ok: false, details: {}, problems: [] };
  const count = u16le(data, 4);
  if (u16le(data, 0) !== 0 || u16le(data, 2) !== 1 || !count) {
    info.problems.push("bad ICO header");
    return info;
  }
  const sizes: string[] = [];
  for (let i = 0; i < count; i++) {
    const e = 6 + 16 * i;
    if (e + 16 > data.length) {
      info.problems.push("ICO directory truncated");
      break;
    }
    const w = data[e] || 256;
    const h = data[e + 1] || 256;
    const size = data[e + 8] | (data[e + 9] << 8) | (data[e + 10] << 16) | (data[e + 11] << 24);
    const at = data[e + 12] | (data[e + 13] << 8) | (data[e + 14] << 16) | (data[e + 15] << 24);
    if (at + size > data.length) info.problems.push(`ICO image ${i} exceeds the file`);
    sizes.push(`${w}x${h}`);
    if (i === 0) {
      info.width = w;
      info.height = h;
    }
  }
  info.details = { images: sizes };
  info.ok = info.problems.length === 0;
  return info;
}

/** Detect the real format from the bytes and decode it. */
export function decodeImage(data: Uint8Array): ImageInfo {
  if (data.length >= 8 && data[0] === 0x89 && data[1] === 0x50) return decodePng(data);
  if (data.length >= 4 && data[0] === 0xff && data[1] === 0xd8) return decodeJpeg(data);
  if (data.length >= 6 && ascii(data, 0, 3) === "GIF") return decodeGif(data);
  if (data.length >= 16 && ascii(data, 0, 4) === "RIFF") return decodeWebp(data);
  if (data.length >= 6 && data[0] === 0 && data[1] === 0 && data[2] === 1 && data[3] === 0) return decodeIco(data);
  const head = new TextDecoder("utf-8", { fatal: false }).decode(data.subarray(0, 2048)).replace(/^﻿/, "").trimStart();
  if (/^(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*(<!DOCTYPE svg[^>]*>\s*)?<svg\b/i.test(head)) {
    return decodeSvg(new TextDecoder().decode(data));
  }
  if (/^<!DOCTYPE html|^<html|^<head|^<body/i.test(head)) {
    return { format: "html", ok: false, details: {}, problems: ["the archived file is an HTML page, not an image"] };
  }
  return { format: "unknown", ok: false, details: {}, problems: ["unrecognised file format"] };
}

export const EXTENSIONS: Record<string, string> = { png: ".png", jpeg: ".jpg", gif: ".gif", webp: ".webp", svg: ".svg", ico: ".ico" };
