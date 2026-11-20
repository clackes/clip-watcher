import { z } from "zod";
import { apply, chain, pose, type Affine } from "../core/affine";
import { mix, parseHex } from "../core/color";
import { back, drift, E, lerp, prog, pulse } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema, type Region } from "../core/schema";
import { wordUnit, type Ctx, type LabelCtx, type Layer, type Rect, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * RETRO: a seventies sign. Fat cream script on a dark base, with a deep extrusion in three sunset stripes.
 * Words arrive as they are spoken: the face pushes out of the wall while the stripes grow from nothing.
 * The back of the extrusion never moves; the face does. The spoken word is raised higher on a deeper
 * extrusion, turns a warmer colour, hovers, and its stripes flow outwards; shouted words are bigger and
 * striped in a second set of colours. Before a pause the extrusion collapses and the words sink into the
 * floor under their line.
 *
 * `label.rotate` is 0 in the JSON: the label's tilt is `labelTiltDeg`, as much of it as the box has room
 * for (the layout would shrink the label instead).
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.3), // dark outline of the face, em
  rim: z.number().min(0).max(0.1), // the dark base shows this far around the stripes, em
  depthX: z.number().min(-0.4).max(0.4), // extrusion of a resting word: right, em of the word's own size
  depthY: z.number().min(0).max(0.4), // and down
  stepEm: z.number().min(0.01).max(0.08), // the extrusion is built from silhouettes this far apart, em
  enterScale: z.number().min(0.5).max(1), // the face pushes out from this scale (over timing.enterS)
  enterOvershoot: z.number().min(0).max(3),
  growDelayS: z.number().min(0).max(0.3), // the extrusion starts growing this long after the word appears
  growS: z.number().min(0.1).max(1), // and takes this long
  growOvershoot: z.number().min(0).max(3),
  activeDepth: z.number().min(1).max(3), // the spoken word's extrusion is this many times deeper (it lifts)
  activeOvershoot: z.number().min(0).max(3),
  emphDepth: z.number().min(1).max(3), // the same for a shouted word
  emojiPop: z.number().min(1).max(1.4), // an emoji has no extrusion to be raised on: it grows to this while spoken
  hover: z.number().min(0).max(0.05), // the spoken word hovers by this much, em
  hoverHz: z.number().min(0.2).max(3),
  flow: z.number().min(0).max(4), // the spoken word's stripes travel outwards: stripes a second
  exitCollapse: z.number().min(0.2).max(1), // share of the exit in which the extrusion collapses
  exitSinkFrom: z.number().min(0).max(0.8), // the words start sinking at this share of the exit
  labelTiltDeg: z.number().min(0).max(10), // a label leans back by this much, degrees
});
type P = z.infer<typeof schema>;
type FX = P["fx"];

type Pt = [number, number];
const TRIES = 9; // halvings when looking for the largest tilt that fits
const SLACK = 1.5; // px kept free at the box edge (anti-aliasing)
const FAR = 48; // 8-bit levels two colours must differ by to count as different
const EMOJI_TOP = 0.82; // an emoji picture stands at least this high over the baseline, em
const LEAST = 4.3; // px of outline a fill needs next to it at any size and scale: the frame checks read it against the 2 to 3 px around it, corners included
const JUDGED = 0.5; // the scale down to which a word on its way in or out is still read (and measured)

/**
 * The parameters for text of size `u` drawn at `scale`: the outline is never under `LEAST` px on screen.
 * In small text, and while a word is small on its way in or out, it is that many px instead of its
 * width in em.
 */
const thick = (fx: FX, u: number, scale = 1): FX => ({ ...fx, halo: Math.max(fx.halo, LEAST / (Math.max(JUDGED, Math.min(1, scale)) * u)) });

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

/**
 * The extrusion: a dark base all along it, then stripes in `cols` from the face (0) to the full offset
 * (dx, dy), each stripe a run of silhouettes `step` px apart. `phase` moves the stripes outwards, in stripes.
 */
