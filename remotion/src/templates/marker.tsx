import { z } from "zod";
import { apply, chain, pose, translate, type Affine } from "../core/affine";
import { back, E, hash01, hashSigned, lerp, prog, pulse } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { dropShadow, inked, path, wordUnit, type Ctx, type LabelCtx, type Rect, type Scene, type Shape, type Unit } from "../core/scene";
import { blockCentre, defineTemplate, em, wordCentre } from "../core/template";

/**
 * MARKER: hand lettering on a slight slope. Words are written on from left to right; a highlighter is
 * swiped across each word as it is spoken and stays on what has been said (the letters keep their dark
 * outline on top of it); a shouted word gets a scribbled underline as well. Everything is wiped away from
 * right to left.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.3), // dark outline of the letters, em
  shadowX: z.number().min(-0.2).max(0.2), // hard shadow offset, em
  shadowY: z.number().min(0).max(0.2),
  jitterDeg: z.number().min(0).max(6), // each word is turned up to this much on its own, degrees
  jitterY: z.number().min(0).max(0.2), // and sits up to this much above or below its line, em
  activeScale: z.number().min(1).max(1.3), // the spoken word grows to this
  activeOvershoot: z.number().min(0).max(4), // how far it overshoots on the way
  bandPadX: z.number().min(0).max(0.4), // highlighter: reaches this far past the word, em
  bandTop: z.number().min(-0.2).max(0.4), // this far above the capitals, em
  bandBottom: z.number().min(-0.2).max(0.4), // and this far below the baseline, em
  bandDeg: z.number().min(0).max(6), // it is turned up to this much against the word, degrees
  bandWobble: z.number().min(0).max(0.1), // its edges wander up to this much, em
  bandWave: z.number().min(0.3).max(6), // over about this length, em
  bandSlant: z.number().min(0).max(0.3), // its ends lean like a chisel tip, em
  bandRound: z.number().min(0).max(0.2), // how round its corners are, em
  bandAlpha: z.number().min(0.3).max(1), // 1 = opaque ink
  swipeS: z.number().min(0.05).max(0.8), // the highlighter crosses a word in this time
  swipeLag: z.number().min(0).max(1), // it waits until this share of the word is written
  scribbleY: z.number().min(0).max(0.5), // scribble under a shouted word: its middle, below the baseline, em
  scribbleAmp: z.number().min(0).max(0.15), // it zigzags this far up and down, em
  scribbleTooth: z.number().min(0.08).max(1), // one stroke of the zigzag is about this long, em
  scribbleW: z.number().min(0.02).max(0.15), // pen width, em
  scribbleRim: z.number().min(0.02).max(0.2), // dark rim around it, em
  scribbleS: z.number().min(0.05).max(1), // it is drawn in this time
  scribbleLagS: z.number().min(0).max(0.5), // starting this long after the word
});
type P = z.infer<typeof schema>;
type C = Ctx<P> | LabelCtx<P>;
type Pt = [number, number];

const r2 = (v: number): number => Math.round(v * 100) / 100;
const line = (pts: Pt[], close = false): string => pts.map((p, i) => `${i ? "L" : "M"}${r2(p[0])} ${r2(p[1])}`).join("") + (close ? "Z" : "");

/** The box that holds a rectangle after transform `m`, grown by `pad`. */
function bounds(m: Affine, x0: number, y0: number, x1: number, y1: number, pad: number): Rect {
  const pts = [apply(m, x0, y0), apply(m, x1, y0), apply(m, x0, y1), apply(m, x1, y1)];
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const lx = Math.min(...xs) - pad;
  const ty = Math.min(...ys) - pad;
  return { x: lx, y: ty, w: Math.max(...xs) + pad - lx, h: Math.max(...ys) + pad - ty };
}

/** A smooth wander in -1..1 along x (two sines with seeded phases): the unsteady hand. */
const wander = (x: number, wave: number, seed: string): number =>
  0.6 * Math.sin((x / wave) * 2 * Math.PI + hash01(seed) * 6.283) + 0.4 * Math.sin((x / wave) * 2 * Math.PI * 2.3 + hash01(`${seed}b`) * 6.283);

