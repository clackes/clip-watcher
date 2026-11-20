import { alias, familyStack, outlineFont } from "./fonts";
import type { FontSpec } from "./schema";

/**
 * Text measurement and glyph outlines. Advances and glyph positions come from the browser's own text
 * layout (a hidden SVG <text>, unhinted: kerning included, linear in the font size). What is drawn is
 * the font's outlines placed at those positions (see `outline`): vector paths are anti-aliased at their
 * exact sub-pixel position, where browser text is snapped to whole pixels vertically and quarter pixels
 * horizontally, which shows as judder in slow motion. Call only after the fonts are loaded (useFonts).
 */

const NS = "http://www.w3.org/2000/svg";
let node: SVGTextElement | null = null;

function probe(): SVGTextElement {
  if (node && node.isConnected) return node;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", "10");
  svg.setAttribute("height", "10");
  svg.setAttribute("aria-hidden", "true");
  svg.style.cssText = "position:absolute;left:-9999px;top:-9999px;visibility:hidden;pointer-events:none";
  node = document.createElementNS(NS, "text");
  svg.appendChild(node);
  document.body.appendChild(svg);
  return node;
}

/** Attributes of a drawn <text> (emoji only: everything else is drawn as outlines). */
export function textAttrs(font: FontSpec, emoji: string, size: number, letterSpacing: number) {
  return {
    fontFamily: familyStack(font, emoji),
    fontSize: size,
    fontWeight: 400,
    fontStyle: "normal" as const,
    letterSpacing: `${letterSpacing}px`,
    style: {
      fontKerning: "normal" as const,
      fontSynthesis: "none" as const,
      fontVariantLigatures: "none" as const,
      textRendering: "geometricPrecision" as const,
      whiteSpace: "pre" as const,
    },
  };
}

function setup(el: SVGTextElement, font: FontSpec, emoji: string, size: number, letterSpacing: number, text: string) {
  el.setAttribute("font-family", familyStack(font, emoji));
  el.setAttribute("font-size", String(size));
  el.setAttribute("font-weight", "400");
  el.setAttribute("font-style", "normal");
  el.setAttribute("letter-spacing", `${letterSpacing}px`);
  // ligatures and contextual forms off: one character is one glyph, the glyph the font's cmap names
  el.style.cssText =
    "font-kerning:normal;font-synthesis:none;font-variant-ligatures:none;text-rendering:geometricPrecision;white-space:pre";
  el.textContent = text;
}

const widthCache = new Map<string, number>();
const REF = 200; // measure once at 200 px and scale: unhinted text scales linearly

/** Advance width of `text` at `size` px with `letterSpacing` em. */
export function textWidth(text: string, font: FontSpec, emoji: string, size: number, letterSpacing = 0): number {
  const key = `${alias(font)}|${letterSpacing}|${text}`;
  let w = widthCache.get(key);
  if (w === undefined) {
    const el = probe();
    setup(el, font, emoji, REF, letterSpacing * REF, text);
    w = el.getComputedTextLength();
    widthCache.set(key, w);
  }
  return (w * size) / REF;
}

export type Glyph = { ch: string; x: number; w: number };
const glyphCache = new Map<string, Glyph[]>();

/** Each character (code point) of `text` with its x offset and advance at `size` px: kerning included. */
export function glyphs(text: string, font: FontSpec, emoji: string, size: number, letterSpacing = 0): Glyph[] {
  const key = `${alias(font)}|${letterSpacing}|${text}`;
  let g = glyphCache.get(key);
  if (!g) {
    const el = probe();
    setup(el, font, emoji, REF, letterSpacing * REF, text);
    const total = el.getComputedTextLength();
    const chars = Array.from(text);
    const starts: number[] = [];
    let i16 = 0;
    for (const ch of chars) {
      starts.push(el.getStartPositionOfChar(i16).x);
      i16 += ch.length;
    }
    g = chars.map((ch, i) => ({ ch, x: starts[i], w: (i + 1 < chars.length ? starts[i + 1] : total) - starts[i] }));
    glyphCache.set(key, g);
  }
  const k = size / REF;
  return g.map((q) => ({ ch: q.ch, x: q.x * k, w: q.w * k }));
}

const pathCache = new Map<string, string>();

