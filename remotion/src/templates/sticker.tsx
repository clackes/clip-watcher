import { z } from "zod";
import { apply, chain, pose, translate } from "../core/affine";
import { mix, parseHex } from "../core/color";
import { back, clamp, E, lerp, prog, pulse, shake, spring, springSettle } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type LabelCtx, type Layer, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * STICKER: every word is a die-cut sticker in its own candy colour: dark outline, fat white border, soft
 * shadow. The stickers are stuck on one after another, each a little askew, with a jelly squash; the one
 * being spoken lifts off the page (bigger, fatter border, deeper shadow), wobbles and gleams (its colour
 * lightens for a moment); shouted ones are bigger, in the emphasis colour and wobble harder. Before a pause they peel off one
 * by one: each swings round its top left corner and falls out of the box.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // dark outline, em
  haloFloor: z.number().min(0).max(10), // ...and never thinner than this on screen, px: what the fill is read against
  border: z.number().min(0.03).max(0.3), // the die-cut border reaches this far past the outline, em
  shadow: z.number().min(0).max(0.2), // the soft shadow spreads this far past the border, em
  shadowY: z.number().min(0).max(0.2), // and falls this far below the sticker, em
  shadowAlpha: z.number().min(0).max(0.9), // how dark it is right at the sticker's edge
  shadowRings: z.number().int().min(1).max(10), // rings the soft edge is built from
  tiltDeg: z.number().min(0).max(12), // stickers sit askew by up to this angle, alternately left and right
  tiltRise: z.number().min(0).max(0.3), // but the ends of a long word never rise more than this, em
  stickFrom: z.number().min(0.5).max(1), // a sticker starts at this scale
  stickHz: z.number().min(1).max(8), // and comes to its size this fast (a spring without overshoot), Hz
  stickTurn: z.number().min(1).max(5), // it lands turned this many times its tilt and straightens
  jelly: z.number().min(0).max(0.3), // squash and stretch on landing, share of its size
  jellyEnds: z.number().min(0).max(0.3), // but the ends of a long word never move more than this, em
  jellyHz: z.number().min(1).max(6),
  jellyDamp: z.number().min(1).max(40), // how fast the jelly settles, 1/s
  liftScale: z.number().min(1).max(1.3), // the spoken sticker grows to this
  liftEnds: z.number().min(0).max(0.3), // but the ends of a long word never move more than this, em
  liftOvershoot: z.number().min(0).max(4),
  liftY: z.number().min(0).max(0.3), // and rises, em
  liftBorder: z.number().min(0).max(0.1), // its border gets this much fatter, em
  liftShadow: z.number().min(0).max(0.2), // and its shadow falls this much further, em
  wobbleDeg: z.number().min(0).max(15), // it wobbles by this much as it lifts
  wobbleRise: z.number().min(0).max(0.3), // but the ends of a long word never swing more than this, em
  wobbleS: z.number().min(0).max(1.5),
  wobbleHz: z.number().min(1).max(6),
  emphWobble: z.number().min(1).max(3), // shouted stickers wobble this many times harder
  emphTilt: z.number().min(1).max(3), // and sit this many times more askew
  emphJelly: z.number().min(1).max(3), // and squash this many times more as they land
  gleam: z.number().min(0).max(0.8), // as it lifts its colour goes this far towards the border colour and back
  gleamS: z.number().min(0.15).max(2), // in this time
  peelDeg: z.number().min(0).max(45), // on the way out a sticker swings this far round its top left corner,
  peelDrop: z.number().min(0).max(3), // but the far end of a long word never drops more than this by the swing alone, em
  peelStepS: z.number().min(0).max(0.3), // stickers come off this long after each other
  peelGuard: z.number().min(0.1).max(0.6), // a sticker is gone once less than this is left inside the box, em
});
type P = z.infer<typeof schema>;

type Look = {
  first: number; // running number of the first word: decides which way stickers tilt and their colours
  enterS: number; // a sticker straightens in this time
  staggerS: number; // stickers go on this long after each other
  fill: (w: WordState) => string;
  // how much the word is lifted (0..1 and a little over) and since when it has been spoken (negative: not yet)
  spoken: (w: WordState) => { lift: number; since: number };
};

