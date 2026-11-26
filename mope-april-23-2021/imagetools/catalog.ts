/** Match archived image URLs to the client's references: category, animal and coverage. */

import { categorise, templateRegex, type AnimalInfo, type ImageRef } from "./client_refs.ts";

export type ArchivedPath = {
  key: string;            // host + path, the storage key
  host: string;
  path: string;           // without query / fragment, no leading slash
  layout: "season" | "theme" | "root";
  season: string | null;  // assets/s/<season>/
  theme: string | null;   // assets/t/<theme>/
  rel: string;            // path inside the season/theme folder (or the whole path for the root layout)
};

export type UniqueTemplate = {
  template: string;
  alternatives: string[];
  holes: string[];
  category: string;
  base: ImageRef["base"];
  via: string[];
  lines: number[];
  regexes: RegExp[];
  literalChars: number;
};

export function splitArchivedUrl(url: string): ArchivedPath {
  const parsed = new URL(url.replace(/^http:\/\//i, "https://"));
  const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
  const path = decodeURIComponent(parsed.pathname).replace(/^\/+/, "");
  const season = /^assets\/s\/([^/]+)\/(.+)$/.exec(path);
  const theme = /^assets\/t\/([^/]+)\/(.+)$/.exec(path);
  return {
    key: `${host}/${path}`,
    host,
    path,
    layout: season ? "season" : theme ? "theme" : "root",
    season: season?.[1] ?? null,
    theme: theme?.[1] ?? null,
    rel: season?.[2] ?? theme?.[2] ?? path,
  };
}

export function uniqueTemplates(references: ImageRef[]): UniqueTemplate[] {
  const map = new Map<string, UniqueTemplate>();
  for (const ref of references) {
    const id = `${ref.category}|${ref.template}`;
    let entry = map.get(id);
    if (!entry) {
      const regexes = ref.alternatives.map(templateRegex).filter((r): r is RegExp => r !== null);
      entry = {
        template: ref.template, alternatives: ref.alternatives, holes: ref.holes, category: ref.category, base: ref.base,
        via: [], lines: [], regexes,
        literalChars: ref.template.replace(/\{[^}]*\}/g, "").length,
      };
      map.set(id, entry);
    }
    if (!entry.via.includes(ref.via)) entry.via.push(ref.via);
    entry.lines.push(ref.line);
  }
  return [...map.values()];
}

/** The most specific client template that can produce this archived path (fewest holes, most literal text). */
export function bestTemplate(rel: string, fullPath: string, templates: UniqueTemplate[]): UniqueTemplate | null {
  let best: UniqueTemplate | null = null;
  for (const t of templates) {
    const target = t.base === "absolute" ? fullPath : rel;
    if (!t.regexes.some((r) => r.test(target))) continue;
    if (!best || t.holes.length < best.holes.length
      || (t.holes.length === best.holes.length && t.literalChars > best.literalChars)) best = t;
  }
  return best;
}

export function animalFor(rel: string, animals: AnimalInfo[]): AnimalInfo | null {
  if (!/^(skins|premium)\//.test(rel)) return null;
  const names = new Map<string, AnimalInfo>();
  for (const a of animals) {
    if (a.skinName) names.set(a.skinName.toLowerCase(), a);
  }
  for (const a of animals) {
    const n = a.aniName.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (!names.has(n)) names.set(n, a);
  }
  const segments = rel.toLowerCase().split("/");
  for (const segment of segments.slice(1, -1)) {
    if (names.has(segment)) return names.get(segment)!;
  }
  const base = segments[segments.length - 1].replace(/\.[a-z0-9]+$/, "");
  for (let cut = base.length; cut >= 3; cut--) {
    const candidate = base.slice(0, cut).replace(/[_-]+$/, "");
    if (names.has(candidate)) return names.get(candidate)!;
  }
  return null;
}

export function categoryFor(archived: ArchivedPath, template: UniqueTemplate | null): string {
  if (template) return template.category;
  return categorise(archived.path, {
    via: "archive",
    base: archived.layout === "root" ? "root" : "season",
    edibleVariant: /_e\.(png|jpe?g|gif|webp)$/i.test(archived.rel),
  });
}

export type Coverage = {
  category: string;
  templates: number;
  templatesWithArchivedFiles: number;
  concretePaths: number;          // alternatives without holes (exact file names the client requests)
  concretePathsArchived: number;
  archivedFiles: number;
};

export function coverage(templates: UniqueTemplate[], archived: Array<{ path: ArchivedPath; category: string }>): {
  byCategory: Coverage[];
  concrete: Array<{ path: string; category: string; archived: boolean; lines: number[] }>;
  templateMatches: Array<{ template: string; category: string; lines: number[]; archived: string[] }>;
} {
  const relSet = new Set(archived.map((a) => a.path.rel));
  const fullSet = new Set(archived.map((a) => a.path.path));
  const byCategory = new Map<string, Coverage>();
  const get = (category: string) => byCategory.get(category) ?? byCategory.set(category, {
    category, templates: 0, templatesWithArchivedFiles: 0, concretePaths: 0, concretePathsArchived: 0, archivedFiles: 0,
  }).get(category)!;
  const concrete: Array<{ path: string; category: string; archived: boolean; lines: number[] }> = [];
  const templateMatches = [];
  for (const t of templates) {
    const c = get(t.category);
    c.templates++;
    const matches = archived.filter((a) => t.regexes.some((r) => r.test(t.base === "absolute" ? a.path.path : a.path.rel)))
      .map((a) => a.path.key);
    if (matches.length) c.templatesWithArchivedFiles++;
    templateMatches.push({ template: t.template, category: t.category, lines: t.lines, archived: [...new Set(matches)] });
    for (const alt of t.alternatives) {
      if (alt.includes("{")) continue;
      const clean = alt.split("?")[0];
      const found = t.base === "absolute" ? fullSet.has(clean) : relSet.has(clean);
      c.concretePaths++;
      if (found) c.concretePathsArchived++;
      concrete.push({ path: clean, category: t.category, archived: found, lines: t.lines });
    }
  }
  for (const a of archived) get(a.category).archivedFiles++;
  return { byCategory: [...byCategory.values()].sort((a, b) => a.category.localeCompare(b.category)), concrete, templateMatches };
}
