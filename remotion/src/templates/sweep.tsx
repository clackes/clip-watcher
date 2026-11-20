import { z } from "zod";
import { chain, pose, translate, type Affine } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, prog, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { dropShadow, wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate, em, wordCentre } from "../core/template";

/**
 * SWEEP: karaoke. One line of tall condensed capitals, all up from the start in the base colour. The
 * highlight colour fills each word from left to right exactly over the time it is spoken, behind a
 * leaning edge, and stays; the word swells as it fills and gives a small bump when it is full. The line
 * grows up out of its baseline to enter; to leave, the colour drains back and the line drops flat.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.3), // outline reach, em
  shadowY: z.number().min(0).max(0.3), // hard shadow drop, em
  riseFreq: z.number().min(1).max(12), // entrance spring: Hz
  riseDamp: z.number().min(0.2).max(1), // entrance spring: damping ratio (below 1 it overshoots)
  riseY: z.number().min(0).max(1), // a word starts this far below its place, em
  lean: z.number().min(-30).max(30), // the fill edge leans this many degrees (top ahead)
  tintS: z.number().min(0.03).max(0.5), // the fill takes this long to reach its full colour (it starts in the base colour)
  fillSwell: z.number().min(0.005).max(0.2), // a word has grown by this much when it is full
  fillLift: z.number().min(0).max(0.2), // and has risen this far, em
  bump: z.number().min(0).max(0.4), // the bump as the fill completes: extra growth
  bumpS: z.number().min(0.1).max(1), // time of the bump and of settling back to size
  emphBump: z.number().min(1).max(4), // a shouted word bumps this many times harder
  drainShare: z.number().min(0.1).max(1), // exit: the colour has drained back after this share of it
  dropFrom: z.number().min(0).max(0.9), // exit: the line starts to drop after this share of it
  dropY: z.number().min(0).max(1), // exit: and sinks this far while it flattens, em
  labelFillS: z.number().min(0.1).max(2), // label: its colour fills in over this time
});
type P = z.infer<typeof schema>;
type C = Ctx<P> | LabelCtx<P>;

/**
 * The fill of one word: `hi` up to share `p` of its width, `base` beyond, behind an edge that leans by
 * `lean` degrees. The gradient axis is laid so that p = 0 is all base and p = 1 all hi. The colours that
 * carry the letters this frame come back as `fg` (the filled part first).
 */
function swept(c: C, w: WordState, p: number, hi: string, base: string): { layer: Layer; fg: string[] } {
  if (p <= 0) return { layer: { kind: "fill", color: base }, fg: [base] };
  if (p >= 1) return { layer: { kind: "fill", color: hi }, fg: [hi] };
  const a = (c.P.fx.lean * Math.PI) / 180;
  const capH = c.layout.capH * (w.box.size / c.layout.size);
  const x0 = c.cx + w.box.x;
  const top = c.cy + w.box.y - capH * 1.15;
  const bottom = c.cy + w.box.y + capH * 0.2;
  // corners project onto the axis (cos a, sin a); the first and the last one bound it
  const from = a >= 0 ? top : bottom;
  const to = a >= 0 ? bottom : top;
  const len = w.box.w * Math.cos(a) + (to - from) * Math.sin(a);
  const grad: Grad = {
    x1: x0,
    y1: from,
    x2: x0 + len * Math.cos(a),
    y2: from + len * Math.sin(a),
    stops: [
      { o: 0, c: hi },
      { o: p, c: hi },
      { o: p, c: base },
      { o: 1, c: base },
    ],
  };
  return { layer: { kind: "fill", color: hi, grad }, fg: [hi, base] };
}

/** How much bigger a word is than at rest: it swells while it fills, bumps when full, then settles. */
function charge(c: C, p: number, sinceFull: number, hard: number): number {
  const fx = c.P.fx;
  if (p < 1) return fx.fillSwell * p;
  const x = clamp(sinceFull / fx.bumpS);
  return fx.fillSwell * (1 - x) * (1 - x) + fx.bump * hard * Math.sin(Math.PI * x) * (1 - x);
}

/**
 * Keeps the line in the caption box with everything that happens around it (outline, shadow, the entrance
 * from below, the overshoot, the bump): moves it up or down as little as needed and, in a box too low even
 * for that, makes it a little smaller. One transform for the whole line, the same for its whole life.
 */
