import { z } from "zod";
import { apply, chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, hash01, lerp, prog, shake, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type LabelCtx, type Layer, type Scene, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * SLAM: one word at a time, as big as the box allows. Every word comes down from far too big, hits,
 * flattens, shakes and throws out a deep extrusion in the accent colour; it leans a few degrees, the next
 * word the other way. Shouted words are the emphasis colour, bigger, and shake longer. Before a pause the
 * word tips over and drops out through the bottom of the box; without a pause the next word knocks it out
 * the same way, only faster (`paging.overlapS`).
 *
 * The block is always drawn through a window (the caption box): the oversized first frames are cut by it,
 * and so is the drop on the way out. At rest nothing is cut: the block is scaled so that its outline,
 * extrusion, lean, squash and shake all fit the box, whatever the box.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // outline reach, em
  rim: z.number().min(0).max(0.06), // the extrusion's dark edge reaches this far past the outline, and its colour stops this far short of it, em
  depthX: z.number().min(0).max(0.3), // the extrusion runs this far to the right, em
  depthY: z.number().min(0).max(0.3), // and this far down, em
  depthSteps: z.number().int().min(2).max(20), // silhouettes the extrusion is built from
  depthShade: z.number().min(0).max(0.8), // how much the far end of the extrusion darkens (0 = one flat colour)
  depthHz: z.number().min(1).max(8), // the extrusion shoots out on impact: spring frequency, Hz
  depthDamp: z.number().min(0.2).max(1), // and damping ratio
  depthIdle: z.number().min(0).max(1), // share of the depth a word keeps once it is no longer being spoken
  slamScale: z.number().min(1.2).max(4), // a word arrives this many times too big
  slamCurve: z.number().min(1).max(4), // how late the fall speeds up (1 = even speed, 2 = most of it in the second half)
  slamShare: z.number().min(0.1).max(1), // the fall never takes more than this share of the time the word is on screen
  coolShare: z.number().min(0.1).max(1), // and the face has its own colour by this share of it,
  coolMinS: z.number().min(0.07).max(0.5), // but the colour change never takes less than this
  slamTwistDeg: z.number().min(0).max(30), // and turned this much further than its lean
  squash: z.number().min(0).max(0.4), // how much it flattens on impact, share of its height
  squashWiden: z.number().min(0).max(1), // share of the squash that goes into width
  squashHz: z.number().min(1).max(6), // the flatten-and-rebound wobble, Hz
  squashDamp: z.number().min(1).max(40), // how fast the wobble dies, 1/s
  shake: z.number().min(0).max(0.15), // positional shake after the impact, em
  shakeS: z.number().min(0).max(1), // how long it lasts
  shakeHz: z.number().min(1).max(6),
  shakeLift: z.number().min(0).max(1), // share of the shake that also goes up and down
  shakeLiftHz: z.number().min(1).max(6), // at its own pace, so the word scribbles instead of sliding along a line
  emphShake: z.number().min(1).max(3), // shouted words shake this many times harder
  emphShakeS: z.number().min(0).max(1.5), // and this long
  leanMinDeg: z.number().min(0).max(10), // every word leans by an angle between these two,
  leanMaxDeg: z.number().min(0).max(10), // alternately left and right (keep `tilt` at the larger one)
  exitTipDeg: z.number().min(0).max(45), // on the way out the word tips over this far while it drops
  exitGuard: z.number().min(0.05).max(0.5), // em of the word still inside the box at the very end of the drop
  knockScale: z.number().min(0.1).max(0.49), // a word knocked out by the next one is at once this small, behind the newcomer,
  knockShare: z.number().min(0.2).max(1), // and gone after this share of the overlap
  labelShade: z.number().min(0.2).max(0.8), // a label's extrusion is its own colour, darkened by this much
});
type P = z.infer<typeof schema>;

type Look = {
  z: number; // a later word is drawn over the one it knocks out
  lean: number; // degrees
  loud: boolean; // shakes harder and longer
  live: number; // 0..1: being spoken (the extrusion is at full depth)
  enterS: number; // the fall takes this long (less for a word that is on screen only briefly)
  colorS: number; // the face cools from its impact colour in this time
  fill: (w: WordState, heat: number) => string; // face colour; heat 1 = still falling, 0 = cooled down
  near: string; // extrusion colour next to the face
  far: string; // and at its far end
};