function stripes(cols: string[], base: string, halo: number, rim: number, dx: number, dy: number, step: number, phase: number): Layer[] {
  const depth = Math.hypot(dx, dy);
  const out: Layer[] = [];
  if (depth < 0.05) return out;
  const nb = Math.max(1, Math.ceil(depth / (step * 1.5)));
  for (let i = nb; i >= 1; i--) out.push({ kind: "stroke", color: base, width: halo + rim, dx: (dx * i) / nb, dy: (dy * i) / nb });
  const n = cols.length;
  // stripe edges along the depth (0..1), where p * n - phase is a whole number
  const edges = [1];
  for (let k = Math.floor(n - phase); k >= Math.ceil(-phase); k--) {
    const p = (k + phase) / n;
    if (p > 0 && p < 1) edges.push(p);
  }
  edges.push(0);
  for (let e = 0; e + 1 < edges.length; e++) {
    const far = edges[e];
    const near = edges[e + 1];
    const idx = Math.floor(((far + near) / 2) * n - phase);
    const color = cols[((idx % n) + n) % n];
    const m = Math.max(1, Math.ceil(((far - near) * depth) / step));
    for (let i = m; i >= 1; i--) {
      const p = near + ((far - near) * i) / m;
      out.push({ kind: "stroke", color, width: halo, dx: dx * p, dy: dy * p });
    }
  }
  return out;
}

/** One word (or label word) of the sign at one instant. */
type Sign = {
  scale: number; // of the face about the word's centre
  depth: number; // extrusion as a multiple of the resting one
  lit: number; // 0..1 how spoken the word is
  sink: number; // 0..1 how far the word has sunk into the floor
  phase: number; // stripe flow
  face: string;
  cols: string[];
};

/** The window a line sinks out of: everything above the floor under its extrusion. */
function floorClip(c: Ctx | LabelCtx, floor: number): Rect {
  return { x: 0, y: 0, w: c.width, h: Math.max(0, floor) };
}

/** `floor`: the height (overlay px) the word sinks into when `s.sink` rises; `sinking`: the exit has begun. */
function signUnit(c: Ctx | LabelCtx, w: WordState, fx: FX, s: Sign, k: number, outer: Affine, floor: number, sinking: boolean, z: number): Unit {
  const u = w.box.size;
  const d0x = fx.depthX * u * k;
  const d0y = fx.depthY * u * k;
  const capH = c.layout.capH * (u / c.layout.size);
  // An emoji is a picture: it gets the outline and no extrusion (the Stage grows an emoji's halos from
  // the picture's alpha, and a run of shifted halos smears whatever faint pixels its corners have). With
  // nothing to be raised on, it grows a little while it is spoken instead. It also only ever moves
  // briskly: the browser draws emoji on whole pixels, and a slow drift would show as stutter.
  const flat = isEmoji(w.text, w.box.font);
  // Sinking stops with the top of the word level with the floor: its outline still shows, so the word
  // is never on screen with nothing to see. The top is where it is once the extrusion has collapsed.
  const top = flat ? w.cy + capH / 2 - EMOJI_TOP * u : w.cy - capH / 2 + d0y;
  const highest = Math.min(apply(outer, w.cx - w.box.w / 2, top)[1], apply(outer, w.cx + w.box.w / 2, top)[1]);
  const drop = Math.max(0, floor - highest) * s.sink;
  const halo = thick(fx, u, s.scale).halo * u; // never under its least px on screen, however small the face is yet
  // the back of the extrusion stays where it is; the face moves away from it
  const m = flat
    ? pose([w.cx, w.cy], { scale: s.scale * (1 + (fx.emojiPop - 1) * s.lit), y: drop })
    : pose([w.cx, w.cy], { scale: s.scale, x: d0x * (1 - s.depth * s.scale), y: d0y * (1 - s.depth * s.scale) + drop });
  return wordUnit(c, w, {
    m: chain(m, outer),
    z,
    clip: sinking ? floorClip(c, floor) : undefined,
    fg: [s.face],
    active: "label" in c ? true : undefined,
    layers: [
      ...(flat ? [] : stripes(s.cols, c.colors.stroke, halo, fx.rim * u, d0x * s.depth, d0y * s.depth, fx.stepEm * u, s.phase)),
      { kind: "stroke", color: c.colors.stroke, width: halo },
      { kind: "fill", color: s.face },
    ],
  });
}

