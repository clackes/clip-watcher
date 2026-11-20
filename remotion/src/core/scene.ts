import type { Affine } from "./affine";
import { IDENTITY } from "./affine";
import { isEmoji } from "./fonts";
import type { Layout, WordBox } from "./layout";
import type { Glyph } from "./measure";
import type { BaseParams, Colors, FontSpec, Label, Page, Region } from "./schema";

/**
 * What a template returns for one instant: a flat list of text units and shapes in overlay pixels. A
 * template is a pure function (context) -> Scene; the Stage draws it. Keeping the scene as data is what
 * lets the frame-by-frame harness see every position, scale and colour of every frame.
 */

/** A linear gradient in the unit's own coordinates (before its transform), so it travels with the unit. */
export type Grad = { x1: number; y1: number; x2: number; y2: number; stops: { o: number; c: string }[] };

export type Layer =
  // the glyphs themselves
  | { kind: "fill"; color: string; grad?: Grad; dx?: number; dy?: number }
  // a solid silhouette of the glyphs grown outwards by `width` px (round joins): outline, halo, extrusion step
  | { kind: "stroke"; color: string; width: number; dx?: number; dy?: number }
  // a soft halo: `steps` silhouettes from `width` px down to 0, `alpha` each (decoration only, never the text)
  | { kind: "glow"; color: string; width: number; alpha: number; steps: number; dx?: number; dy?: number };

export type Rect = { x: number; y: number; w: number; h: number };

export type Unit = {
  id: string; // unique in the frame, stable over the unit's life: "p3w1", "p3w1c4", "l2w0"
  page: number; // page id (or label id for label units)
  word: number; // word index in the page
  letter: number; // character index in the word, -1 for a whole word
  label: boolean;
  text: string;
  x: number; // baseline-left, overlay px, before `m`
  y: number;
  w: number; // advance width at `size`
  size: number;
  font: FontSpec;
  glyphs: { ch: string; x: number }[]; // the characters and where each starts, from the unit's x
  m: Affine; // transform in overlay coordinates
  z: number; // higher draws on top; units with equal z are drawn layer by layer, not unit by unit
  layers: Layer[]; // back to front; the last "fill" is the legible one
  clip?: Rect; // a window fixed on the overlay; the unit shows only inside it
  fg: string[]; // the colours that carry the letter shapes this frame (what contrast is measured from)
  active: boolean; // this is the word being spoken
  emph: boolean;
  emoji: boolean; // the text is an emoji: a colour picture, drawn as it is with the outline around it
};

export type Shape = {
  id: string;
  role: "plate" | "decor"; // plate: text sits on it and is measured against it; decor: anything else
  kind: "rect" | "ellipse" | "path";
  x: number; // rect: top-left; ellipse: centre
  y: number;
  w: number; // rect: size; ellipse: radii
  h: number;
  r?: number; // rect corner radius
  d?: string; // path data (kind "path"), overlay px before `m`
  fill: string | null;
  grad?: Grad;
  stroke?: string;
  strokeWidth?: number;
  alpha?: number; // decoration only; plates are opaque
  m: Affine;
  z: number;
  clip?: Rect;
};

export type Scene = { units: Unit[]; shapes: Shape[] };

export type WordState = {
  index: number;
  text: string;
  emph: boolean;
  start: number; // seconds from the page start
  end: number;
  activeEnd: number;
  phase: "ahead" | "active" | "done";
  a: number; // 0..1 how highlighted the word is right now (eased with timing.activeInS / activeOutS)
  since: number; // seconds since the word started (negative before)
  box: WordBox;
  cx: number; // optical centre in overlay px
  cy: number;
};

export type Ctx<P extends BaseParams = BaseParams> = {
  P: P;
  t: number; // seconds since the page (or label) appeared
  T: number; // seconds on the overlay timeline
  dur: number; // how long the page is on screen
  fps: number;
  exit: number; // seconds of exit animation (0 = the next page replaces this one with a cut)
  exitT: number; // local time the exit starts (== dur when exit is 0)
  out: number; // 0..1 linear progress of the exit
  page: Page;
  index: number; // number of this page in the clip
  words: WordState[];
  layout: Layout;
  cx: number; // block centre in overlay px
  cy: number;
  region: Region;
  colors: Colors;
  emoji: string; // system emoji family, or ""
  width: number; // overlay size
  height: number;
};

export type LabelCtx<P extends BaseParams = BaseParams> = Omit<Ctx<P>, "page" | "words"> & {
  label: Label;
  words: WordState[]; // the label's own words, all "active"
};