/** The first share `p` of a polyline, by length. */
function partial(pts: Pt[], p: number): Pt[] {
  if (p >= 1) return pts;
  const len = pts.slice(1).map((q, i) => Math.hypot(q[0] - pts[i][0], q[1] - pts[i][1]));
  let left = len.reduce((a, b) => a + b, 0) * Math.max(0, p);
  const out: Pt[] = [pts[0]];
  for (let i = 0; i < len.length && left > 0; i++) {
    const k = Math.min(1, left / Math.max(1e-6, len[i]));
    out.push([lerp(pts[i][0], pts[i + 1][0], k), lerp(pts[i][1], pts[i + 1][1], k)]);
    left -= len[i];
  }
  return out;
}

/** The same polyline, a little longer at both ends (for the rim under a stroke with flat ends). */
function longer(pts: Pt[], by: number): Pt[] {
  if (pts.length < 2) return pts;
  const ext = (a: Pt, b: Pt): Pt => {
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1;
    return [a[0] + ((a[0] - b[0]) / d) * by, a[1] + ((a[1] - b[1]) / d) * by];
  };
  return [ext(pts[0], pts[1]), ...pts, ext(pts[pts.length - 1], pts[pts.length - 2])];
}

/**
 * A scribbled underline from xl to xr around height y: a quick zigzag, every stroke a little different.
 * It only ever runs to the right, so drawing it part by part always makes it longer.
 */
function scribble(c: C, seed: string, xl: number, xr: number, y: number): Pt[] {
  const fx = c.P.fx;
  const u = em(c);
  const n = Math.max(2, Math.round((xr - xl) / (fx.scribbleTooth * u)));
  const pts: Pt[] = [];
  for (let j = 0; j <= n; j++) {
    const x = lerp(xl, xr, j / n) + (j > 0 && j < n ? hashSigned(`${seed}x${j}`) * 0.2 * ((xr - xl) / n) : 0);
    pts.push([x, y + (j % 2 === 0 ? 1 : -1) * fx.scribbleAmp * u * (0.65 + 0.35 * hash01(`${seed}a${j}`))]);
  }
  return pts;
}

/** The outline of a pen stroke of half-width `hw` along a polyline: one side forward, the other back. */
function ribbon(pts: Pt[], hw: number): Pt[] {
  const n = pts.length;
  const dir = (a: Pt, b: Pt): Pt => {
    const d = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    return [(b[0] - a[0]) / d, (b[1] - a[1]) / d];
  };
  const left: Pt[] = [];
  const right: Pt[] = [];
  for (let i = 0; i < n; i++) {
    const d1 = dir(pts[Math.max(0, i - 1)], pts[Math.max(1, i)]);
    const d2 = dir(pts[Math.min(n - 2, i)], pts[Math.min(n - 1, i + 1)]);
    let nx = -(d1[1] + d2[1]);
    let ny = d1[0] + d2[0];
    const len = Math.hypot(nx, ny) || 1;
    nx /= len;
    ny /= len;
    const k = hw / Math.max(0.5, -nx * d1[1] + ny * d1[0]); // mitred corner, at most twice the half-width
    left.push([pts[i][0] + nx * k, pts[i][1] + ny * k]);
    right.push([pts[i][0] - nx * k, pts[i][1] - ny * k]);
  }
  return [...left, ...right.reverse()];
}

/** The scribble as shapes: the rim, then the stroke, drawn up to share `p`. Both are filled outlines. */
function scribbleShapes(c: C, id: string, pts: Pt[], p: number, colour: string, rim: number, m: Affine, z: number, clip?: Rect): Shape[] {
  const part = partial(pts, p);
  if (part.length < 2) return [];
  const hw = (c.P.fx.scribbleW * em(c)) / 2;
  return [
    path(`${id}k`, line(ribbon(longer(part, rim), hw + rim), true), { fill: c.colors.stroke, m, z, clip }),
    path(id, line(ribbon(part, hw), true), { fill: colour, m, z, clip }),
  ];
}

