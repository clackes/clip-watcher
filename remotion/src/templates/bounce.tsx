import { z } from "zod";
import { apply, chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { back, clamp, E, lerp, prog, pulse, shake, spring, springSettle } from "../core/ease";
import { fontMetrics } from "../core/measure";
import { paramsSchema } from "../core/schema";
import { dropShadow, ellipse, inked, wordUnit, type Ctx, type LabelCtx, type Scene, type Shape, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * BOUNCE: follow the bouncing ball. A glossy ball drops onto the first word and then hops in arcs from
 * word to word, landing on each one exactly when it is spoken; the word gives way under it, springs back
 * and takes the highlight, and the ball flattens on every landing too. On a shouted word it bounces twice
 * and the word flashes and shakes. Before a pause the ball bounces away through the top of the box and
 * the words drop out through the bottom.
 *
 * The line sits low in the box: the room above it belongs to the ball (the block is scaled so that the
 * highest arc fits, whatever the box).
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // outline of the words, em
  shadowY: z.number().min(0).max(0.2), // hard shadow under the words, em
  ballR: z.number().min(0.05).max(0.4), // radius of the ball, em
  ballRim: z.number().min(0.02).max(0.1), // its dark rim, em
  ballGap: z.number().min(0).max(0.2), // air between the resting ball and the word's outline, em
  shine: z.number().min(0).max(1), // the glint on the ball: how far its colour goes towards the plate text colour
  shineR: z.number().min(0).max(0.8), // its size, share of the ball's radius
  shineAt: z.number().min(0).max(0.8), // how far up and left of the centre it sits, share of the radius
  arc: z.number().min(0.1).max(1.2), // height of a full hop, em
  hopS: z.number().min(0.1).max(1), // a hop takes at most this long (the ball waits on the word until then)
  dropFrom: z.number().min(0).max(1.2), // the ball falls onto the first word from this high, em
  dropS: z.number().min(0.03).max(0.5), // in this time
  ballSquash: z.number().min(0).max(0.5), // how much the ball flattens on landing
  ballWiden: z.number().min(0).max(1), // share of that which goes into its width
  ballHz: z.number().min(1).max(6),
  ballDamp: z.number().min(1).max(40), // how fast its wobble dies, 1/s
  wordSquash: z.number().min(0).max(0.4), // how much a word gives way under the ball, share of its height
  wordWiden: z.number().min(0).max(1), // share of that which goes into its width
  wordHz: z.number().min(1).max(6),
  wordDamp: z.number().min(1).max(40),
  activeScale: z.number().min(1).max(1.3), // the spoken word is this much bigger
  growEnds: z.number().min(0).max(0.4), // but however it swells and spreads, the ends of a long word never move out more than this, em
  activeOvershoot: z.number().min(0).max(4),
  emphSquash: z.number().min(1).max(2.5), // a shouted word gives way this many times more
  emphBounce: z.number().min(0).max(0.6), // and the ball bounces on it a second time, this high, em
  emphBounceS: z.number().min(0.1).max(0.6), // the second bounce lasts this long
  emphFlash: z.number().min(0).max(1), // the shouted word flashes this far towards the text colour as the ball lands
  flashInS: z.number().min(0.07).max(0.3), // the flash comes up in this time (it fades in timing.colorS)
  emphShakeDeg: z.number().min(0).max(15),
  emphShakeRise: z.number().min(0).max(0.3), // the ends of a long word never swing more than this, em
  emphShakeS: z.number().min(0).max(1),
  emphShakeHz: z.number().min(1).max(6),
  enterFrom: z.number().min(0.5).max(1), // words start at this scale
  enterFall: z.number().min(0).max(1), // and this far above their place, em
  enterHz: z.number().min(1).max(8), // they settle with this spring
  enterDamp: z.number().min(0.2).max(1),
  flyShare: z.number().min(0.2).max(1), // on the way out the ball is gone after this share of the exit
  flySlow: z.number().min(0).max(0.9), // how much it slows down on the way up (0 = even speed)
  flyDrift: z.number().min(-2).max(2), // how far it drifts sideways on the way, em
  dropStepS: z.number().min(0).max(0.2), // words drop this long after each other
  dropTipDeg: z.number().min(0).max(45), // tipping over this far as they go
  dropGuard: z.number().min(0.1).max(0.6), // a word is gone once less than this is left inside the box, em
});
type P = z.infer<typeof schema>;

