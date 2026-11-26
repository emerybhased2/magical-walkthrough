/**
 * Decode every image reference in the readable client (static analysis, no client code runs).
 *
 * The 2025 client loads images through a few helpers (see images/README.md):
 *   loader(path)                       root-relative + "?v=<version>"            (L2331, L2348)
 *   obj.getSkinImageObj(rel)           ./assets/s/<mopeSeasonID>/<rel>  or ./assets/t/<theme>/<rel>
 *   obj.getImageObjForPath(rel, s)     ./assets/s/<season>/<rel>
 *   obj.getSkinImagePathForShop(rel)   ./assets/s/<season>/skins/<rel>
 * and builds <rel> from string concatenation, constants, ternaries and small path helpers
 * such as getWing()/getHead()/getSpeciesPath() on the animal base class.
 *
 * This module parses the client with acorn and evaluates those expressions symbolically:
 * literals, constants (vars assigned exactly once to a literal), ternaries (both branches),
 * template literals and calls to `this.<method>(...)` resolved through the prototype chain
 * (X.prototype = Object.create(Y.prototype)) and interpreted from their syntax tree.
 * Anything that depends on runtime state becomes a named hole: {animalSpecies}, {_0x46d086}.
 */

import * as acorn from "acorn";
import * as walk from "acorn-walk";

type Node = acorn.Node & Record<string, any>;

