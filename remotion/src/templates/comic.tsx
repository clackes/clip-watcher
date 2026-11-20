import { z } from "zod";
import { apply, chain, pose, type Affine } from "../core/affine";
import { fromOklab, mix, parseHex, toHex, toOklab } from "../core/color";
import { back, E, hash01, lerp, pulse, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema, type Region } from "../core/schema";
import { extrude, path, wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Scene, type Shape, type Unit, type WordState } from "../core/scene";
import { defineTemplate, em } from "../core/template";

/**
 * COMIC: a comic-book title. Slanted caps with a yellow-to-orange gradient, a thick black outline and a
 * red 3D side with its own black line. Every word spins in on an elastic spring, one after the other; the
 * spoken word punches up and turns white-to-cyan; behind every shouted word a two-colour starburst bursts
 * open, its spikes travelling round. Before a pause the block zooms away, every word spinning. Labels sit
 * on a burst of their own.
 *
 * The caption box can be very low (107 px) or roomy (340 px), so nothing here turns or grows by a fixed
 * amount: the slant, every spin, every punch and every burst is the largest one, up to its parameter, that
 * stays inside the box. `label.rotate` is 0 in the JSON for the same reason (the layout would shrink the
 * label to make room for it); the label's tilt is `labelSlantDeg`, fitted like the rest.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.3), // black outline around the letters, em
  overhang: z.number().min(0).max(0.2), // the font's italic letters lean this far out of their own width, em
  rim: z.number().min(0).max(0.1), // black line around the 3D side, em
  sideX: z.number().min(-0.3).max(0.3), // how far the 3D side reaches right, em
  sideY: z.number().min(0).max(0.3), // and down, em
  sideSteps: z.number().int().min(1).max(8), // silhouettes the side is built from
  gradTop: z.number().min(0).max(1), // the top colour holds down to this share of the cap height
  gradBottom: z.number().min(0).max(1), // and the bottom colour starts here
  slantDeg: z.number().min(0).max(12), // the block rises to the right by this much, degrees
  clearBelow: z.number().min(0).max(1), // room kept under the text for bursts when the box has it, em
  enterScale: z.number().min(0.5).max(1), // a word pops in from this scale
  enterFreq: z.number().min(1).max(5), // pop spring, Hz
  enterDamp: z.number().min(0.2).max(1), // pop spring damping (low = elastic)
  spinDeg: z.number().min(0).max(90), // and from this far turned, alternating left and right
  spinFreq: z.number().min(1).max(4), // spin spring, Hz
  spinDamp: z.number().min(0.2).max(1),
  activeScale: z.number().min(1).max(1.5), // the spoken word punches up to this
  activeLift: z.number().min(0).max(0.3), // and lifts, em
  activeTiltDeg: z.number().min(0).max(8), // and leans, degrees
  activeOvershoot: z.number().min(0).max(4),
  emphScale: z.number().min(1).max(1.6), // a shouted word punches up to this while spoken
  burstPadX: z.number().min(0).max(1.5), // the burst reaches this far past its word, em
  burstPadY: z.number().min(0).max(1.5),
  burstPoints: z.number().int().min(6).max(28), // spikes
  burstInner: z.number().min(0.4).max(0.95), // valley radius as a share of the spike radius
  burstJag: z.number().min(0).max(0.4), // how uneven the spikes are
  burstCore: z.number().min(0.3).max(0.95), // size of the light inner burst as a share of the outer one
  // the inner burst is the burst colour taken this far towards white (never white itself: no letter colour
  // may be repeated right behind the letters)
  burstTint: z.number().min(0).max(0.8),
  burstRim: z.number().min(0.02).max(0.15), // dark line around the burst, em
  burstTurn: z.number().min(-90).max(90), // the spikes travel round at this speed, degrees a second (the core the other way)
  burstDelayS: z.number().min(0).max(0.5), // the burst opens this long after its word arrived
  burstFrom: z.number().min(0).max(1), // from this share of its size (about its word's: it grows out from behind the letters)
  burstFreq: z.number().min(1).max(5), // opening spring, Hz
  burstDamp: z.number().min(0.2).max(1),
  burstKick: z.number().min(0).max(0.5), // the burst swells by this share while its word is spoken
  exitSpinDeg: z.number().min(0).max(360), // every word turns this far while the block zooms away
  exitSwingDeg: z.number().min(0).max(20), // and the block itself swings by this much
  labelSlantDeg: z.number().min(0).max(12), // resting tilt of a label, degrees
  labelSpinDeg: z.number().min(0).max(90), // a label spins in from this far
  labelPadX: z.number().min(0).max(2), // the label's burst reaches this far past the text, em
  labelPadY: z.number().min(0).max(2),
  labelPoints: z.number().int().min(6).max(32),
  labelTop: z.number().min(0).max(1), // how far the top of a label's letters goes towards white
  labelHueGap: z.number().min(0).max(180), // the label's burst keeps this many degrees of hue from its letters
});
type P = z.infer<typeof schema>;
type FX = P["fx"];

type Box = { l: number; t: number; r: number; b: number };
type Pt = [number, number];
const SAMPLES = 24; // instants of a motion that are tried when fitting it into the box
const TRIES = 9; // halvings when looking for the largest amplitude that fits
const SLACK = 1.5; // px kept free at the box edge (anti-aliasing)
const RAMP = 3; // segments a two-colour gradient is drawn with: OKLab steps, so no muddy middle
const GREY = 0.045; // OKLCh chroma under which a colour has no hue to speak of (palette.CHROMA_MIN)
const LEAST = 4.3; // px of outline a fill needs next to it at any size and scale: the frame checks read it against the 2 to 3 px around it, corners included
const JUDGED = 0.5; // the scale down to which a word on its way in or out is still read (and measured)

/**
 * The parameters for text of size `u` drawn at `scale`: the outline is never under `LEAST` px on screen.
 * In small text, and while a word is small on its way in or out, it is that many px instead of its
 * width in em.
 */