const letters = (c: C, fill: string) => {
  const fx = c.P.fx;
  const u = em(c);
  return inked(fill, c.colors.stroke, fx.halo * u, [dropShadow(c.colors.shadow, fx.halo * u, fx.shadowX * u, fx.shadowY * u)]);
};

/** Room around a word for its outline and shadow, px. */
const inkPad = (c: C): number => (c.P.fx.halo + Math.max(Math.abs(c.P.fx.shadowX), c.P.fx.shadowY)) * em(c) + 3;

/**
 * The block turned by `rot` degrees and kept in the caption box with everything drawn around it: moved up
 * or down as little as needed (usually not at all) and, in a box too low even for that, made a little
 * smaller. `up` and `down`: how far ink reaches above the first line's capitals and below the last
 * baseline before the turn, px; a turned block's ends reach further.
 */
function settle(c: C, rot: number, up: number, down: number): Affine {
  const sag = Math.abs(Math.sin((rot * Math.PI) / 180)) * ((c.layout.right - c.layout.left) / 2 + c.P.fx.bandPadX * em(c));
  const top = c.cy + c.layout.top - up - sag;
  const bottom = c.cy + c.layout.bottom + down + sag;
  const [lo, hi] = [c.region.y0 + 2, c.region.y1 - 2];
  const s = Math.min(1, (hi - lo) / (bottom - top));
  const mid = (top + bottom) / 2;
  const half = ((bottom - top) / 2) * s;
  return chain(pose(blockCentre(c), { rot }), pose([c.cx, mid], { scale: s }), translate(0, Math.min(hi - half, Math.max(lo + half, mid)) - mid));
}