export const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|ico)(?=$|[?#'"])/i;
const HOLE_OPEN = "\u0001";
const HOLE_CLOSE = "\u0002";
const MAX_ALTERNATIVES = 64;

/** Helpers whose path argument is relative to ./assets/s/<season>/ (and themes, for skins). */
export const SEASON_HELPERS: Record<string, { base: string; themes: boolean }> = {
  getSkinImageObj: { base: "season", themes: true },
  getSkinImageObjAsync: { base: "season", themes: true },
  getSkinImagePath: { base: "season", themes: true },
  getSkinImageObjForSeason: { base: "season", themes: true },
  getObjImageObjForSeason: { base: "season", themes: false },
  getImageObjForPath: { base: "season", themes: false },
  getImagePath: { base: "season", themes: false },
  getSkinImagePathForShop: { base: "shop", themes: true },
};

export type ImageRef = {
  line: number;
  template: string;          // "skins/land/peacock/{animalSpecies}/{_0x46d086}.png"
  alternatives: string[];    // every expansion of literal choices (ternaries); holes kept as {name}
  holes: string[];
  via: string;               // helper or loader the path is given to, or "expression"
  base: "season" | "shop" | "absolute" | "root" | "inferred";
  themes: boolean;           // may also load from ./assets/t/<theme>/
  className: string | null;  // prototype the code belongs to
  category: string;
  edibleVariant: boolean;    // the client draws an "edible" outlined variant (_e) of this image
};

export type AnimalInfo = { id: number; constant: string; aniName: string; skinName: string | null; aniCol: string | null };

export type ClientCatalog = {
  source: string;
  lines: number;
  constants: number;
  classes: number;
  loaders: string[];
  references: ImageRef[];
  animals: AnimalInfo[];
};

// -- symbolic values -------------------------------------------------------------------

type Scalar = string | number | boolean | null | undefined;
type Value = Scalar[] | "unresolved";   // alternatives

const hole = (label: string) => `${HOLE_OPEN}${label}${HOLE_CLOSE}`;
const isHoleOnly = (v: Scalar) => typeof v === "string" && v.startsWith(HOLE_OPEN) && v.endsWith(HOLE_CLOSE)
  && v.indexOf(HOLE_OPEN, 1) < 0;

export function renderHoles(text: string): string {
  return text.replaceAll(HOLE_OPEN, "{").replaceAll(HOLE_CLOSE, "}");
}

function holeNames(text: string): string[] {
  return [...text.matchAll(/\u0001([^\u0002]*)\u0002/g)].map((m) => m[1]);
}

function combine(a: Value, b: Value, op: (x: Scalar, y: Scalar) => Scalar): Value {
  if (a === "unresolved" || b === "unresolved") return "unresolved";
  const out: Scalar[] = [];
  for (const x of a) for (const y of b) {
    const r = op(x, y);
    if (!out.includes(r)) out.push(r);
    if (out.length > MAX_ALTERNATIVES) return "unresolved";
  }
  return out;
}

function plus(x: Scalar, y: Scalar): Scalar {
  if (typeof x === "number" && typeof y === "number") return x + y;
  return String(x ?? "") + String(y ?? "");
}

// -- analysis --------------------------------------------------------------------------------

export class ClientAnalysis {
  readonly ast: Node;
  readonly constants = new Map<string, Scalar>();
  readonly methods = new Map<string, Map<string, Node>>();   // class -> method -> function node
  readonly parents = new Map<string, string>();
  readonly loaders: string[] = [];
  private lineStarts: number[] = [0];
  /** Single-definition locals of the function being analysed (name -> initialiser). */
  scope: Map<string, Node> | null = null;
  private resolving = new Set<string>();
  private scopeCache = new WeakMap<Node, Map<string, Node>>();

  /** Variables of the innermost enclosing function that are defined exactly once. */
  localDefinitions(ancestors: Node[]): Map<string, Node> | null {
    const fn = [...ancestors].reverse().find((a) => a.type === "FunctionExpression" || a.type === "FunctionDeclaration"
      || a.type === "ArrowFunctionExpression");
    if (!fn) return null;
    const cached = this.scopeCache.get(fn);
    if (cached) return cached;
    const defs = new Map<string, Node[]>();
    const add = (name: string, init: Node | null) => (defs.get(name) ?? defs.set(name, []).get(name)!).push(init as Node);
    walk.full(fn.body, (n: Node) => {
      if (n.type === "VariableDeclarator" && n.id.type === "Identifier") add(n.id.name, n.init ?? null);
      else if (n.type === "AssignmentExpression" && n.left.type === "Identifier") add(n.left.name, n.operator === "=" ? n.right : null);
      else if (n.type === "UpdateExpression" && n.argument.type === "Identifier") add(n.argument.name, null);
    });
    const single = new Map<string, Node>();
    for (const [name, inits] of defs) if (inits.length === 1 && inits[0]) single.set(name, inits[0]);
    this.scopeCache.set(fn, single);
    return single;
  }

  constructor(readonly source: string) {
    this.ast = acorn.parse(source, { ecmaVersion: "latest", sourceType: "script", allowHashBang: true }) as Node;
    for (let i = 0; i < source.length; i++) if (source.charCodeAt(i) === 10) this.lineStarts.push(i + 1);
    this.collectConstants();
    this.collectClasses();
    this.collectLoaders();
  }

  line(pos: number): number {
    let lo = 0, hi = this.lineStarts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.lineStarts[mid] <= pos) lo = mid; else hi = mid - 1;
    }
    return lo + 1;
  }

  private collectConstants() {
    const declared = new Map<string, Scalar[]>();
    const assigned = new Map<string, Scalar[]>();
    const tainted = new Set<string>();
    const literal = (n: Node | null | undefined): [boolean, Scalar] => {
      if (!n) return [false, undefined];
      if (n.type === "Literal" && (typeof n.value !== "object" || n.value === null)) return [true, n.value];
      if (n.type === "UnaryExpression" && n.operator === "-" && n.argument.type === "Literal" && typeof n.argument.value === "number") {
        return [true, -n.argument.value];
      }
      return [false, undefined];
    };
    walk.full(this.ast, (n: Node) => {
      if (n.type === "VariableDeclarator" && n.id.type === "Identifier") {
        const [ok, v] = literal(n.init);
        if (ok) (declared.get(n.id.name) ?? declared.set(n.id.name, []).get(n.id.name)!).push(v);
        else tainted.add(n.id.name);
      } else if (n.type === "AssignmentExpression" && n.left.type === "Identifier") {
        const [ok, v] = literal(n.right);
        if (ok && n.operator === "=") (assigned.get(n.left.name) ?? assigned.set(n.left.name, []).get(n.left.name)!).push(v);
        else tainted.add(n.left.name);
      } else if (n.type === "UpdateExpression" && n.argument.type === "Identifier") {
        tainted.add(n.argument.name);
      } else if ((n.type === "FunctionDeclaration" || n.type === "FunctionExpression" || n.type === "ArrowFunctionExpression")) {
        for (const p of n.params) if (p.type === "Identifier") tainted.add(p.name);
        if (n.id?.type === "Identifier") tainted.add(n.id.name);
      } else if (n.type === "CatchClause" && n.param?.type === "Identifier") {
        tainted.add(n.param.name);
      }
    });
    for (const name of new Set([...declared.keys(), ...assigned.keys()])) {
      if (tainted.has(name)) continue;
      const values = [...(declared.get(name) ?? []), ...(assigned.get(name) ?? [])];
      if (values.length === 1) this.constants.set(name, values[0]);
    }
  }

  private collectClasses() {
    walk.full(this.ast, (n: Node) => {
      if (n.type !== "AssignmentExpression" || n.operator !== "=" || n.left.type !== "MemberExpression") return;
      const left = n.left;
      // X.prototype.m = function () {...}
      if (left.object.type === "MemberExpression" && left.object.property?.name === "prototype"
        && left.object.object.type === "Identifier" && !left.computed && left.property.type === "Identifier"
        && (n.right.type === "FunctionExpression" || n.right.type === "ArrowFunctionExpression")) {
        const cls = left.object.object.name;
        (this.methods.get(cls) ?? this.methods.set(cls, new Map()).get(cls)!).set(left.property.name, n.right);
      }
      // X.prototype = Object.create(Y.prototype)
      if (left.object.type === "Identifier" && left.property?.name === "prototype" && n.right.type === "CallExpression"
        && n.right.callee.type === "MemberExpression" && n.right.callee.property?.name === "create"
        && n.right.arguments[0]?.type === "MemberExpression" && n.right.arguments[0].object.type === "Identifier") {
        this.parents.set(left.object.name, n.right.arguments[0].object.name);
      }
    });
  }

  /** Functions that create an Image and append the "?v=" cache-buster: the client's loaders. */
  private collectLoaders() {
    walk.full(this.ast, (n: Node) => {
      if (n.type !== "VariableDeclarator" || n.id.type !== "Identifier" || !n.init) return;
      if (n.init.type !== "FunctionExpression" && n.init.type !== "ArrowFunctionExpression") return;
      let image = false, version = false;
      walk.full(n.init, (m: Node) => {
        if (m.type === "NewExpression" && m.callee.type === "Identifier" && m.callee.name === "Image") image = true;
        if (m.type === "Literal" && m.value === "?v=") version = true;
      });
      if (image && version) this.loaders.push(n.id.name);
    });
  }

  findMethod(cls: string | null, name: string): Node | null {
    for (let c = cls, depth = 0; c && depth < 20; c = this.parents.get(c) ?? null, depth++) {
      const m = this.methods.get(c)?.get(name);
      if (m) return m;
    }
    return null;
  }

  // -- symbolic evaluation -----------------------------------------------------------------
  evaluate(node: Node, cls: string | null, env: Map<string, Value> = new Map(), depth = 0): Value {
    switch (node.type) {
      case "Literal":
        return typeof node.value === "object" && node.value !== null ? "unresolved" : [node.value];
      case "TemplateLiteral": {
        let acc: Value = [node.quasis[0].value.cooked ?? ""];
        node.expressions.forEach((e: Node, i: number) => {
          acc = combine(acc, this.evaluate(e, cls, env, depth), plus);
          acc = combine(acc, [node.quasis[i + 1].value.cooked ?? ""], plus);
        });
        return acc;
      }
      case "Identifier": {
        if (env.has(node.name)) return env.get(node.name)!;
        if (node.name === "undefined") return [undefined];
        if (this.constants.has(node.name)) return [this.constants.get(node.name)!];
        const definition = this.scope?.get(node.name);
        if (definition && !this.resolving.has(node.name) && depth < 6) {
          this.resolving.add(node.name);
          try {
            const v = this.evaluate(definition, cls, env, depth + 1);
            if (v !== "unresolved") return v;
          } finally {
            this.resolving.delete(node.name);
          }
        }
        return [hole(node.name)];
      }
      case "ThisExpression":
        return [hole("this")];
      case "MemberExpression": {
        if (node.object.type === "ThisExpression" && !node.computed) return [hole(node.property.name)];
        const path = this.memberPath(node);
        return [hole(path ?? "expr")];
      }
      case "BinaryExpression": {
        const a = this.evaluate(node.left, cls, env, depth), b = this.evaluate(node.right, cls, env, depth);
        if (node.operator === "+") return combine(a, b, plus);
        if (a === "unresolved" || b === "unresolved") return "unresolved";
        if (a.some(isHoleOnly) || b.some(isHoleOnly)) return [hole("cond")];
        const ops: Record<string, (x: any, y: any) => Scalar> = {
          "==": (x, y) => x == y, "===": (x, y) => x === y, "!=": (x, y) => x != y, "!==": (x, y) => x !== y,
          "<": (x, y) => x < y, ">": (x, y) => x > y, "<=": (x, y) => x <= y, ">=": (x, y) => x >= y,
          "-": (x, y) => x - y, "*": (x, y) => x * y,
        };
        return ops[node.operator] ? combine(a, b, ops[node.operator]) : [hole("expr")];
      }
      case "LogicalExpression": {
        const a = this.evaluate(node.left, cls, env, depth);
        if (a === "unresolved" || a.length !== 1 || isHoleOnly(a[0])) return [hole("expr")];
        const truthy = !!a[0];
        if (node.operator === "&&") return truthy ? this.evaluate(node.right, cls, env, depth) : a;
        if (node.operator === "||") return truthy ? a : this.evaluate(node.right, cls, env, depth);
        return [hole("expr")];
      }
      case "UnaryExpression": {
        const a = this.evaluate(node.argument, cls, env, depth);
        if (node.operator === "!" && a !== "unresolved" && a.length === 1 && !isHoleOnly(a[0])) return [!a[0]];
        return [hole("expr")];
      }
      case "ConditionalExpression": {
        const t = this.evaluate(node.test, cls, env, depth);
        if (t !== "unresolved" && t.length === 1 && !isHoleOnly(t[0]) && !(typeof t[0] === "string" && t[0].includes(HOLE_OPEN))) {
          return this.evaluate(t[0] ? node.consequent : node.alternate, cls, env, depth);
        }
        const a = this.evaluate(node.consequent, cls, env, depth), b = this.evaluate(node.alternate, cls, env, depth);
        if (a === "unresolved" || b === "unresolved") return "unresolved";
        const out = [...a, ...b.filter((v) => !a.includes(v))];
        return out.length > MAX_ALTERNATIVES ? "unresolved" : out;
      }
      case "SequenceExpression": {
        let last: Value = [undefined];
        for (const e of node.expressions) last = this.evaluate(e, cls, env, depth);
        return last;
      }
      case "AssignmentExpression": {
        if (node.left.type !== "Identifier") return this.evaluate(node.right, cls, env, depth);
        const right = this.evaluate(node.right, cls, env, depth);
        const value = node.operator === "=" ? right
          : node.operator === "+=" ? combine(env.get(node.left.name) ?? [hole(node.left.name)], right, plus) : "unresolved";
        env.set(node.left.name, value);
        return value;
      }
      case "CallExpression":
        return this.evaluateCall(node, cls, env, depth);
      default:
        return [hole("expr")];
    }
  }

  private memberPath(node: Node): string | null {
    if (node.type === "Identifier") return node.name;
    if (node.type === "ThisExpression") return "this";
    if (node.type === "MemberExpression" && !node.computed && node.property.type === "Identifier") {
      const inner = this.memberPath(node.object);
      return inner ? `${inner}.${node.property.name}` : null;
    }
    if (node.type === "MemberExpression" && node.computed) {
      const inner = this.memberPath(node.object);
      return inner ? `${inner}[]` : null;
    }
    return null;
  }

  private evaluateCall(node: Node, cls: string | null, env: Map<string, Value>, depth: number): Value {
    const callee = node.callee;
    const label = (this.memberPath(callee) ?? "call").replace(/^this\./, "") + "()";
    if (callee.type !== "MemberExpression" || callee.object.type !== "ThisExpression" || callee.computed || depth > 4) {
      return [hole(label)];
    }
    const method = this.findMethod(cls, callee.property.name);
    if (!method) return [hole(label)];
    const args = node.arguments.map((a: Node) => this.evaluate(a, cls, env, depth));
    const local = new Map<string, Value>();
    method.params.forEach((p: Node, i: number) => {
      if (p.type === "Identifier") local.set(p.name, i < args.length ? args[i] : [undefined]);
    });
    const result = this.runBody(method.body, cls, local, depth + 1);
    return result === "unresolved" ? [hole(label)] : result;
  }

  /** Interpret a small function body: var declarations, expression statements, return, if. */
  private runBody(body: Node, cls: string | null, env: Map<string, Value>, depth: number): Value {
    if (body.type !== "BlockStatement") return this.evaluate(body, cls, env, depth);
    for (const statement of body.body) {
      if (statement.type === "VariableDeclaration") {
        for (const d of statement.declarations) {
          if (d.id.type !== "Identifier") return "unresolved";
          env.set(d.id.name, d.init ? this.evaluate(d.init, cls, env, depth) : [undefined]);
        }
      } else if (statement.type === "ExpressionStatement") {
        this.evaluate(statement.expression, cls, env, depth);
      } else if (statement.type === "ReturnStatement") {
        return statement.argument ? this.evaluate(statement.argument, cls, env, depth) : [undefined];
      } else if (statement.type === "IfStatement") {
        const t = this.evaluate(statement.test, cls, env, depth);
        if (t === "unresolved" || t.length !== 1 || isHoleOnly(t[0])) return "unresolved";
        const branch = t[0] ? statement.consequent : statement.alternate;
        if (branch) {
          const r = this.runBody(branch.type === "BlockStatement" ? branch : { type: "BlockStatement", body: [branch] } as Node,
            cls, env, depth);
          if (r !== "unresolved" && !(r.length === 1 && r[0] === NO_RETURN)) return r;
          if (r === "unresolved") return r;
        }
      } else if (statement.type === "EmptyStatement") {
        continue;
      } else {
        return "unresolved";
      }
    }
    return [NO_RETURN];
  }

  // -- the animal table (_0x373b4f, L804) ------------------------------------------------------
  animals(): AnimalInfo[] {
    let best: AnimalInfo[] = [];
    walk.full(this.ast, (n: Node) => {
      if (n.type !== "SwitchStatement") return;
      const rows: AnimalInfo[] = [];
      let pending: Array<{ constant: string; id: number }> = [];
      for (const c of n.cases) {
        if (c.test?.type === "Identifier" && typeof this.constants.get(c.test.name) === "number") {
          pending.push({ constant: c.test.name, id: this.constants.get(c.test.name) as number });
        } else if (c.test?.type === "Literal" && typeof c.test.value === "number") {
          pending.push({ constant: String(c.test.value), id: c.test.value });
        }
        if (!c.consequent.length) continue;
        const fields: Record<string, string> = {};
        walk.full({ type: "Program", body: c.consequent } as any, (m: Node) => {
          if (m.type === "AssignmentExpression" && m.left.type === "MemberExpression" && !m.left.computed
            && m.right.type === "Literal" && typeof m.right.value === "string") fields[m.left.property.name] = m.right.value;
        });
        if (fields.aniName !== undefined) {
          for (const p of pending) rows.push({ id: p.id, constant: p.constant, aniName: fields.aniName, skinName: fields.skinName ?? null, aniCol: fields.aniCol ?? null });
        }
        pending = [];
      }
      if (rows.length > best.length) best = rows;
    });
    return best.sort((a, b) => a.id - b.id);
  }
}