const thick = (fx: FX, u: number, scale = 1): FX => ({ ...fx, halo: Math.max(fx.halo, LEAST / (Math.max(JUDGED, Math.min(1, scale)) * u)) });

const corners = (b: Box, m?: Affine): Pt[] => {
  const p: Pt[] = [
    [b.l, b.t],
    [b.r, b.t],
    [b.r, b.b],
    [b.l, b.b],
  ];
  return m ? p.map(([x, y]) => apply(m, x, y)) : p;
};

/** How far the points stick out of the region (px; 0 or less = inside). */
function over(r: Region, pts: Pt[]): number {
  let o = -Infinity;
  for (const [x, y] of pts) o = Math.max(o, r.x0 + SLACK - x, x - (r.x1 - SLACK), r.y0 + SLACK - y, y - (r.y1 - SLACK));
  return o;
}

/** The largest amplitude in 0..max that stays inside (out(amp) <= 0). */
function largest(max: number, out: (amp: number) => number): number {
  if (max <= 0) return 0;
  if (out(max) <= 0) return max;
  let lo = 0;
  let hi = max;
  for (let i = 0; i < TRIES; i++) {
    const mid = (lo + hi) / 2;
    if (out(mid) <= 0) lo = mid;
    else hi = mid;
  }
  return lo;
}

/** Worst overflow of a motion over its samples: `at(k)` gives the points at sample k of 0..SAMPLES. */
function worst(r: Region, at: (k: number) => Pt[]): number {
  let o = -Infinity;
  for (let k = 0; k <= SAMPLES; k++) o = Math.max(o, over(r, at(k / SAMPLES)));
  return o;
}

/** Ink box of a word's fill before any transform, grown by `pad` px (what its layers reach). */
function wordBox(c: Ctx | LabelCtx, w: WordState, pad: number): Box {
  const capH = c.layout.capH * (w.box.size / c.layout.size);
  return { l: w.cx - w.box.w / 2 - pad, r: w.cx + w.box.w / 2 + pad, t: w.cy - capH / 2 - pad, b: w.cy + capH / 2 + pad };
}

/**
 * Where the block rests: turned by as much of `slant` as fits, then moved up so that `clear` px stay free
 * under it when the box has the room (bursts need it), and never out of the box. `pts` are the corners
 * of everything in the block, at its largest.
 */
