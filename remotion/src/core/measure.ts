import { alias, cssStretch, cssWeight, familyStack } from "./fonts";
import type { FontSpec } from "./schema";

/**
 * Text measurement with the same engine that draws the text: a hidden SVG <text> element. Canvas
 * measureText can differ from SVG layout (hinting, letter-spacing), and a width that is off by a pixel is a
 * word that touches its neighbour. Call only after the fonts are loaded (useFonts).
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

/** Attributes shared by the measuring element and every drawn <text>: they must match exactly. */
export function textAttrs(font: FontSpec, emoji: string, size: number, letterSpacing: number) {
  return {
    fontFamily: familyStack(font, emoji),
    fontSize: size,
    fontWeight: cssWeight(font),
    fontStretch: cssStretch(font),
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
  el.setAttribute("font-weight", String(cssWeight(font)));
  el.setAttribute("font-stretch", cssStretch(font));
  el.setAttribute("font-style", "normal");
  el.setAttribute("letter-spacing", `${letterSpacing}px`);
  el.style.cssText =
    "font-kerning:normal;font-synthesis:none;font-variant-ligatures:none;text-rendering:geometricPrecision;white-space:pre";
  el.textContent = text;
}

const widthCache = new Map<string, number>();
const REF = 200; // measure once at 200 px and scale: geometricPrecision text scales linearly

/** Advance width of `text` at `size` px with `letterSpacing` em. */
export function textWidth(text: string, font: FontSpec, emoji: string, size: number, letterSpacing = 0): number {
  const key = `${alias(font)}|${cssWeight(font)}|${letterSpacing}|${text}`;
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
  const key = `${alias(font)}|${cssWeight(font)}|${letterSpacing}|${text}`;
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

export type FontMetrics = { capH: number; xH: number; asc: number; desc: number }; // em
const metricCache = new Map<string, FontMetrics>();

/** Ink metrics of the font in em: cap height, x-height, tallest ascender, deepest descender. */
export function fontMetrics(font: FontSpec): FontMetrics {
  const key = `${alias(font)}|${cssWeight(font)}`;
  let m = metricCache.get(key);
  if (!m) {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) throw new Error("no 2d canvas for font metrics");
    ctx.font = `${cssWeight(font)} ${REF}px "${alias(font)}"`;
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

/** True when every character of `text` is drawn from the caption font or the emoji font: no fallback. */
export function covered(text: string, font: FontSpec, emoji: string): boolean {
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return false;
  const stack = `${cssWeight(font)} 64px ${familyStack(font, emoji)}`;
  // A character missing from both fonts falls through to the last family. If two very different last
  // families give the same width, nothing fell through.
  ctx.font = `${stack}, monospace`;
  const a = ctx.measureText(text).width;
  ctx.font = `${stack}, serif`;
  const b = ctx.measureText(text).width;
  ctx.font = `${stack}, cursive`;
  const c = ctx.measureText(text).width;
  return Math.abs(a - b) < 0.01 && Math.abs(a - c) < 0.01;
}