const NO_RETURN = "\u0003no-return";

// -- reference extraction --------------------------------------------------------------------

function classOf(ancestors: Node[]): string | null {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const a = ancestors[i];
    if (a.type === "AssignmentExpression" && a.left.type === "MemberExpression" && a.left.object.type === "MemberExpression"
      && a.left.object.property?.name === "prototype" && a.left.object.object.type === "Identifier") return a.left.object.object.name;
    if ((a.type === "FunctionDeclaration") && a.id) return a.id.name;
  }
  return null;
}

export function normaliseRelative(path: string): { rel: string; base: ImageRef["base"] | null } {
  let p = path.replace(/^\.\//, "").replace(/^\//, "");
  if (/^assets\/(s|t)\//.test(p)) return { rel: p, base: "absolute" };
  return { rel: p, base: null };
}

export function categorise(rel: string, ref: { via: string; base: string; edibleVariant: boolean }): string {
  const p = rel.replace(/^assets\/[st]\/[^/]+\//, "");
  if (ref.base === "shop") return "shop-skins";
  if (/^cdn-cgi\//.test(p)) return "cloudflare";
  if (/(^|\/)favicon[^/]*$/.test(p)) return "ui";
  if (/(^|\/)abilit|^assets\/abilities\//i.test(p)) return "abilities";
  if (/^skins\//.test(p) || /^premium\//.test(p)) return "animals";
  if (/^gameobj\//.test(p)) return "game-objects";
  if (/^img\/disasters\//.test(p)) return "disasters";
  if (/^img\//.test(p) && ref.edibleVariant) return "food";
  if (/^shop\//.test(p)) return "shop-ui";
  if (/^img\/(update|updates|promo)/.test(p) || /^img\/promo_/.test(p)) return "promo";
  if (/^img\//.test(p) && (ref.base === "season" || ref.base === "absolute")) return "world-and-effects";
  if (/^img\//.test(p)) return "ui";
  if (/^assets\/t\//.test(rel)) return "themes";
  return "other";
}

export function analyseClient(source: string, label = "client.js"): ClientCatalog {
  const analysis = new ClientAnalysis(source);
  const references: ImageRef[] = [];
  const seen = new Set<string>();
  const loaderSet = new Set(analysis.loaders);

  walk.fullAncestor(analysis.ast, (node: Node, _state: unknown, ancestors: Node[]) => {
    const parent = ancestors[ancestors.length - 2];
    let candidate = false;
    if (node.type === "Literal" && typeof node.value === "string" && IMAGE_EXT.test(node.value)) {
      candidate = !(parent?.type === "BinaryExpression" && parent.operator === "+") && parent?.type !== "TemplateLiteral";
    } else if (node.type === "BinaryExpression" && node.operator === "+") {
      if (parent?.type === "BinaryExpression" && parent.operator === "+") return;
      const text = analysis.source.slice(node.start, node.end);
      candidate = IMAGE_EXT.test(text);
    } else if (node.type === "TemplateLiteral") {
      candidate = node.quasis.some((q: Node) => IMAGE_EXT.test(q.value.cooked ?? ""));
    }
    // arguments of the helpers that are not literal paths (this.getWing(2), this.objSkinPath, ...)
    const call = parent?.type === "CallExpression" && parent.arguments[0] === node ? parent : null;
    const name = call?.callee.type === "MemberExpression" && !call.callee.computed ? call.callee.property.name : null;
    const helper = name && Object.hasOwn(SEASON_HELPERS, name) ? name : null;
    const loader = call?.callee.type === "Identifier" && loaderSet.has(call.callee.name) ? call.callee.name : null;
    if (!candidate && !helper && !loader) return;
    // skip expressions that are themselves inside a helper/loader argument (handled at the argument)
    if (!call) {
      for (let i = ancestors.length - 2; i >= 0; i--) {
        const a = ancestors[i];
        if (a.type === "CallExpression" && a.arguments.includes(ancestors[i + 1])) {
          const h = a.callee.type === "MemberExpression" && !a.callee.computed ? a.callee.property.name : null;
          const l = a.callee.type === "Identifier" && loaderSet.has(a.callee.name);
          if ((h && Object.hasOwn(SEASON_HELPERS, h)) || l) return;
          break;
        }
        if (a.type.endsWith("Statement") || a.type.startsWith("Function")) break;
      }
    }
    const key = `${node.start}:${node.end}`;
    if (seen.has(key)) return;
    seen.add(key);

    const cls = classOf(ancestors);
    analysis.scope = analysis.localDefinitions(ancestors);
    const value = analysis.evaluate(node, cls);
    analysis.scope = null;
    const raw = value === "unresolved" ? [hole("unresolved")] : value.filter((v) => typeof v === "string" && v.length) as string[];
    if (!raw.length) return;
    const text = analysis.source.slice(node.start, node.end);
    const edibleVariant = /isEdibleOutlined\(\)/.test(text) || raw.some((r) => /_e\.png$/.test(r));
    const via = helper ? helper : loader ? "loader" : "expression";
    let base: ImageRef["base"] = helper ? SEASON_HELPERS[helper].base as ImageRef["base"]
      : loader ? "root" : "inferred";
    const alternatives = raw.map((r) => normaliseRelative(r));
    if (alternatives.every((a) => a.base === "absolute")) base = "absolute";
    if (base === "inferred" && alternatives.every((a) => /^(skins|gameobj)\//.test(a.rel))) base = "season";
    const rendered = alternatives.map((a) => renderHoles(a.rel));
    const template = renderHoles(raw.length === 1 ? alternatives[0].rel
      : commonTemplate(alternatives.map((a) => a.rel)));
    const ref: ImageRef = {
      line: analysis.line(node.start),
      template,
      alternatives: rendered,
      holes: [...new Set(alternatives.flatMap((a) => holeNames(a.rel)))],
      via,
      base,
      themes: !!(helper && SEASON_HELPERS[helper].themes),
      className: cls,
      category: "",
      edibleVariant,
    };
    ref.category = categorise(rendered[0], ref);
    references.push(ref);
  });

  references.sort((a, b) => a.line - b.line);
  return {
    source: label,
    lines: analysis.source.split("\n").length,
    constants: analysis.constants.size,
    classes: analysis.methods.size,
    loaders: analysis.loaders,
    references,
    animals: analysis.animals(),
  };
}

/** One display template for several alternatives: shared prefix/suffix with {a|b} in the middle. */
function commonTemplate(values: string[]): string {
  let prefix = values[0];
  for (const v of values) while (!v.startsWith(prefix)) prefix = prefix.slice(0, -1);
  let suffix = values[0].slice(prefix.length);
  for (const v of values) while (!v.endsWith(suffix) || v.length - suffix.length < prefix.length) suffix = suffix.slice(1);
  const middles = values.map((v) => v.slice(prefix.length, v.length - suffix.length));
  return `${prefix}[${middles.join("|")}]${suffix}`;
}

/** Regular expression for archived paths (relative to the season/theme root) a template can produce. */
export function templateRegex(alternative: string): RegExp | null {
  const parts = alternative.split(/(\{[^}]*\})/);
  // Too generic to attribute a file (e.g. "{getSkinImagePath()}.png", "img/{x}.png"): it must start with
  // literal text and carry at least 6 literal characters besides slashes and the extension.
  if (alternative.startsWith("{")) return null;
  const literal = alternative.replace(/^(\.\/|\/)?assets\/[st]\/(\{[^}]*\}|[^/]+)\//, "").replace(/\{[^}]*\}/g, "")
    .replace(IMAGE_EXT, "").replace(/[/._-]/g, "");
  if (literal.length < 6) return null;
  const source = parts.map((p) => {
    if (!p.startsWith("{")) return p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return /\(\)|Path|Skin|Folder|Img|unresolved|expr|this/.test(p) ? ".+?" : "[^/]+?";
  }).join("");
  return new RegExp(`^${source}$`);
}