function rest(c: Ctx | LabelCtx, pts: Pt[], slant: number, clear: number): { m: Affine; deg: number } {
  const r = c.region;
  const span = (m: Affine) => {
    const q = pts.map(([x, y]) => apply(m, x, y));
    return { l: Math.min(...q.map((p) => p[0])), r: Math.max(...q.map((p) => p[0])), t: Math.min(...q.map((p) => p[1])), b: Math.max(...q.map((p) => p[1])) };
  };
  const turned = (deg: number) => pose([c.cx, c.cy], { rot: -deg });
  const deg = largest(slant, (d) => {
    const s = span(turned(d));
    return Math.max(s.b - s.t - (r.y1 - r.y0), s.r - s.l - (r.x1 - r.x0)) + 2 * SLACK;
  });
  const s = span(turned(deg));
  let ty = Math.min(0, r.y1 - SLACK - clear - s.b);
  ty = Math.max(ty, Math.min(0, r.y0 + SLACK - s.t));
  if (s.b + ty > r.y1 - SLACK || s.t + ty < r.y0 + SLACK) ty = (r.y0 + r.y1) / 2 - (s.t + s.b) / 2;
  const tx = s.r > r.x1 - SLACK || s.l < r.x0 + SLACK ? (r.x0 + r.x1) / 2 - (s.l + s.r) / 2 : 0;
  return { m: pose([c.cx, c.cy], { rot: -deg, x: tx, y: ty }), deg };
}

/** Radii of the largest ellipse, up to (rx, ry), that keeps within roomX / roomY of its centre when turned by `deg`. */
function fitEllipse(rx: number, ry: number, roomX: number, roomY: number, deg: number): [number, number] {
  const s = Math.sin((deg * Math.PI) / 180);
  const k = Math.cos((deg * Math.PI) / 180);
  const x = Math.min(rx, Math.sqrt(Math.max(0, roomX * roomX - ry * s * (ry * s))) / k);
  const y = Math.min(ry, Math.sqrt(Math.max(0, roomY * roomY - x * s * (x * s))) / k);
  return [Math.max(0, x), Math.max(0, y)];
}

/** A starburst inside the ellipse (rx, ry): `n` spikes of uneven length, turned by `phase` radians. */
function burstPath(cx: number, cy: number, rx: number, ry: number, n: number, inner: number, jag: number, phase: number, seed: string): string {
  let d = "";
  for (let k = 0; k < 2 * n; k++) {
    const a = phase + (Math.PI * k) / n;
    const f = k % 2 === 0 ? 1 - jag * hash01(`${seed}s${k}`) : inner * (1 - 0.5 * jag * hash01(`${seed}v${k}`));
    d += `${k === 0 ? "M" : "L"}${(cx + rx * f * Math.cos(a)).toFixed(2)} ${(cy + ry * f * Math.sin(a)).toFixed(2)}`;
  }
  return `${d}Z`;
}

/**
 * A burst: dark rim, the burst, a lighter core turning the other way. All rims are drawn under all bursts
 * and those under all cores, so bursts that touch merge into one splash.
 */
function burst(
  id: string, fx: FX, cx: number, cy: number, rx: number, ry: number, n: number, turn: number, rim: number,
  col: { fill: string; core: string; line: string }, m: Affine, z: number,
): Shape[] {
  const d = burstPath(cx, cy, rx, ry, n, fx.burstInner, fx.burstJag, turn, id);
  const core = burstPath(cx, cy, rx * fx.burstCore, ry * fx.burstCore, n, fx.burstInner, fx.burstJag, Math.PI / n - turn, `${id}c`);
  return [
    path(`${id}r`, d, { fill: col.line, stroke: col.line, strokeWidth: rim * 2, m, z }),
    path(`${id}f`, d, { fill: col.fill, m, z: z + 1 }),
    path(`${id}c`, core, { fill: col.core, m, z: z + 2 }),
  ];
}