/** True when two colours are clearly different ones (a stripe in the face's own colour would read as face). */
const apart = (a: string, b: string): boolean => {
  const p = parseHex(a);
  const q = parseHex(b);
  return Math.max(Math.abs(p[0] - q[0]), Math.abs(p[1] - q[1]), Math.abs(p[2] - q[2])) > FAR;
};

/** How far past 1 a back ease with overshoot `k` goes at its highest. */
const backPeak = (k: number): number => (4 * k ** 3) / (27 * (k + 1) ** 2);

/** How much larger than life a face is at the top of its entrance. */
const popPeak = (fx: FX): number => (1 - fx.enterScale) * backPeak(fx.enterOvershoot);

/** How much of the resting depth the box has room for under the lowest line (1 = all of it), after moving up by `up`. */
function depthFit(c: Ctx | LabelCtx, fx: FX): { k: number; up: number } {
  const r = c.region;
  let need = 0; // px the deepest extrusion reaches under the block
  let rise = 0; // px the highest face reaches over it
  const pop = popPeak(fx);
  for (const w of c.words) {
    const u = w.box.size;
    const line = c.layout.lines[w.box.line];
    const top = Math.max(fx.activeDepth, fx.emphDepth) * (1 + backPeak(Math.max(fx.activeOvershoot, fx.growOvershoot))) - 1;
    const edge = (fx.halo + fx.rim) * u;
    need = Math.max(need, fx.depthY * u + edge + pop * (c.cy + line.bottom - w.cy + edge));
    rise = Math.max(rise, (fx.depthY * top + fx.hover) * u + edge + pop * (w.cy - (c.cy + line.top) + edge));
  }
  const below = r.y1 - SLACK - (c.cy + c.layout.bottom);
  const above = c.cy + c.layout.top - (r.y0 + SLACK);
  const up = Math.max(0, Math.min(need - below, above - rise));
  const most = Math.max(...c.words.map((w) => fx.depthY * w.box.size));
  const fixed = need - most; // outline, rim and the entrance's overshoot: these do not shrink
  const k = most > 0 ? Math.max(0, Math.min(1, (below + up - fixed) / most)) : 1;
  return { k, up };
}