const rad = (deg: number) => (deg * Math.PI) / 180;
const PEAK_STEPS = 24; // samples taken to find the top of the lift's overshoot
// a movement that has died down to this share of its size is over: an endless tail would nudge soft edges by
// fractions of a pixel for ever, which shows as shimmer
const SNAP = 1e-4;
const deg = (r: number) => (r * 180) / Math.PI;
// two colours closer than this (8-bit levels per channel) cannot be told apart across a thin outline
const SAME = 48;
const alike = (a: string, b: string) => {
  const p = parseHex(a);
  const q = parseHex(b);
  return Math.max(Math.abs(p[0] - q[0]), Math.abs(p[1] - q[1]), Math.abs(p[2] - q[2])) < SAME;
};

/** A damped wobble that starts at 0, peaks at 1 a little later and rings out. */
function wobble(t: number, hz: number, damp: number): number {
  if (t <= 0 || Math.exp(-damp * t) < SNAP) return 0;
  const w = 2 * Math.PI * hz;
  const crest = Math.atan(w / damp) / w;
  return (Math.exp(-damp * t) * Math.sin(w * t)) / (Math.exp(-damp * crest) * Math.sin(w * crest));
}

function stickers(c: Ctx<P> | LabelCtx<P>, o: Look): Scene {
  const fx = c.P.fx;
  const { colors } = c;
  const L = c.layout;
  const R = c.region;
  const U = Math.max(...c.words.map((w) => w.box.size)); // one em of the biggest word

  // room the stickers need around the ink of the block, and the scale at which all of it fits the box
  const reach = (fx.halo + fx.border + fx.liftBorder) * U;
  const rise = (fx.tiltRise * fx.emphTilt * Math.max(1, fx.stickTurn * fx.stickFrom) + fx.wobbleRise * fx.emphWobble) * U;
  const liftTop = Math.max(...Array.from({ length: PEAK_STEPS + 1 }, (_, k) => back(fx.liftOvershoot)(k / PEAK_STEPS))); // peak of the lift
  const swell = ((fx.liftScale - 1) * liftTop * L.capH * (U / L.size)) / 2; // a lifted sticker is taller
  const padT = reach + rise + swell + fx.liftY * liftTop * U;
  const padB = reach + rise + swell + (fx.shadow + fx.shadowY + fx.liftShadow) * U;
  const padX = reach + (fx.shadow + fx.liftEnds * liftTop + fx.jellyEnds * fx.emphJelly) * U;
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

  const n = c.words.length;
  const step = n > 1 ? Math.min(fx.peelStepS, c.exit / (2 * (n - 1))) : 0;
  const peelS = c.exit - step * (n - 1);
  // the lowest line comes off first (nothing falls across a sticker that is still on), each line left to right
  const turnOf = new Map(
    [...c.words].sort((a, b) => b.box.line - a.box.line || a.index - b.index).map((w, k) => [w.index, k] as const),
  );
  const ringAlpha = 1 - Math.pow(1 - fx.shadowAlpha, 1 / fx.shadowRings);
  const window = { x: R.x0, y: R.y0, w: R.x1 - R.x0, h: R.y1 - R.y0 };
  const units: Unit[] = [];

  for (const w of c.words) {
    const i = w.index;
    const ti = c.t - i * o.staggerS; // this sticker's own clock
    if (ti < 0) continue;
    const u = w.box.size;
    const capH = L.capH * (u / L.size);
    const x0 = c.cx + w.box.x;
    const x1 = x0 + w.box.w;
    const base = c.cy + w.box.y;
    const dir = (o.first + i) % 2 ? 1 : -1;
    const ends = w.box.w / 2 / u; // half the word, em: what turns and swells is held to a distance at the ends
    const gain = w.emph ? fx.emphTilt : 1;
    const tilt = dir * Math.min(fx.tiltDeg * gain, deg(Math.atan((fx.tiltRise * gain) / ends)));

    // sticking on
    const size = ti > springSettle(fx.stickHz, 1, SNAP) ? 1 : lerp(fx.stickFrom, 1, spring(ti, fx.stickHz, 1));
    const turned = 1 + (fx.stickTurn - 1) * (1 - E.outCubic(prog(ti, 0, o.enterS)));
    const soft = w.emph ? fx.emphJelly : 1;
    const q = Math.min(fx.jelly * soft, (fx.jellyEnds * soft) / ends) * wobble(ti, fx.jellyHz, fx.jellyDamp);

    // being spoken
    const { lift, since } = o.spoken(w);
    const loud = w.emph ? fx.emphWobble : 1;
    const sway = dir * Math.min(fx.wobbleDeg * loud, deg(Math.atan((fx.wobbleRise * loud) / ends))) * shake(since, fx.wobbleS, fx.wobbleHz);
    const scale = size * (1 + Math.min(fx.liftScale - 1, fx.liftEnds / ends) * lift);
    const own = pose([w.cx, w.cy], { scale, sx: 1 + q, sy: 1 - q, rot: tilt * turned + sway, y: -fx.liftY * u * lift });

    // peeling off: it swings round its top left corner and falls
    const pe = c.exit > 0 ? prog(c.t, c.exitT + (turnOf.get(i) ?? i) * step, peelS) : 0;
    const swing = Math.min(fx.peelDeg, deg(Math.asin(Math.min(1, fx.peelDrop / (2 * ends)))));
    const corner = apply(own, x0, base - capH);
    const r = (fx.halo + fx.border) * u;
    const box = [
      [x0 - r, base - capH - r],
      [x1 + r, base - capH - r],
      [x0 - r, base + r],
      [x1 + r, base + r],
    ];
    const swung = chain(own, pose(corner, { rot: swing }), block);
    const fallBy = Math.max(0, R.y1 - Math.min(...box.map(([x, y]) => apply(swung, x, y)[1]))) / fit;
    const m = chain(own, pose(corner, { rot: swing * E.outQuad(pe) }), translate(0, fallBy * E.inQuad(pe)), block);
    if (pe > 0 && Math.min(...box.map(([x, y]) => apply(m, x, y)[1])) > R.y1 - fx.peelGuard * u * fit) continue;

    // colour: its own, lightened for a moment as the word is spoken
    const fill = mix(o.fill(w), colors.accent, fx.gleam * Math.sin(Math.PI * prog(since, 0, fx.gleamS)) ** 2);

    // the stack: soft shadow (straight down on screen, however the sticker is turned), border, outline, fill
    const turn = rad(tilt * turned + sway + swing * E.outQuad(pe));
    const fallen = (fx.shadowY + fx.liftShadow * clamp(lift)) * u;
    const dark = Math.max(fx.halo * u, fx.haloFloor / (fit * scale * (1 - Math.abs(q))));
    const edge = dark + (fx.border + fx.liftBorder * clamp(lift)) * u;
    const layers: Layer[] = [];
    for (let k = fx.shadowRings; k >= 1; k--) {
      layers.push({
        kind: "glow",
        color: colors.shadow,
        width: edge + (fx.shadow * u * k) / fx.shadowRings,
        alpha: ringAlpha,
        steps: 1,
        dx: fallen * Math.sin(turn),
        dy: fallen * Math.cos(turn),
      });
    }
    // (an emoji is a picture: it keeps its outline but gets no border, see the note in the report on emoji halos)
    if (!isEmoji(w.text, w.box.font)) {
      layers.push({ kind: "stroke", color: alike(fill, colors.accent) ? colors.accent2 : colors.accent, width: edge });
    }
    layers.push({ kind: "stroke", color: colors.stroke, width: dark }, { kind: "fill", color: fill });
    units.push(
      wordUnit(c, w, {
        m,
        z: (w.phase === "active" && !("label" in c) ? n : 0) + i, // later stickers go on top, the spoken one on top of all
        clip: pe > 0 ? window : undefined,
        fg: [fill],
        layers,
      }),
    );
  }
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "sticker",
  schema,
  build: (c): Scene => {
    const { P, colors } = c;
    const fx = P.fx;
    return stickers(c, {
      first: c.index * P.paging.maxWords,
      enterS: P.timing.enterS,
      staggerS: P.timing.staggerS,
      fill: (w) => (w.emph ? colors.emph : colors.cycle[(c.index * P.paging.maxWords + w.index) % colors.cycle.length]),
      spoken: (w) => ({
        lift: pulse(c.t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.liftOvershoot)),
        since: w.since,
      }),
    });
  },
  // a label is a sticker in the label's colour: it goes on, wobbles and gleams once, and peels off
  label: (c): Scene =>
    stickers(c, {
      first: c.label.id,
      enterS: c.P.label.enterS,
      staggerS: c.P.timing.staggerS,
      fill: () => c.colors.text,
      spoken: (w) => ({ lift: 0, since: c.t - w.index * c.P.timing.staggerS }),
    }),
});