/** Colour between a and b going round the hue circle (OKLCh), so orange reaches cyan through lime and not through grey. */
function sweep(a: string, b: string, t: number): string {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const A = toOklab(parseHex(a));
  const B = toOklab(parseHex(b));
  const ca = Math.hypot(A[1], A[2]);
  const cb = Math.hypot(B[1], B[2]);
  if (ca < GREY || cb < GREY) return mix(a, b, t);
  const ha = Math.atan2(A[2], A[1]);
  let dh = Math.atan2(B[2], B[1]) - ha;
  if (dh > Math.PI) dh -= 2 * Math.PI;
  if (dh < -Math.PI) dh += 2 * Math.PI;
  const h = ha + dh * t;
  const ch = lerp(ca, cb, t);
  return toHex(fromOklab([lerp(A[0], B[0], t), ch * Math.cos(h), ch * Math.sin(h)]));
}

/** A gradient from `top` to `bottom` between two heights, and the colours it runs through. */
function ramp(top: string, bottom: string, y0: number, y1: number): { grad: Grad; fg: string[] } {
  const fg = Array.from({ length: RAMP + 1 }, (_, k) => mix(top, bottom, k / RAMP));
  return { fg, grad: { x1: 0, y1: y0, x2: 0, y2: Math.max(y0 + 1, y1), stops: fg.map((c, k) => ({ o: k / RAMP, c })) } };
}

/**
 * Outline, 3D side with its own dark line, gradient fill. `flat` (an emoji) gets the outline alone: the
 * Stage grows an emoji's halos from the picture's alpha, and a run of shifted halos smears whatever faint
 * pixels its corners have.
 */
function letters(fx: FX, u: number, grad: Grad, stroke: string, side: string, flat = false): Layer[] {
  const dx = fx.sideX * u;
  const dy = fx.sideY * u;
  const face: Layer[] = [
    { kind: "stroke", color: stroke, width: fx.halo * u },
    { kind: "fill", color: grad.stops[0].c, grad },
  ];
  if (flat) return face;
  return [
    ...extrude(stroke, (fx.halo + fx.rim) * u, dx, dy, fx.sideSteps),
    ...extrude(side, fx.halo * u, dx, dy, fx.sideSteps),
    ...face,
  ];
}

/** Everything a layer stack reaches past the fill, px. */
const reach = (fx: FX, u: number): number => (fx.overhang + fx.halo + fx.rim + Math.hypot(fx.sideX, fx.sideY)) * u + 1;

const hueOf = (hex: string): { h: number; c: number } => {
  const [, a, b] = toOklab(parseHex(hex));
  return { h: (Math.atan2(b, a) * 180) / Math.PI, c: Math.hypot(a, b) };
};
const hueGap = (a: number, b: number): number => {
  const d = Math.abs(a - b) % 360;
  return d > 180 ? 360 - d : d;
};

