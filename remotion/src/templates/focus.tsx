import { z } from "zod";
import { chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, lerp, prog, spring, springSettle } from "../core/ease";
import { fontMetrics } from "../core/measure";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type LabelCtx, type Layer, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * FOCUS: a lens moves along the line. The word being spoken is large and coloured, with a soft glow under
 * it; its neighbours are small. As the lens moves on, the big word shrinks, the next one swells and the
 * whole line re-flows around them: every position is computed from the widths at that instant, so nothing
 * ever jumps. Shouted words swell even more. Words grow out of the baseline one after another and shrink
 * back into it before a pause.
 *
 * The layout is only asked for the small size and the word widths; where each word stands is worked out
 * here, on one baseline per line, centred, and scaled so that the biggest word still fits the box.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // outline, em of the word as drawn (it grows with the word)
  haloFloor: z.number().min(0).max(10), // ...and never thinner than this on screen, px, however small the text (what the fill is read against)
  shadowY: z.number().min(0).max(0.2), // hard shadow under the words, em
  focusScale: z.number().min(1).max(3), // the spoken word is this many times the size of its neighbours
  emphScale: z.number().min(1).max(3.5), // a shouted word this many times
  lensS: z.number().min(0.07).max(0.6), // the lens takes this long to move on to the next word
  glow: z.number().min(0).max(0.3), // the glow under the big word reaches this far past its outline, em
  glowAlpha: z.number().min(0).max(0.9), // how strong it is right at the outline
  glowRings: z.number().int().min(1).max(10), // rings the glow is built from
  growHz: z.number().min(1).max(8), // words grow out of the baseline with this spring
  growDamp: z.number().min(0.3).max(1),
  shrinkStepS: z.number().min(0).max(0.2), // before a pause they shrink back one after another, this far apart
  shrinkShare: z.number().min(0).max(0.9), // but all those delays together take at most this share of the exit
});
type P = z.infer<typeof schema>;

type Look = {
  staggerS: number; // words grow in this long after each other
  big: (w: WordState) => number; // how many times its resting size a word is under the lens
  focus: (w: WordState) => number; // 0..1: how much of the lens is on the word right now
  fill: (w: WordState) => string;
  glow: (w: WordState) => string;
};

const GONE = 0.03; // a word this small is not drawn any more
const HALF = 0.5; // below half its size a word is on its way in or out: the outline floor lets go
// a movement that has died down to this share of its size is over: an endless tail would nudge soft edges by
// fractions of a pixel for ever, which shows as shimmer
const SNAP = 1e-4;

