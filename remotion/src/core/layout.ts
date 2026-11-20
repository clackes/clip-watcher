import { fontMetrics, glyphs, textWidth, type Glyph } from "./measure";
import type { FontSpec } from "./schema";

/**
 * Line breaking and fitting. A page of words is set on 1..maxLines lines at the largest size that keeps it
 * inside a box: long words shrink, long phrases wrap. Nothing here knows about time; a page has one
 * layout for its whole life, so nothing reflows while it is on screen.
 */

// scale: size factor of this word (shouted words); font: its own font when it is not the page's
export type WordIn = { text: string; scale: number; font?: FontSpec };

export type WordBox = {
  index: number; // position in the page
  line: number;
  text: string;
  x: number; // left edge, relative to the block centre
  y: number; // baseline, relative to the block centre
  w: number; // advance width
  size: number; // font size of this word
  font: FontSpec; // the font this word is set in
  cx: number; // optical centre of the word (middle of its width, middle of the cap height)
  cy: number;
  glyphs: Glyph[]; // per character: x offset from the word's left edge and advance
};

export type LineBox = { index: number; x: number; y: number; w: number; top: number; bottom: number; words: number[] };

export type Layout = {
  size: number; // font size after fitting
  fit: number; // size / nominal size: 1 when nothing had to shrink
  words: WordBox[];
  lines: LineBox[];
  // ink box of the block relative to its centre (glyph outlines, before stroke and animation)
  left: number;
  right: number;
  top: number;
  bottom: number;
  capH: number; // cap height in px at `size`
};

export type FitOptions = {
  font: FontSpec;
  emoji: string; // system emoji family, or ""
  size: number;
  lineHeight: number; // em
  letterSpacing: number; // em
  wordGap: number; // em
  grow: number; // room each side of a word for its own growth, share of its width
  maxLines: number;
  boxW: number;
  boxH: number;
  upper: boolean; // all caps: no descenders to make room for
};

/** Every way to cut n items into k consecutive, non-empty groups (n is at most a dozen words). */
function splits(n: number, k: number): number[][] {
  const out: number[][] = [];
  const walk = (start: number, left: number, cuts: number[]) => {
    if (left === 1) {
      out.push([...cuts, n]);
      return;
    }
    for (let end = start + 1; end <= n - (left - 1); end++) walk(end, left - 1, [...cuts, end]);
  };
  walk(0, k, []);
  return out; // each entry: the exclusive end index of every group
}

export function fitWords(words: WordIn[], o: FitOptions): Layout {
  const fm = fontMetrics(o.font);
  const REF = 100;
  const ink100 = words.map((w) => textWidth(w.text, w.font ?? o.font, o.emoji, REF * w.scale, o.letterSpacing));
  const w100 = ink100.map((w) => w * (1 + 2 * o.grow)); // the slot a word gets: its width plus room to grow
  const gap100 = o.wordGap * REF;
  const maxScale = Math.max(1, ...words.map((w) => w.scale));
  const above = (o.upper ? fm.capH : Math.max(fm.capH, fm.asc)) * maxScale; // ink above the first baseline, em
  const below = (o.upper ? 0.03 : fm.desc) * maxScale; // ink below the last baseline, em

  let best: { size: number; ends: number[]; widths: number[] } | null = null;
  for (let k = 1; k <= Math.min(o.maxLines, words.length); k++) {
    let pick: { ends: number[]; widths: number[]; max: number; spread: number } | null = null;
    for (const ends of splits(words.length, k)) {
      const widths: number[] = [];
      let a = 0;
      for (const b of ends) {
        let w = 0;
        for (let i = a; i < b; i++) w += w100[i] + (i > a ? gap100 : 0);
        widths.push(w);
        a = b;
      }
      const max = Math.max(...widths);
      const spread = max - Math.min(...widths);
      if (!pick || max < pick.max - 0.01 || (Math.abs(max - pick.max) <= 0.01 && spread < pick.spread)) {
        pick = { ends, widths, max, spread };
      }
    }
    if (!pick) continue;
    const hEm = above + below + (k - 1) * o.lineHeight * maxScale;
    const size = Math.min(o.size, (o.boxW * REF) / pick.max, o.boxH / hEm);
    // more lines only when they buy a clearly larger size
    if (!best || size > best.size * 1.04) best = { size, ends: pick.ends, widths: pick.widths };
  }
  if (!best) throw new Error("fitWords: no words");

  const size = best.size;
  const k = size / REF;
  const pitch = o.lineHeight * size * maxScale;
  const capH = fm.capH * size;
  const n = best.ends.length;
  const blockH = capH * maxScale + (n - 1) * pitch; // first cap top to last baseline
  const top0 = -blockH / 2;
  const out: WordBox[] = [];
  const lines: LineBox[] = [];
  let a = 0;
  let left = 0;
  let right = 0;
  best.ends.forEach((b, li) => {
    const lw = best.widths[li] * k;
    const baseline = top0 + capH * maxScale + li * pitch;
    let x = -lw / 2;
    const idx: number[] = [];
    for (let i = a; i < b; i++) {
      const slot = w100[i] * k;
      const w = ink100[i] * k;
      const ws = size * words[i].scale;
      const wx = x + (slot - w) / 2;
      out.push({
        index: i,
        line: li,
        text: words[i].text,
        x: wx,
        y: baseline,
        w,
        size: ws,
        font: words[i].font ?? o.font,
        cx: wx + w / 2,
        cy: baseline - (fm.capH * ws) / 2,
        glyphs: glyphs(words[i].text, words[i].font ?? o.font, o.emoji, ws, o.letterSpacing),
      });
      idx.push(i);
      x += slot + gap100 * k;
    }
    lines.push({
      index: li,
      x: -lw / 2,
      y: baseline,
      w: lw,
      top: baseline - above * size,
      bottom: baseline + below * size,
      words: idx,
    });
    left = Math.min(left, -lw / 2);
    right = Math.max(right, lw / 2);
    a = b;
  });
  return {
    size,
    fit: size / o.size,
    words: out,
    lines,
    left,
    right,
    top: lines[0].top,
    bottom: lines[lines.length - 1].bottom,
    capH,
  };
}

/** Where the block centre goes: on the anchor when the block fits there, else pushed inside the box. */
export function placeY(layout: Layout, anchorY: number, y0: number, y1: number): number {
  const lo = y0 - layout.top;
  const hi = y1 - layout.bottom;
  if (lo > hi) return (y0 + y1) / 2 - (layout.top + layout.bottom) / 2;
  return Math.min(hi, Math.max(lo, anchorY));
}