type Cmd = { type: string; x?: number; y?: number; x1?: number; y1?: number; x2?: number; y2?: number };

/**
 * Outline commands as SVG path data, every contour closed. Written here rather than with the parser's
 * own toPathData: that one leaves contours open (a stroke then skips the closing edge) and prints "NaN"
 * for some coordinates, which cuts the path off at that point. A coordinate that is not a number fails
 * the render instead of drawing half a word.
 */
function pathData(cmds: Cmd[], what: string): string {
  const n = (v: number | undefined): string => {
    if (v === undefined || !Number.isFinite(v)) throw new Error(`glyph outline has a bad coordinate: ${what}`);
    const r = Math.round(v * 1000) / 1000;
    return Object.is(r, -0) ? "0" : String(r);
  };
  let d = "";
  let open = false;
  for (const c of cmds) {
    if (c.type === "M") {
      if (open) d += "Z";
      d += `M${n(c.x)} ${n(c.y)}`;
      open = true;
    } else if (c.type === "L") d += `L${n(c.x)} ${n(c.y)}`;
    else if (c.type === "Q") d += `Q${n(c.x1)} ${n(c.y1)} ${n(c.x)} ${n(c.y)}`;
    else if (c.type === "C") d += `C${n(c.x1)} ${n(c.y1)} ${n(c.x2)} ${n(c.y2)} ${n(c.x)} ${n(c.y)}`;
    else if (c.type === "Z") {
      if (open) d += "Z";
      open = false;
    }
  }
  return open ? `${d}Z` : d;
}

/**
 * SVG path data of glyphs at `size` px: the baseline is y = 0, each glyph starts at its x. The outlines
 * are the font's own, so this is exactly the text, as a shape.
 */
export function outline(font: FontSpec, items: { ch: string; x: number }[], size: number): string {
  const key = `${alias(font)}|${size.toFixed(3)}|${items.map((g) => `${g.ch}${g.x.toFixed(3)}`).join("")}`;
  let d = pathCache.get(key);
  if (d === undefined) {
    const f = outlineFont(font);
    d = items
      .map((g) => {
        const glyph = f.charToGlyph(g.ch);
        return glyph.index === 0 ? "" : pathData(glyph.getPath(g.x, 0, size).commands, `${font.file} "${g.ch}"`);
      })
      .join("");
    if (pathCache.size > 4000) pathCache.clear();
    pathCache.set(key, d);
  }
  return d;
}

export type FontMetrics = { capH: number; xH: number; asc: number; desc: number }; // em
const metricCache = new Map<string, FontMetrics>();

/** Ink metrics of the font in em: cap height, x-height, tallest ascender, deepest descender. */
export function fontMetrics(font: FontSpec): FontMetrics {
  const key = alias(font);
  let m = metricCache.get(key);
  if (!m) {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) throw new Error("no 2d canvas for font metrics");
    ctx.font = `${REF}px "${alias(font)}"`;
    const ink = (s: string) => ctx.measureText(s);
    m = {
      capH: ink("HXE").actualBoundingBoxAscent / REF,
      xH: ink("xz").actualBoundingBoxAscent / REF,
      asc: ink("bdfhkl").actualBoundingBoxAscent / REF,
      desc: ink("gjpqy").actualBoundingBoxDescent / REF,
    };
    metricCache.set(key, m);
  }
  return m;
}

/** True when every character of `text` has a glyph in the caption font or, failing that, is an emoji
 * drawn by the system emoji font: nothing falls through to another font. */
export function covered(text: string, font: FontSpec, emoji: string): boolean {
  const f = outlineFont(font);
  const rest = Array.from(text).filter((ch) => f.charToGlyphIndex(ch) === 0);
  if (rest.length === 0) return true;
  if (!emoji) return false;
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return false;
  // a character the emoji font lacks falls through to the last family: two very different last families
  // then give two different widths
  const s = rest.join("");
  ctx.font = `64px "${emoji}", monospace`;
  const a = ctx.measureText(s).width;
  ctx.font = `64px "${emoji}", serif`;
  const b = ctx.measureText(s).width;
  return Math.abs(a - b) < 0.01 && rest.every((ch) => /\p{Extended_Pictographic}/u.test(ch));
}
