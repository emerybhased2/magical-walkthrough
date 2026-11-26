/** Minimal WARC response-record parser (one decompressed record, as range-read from Common Crawl). */

import { gunzipSync, inflateSync } from "node:zlib";

export type WarcRecord = {
  warcType: string;
  headers: Record<string, string>;
  httpStatus: number | null;
  httpHeaders: Record<string, string>;
  payload: Uint8Array;   // HTTP entity as stored (after de-chunking)
  body: Uint8Array;      // payload with any Content-Encoding removed
  notes: string[];
};

function indexOfCrlfCrlf(data: Uint8Array, from = 0): number {
  for (let i = from; i + 3 < data.length; i++) {
    if (data[i] === 13 && data[i + 1] === 10 && data[i + 2] === 13 && data[i + 3] === 10) return i;
  }
  return -1;
}

function parseHeaderBlock(text: string): [string, Record<string, string>] {
  const lines = text.split("\r\n");
  const headers: Record<string, string> = {};
  for (const line of lines.slice(1)) {
    const colon = line.indexOf(":");
    if (colon > 0) headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
  }
  return [lines[0], headers];
}

export function dechunk(data: Uint8Array): Uint8Array {
  const parts: Uint8Array[] = [];
  let pos = 0;
  while (pos < data.length) {
    let lineEnd = pos;
    while (lineEnd + 1 < data.length && !(data[lineEnd] === 13 && data[lineEnd + 1] === 10)) lineEnd++;
    const size = parseInt(new TextDecoder().decode(data.subarray(pos, lineEnd)).split(";")[0].trim(), 16);
    if (!Number.isFinite(size)) throw new Error("bad chunk size");
    if (size === 0) break;
    parts.push(data.subarray(lineEnd + 2, lineEnd + 2 + size));
    pos = lineEnd + 2 + size + 2;
  }
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

export function parseWarcRecord(record: Uint8Array): WarcRecord {
  const notes: string[] = [];
  const headerEnd = indexOfCrlfCrlf(record);
  if (headerEnd < 0) throw new Error("no WARC header block");
  const [version, headers] = parseHeaderBlock(new TextDecoder("latin1").decode(record.subarray(0, headerEnd)));
  if (!version.startsWith("WARC/")) throw new Error("not a WARC record");
  const contentLength = Number(headers["content-length"] ?? record.length - headerEnd - 4);
  const block = record.subarray(headerEnd + 4, headerEnd + 4 + contentLength);
  const warcType = headers["warc-type"] ?? "";
  const result: WarcRecord = { warcType, headers, httpStatus: null, httpHeaders: {}, payload: block, body: block, notes };
  if (warcType !== "response" && warcType !== "revisit") return result;
  const httpEnd = indexOfCrlfCrlf(block);
  if (httpEnd < 0) return result;
  const [statusLine, httpHeaders] = parseHeaderBlock(new TextDecoder("latin1").decode(block.subarray(0, httpEnd)));
  result.httpStatus = Number(statusLine.split(" ")[1]);
  result.httpHeaders = httpHeaders;
  let payload = block.subarray(httpEnd + 4);
  if ((httpHeaders["transfer-encoding"] ?? "").toLowerCase().includes("chunked")) {
    try {
      payload = dechunk(payload);
      notes.push("de-chunked");
    } catch {
      notes.push("chunked encoding could not be removed");
    }
  }
  result.payload = payload;
  const encoding = (httpHeaders["content-encoding"] ?? "").toLowerCase();
  if (encoding === "gzip" || encoding === "x-gzip") {
    try {
      result.body = new Uint8Array(gunzipSync(payload));
      notes.push("gzip content-encoding removed");
    } catch {
      result.body = payload;
    }
  } else if (encoding === "deflate") {
    try {
      result.body = new Uint8Array(inflateSync(payload));
      notes.push("deflate content-encoding removed");
    } catch {
      result.body = payload;
    }
  } else {
    result.body = payload;
  }
  return result;
}