export default defineTemplate<P>({
  name: "comic",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = thick(P.fx, c.layout.size);
    const u = em(c);
    const pad = reach(fx, u);
    const peak = 1 + 0.08 * fx.activeOvershoot; // a back ease with this overshoot tops out about here
    const punch = (w: WordState) => (w.emph ? fx.emphScale : fx.activeScale) - 1;
    const capOf = (w: WordState) => c.layout.capH * (w.box.size / c.layout.size);

    // the block at rest: placed with every word as big and as high as its punch takes it
    const grown = c.words.flatMap((w) => corners(wordBox(c, w, pad), pose([w.cx, w.cy], { scale: 1 + punch(w) * peak, y: -fx.activeLift * u * peak })));
    const { m: resting, deg: slant } = rest(c, grown, fx.slantDeg, fx.clearBelow * u);
    const centre = apply(resting, c.cx, c.cy);
    const away = (out: number, swing: number): Affine =>
      chain(resting, pose(centre, { scale: 1 - E.inCubic(out), rot: swing * E.inQuad(out) }));
    const brief = P.timing.exitS > 0 ? Math.min(1, c.exit / P.timing.exitS) : 1; // an exit cut short turns less
    const swing = largest(fx.exitSwingDeg * brief, (a) => worst(c.region, (k) => c.words.flatMap((w) => corners(wordBox(c, w, pad), away(k, a)))));
    const page = c.out > 0 ? away(c.out, swing) : resting;

    const shapes: Shape[] = [];
    const units: Unit[] = [];
    const settle = 1.2 / Math.min(fx.spinFreq, fx.enterFreq); // entrances are fitted over this long
    for (const w of c.words) {
      const since = t - w.index * P.timing.staggerS; // seconds since this word popped in
      if (since < 0) continue;
      const box = wordBox(c, w, pad);
      const at: Pt = [w.cx, w.cy];
      // how much this word may grow where it stands, and so how hard it punches
      const roomy = largest(1, (g) => over(c.region, corners(box, chain(pose(at, { scale: 1 + g }), resting))));
      const grow = Math.min(punch(w), roomy / peak);
      const lift = Math.min(fx.activeLift * u, (roomy * capOf(w)) / 2);
      const dir = w.index % 2 === 0 ? -1 : 1;
      const own = (s: number, spin: number, act: number, out: number, spinOut: number): Affine =>
        pose(at, {
          scale: Math.min(1 + roomy, lerp(fx.enterScale, 1, spring(s, fx.enterFreq, fx.enterDamp)) * (1 + grow * act)),
          rot: dir * spin * (1 - spring(s, fx.spinFreq, fx.spinDamp)) - dir * fx.activeTiltDeg * act + dir * spinOut * E.inQuad(out),
          y: -lift * act,
        });
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));
      const spin = largest(fx.spinDeg, (amp) => worst(c.region, (k) => corners(box, chain(own(settle * k, amp, peak, 0, 0), resting))));
      // the exit starts from wherever the highlight is by then
      const a0 = pulse(c.exitT, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));
      const spinOut =
        c.out > 0 ? largest(fx.exitSpinDeg * brief, (amp) => worst(c.region, (k) => corners(box, chain(own(settle, 0, a0, k, amp), away(k, swing))))) : 0;

      const tint = Math.min(1, Math.max(0, w.a));
      const y1 = c.cy + w.box.y;
      const y0 = y1 - capOf(w);
      const g = ramp(mix(colors.text, colors.accent2, tint), sweep(colors.accent, colors.active, tint), lerp(y0, y1, fx.gradTop), lerp(y0, y1, fx.gradBottom));
      units.push(
        wordUnit(c, w, {
          m: chain(own(since, spin, a, c.out, spinOut), page),
          z: w.phase === "active" ? 1 : 0,
          fg: g.fg,
          layers: letters(thick(fx, u, lerp(fx.enterScale, 1, spring(since, fx.enterFreq, fx.enterDamp)) * (1 - E.inCubic(c.out))), u, g.grad, colors.stroke, colors.shadow, isEmoji(w.text, w.box.font)),
        }),
      );

      if (w.emph && since > fx.burstDelayS) {
        // it springs open to its size, or to what the box allows around the word if that is less
        const rim = fx.burstRim * u;
        const [bx, by] = apply(resting, w.cx, w.cy);
        const roomX = Math.min(bx - c.region.x0, c.region.x1 - bx) - rim - SLACK;
        const roomY = Math.min(by - c.region.y0, c.region.y1 - by) - rim - SLACK;
        const k = lerp(fx.burstFrom, 1, spring(since - fx.burstDelayS, fx.burstFreq, fx.burstDamp)) * (1 + fx.burstKick * a);
        const [rx, ry] = fitEllipse((w.box.w / 2 + fx.burstPadX * u) * k, (capOf(w) / 2 + fx.burstPadY * u) * k, roomX, roomY, slant + swing);
        const col = { fill: colors.emph, core: mix(colors.emph, colors.accent2, fx.burstTint), line: colors.stroke };
        shapes.push(...burst(`p${c.page.id}b${w.index}`, fx, w.cx, w.cy, rx, ry, fx.burstPoints, (fx.burstTurn * t * Math.PI) / 180, rim, col, page, -4));
      }
    }
    return { units, shapes };
  },

  label: (c): Scene => {
    const { P, t, colors } = c;
    const fx = thick(P.fx, c.layout.size);
    const u = em(c);
    const pad = reach(fx, u);
    const rim = fx.burstRim * u;
    const boxes = c.words.map((w) => wordBox(c, w, pad));
    const block: Box = {
      l: Math.min(...boxes.map((b) => b.l)),
      r: Math.max(...boxes.map((b) => b.r)),
      t: Math.min(...boxes.map((b) => b.t)),
      b: Math.max(...boxes.map((b) => b.b)),
    };
    const mid: Pt = [(block.l + block.r) / 2, (block.t + block.b) / 2];
    // the burst wants this much around the text; the block is placed where it gets the most of it
    const wantX = (block.r - block.l) / 2 - pad + fx.labelPadX * u;
    const wantY = (block.b - block.t) / 2 - pad + fx.labelPadY * u;
    const { m: resting, deg: slant } = rest(c, boxes.flatMap((b) => corners(b)), fx.labelSlantDeg, Math.max(0, wantY + rim - (block.b - mid[1])));
    const [ox, oy] = apply(resting, mid[0], mid[1]);
    const roomX = Math.min(ox - c.region.x0, c.region.x1 - ox) - rim - SLACK;
    const roomY = Math.min(oy - c.region.y0, c.region.y1 - oy) - rim - SLACK;
    const roomy = largest(1, (g) => over(c.region, boxes.flatMap((b) => corners(b, chain(pose(mid, { scale: 1 + g }), resting)))));

    const pop = (s: number) => Math.min(1 + roomy, lerp(fx.enterScale, 1, spring(s, fx.enterFreq, fx.enterDamp)));
    const settle = 1.2 / Math.min(fx.spinFreq, fx.enterFreq);
    const spinIn = largest(fx.labelSpinDeg, (amp) =>
      worst(c.region, (k) => boxes.flatMap((b) => corners(b, chain(pose(mid, { scale: pop(settle * k), rot: -amp * (1 - spring(settle * k, fx.spinFreq, fx.spinDamp)) }), resting)))),
    );
    const size = pop(t) * (1 - E.inCubic(c.out));
    // in: the letters pop and spin as one piece; out: they zoom away, every word spinning where it stands
    const piece = chain(pose(mid, { scale: size, rot: -spinIn * (1 - spring(t, fx.spinFreq, fx.spinDamp)) }), resting);
    const brief = P.label.exitS > 0 ? Math.min(1, c.exit / P.label.exitS) : 1;

    // letters in the kind's colour; the burst in the first colour that is clearly another hue
    const kind = hueOf(colors.text);
    const fill = [colors.emph, ...colors.cycle, colors.accent].find((x) => kind.c < GREY || hueGap(hueOf(x).h, kind.h) >= fx.labelHueGap) ?? colors.emph;
    const units = c.words.map((w, i) => {
      const capH = c.layout.capH * (w.box.size / c.layout.size);
      const y1 = c.cy + w.box.y;
      const g = ramp(mix(colors.text, colors.accent2, fx.labelTop), colors.text, lerp(y1 - capH, y1, fx.gradTop), lerp(y1 - capH, y1, fx.gradBottom));
      const dir = i % 2 === 0 ? 1 : -1;
      const spinOut =
        c.out > 0
          ? largest(fx.exitSpinDeg * brief, (amp) =>
              worst(c.region, (k) => corners(boxes[i], chain(pose([w.cx, w.cy], { rot: dir * amp * E.inQuad(k) }), pose(mid, { scale: 1 - E.inCubic(k) }), resting))),
            )
          : 0;
      return wordUnit(c, w, {
        m: chain(pose([w.cx, w.cy], { rot: dir * spinOut * E.inQuad(c.out) }), piece),
        z: 5,
        active: true,
        fg: g.fg,
        layers: letters(thick(fx, u, size), u, g.grad, colors.stroke, colors.shadow),
      });
    });
    // the burst grows with the letters but does not spin with them: its spikes travel instead
    const open = lerp(fx.burstFrom, 1, spring(t - fx.burstDelayS, fx.burstFreq, fx.burstDamp)) * size;
    const [rx, ry] = fitEllipse(wantX * open, wantY * open, roomX, roomY, slant);
    const col = { fill, core: mix(fill, colors.accent2, fx.burstTint), line: colors.stroke };
    const shapes = t > fx.burstDelayS ? burst(`l${c.label.id}b`, fx, mid[0], mid[1], rx, ry, fx.labelPoints, (fx.burstTurn * t * Math.PI) / 180, rim, col, resting, 1) : [];
    return { units, shapes };
  },
});