export default defineTemplate<P>({
  name: "retro",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = thick(P.fx, c.layout.size);
    const { k, up } = depthFit(c, fx);
    const outer = pose([c.cx, c.cy], { y: -up });
    const collapse = E.inOutCubic(prog(c.out, 0, fx.exitCollapse));
    const sunk = E.inCubic(prog(c.out, fx.exitSinkFrom, 1 - fx.exitSinkFrom));
    const units: Unit[] = [];
    for (const w of c.words) {
      if (w.phase === "ahead") continue; // build mode: a word is there from the moment it is spoken
      const u = w.box.size;
      const line = c.layout.lines[w.box.line];
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));
      const lit = Math.min(1, Math.max(0, w.a));
      const deep = (w.emph ? fx.emphDepth : fx.activeDepth) - 1;
      const grown = back(fx.growOvershoot)(prog(w.since, fx.growDelayS, fx.growS));
      const hover = (fx.hover / Math.max(0.01, fx.depthY * k)) * lit * drift(t, fx.hoverHz, `h${c.page.id}w${w.index}`);
      // the stripes travel while the word is spoken, and run on to the next whole stripe as it lets go
      const spoken = Math.min(w.since, w.activeEnd - w.start);
      const run = fx.flow * spoken;
      const rest = (Math.ceil(run) - run) * E.inOutQuad(prog(t, w.activeEnd, P.timing.activeOutS));
      // the floor this line sinks into: just under the back of its extrusion
      const floor = Math.min(c.region.y1, c.cy - up + line.bottom + (fx.depthY * k + fx.halo + fx.rim) * u + SLACK);
      units.push(
        signUnit(
          c,
          w,
          fx,
          {
            scale: lerp(fx.enterScale, 1, back(fx.enterOvershoot)(prog(w.since, 0, P.timing.enterS))),
            depth: (grown * (1 + deep * a) + hover) * (1 - collapse),
            lit: a,
            sink: sunk,
            phase: run + rest,
            face: mix(colors.text, colors.active, lit),
            cols: w.emph ? [colors.emph, colors.accent, colors.accent2] : colors.cycle,
          },
          k,
          outer,
          floor,
          c.out > 0,
          w.phase === "active" ? 1 : 0,
        ),
      );
    }
    return { units, shapes: [] };
  },

  label: (c): Scene => {
    const { P, t, colors } = c;
    const fx = thick(P.fx, c.layout.size);
    const { k, up } = depthFit(c, fx);
    // everything a word reaches: the back of the extrusion down and right, the raised face up and left
    const lifted = fx.activeDepth * (1 + backPeak(fx.growOvershoot)) - 1;
    const big = 1 + popPeak(fx); // the face at the top of its entrance
    const corners = (m: Affine): Pt[] =>
      c.words.flatMap((w) => {
        const u = w.box.size;
        const line = c.layout.lines[w.box.line];
        const l = w.cx - (w.box.w / 2) * big - (fx.depthX * k * lifted + fx.halo) * u;
        const r = w.cx + (w.box.w / 2) * big + (fx.depthX * k + fx.halo + fx.rim) * u;
        const top = w.cy - (w.cy - c.cy - line.top) * big - (fx.depthY * k * lifted + fx.hover + fx.halo) * u;
        const bot = w.cy + (c.cy + line.bottom - w.cy) * big + (fx.depthY * k + fx.halo + fx.rim) * u;
        return [apply(m, l, top), apply(m, r, top), apply(m, r, bot), apply(m, l, bot)];
      });
    // leaning back as far as the box lets it: turned about the block's centre, then up off the floor
    const leaning = (deg: number): Affine => {
      const low = Math.max(...corners(pose([c.cx, c.cy], { rot: -deg, y: -up })).map((p) => p[1]));
      return pose([c.cx, c.cy], { rot: -deg, y: -up - Math.max(0, low - (c.region.y1 - SLACK)) });
    };
    const tilt = largest(fx.labelTiltDeg, (deg) => over(c.region, corners(leaning(deg))));
    const outer = leaning(tilt);
    const collapse = E.inOutCubic(prog(c.out, 0, fx.exitCollapse));
    const sunk = E.inCubic(prog(c.out, fx.exitSinkFrom, 1 - fx.exitSinkFrom));
    const lowest = Math.min(c.region.y1, Math.max(...corners(outer).map((p) => p[1])) + SLACK);
    // the sunset stripes, without any that is the colour of this label's face (other stripes step in)
    const cols = [...colors.cycle, colors.emph, colors.accent, colors.accent2].filter((x) => apart(x, colors.text)).slice(0, colors.cycle.length);
    const units = c.words.map((w) =>
      signUnit(
        c,
        w,
        fx,
        {
          scale: lerp(fx.enterScale, 1, back(fx.enterOvershoot)(prog(t, 0, P.label.enterS))),
          depth: (back(fx.growOvershoot)(prog(t, fx.growDelayS, fx.growS)) * fx.activeDepth + (fx.hover / Math.max(0.01, fx.depthY * k)) * drift(t, fx.hoverHz, `l${c.label.id}`)) * (1 - collapse),
          lit: 0,
          sink: sunk,
          phase: fx.flow * t,
          face: colors.text,
          cols: cols.length > 0 ? cols : colors.cycle,
        },
        k,
        outer,
        lowest,
        c.out > 0,
        5,
      ),
    );
    return { units, shapes: [] };
  },
});