function boxed(c: C, emoji: boolean): Affine {
  const fx = c.P.fx;
  const u = em(c);
  const capH = c.layout.capH;
  const most = fx.fillSwell + 0.6 * fx.bump * fx.emphBump; // the most a word grows: the bump peaks at about 0.6 of `bump`
  const over = fx.riseDamp < 1 ? Math.exp((-Math.PI * fx.riseDamp) / Math.sqrt(1 - fx.riseDamp * fx.riseDamp)) : 0; // of the entrance spring
  const up = fx.halo * u + (over + most / 2) * capH + (fx.fillLift * u * most) / fx.fillSwell + 3;
  const down = (fx.halo + fx.shadowY + fx.riseY + (emoji ? 0.14 : 0)) * u + (most * capH) / 2 + 3; // an emoji hangs below the baseline
  const top = c.cy + c.layout.top - up;
  const bottom = c.cy + c.layout.bottom + down;
  const [lo, hi] = [c.region.y0 + 2, c.region.y1 - 2];
  const s = Math.min(1, (hi - lo) / (bottom - top));
  const mid = (top + bottom) / 2;
  const half = ((bottom - top) / 2) * s;
  return chain(pose([c.cx, mid], { scale: s }), translate(0, Math.min(hi - half, Math.max(lo + half, mid)) - mid));
}

const outline = (c: C): Layer[] => {
  const fx = c.P.fx;
  const u = em(c);
  return [dropShadow(c.colors.shadow, fx.halo * u, 0, fx.shadowY * u), { kind: "stroke", color: c.colors.stroke, width: fx.halo * u }];
};

export default defineTemplate<P>({
  name: "sweep",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    const drain = c.exit > 0 ? E.inOutSine(prog(c.out, 0, fx.drainShare)) : 0;
    const fall = c.exit > 0 ? E.inCubic(prog(c.out, fx.dropFrom, 1 - fx.dropFrom)) : 0;
    const box = boxed(
      c,
      c.words.some((w) => isEmoji(w.text, w.box.font)),
    );
    const units: Unit[] = [];
    for (const w of c.words) {
      const t0 = w.index * P.timing.staggerS;
      if (t < t0) continue; // the line comes up word after word
      const up = spring(t - t0, fx.riseFreq, fx.riseDamp);
      const settle = 1 - E.outCubic(prog(t, t0, P.timing.enterS));
      // grows up out of its baseline, and drops flat onto it to leave
      const stand = pose([w.cx, c.cy + w.box.y], { sy: up * (1 - fall), y: settle * fx.riseY * u + fall * fx.dropY * u });

      const dur = Math.max(1e-3, Math.min(w.end, w.activeEnd) - w.start);
      const p = prog(t, w.start, dur); // linear: the edge keeps time with the voice
      // an emoji is a picture, drawn on whole pixels: it cannot swell slowly without stepping, so it pops for its time instead
      const grow = isEmoji(w.text, w.box.font) ? fx.bump * clamp(w.a) : charge(c, p, t - w.start - dur, w.emph ? fx.emphBump : 1);
      const own = pose(wordCentre(w), { scale: 1 + grow, y: (-fx.fillLift * u * grow) / fx.fillSwell });

      // the fill starts in the base colour and warms up (in its own time, however short the word); draining,
      // it cools down again as it runs back
      const tint = E.outCubic(prog(t, w.start, fx.tintS)) * (1 - drain);
      const hi = mix(colors.text, w.emph ? colors.emph : colors.active, tint);
      const { layer, fg } = swept(c, w, p * (1 - drain), hi, colors.text);
      units.push(wordUnit(c, w, { m: chain(own, stand, box), z: w.phase === "active" ? 1 : 0, fg, layers: [...outline(c), layer] }));
    }
    return { units, shapes: [] };
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    const u = em(c);
    const up = spring(c.t, fx.riseFreq, fx.riseDamp);
    const settle = 1 - E.outCubic(prog(c.t, 0, c.P.label.enterS));
    const fall = E.inCubic(c.out);
    const turn = chain(pose([c.cx, c.cy], { rot: c.P.label.rotate }), boxed(c, false));
    // one sweep across the whole label, line by line, in the label's colour over the plate-text colour
    const n = c.layout.lines.length;
    const all = prog(c.t, 0, fx.labelFillS) * n;
    const drain = E.inOutSine(prog(c.out, 0, fx.drainShare));
    const units = c.words.map((w) => {
      const L = c.layout.lines[w.box.line];
      const lineP = clamp(all - w.box.line); // this line's share of the sweep
      const p = clamp((lineP * L.w - (w.box.x - L.x)) / Math.max(1, w.box.w));
      const since = c.t - (fx.labelFillS * (w.box.line + (w.box.x - L.x + w.box.w) / Math.max(1, L.w))) / n;
      const grow = charge(c, p, since, 1);
      const stand = pose([w.cx, c.cy + w.box.y], { sy: up * (1 - fall), y: settle * fx.riseY * u + fall * fx.dropY * u });
      const own = pose(wordCentre(w), { scale: 1 + grow, y: (-fx.fillLift * u * grow) / fx.fillSwell });
      const tint = E.outCubic(prog(c.t, 0, fx.tintS)) * (1 - drain);
      const hi = mix(c.colors.plateText, c.colors.text, tint);
      const { layer, fg } = swept(c, w, p * (1 - drain), hi, c.colors.plateText);
      return wordUnit(c, w, { m: chain(own, stand, turn), z: 5, active: true, fg, layers: [...outline(c), layer] });
    });
    return { units, shapes: [] };
  },
});