const rad = (deg: number) => (deg * Math.PI) / 180;
const EPS_S = 1e-6; // slack when comparing times
const GONE = 0.03; // a word this small is not drawn any more

/** A damped wobble that starts at 0 on impact, peaks at 1 a little later and rings out. */
function wobble(tau: number, hz: number, damp: number): number {
  if (tau <= 0) return 0;
  const w = 2 * Math.PI * hz;
  const crest = Math.atan(w / damp) / w; // where the first crest is
  return (Math.exp(-damp * tau) * Math.sin(w * tau)) / (Math.exp(-damp * crest) * Math.sin(w * crest));
}

function slam(c: Ctx<P> | LabelCtx<P>, o: Look): Scene {
  const fx = c.P.fx;
  const L = c.layout;
  const R = c.region;
  const U = Math.max(...c.words.map((w) => w.box.size)); // one em of the biggest word
  const side = o.lean < 0 ? -1 : 1;
  // (a word that follows another without a pause lands after that one has been knocked out: `overlapS`)
  const fallS = Math.min(o.enterS, Math.max(c.P.paging.overlapS, fx.slamShare * c.dur));
  const coolS = clamp(fx.coolShare * c.dur - fallS, fx.coolMinS, Math.max(fx.coolMinS, o.colorS));
  const tau = c.t - fallS; // seconds since the impact

  // room the block needs around its ink, and the scale at which all of it fits the box
  const sin = Math.abs(Math.sin(rad(o.lean)));
  const cos = Math.cos(rad(o.lean));
  const bw = L.right - L.left;
  const bh = L.bottom - L.top;
  const gain = o.loud ? fx.emphShake : 1;
  const zeta = Math.min(fx.depthDamp, 0.999);
  const over = Math.exp((-Math.PI * zeta) / Math.sqrt(1 - zeta * zeta)); // overshoot of the depth spring
  const rebound = Math.exp(-fx.squashDamp / (2 * fx.squashHz)); // the stretch that follows the squash
  const edge = (fx.halo + fx.rim) * U;
  const padX = edge + (fx.depthX * U * (1 + over)) / 2 + fx.shake * gain * U + (fx.squash * fx.squashWiden * bw) / 2;
  const padY = edge + (fx.depthY * U * (1 + over)) / 2 + fx.shake * fx.shakeLift * gain * U + (fx.squash * rebound * bh) / 2;
  const needW = bw * cos + bh * sin + 2 * padX;
  const needH = bw * sin + bh * cos + 2 * padY;
  const fit = Math.min(1, (R.x1 - R.x0) / needW, (R.y1 - R.y0) / needH);
  const mid: [number, number] = [c.cx + (L.left + L.right) / 2, c.cy + (L.top + L.bottom) / 2];
  const home: [number, number] = [
    clamp(mid[0], R.x0 + (needW * fit) / 2, R.x1 - (needW * fit) / 2),
    clamp(mid[1], R.y0 + (needH * fit) / 2, R.y1 - (needH * fit) / 2),
  ];

  // the fall, the impact and what follows
  const fallIn = Math.pow(prog(c.t, 0, fallS), fx.slamCurve);
  const zoom = lerp(fx.slamScale, 1, fallIn);
  const twist = side * fx.slamTwistDeg * (1 - fallIn);
  const q = fx.squash * wobble(tau, fx.squashHz, fx.squashDamp);
  const amp = fx.shake * gain * U * fit;
  const shakeS = o.loud ? fx.emphShakeS : fx.shakeS;
  const jx = side * amp * shake(tau, shakeS, fx.shakeHz);
  const jy = amp * fx.shakeLift * shake(tau, shakeS, fx.shakeLiftHz);
  // (a word that is only an emoji has no extrusion: nothing grows, nothing shifts)
  const flat = c.words.every((w) => isEmoji(w.text, w.box.font));
  const depth = flat ? 0 : spring(tau, fx.depthHz, fx.depthDamp) * lerp(fx.depthIdle, 1, o.live);
  const heat = 1 - E.inOutSine(prog(c.t, fallS, coolS));

  // the way out. Without a pause the next word is already coming down on this one (`paging.overlapS`): it is
  // knocked back, small at once and gone before the newcomer lands. Before a pause it tips over and drops
  // until only `exitGuard` of it is left in the box.
  const knocked = c.exit > 0 && c.exit <= c.P.paging.overlapS + EPS_S && c.t >= c.exitT;
  const knock = knocked ? fx.knockScale * (1 - prog(c.t, c.exitT, c.exit * fx.knockShare)) : 1;
  if (knock < GONE) return { units: [], shapes: [] };
  const drop = knocked ? 0 : E.inQuad(c.out);
  const tipped = chain(pose(mid, { scale: fit, rot: o.lean + side * fx.exitTipDeg }), translate(home[0] - mid[0], home[1] - mid[1]));
  const top = Math.min(
    ...[
      [L.left, L.top],
      [L.right, L.top],
      [L.left, L.bottom],
      [L.right, L.bottom],
    ].map(([x, y]) => apply(tipped, c.cx + x, c.cy + y)[1]),
  );
  const dropBy = Math.max(0, R.y1 - fx.exitGuard * U * fit - top);

  const m = chain(
    // the far end of the extrusion stays where it is: the face rises as the extrusion grows
    translate((-fx.depthX * U * depth) / 2, (-fx.depthY * U * depth) / 2),
    pose(mid, {
      scale: fit * (knocked ? knock : zoom), // knocked out, it is small at once, wherever its own fall had got to
      sx: 1 + q * fx.squashWiden,
      sy: 1 - q,
      rot: o.lean + twist + side * fx.exitTipDeg * drop,
    }),
    translate(home[0] - mid[0] + jx, home[1] - mid[1] + jy + dropBy * drop),
  );
  const window = { x: R.x0, y: R.y0, w: R.x1 - R.x0, h: R.y1 - R.y0 };
  const n = fx.depthSteps;

  const units = c.words.map((w) => {
    const u = w.box.size;
    const halo = fx.halo * u;
    const ox = fx.depthX * u * depth;
    const oy = fx.depthY * u * depth;
    const fill = o.fill(w, heat);
    // an emoji is a picture, not an outline: it keeps the outline and gets no extrusion (every silhouette of a
    // picture is a filter pass, and faint specks in the emoji artwork grow into squares with each one)
    const steps = isEmoji(w.text, w.box.font) ? [] : Array.from({ length: n }, (_, k) => n - k);
    const layers: Layer[] = [];
    for (const i of steps) {
      layers.push({ kind: "stroke", color: c.colors.shadow, width: halo + fx.rim * u, dx: (ox * i) / n, dy: (oy * i) / n });
    }
    for (const i of steps) {
      layers.push({ kind: "stroke", color: mix(o.near, o.far, (i - 1) / (n - 1)), width: halo - fx.rim * u, dx: (ox * i) / n, dy: (oy * i) / n });
    }
    layers.push({ kind: "stroke", color: c.colors.stroke, width: halo }, { kind: "fill", color: fill });
    return wordUnit(c, w, { m, z: o.z, clip: window, fg: [fill], layers });
  });
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "slam",
  schema,
  build: (c): Scene => {
    const { P, colors } = c;
    const fx = P.fx;
    const text = c.words.map((w) => w.text).join(" ");
    return slam(c, {
      z: c.index,
      lean: (c.index % 2 ? 1 : -1) * lerp(fx.leanMinDeg, fx.leanMaxDeg, hash01(`${c.page.id}${text}`)),
      loud: c.words.some((w) => w.emph),
      live: Math.max(...c.words.map((w) => clamp(w.a))),
      enterS: P.timing.enterS,
      colorS: P.timing.colorS,
      // an ordinary word lands in the emphasis colour and cools to its own; a shouted one lands pale and heats up
      fill: (w, heat) =>
        mix(w.emph ? colors.emph : mix(colors.text, colors.active, clamp(w.a)), w.emph ? colors.active : colors.emph, heat),
      near: colors.accent,
      far: mix(colors.accent, colors.shadow, fx.depthShade),
    });
  },
  label: (c): Scene => {
    const { P, colors } = c;
    const fx = P.fx;
    return slam(c, {
      z: 0,
      lean: P.label.rotate,
      loud: c.label.kind !== "action",
      live: 1,
      enterS: P.label.enterS,
      colorS: P.timing.colorS,
      fill: (_w, heat) => mix(colors.text, colors.emph, heat),
      near: mix(colors.text, colors.shadow, fx.labelShade),
      far: mix(colors.text, colors.shadow, fx.labelShade + (1 - fx.labelShade) * fx.depthShade),
    });
  },
});