type Look = {
  staggerS: number; // words come in this long after each other
  landS: number[]; // when the ball lands on each word, page clock
  fill: (w: WordState, flash: number) => string; // flash: 0..1, the landing flash
  lift: (w: WordState) => number; // 0..1 (and a little over): how much the word is the spoken one
};

// letters that stop at the x-height: a word made only of these is lower, and the ball lands lower on it
const LOW = /^[acegmnopqrsuvwxyz]+$/;

/** A damped wobble that starts at 0, peaks at 1 a little later and rings out. */
function wobble(t: number, hz: number, damp: number): number {
  if (t <= 0 || Math.exp(-damp * t) < SNAP) return 0;
  const w = 2 * Math.PI * hz;
  const crest = Math.atan(w / damp) / w;
  return (Math.exp(-damp * t) * Math.sin(w * t)) / (Math.exp(-damp * crest) * Math.sin(w * crest));
}

const hump = (x: number) => (x <= 0 || x >= 1 ? 0 : 4 * x * (1 - x));
const PEAK_STEPS = 24; // samples taken to find the top of the highlight's overshoot
// a movement that has died down to this share of its size is over: an endless tail would nudge soft edges by
// fractions of a pixel for ever, which shows as shimmer
const SNAP = 1e-4;

function bounce(c: Ctx<P> | LabelCtx<P>, o: Look): Scene {
  const fx = c.P.fx;
  const { colors } = c;
  const L = c.layout;
  const R = c.region;
  const U = Math.max(...c.words.map((w) => w.box.size)); // one em of the biggest word
  const fm = fontMetrics(c.P.font);
  const tall = Math.max(fm.capH, fm.asc);
  const r = fx.ballR * U;
  const rim = fx.ballRim * U;

  // room around the ink of the block: the ball's arc above, outline and shadow elsewhere; and the scale
  // at which all of it fits the box
  const zeta = Math.min(fx.enterDamp, 0.999);
  const over = Math.exp((-Math.PI * zeta) / Math.sqrt(1 - zeta * zeta));
  const liftTop = Math.max(...Array.from({ length: PEAK_STEPS + 1 }, (_, k) => back(fx.activeOvershoot)(k / PEAK_STEPS)));
  const swell = fx.activeScale - 1;
  const swollen = swell * liftTop; // the spoken word at the top of its overshoot
  const padT = (fx.halo + fx.ballGap + swollen * tall) * U + 2 * (r + rim) + Math.max(fx.arc, fx.dropFrom, fx.emphBounce) * U;
  const padB = (fx.halo + fx.shadowY + swollen * (fm.desc + fx.halo + fx.shadowY) + over * fx.enterFall + fx.emphShakeRise) * U;
  const padX = (fx.halo + fx.growEnds * liftTop) * U;
  const bw = L.right - L.left;
  const bh = L.bottom - L.top;
  const fit = Math.min(1, (R.x1 - R.x0) / (bw + 2 * padX), (R.y1 - R.y0) / (bh + padT + padB));
  const mid: [number, number] = [c.cx + (L.left + L.right) / 2, c.cy + (L.top + L.bottom) / 2];
  const block = chain(
    pose(mid, { scale: fit }),
    translate(
      clamp(mid[0], R.x0 + (bw / 2 + padX) * fit, R.x1 - (bw / 2 + padX) * fit) - mid[0],
      clamp(mid[1], R.y0 + (bh / 2 + padT) * fit, R.y1 - (bh / 2 + padB) * fit) - mid[1],
    ),
  );
  const window = { x: R.x0, y: R.y0, w: R.x1 - R.x0, h: R.y1 - R.y0 };

  // every word at this instant
  const n = c.words.length;
  const step = n > 1 ? Math.min(fx.dropStepS, c.exit / (2 * (n - 1))) : 0;
  const dropS = c.exit - step * (n - 1);
  const words = c.words.map((w, i) => {
    const u = w.box.size;
    const tl = c.t - i * o.staggerS; // the word's own clock
    const s = tl > springSettle(fx.enterHz, fx.enterDamp, SNAP) ? 1 : spring(tl, fx.enterHz, fx.enterDamp);
    const landed = c.t - o.landS[i];
    const q = fx.wordSquash * (w.emph ? fx.emphSquash : 1) * wobble(landed, fx.wordHz, fx.wordDamp);
    const scale = lerp(fx.enterFrom, 1, s) * (1 + Math.min(swell, fx.growEnds / (w.box.w / 2 / u)) * o.lift(w));
    const y = -(1 - s) * fx.enterFall * u;
    const base = c.cy + w.box.y;
    const top = (LOW.test(w.text) ? fm.xH : tall) * u;
    // where the ball's centre is when it rests on this word (the word's own squash and size included)
    const rest = base + y - (top + fx.halo * u) * scale * (1 - q) - fx.ballGap * U - rim - r;
    return { w, i, u, tl, q, scale, y, base, rest, landed };
  });

  // the ball: it only visits the words of the first line
  const stops = words.filter((v) => v.w.box.line === 0);
  const shapes: Shape[] = [];
  if (stops.length > 0) {
    let k = -1;
    stops.forEach((v, j) => {
      if (v.landed >= 0) k = j;
    });
    let bx = stops[0].w.cx;
    let by = stops[0].rest;
    if (k < 0) {
      const p = prog(c.t, 0, o.landS[stops[0].i]);
      by -= fx.dropFrom * U * (1 - p * p);
    } else if (k + 1 < stops.length) {
      const a = stops[k];
      const b = stops[k + 1];
      const t1 = o.landS[b.i];
      const t0 = Math.max(o.landS[a.i], t1 - fx.hopS);
      const p = prog(c.t, t0, t1 - t0);
      bx = lerp(a.w.cx, b.w.cx, p);
      by = lerp(a.rest, b.rest, p) - fx.arc * U * Math.min(1, (t1 - t0) / fx.hopS) * 4 * p * (1 - p);
    } else {
      bx = stops[k].w.cx;
      by = stops[k].rest;
    }
    const qb = k >= 0 ? fx.ballSquash * wobble(stops[k].landed, fx.ballHz, fx.ballDamp) : 0;
    // on a shouted word it bounces a second time, once the word has given way, if there is time before the next hop
    if (k >= 0 && stops[k].w.emph) {
      const leaves = k + 1 < stops.length ? Math.max(o.landS[stops[k].i], o.landS[stops[k + 1].i] - fx.hopS) : Infinity;
      const gave = 1 / (4 * fx.wordHz); // the word is at its lowest a quarter of its wobble after the landing
      if (leaves - o.landS[stops[k].i] >= gave + fx.emphBounceS) by -= fx.emphBounce * U * hump((stops[k].landed - gave) / fx.emphBounceS);
    }
    // on the way out it bounces away through the top of the box
    const fly = c.exit > 0 ? prog(c.t, c.exitT, c.exit * fx.flyShare) : 0;
    const flown = fly * (1 + fx.flySlow * (1 - fly));
    const [, sy0] = apply(block, bx, by);
    const up = (Math.max(0, sy0 - R.y0) / fit + r + rim) * flown;
    const cx = bx + fx.flyDrift * U * flown;
    const cy = by + r * qb - up;
    const rx = r * (1 + qb * fx.ballWiden);
    const ry = r * (1 - qb);
    if (apply(block, cx, cy + ry + rim)[1] > R.y0) {
      const id = "label" in c ? `l${c.label.id}` : `p${c.page.id}`;
      const clip = fly > 0 ? window : undefined;
      shapes.push(
        ellipse(`${id}ball`, cx, cy, rx, ry, { fill: colors.accent, stroke: colors.stroke, strokeWidth: 2 * rim, m: block, z: 5, clip }),
        ellipse(`${id}shine`, cx - rx * fx.shineAt, cy - ry * fx.shineAt, rx * fx.shineR, ry * fx.shineR, {
          fill: mix(colors.accent, colors.plateText, fx.shine),
          m: block,
          z: 6,
          clip,
        }),
      );
    }
  }

  const units: Unit[] = [];
  for (const v of words) {
    if (v.tl < 0) continue;
    const { w, i, u } = v;
    const dir = i % 2 ? 1 : -1;
    // the landing flash comes up evenly. (Eased out it was four fifths up after a third of its time: from a
    // strong colour towards white that is nearly the whole change in sRGB, in one frame at 30 fps whenever
    // the ball landed just after a frame.)
    const flash = v.landed < 0 ? 0 : prog(v.landed, 0, fx.flashInS) * (1 - E.inOutSine(prog(v.landed, fx.flashInS, c.P.timing.colorS)));
    const fill = o.fill(w, flash);
    const ends = w.box.w / 2 / u; // half the word, em
    const own = pose([w.cx, v.base], {
      scale: v.scale,
      sx: Math.min(1 + v.q * fx.wordWiden, (1 + fx.growEnds / ends) / v.scale),
      sy: 1 - v.q,
      rot: w.emph
        ? dir * Math.min(fx.emphShakeDeg, (Math.atan(fx.emphShakeRise / ends) * 180) / Math.PI) * shake(v.landed, fx.emphShakeS, fx.emphShakeHz)
        : 0,
      y: v.y,
    });
    // on the way out the words tip over and drop through the bottom of the box, one after another
    const pe = c.exit > 0 ? prog(c.t, c.exitT + i * step, dropS) : 0;
    const reach = (fx.halo + fx.shadowY) * u;
    const capH = Math.max(fm.capH, fm.asc) * u;
    const box = [
      [c.cx + w.box.x - reach, v.base - capH - reach],
      [c.cx + w.box.x + w.box.w + reach, v.base - capH - reach],
      [c.cx + w.box.x - reach, v.base + reach],
      [c.cx + w.box.x + w.box.w + reach, v.base + reach],
    ];
    const tipped = chain(own, pose([w.cx, w.cy], { rot: dir * fx.dropTipDeg }), block);
    const fallBy = Math.max(0, R.y1 - Math.min(...box.map(([x, y]) => apply(tipped, x, y)[1]))) / fit;
    const m = chain(own, pose([w.cx, w.cy], { rot: dir * fx.dropTipDeg * E.inQuad(pe) }), translate(0, fallBy * E.inQuad(pe)), block);
    if (pe > 0 && Math.min(...box.map(([x, y]) => apply(m, x, y)[1])) > R.y1 - fx.dropGuard * u * fit) continue;
    units.push(
      wordUnit(c, w, {
        m,
        z: w.phase === "active" ? 1 : 0,
        clip: pe > 0 ? window : undefined,
        fg: [fill],
        layers: inked(fill, colors.stroke, fx.halo * u, [dropShadow(colors.shadow, fx.halo * u, 0, fx.shadowY * u)]),
      }),
    );
  }
  return { units, shapes };
}

export default defineTemplate<P>({
  name: "bounce",
  schema,
  build: (c): Scene => {
    const { P, colors } = c;
    const fx = P.fx;
    return bounce(c, {
      staggerS: P.timing.staggerS,
      // the ball lands on a word when it is spoken; the first one it reaches by falling in
      landS: c.words.map((w) => Math.max(w.start, fx.dropS)),
      fill: (w, flash) => (w.emph ? mix(colors.emph, colors.text, fx.emphFlash * flash) : mix(colors.text, colors.active, clamp(w.a))),
      lift: (w) => pulse(c.t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot)),
    });
  },
  // a label: the ball falls onto it and then visits its words one after another
  label: (c): Scene =>
    bounce(c, {
      staggerS: c.P.timing.staggerS,
      landS: c.words.map((_w, i) => c.P.fx.dropS + i * c.P.fx.hopS),
      fill: () => c.colors.text,
      lift: () => 0,
    }),
});