export default defineTemplate<P>({
  name: "marker",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    const pad = inkPad(c);
    const edge = (fx.bandWobble + fx.bandRound / 2 + fx.jitterY) * u + (fx.activeScale - 1) * c.layout.capH + 3;
    const up = fx.bandTop * u + edge;
    const down = Math.max(fx.bandBottom, fx.scribbleY + fx.scribbleAmp + fx.scribbleW / 2 + fx.scribbleRim, fx.halo + fx.shadowY) * u + edge;
    const page = settle(c, -P.tilt, up, down);
    // exit: one window over the whole block, closing from the right
    const L = c.cx + c.layout.left - pad - fx.bandPadX * u;
    const R = c.cx + c.layout.right + pad + fx.bandPadX * u;
    const leaving = c.out > 0;
    const wipe: Rect = {
      x: c.region.x0,
      y: c.region.y0,
      w: Math.max(0, lerp(R, L, E.inQuad(c.out)) - c.region.x0), // gathers speed, so the last frame still shows what is left
      h: c.region.y1 - c.region.y0,
    };

    const units: Unit[] = [];
    const shapes: Shape[] = [];
    for (const w of c.words) {
      const seed = `p${c.page.id}w${w.index}`;
      const capH = c.layout.capH * (w.box.size / c.layout.size);
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));
      const own = pose(wordCentre(w), {
        scale: 1 + (fx.activeScale - 1) * a,
        rot: hashSigned(`${seed}r`) * fx.jitterDeg,
        y: hashSigned(`${seed}y`) * fx.jitterY * u,
      });
      const m = chain(own, page);
      const x0 = c.cx + w.box.x;
      const x1 = x0 + w.box.w;
      const base = c.cy + w.box.y;

      // written on: a window that opens from the left, word after word
      const t0 = w.index * P.timing.staggerS;
      const written = E.outQuad(prog(t, t0, P.timing.enterS));
      const room = pad + (fx.activeScale - 1) * w.box.w;
      const full = bounds(m, x0, base - capH, x1, base + 0.1 * capH, room);
      const lead = Math.max(0, room - fx.halo * u - 2); // the window starts at the ink, so the first frame already shows some
      const clip: Rect | undefined = leaving ? wipe : written < 1 ? { ...full, w: lead + (full.w - lead) * written } : undefined;
      units.push(wordUnit(c, w, { m, z: 0, fg: [colors.text], clip, layers: letters(c, colors.text) }));

      // the highlighter: swiped across from the moment the word is spoken, and it stays
      const from = Math.max(w.start, t0 + fx.swipeLag * P.timing.enterS);
      const swipe = E.outCubic(prog(t, from, fx.swipeS));
      if (swipe > 0) {
        const xl = x0 - fx.bandPadX * u;
        const xe = lerp(xl, x1 + fx.bandPadX * u, swipe);
        const yt = base - capH - fx.bandTop * u;
        const yb = base + fx.bandBottom * u;
        const slant = Math.min(fx.bandSlant * u, (xe - xl) / 2);
        const n = Math.max(2, Math.ceil((xe - xl) / (0.12 * u)));
        const top: Pt[] = [];
        const bottom: Pt[] = [];
        for (let i = 0; i <= n; i++) {
          const k = i / n;
          const x = lerp(xl, xe, k);
          // the wander is tied to the place on the word, so an edge already drawn does not move as it grows
          top.push([x + slant, yt + wander(x - xl, fx.bandWave * u, `${seed}t`) * fx.bandWobble * u]);
          bottom.push([x - slant, yb + wander(x - xl, fx.bandWave * u, `${seed}u`) * fx.bandWobble * u]);
        }
        const turn = pose(wordCentre(w), { rot: hashSigned(`${seed}h`) * fx.bandDeg });
        shapes.push(
          path(`${seed}hl`, line([...top, ...bottom.reverse()], true), {
            fill: colors.active,
            stroke: colors.active,
            strokeWidth: fx.bandRound * u,
            alpha: fx.bandAlpha,
            m: chain(turn, page),
            z: -3,
            clip: leaving ? wipe : undefined,
          }),
        );
      }

      // a shouted word: a scribbled underline on top of the highlighter
      if (w.emph) {
        const p = E.inOutSine(prog(t, from + fx.scribbleLagS, fx.scribbleS));
        const pts = scribble(c, seed, x0, x1, base + fx.scribbleY * u);
        shapes.push(...scribbleShapes(c, `${seed}sc`, pts, p, colors.emph, fx.scribbleRim * u, m, -1, leaving ? wipe : undefined));
      }
    }
    return { units, shapes };
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    const u = em(c);
    const pad = inkPad(c);
    const x0 = c.cx + c.layout.left;
    const x1 = c.cx + c.layout.right;
    const last = c.layout.lines[c.layout.lines.length - 1];
    // underlined with the same pen; the underline has the letters' colour, so it gets their full outline too
    const rim = Math.max(fx.scribbleRim, fx.halo) * u;
    const below = (fx.scribbleY + fx.scribbleAmp + fx.scribbleW / 2) * u + rim;
    const under = c.cy + last.y + below;
    const page = settle(c, c.P.label.rotate, pad, below + 3);
    const full = bounds(page, x0, c.cy + c.layout.top, x1, under, pad);
    const written = E.outQuad(prog(c.t, 0, c.P.label.enterS));
    const lead = Math.max(0, pad - fx.halo * u - 2);
    const right = c.out > 0 ? lerp(full.x + full.w, full.x, E.inQuad(c.out)) : lerp(full.x + lead, full.x + full.w, written);
    const clip: Rect | undefined = c.out > 0 || written < 1 ? { ...full, w: Math.max(0, right - full.x) } : undefined;
    const units = c.words.map((w) =>
      wordUnit(c, w, { m: page, z: 5, active: true, fg: [c.colors.text], clip, layers: letters(c, c.colors.text) }),
    );
    const line0 = c.cx + last.x;
    const pts = scribble(c, `l${c.label.id}`, line0, line0 + last.w, c.cy + last.y + fx.scribbleY * u);
    const p = E.inOutSine(prog(c.t, c.P.label.enterS * fx.swipeLag, fx.scribbleS));
    const shapes = scribbleShapes(c, `l${c.label.id}sc`, pts, p, c.colors.text, rim, page, 4, c.out > 0 ? clip : undefined);
    return { units, shapes };
  },
});