function focusScene(c: Ctx<P> | LabelCtx<P>, o: Look): Scene {
  const fx = c.P.fx;
  const { colors } = c;
  const L = c.layout;
  const R = c.region;
  const S = L.size;
  const fm = fontMetrics(c.P.font);
  const gap = c.P.wordGap * S;
  const zeta = Math.min(fx.growDamp, 0.999);
  const over = 1 + Math.exp((-Math.PI * zeta) / Math.sqrt(1 - zeta * zeta)); // the growth spring overshoots to this
  const n = c.words.length;

  // the line(s) as they are right now: every word at its own size, centred on its baseline
  const lines = L.lines.map((ln) => ln.words.map((i) => c.words[i]));
  const size = c.words.map((w) => lerp(1, o.big(w), clamp(o.focus(w)))); // scale of each word at this instant
  const bigMax = Math.max(...c.words.map((w) => o.big(w)));
  const edge = (fx.halo + fx.glow) * S * bigMax * over; // what the biggest word has around its ink
  // (the gap between two words grows with them: it is `wordGap` of their mean size)
  let widest = 0;
  for (const ws of lines) {
    const flat = ws.reduce((a, w) => a + w.box.w, 0) + gap * (ws.length - 1);
    widest = Math.max(widest, flat + Math.max(...ws.map((w) => (o.big(w) * over - 1) * (w.box.w + (ws.length > 1 ? gap : 0)))));
  }
  const up = Math.max(fm.capH, fm.asc) * S * bigMax * over + edge; // above the first baseline
  const down = (fm.desc + fx.shadowY) * S * bigMax * over + edge; // below the last one
  const first = c.cy + L.lines[0].y;
  const last = c.cy + L.lines[L.lines.length - 1].y;
  const fit = Math.min(1, (R.x1 - R.x0) / (widest + 2 * edge), (R.y1 - R.y0) / (last - first + up + down));
  // scaled about the middle of the baselines, then moved so that the tallest word stays inside the box
  const mid: [number, number] = [c.cx, (first + last) / 2];
  const half = (last - first) / 2;
  const block = chain(
    pose(mid, { scale: fit }),
    translate(0, clamp(mid[1], R.y0 + (half + up) * fit, R.y1 - (half + down) * fit) - mid[1]),
  );

  const step = n > 1 ? Math.min(fx.shrinkStepS, (c.exit * fx.shrinkShare) / (n - 1)) : 0;
  const shrinkS = c.exit - step * (n - 1);
  const ringAlpha = 1 - Math.pow(1 - fx.glowAlpha, 1 / fx.glowRings);
  const units: Unit[] = [];
  for (const ws of lines) {
    const gapAfter = (k: number) => (k + 1 < ws.length ? (gap * (size[ws[k].index] + size[ws[k + 1].index])) / 2 : 0);
    const width = ws.reduce((a, w, k) => a + w.box.w * size[w.index] + gapAfter(k), 0);
    let x = c.cx - width / 2;
    for (const [k, w] of ws.entries()) {
      const i = w.index;
      const s = size[i];
      const at = x + (w.box.w * s) / 2; // where the middle of the word is now
      x += w.box.w * s + gapAfter(k);
      const tl = c.t - i * o.staggerS; // the word's own clock
      if (tl < 0) continue;
      const out = c.exit > 0 ? E.inCubic(prog(c.t, c.exitT + i * step, shrinkS)) : 0;
      const grown = (tl > springSettle(fx.growHz, fx.growDamp, SNAP) ? 1 : spring(tl, fx.growHz, fx.growDamp)) * (1 - out);
      if (grown < GONE) continue;
      const home: [number, number] = [c.cx + w.box.x + w.box.w / 2, c.cy + w.box.y]; // middle of the word on its baseline
      const m = chain(pose(home, { scale: s * grown }), translate(at - home[0], 0), block);
      const u = w.box.size;
      const halo = Math.max(fx.halo * u, fx.haloFloor / (fit * s * Math.max(grown, HALF)));
      const f = clamp(o.focus(w));
      const fill = o.fill(w);
      const layers: Layer[] = [];
      for (let k = fx.glowRings; k >= 1; k--) {
        layers.push({ kind: "glow", color: o.glow(w), width: halo + ((fx.glow * k) / fx.glowRings) * u, alpha: ringAlpha * f, steps: 1 });
      }
      layers.push(
        { kind: "stroke", color: colors.shadow, width: halo, dy: fx.shadowY * u },
        { kind: "stroke", color: colors.stroke, width: halo },
        { kind: "fill", color: fill },
      );
      // one z for all: the layers are drawn one kind at a time, so a glow never lies over a neighbour's letters
      units.push(wordUnit(c, w, { m, z: 0, fg: [fill], layers }));
    }
  }
  return { units, shapes: [] };
}

/** Where the lens is, in words (0 = on the first word): it sets off for a word when that word starts and
 * takes `lensS` to get there; when the next word starts sooner, it goes on from wherever it has got to. */
function lensAt(starts: number[], t: number, lensS: number): number {
  let from = 0;
  let k = 0;
  for (let j = 1; j < starts.length && t >= starts[j]; j++) {
    from = lerp(from, j - 1, E.inOutCubic(prog(starts[j], starts[j - 1], lensS)));
    k = j;
  }
  return lerp(from, k, E.inOutCubic(prog(t, starts[k], lensS)));
}

export default defineTemplate<P>({
  name: "focus",
  schema,
  build: (c): Scene => {
    const { P, colors } = c;
    const fx = P.fx;
    const lens = lensAt(
      c.words.map((w) => w.start),
      c.t,
      fx.lensS,
    );
    // after the last word the lens lets go: every word is small again before the exit
    const end = c.words[c.words.length - 1].activeEnd;
    const on = 1 - E.inOutCubic(prog(c.t, end, fx.lensS));
    return focusScene(c, {
      staggerS: P.timing.staggerS,
      big: (w) => (w.emph ? fx.emphScale : fx.focusScale),
      focus: (w) => Math.max(0, 1 - Math.abs(lens - w.index)) * on,
      fill: (w) => mix(w.emph ? colors.emph : colors.text, w.emph ? colors.emph : colors.active, clamp(w.a)),
      glow: (w) => (w.emph ? colors.emph : colors.accent), // a shouted word glows in its own colour
    });
  },
  // a label is under the lens as a whole: full size, glowing, grown out of the baseline
  label: (c): Scene =>
    focusScene(c, {
      staggerS: c.P.timing.staggerS,
      big: () => 1,
      focus: () => 1,
      fill: () => c.colors.text,
      glow: () => c.colors.accent,
    }),
});