export const emptyScene = (): Scene => ({ units: [], shapes: [] });

export type UnitOpts = {
  m?: Affine;
  z?: number;
  layers: Layer[];
  fg: string[];
  clip?: Rect;
  active?: boolean;
  key?: string; // suffix for the unit id when a template draws the same word more than once per frame
};

/** A unit for a whole word of the current page. */
export function wordUnit(c: Ctx | LabelCtx, w: WordState, o: UnitOpts): Unit {
  const label = "label" in c;
  const owner = label ? (c as LabelCtx).label.id : (c as Ctx).page.id;
  return {
    id: `${label ? "l" : "p"}${owner}w${w.index}${o.key ?? ""}`,
    page: owner,
    word: w.index,
    letter: -1,
    label,
    text: w.text,
    x: c.cx + w.box.x,
    y: c.cy + w.box.y,
    w: w.box.w,
    size: w.box.size,
    font: w.box.font,
    glyphs: w.box.glyphs.map((g) => ({ ch: g.ch, x: g.x })),
    m: o.m ?? IDENTITY,
    z: o.z ?? 0,
    layers: o.layers,
    fg: o.fg,
    clip: o.clip,
    active: o.active ?? w.phase === "active",
    emph: w.emph,
    emoji: isEmoji(w.text, w.box.font),
  };
}

/** One unit per character of a word, for per-letter animation. `fn` gets the glyph and its centre. */
export function letterUnits(
  c: Ctx | LabelCtx,
  w: WordState,
  fn: (g: Glyph, k: number, n: number, gx: number, gy: number) => UnitOpts | null,
): Unit[] {
  const label = "label" in c;
  const owner = label ? (c as LabelCtx).label.id : (c as Ctx).page.id;
  const out: Unit[] = [];
  const n = w.box.glyphs.length;
  w.box.glyphs.forEach((g, k) => {
    if (!g.ch.trim()) return;
    const gx = c.cx + w.box.x + g.x + g.w / 2;
    const o = fn(g, k, n, gx, w.cy);
    if (!o) return;
    out.push({
      id: `${label ? "l" : "p"}${owner}w${w.index}c${k}${o.key ?? ""}`,
      page: owner,
      word: w.index,
      letter: k,
      label,
      text: g.ch,
      x: c.cx + w.box.x + g.x,
      y: c.cy + w.box.y,
      w: g.w,
      size: w.box.size,
      font: w.box.font,
      glyphs: [{ ch: g.ch, x: 0 }],
      m: o.m ?? IDENTITY,
      z: o.z ?? 0,
      layers: o.layers,
      fg: o.fg,
      clip: o.clip,
      active: o.active ?? w.phase === "active",
      emph: w.emph,
      emoji: isEmoji(g.ch, w.box.font),
    });
  });
  return out;
}

type ShapeOpts = Partial<Omit<Shape, "id" | "kind" | "x" | "y" | "w" | "h">> & { fill: string | null };

export function rect(id: string, x: number, y: number, w: number, h: number, o: ShapeOpts): Shape {
  return { id, kind: "rect", role: "plate", x, y, w, h, m: IDENTITY, z: -1, ...o };
}

export function ellipse(id: string, cx: number, cy: number, rx: number, ry: number, o: ShapeOpts): Shape {
  return { id, kind: "ellipse", role: "decor", x: cx, y: cy, w: rx, h: ry, m: IDENTITY, z: -1, ...o };
}

export function path(id: string, d: string, o: ShapeOpts): Shape {
  return { id, kind: "path", role: "decor", x: 0, y: 0, w: 0, h: 0, d, m: IDENTITY, z: -1, ...o };
}

/** The usual outline + fill stack. `halo` is how far the outline reaches outside the glyph, px. */
export function inked(fill: string, stroke: string, halo: number, extra: Layer[] = []): Layer[] {
  return [...extra, { kind: "stroke", color: stroke, width: halo }, { kind: "fill", color: fill }];
}

/** A hard drop shadow: the outlined silhouette again, shifted. Put it before the outline in the stack. */
export function dropShadow(color: string, halo: number, dx: number, dy: number): Layer {
  return { kind: "stroke", color, width: halo, dx, dy };
}

/** An extrusion: `steps` silhouettes from the full offset back to the glyph. */
export function extrude(color: string, halo: number, dx: number, dy: number, steps: number): Layer[] {
  const out: Layer[] = [];
  const n = Math.max(1, Math.round(steps));
  for (let i = n; i >= 1; i--) out.push({ kind: "stroke", color, width: halo, dx: (dx * i) / n, dy: (dy * i) / n });
  return out;
}
