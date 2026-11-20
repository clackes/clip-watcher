import { z } from "zod";
import { pose } from "../core/affine";
import { mix, parseHex } from "../core/color";
import { back, E, hash01, lerp, prog, pulse } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { ellipse, path, wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Rect, type Scene, type Shape, type Unit } from "../core/scene";
import { defineTemplate, em } from "../core/template";

/**
 * SURF: the line rides a wave. A white water line runs under every line of text and a sine travels
 * along it without end; each word sits on it, bobbing at its own place on the wave and tilting with the
 * slope. Brush script in an ocean gradient on a navy outline with a thin white rim. Words surf in from the
 * left over an arc while the water line draws itself; the water swells under the spoken word, which rides
 * the crest: highest, biggest, in the highlight colour. Shouted words splash down (squash, wobble, spray).
 * Before a pause everything is washed away to the right and down.
 *
 * The wave is a function of the overlay's own time, so a new phrase lands on the same water. All heights
 * shrink together when the caption box is low. `label.rotate` is not used: a label rides the wave too.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.3), // navy outline, em
  rim: z.number().min(0).max(0.1), // white rim around it, em
  gradTop: z.number().min(0).max(1), // the top colour holds down to this share of the letter height
  gradBottom: z.number().min(0).max(1), // and the bottom colour starts here
  deep: z.number().min(0).max(1), // spoken and shouted words: how far their lower colour goes towards the water colour
  waveAmp: z.number().min(0).max(0.3), // height of the travelling wave, em
  waveLen: z.number().min(1).max(20), // its length, em
  waveHz: z.number().min(0).max(2), // crests passing a point each second (it travels to the right)
  tiltGain: z.number().min(0).max(2), // how much of the slope under them words take as tilt
  crest: z.number().min(0).max(0.5), // the water rises this much under the spoken word, em
  crestWidth: z.number().min(0.3).max(4), // half-width of that swell at least, em (else half the word)
  activeScale: z.number().min(1).max(1.5), // the spoken word grows to this
  activeOvershoot: z.number().min(0).max(4),
  lineY: z.number().min(0).max(0.6), // the water line runs this far under the baseline, em
  lineThick: z.number().min(0.02).max(0.2), // its thickness at the middle, em
  lineRim: z.number().min(0.02).max(0.1), // its navy outline, em
  linePad: z.number().min(0).max(1), // it runs this far past the text on both sides, em
  lineStepEm: z.number().min(0.05).max(0.5), // drawn with a point every so often, em
  enterSlide: z.number().min(0).max(4), // a word surfs in from this far to the left, em
  enterArc: z.number().min(0).max(1), // over an arc this high, em
  enterScale: z.number().min(0.5).max(1),
  enterTilt: z.number().min(0).max(1), // how much of its path's slope a word takes as tilt on the way in
  splashSquash: z.number().min(0).max(0.4), // a shouted word is squashed by this share when it lands
  splashDeg: z.number().min(0).max(8), // and wobbles by this much, degrees
  splashHz: z.number().min(1).max(4.5),
  splashS: z.number().min(0.1).max(1.5), // the wobble dies out over this long
  drops: z.number().int().min(0).max(12), // drops of spray
  dropSize: z.number().min(0.02).max(0.15), // radius of the largest, em
  dropRim: z.number().min(0.005).max(0.06), // their navy outline, em
  dropLag: z.number().min(0).max(1), // they leave up to this share of their flight after the splash
  dropUp: z.number().min(0).max(1.5), // how high they fly, em
  dropOut: z.number().min(0).max(1.5), // and how far sideways, em
  dropS: z.number().min(0.2).max(1.5), // their time in the air
  exitX: z.number().min(0).max(4), // washed away: this far right, em
  exitY: z.number().min(0).max(2), // and down, em
  exitDeg: z.number().min(0).max(60), // tipping over by this much
  exitScale: z.number().min(0).max(1), // and shrinking to this
  exitStagger: z.number().min(0).max(0.3), // each word leaves this share of the exit after the one on its right
});
type P = z.infer<typeof schema>;
type FX = P["fx"];

const RAMP = 3; // segments a two-colour gradient is drawn with
const SLACK = 1.5; // px kept free at the box edge (anti-aliasing)
const FAR = 48; // 8-bit levels: a rim closer than this to a letter colour would read as part of the letter
const ENDS = 1.5; // the ends of a tilted word travel up to this many times the wave's height
const FOOT = 6; // px inside the box a word's foot stays when it is washed away
const LEAN = 9; // degrees a word may tilt on the wave, at most (kept free beside a word at the edge of the box)
const LEAST = 4.3; // px of outline a fill needs next to it at any size and scale: the frame checks read it against the 2 to 3 px around it, corners included
const JUDGED = 0.5; // the scale down to which a word on its way in or out is still read (and measured)

/**
 * The parameters for text of size `u` drawn at `scale`: the outline is never under `LEAST` px on screen.
 * In small text, and while a word is small on its way in or out, it is that many px instead of its
 * width in em.
 */
const thick = (fx: FX, u: number, scale = 1): FX => ({ ...fx, halo: Math.max(fx.halo, LEAST / (Math.max(JUDGED, Math.min(1, scale)) * u)) });

/** A gradient from `top` to `bottom` between two heights, and the colours it runs through. */
function ramp(top: string, bottom: string, y0: number, y1: number): { grad: Grad; fg: string[] } {
  const fg = Array.from({ length: RAMP + 1 }, (_, k) => mix(top, bottom, k / RAMP));
  return { fg, grad: { x1: 0, y1: y0, x2: 0, y2: Math.max(y0 + 1, y1), stops: fg.map((c, k) => ({ o: k / RAMP, c })) } };
}

/**
 * White rim, navy outline, gradient fill. An emoji (`plain`) gets the navy outline alone: the Stage grows
 * an emoji's halos from the picture's alpha, faint corner pixels and all, and one halo shows them least.
 */
const letters = (fx: FX, u: number, grad: Grad, navy: string, white: string, plain = false): Layer[] => [
  // the rim is decoration around the outline, not the outline: a one-step solid glow, so the frame checks
  // measure the letters against the navy alone
  ...(plain ? [] : [{ kind: "glow" as const, color: white, width: (fx.halo + fx.rim) * u, alpha: 1, steps: 1 }]),
  { kind: "stroke", color: navy, width: fx.halo * u },
  { kind: "fill", color: grad.stops[0].c, grad },
];

/** The sea under one line of words at one instant: its height at any x (px, up is negative). */
type Sea = (x: number) => number;

type Swell = { x: number; a: number; half: number }; // a rise of the water: centre, strength 0..1, half-width px

function sea(fx: FX, u: number, k: number, T: number, swells: Swell[]): Sea {
  const amp = fx.waveAmp * u * k;
  const q = (2 * Math.PI) / (fx.waveLen * u);
  const w = 2 * Math.PI * fx.waveHz;
  const crest = fx.crest * u * k;
  return (x) => {
    let y = amp * Math.sin(q * x - w * T);
    for (const s of swells) y -= crest * s.a * Math.exp(-0.5 * ((x - s.x) / s.half) ** 2);
    return y;
  };
}

/**
 * A word afloat with its middle at x: it lies on the water under its two ends and its middle, so a short
 * word follows every ripple and a long one barely tips. Height (px) and tilt (degrees).
 */
function afloat(water: Sea, x: number, half: number, gain: number): { y: number; deg: number } {
  const l = water(x - half);
  const r = water(x + half);
  return { y: (l + 2 * water(x) + r) / 4, deg: (Math.atan2(r - l, 2 * half) * 180 * gain) / Math.PI };
}

/**
 * The way in: from `slide` px to the left, over an arc, nose along the path (as far as a word of this
 * half-width can tip without rising above the arc). `p` is 0..1.
 */
function arrive(p: number, slide: number, arc: number, half: number, tilt: number): { x: number; y: number; deg: number } {
  const along = (Math.atan2(-arc * Math.PI * Math.cos(Math.PI * p), slide) * 180 * tilt) / Math.PI;
  const most = (Math.atan2(arc, half) * 180) / Math.PI;
  return { x: -slide * (1 - p), y: -arc * Math.sin(Math.PI * p), deg: p < 1 ? Math.max(-most, Math.min(most, along)) * (1 - p) : 0 };
}

/** The water line from x0 to x1 (px) as two shapes: navy under white, thick in the middle, pointed at the ends. */
function waterLine(id: string, fx: FX, u: number, s: Sea, base: number, x0: number, x1: number, navy: string, white: string, clip?: Rect): Shape[] {
  if (x1 - x0 < 2) return [];
  const n = Math.max(4, Math.ceil((x1 - x0) / (fx.lineStepEm * u)));
  const top: string[] = [];
  const bottom: string[] = [];
  for (let i = 0; i <= n; i++) {
    const f = i / n;
    const x = lerp(x0, x1, f);
    const y = base + s(x);
    const half = (fx.lineThick * u * Math.sqrt(Math.sin(Math.PI * f))) / 2;
    top.push(`${x.toFixed(2)} ${(y - half).toFixed(2)}`);
    bottom.push(`${x.toFixed(2)} ${(y + half).toFixed(2)}`);
  }
  const d = `M${top.join("L")}L${bottom.reverse().join("L")}Z`;
  return [
    path(`${id}n`, d, { fill: navy, stroke: navy, strokeWidth: fx.lineRim * u * 2, z: -3, clip }),
    path(`${id}w`, d, { fill: white, z: -2, clip }),
  ];
}

/**
 * How much of the wave's height the box has room for (1 = all), and how far the block moves (px, down is
 * positive) to sit in the middle of what it needs.
 */
function fitHeights(c: Ctx | LabelCtx, fx: FX, u: number): { k: number; ty: number; arc: number; squash: number } {
  const r = c.region;
  const first = c.layout.lines[0];
  const last = c.layout.lines[c.layout.lines.length - 1];
  const rise = first.y - first.top; // the letters above their baseline: words grow from the baseline up
  const desc = last.bottom - last.y; // and the layout's room under it
  const grow = fx.activeScale * (1 + 0.04 * fx.activeOvershoot) - 1;
  const upFixed = (fx.halo + fx.rim) * u + grow * rise + SLACK;
  const upVar = (fx.waveAmp * ENDS + fx.crest) * u;
  const downFixed = Math.max((fx.halo + fx.rim) * u + grow * desc, (fx.lineY + fx.lineThick / 2 + fx.lineRim) * u - desc) + SLACK;
  const downVar = fx.waveAmp * ENDS * u;
  const above = c.cy + c.layout.top - r.y0;
  const below = r.y1 - (c.cy + c.layout.bottom);
  const spare = above + below - upFixed - downFixed;
  const k = Math.max(0, Math.min(1, spare / Math.max(1e-6, upVar + downVar)));
  const lo = upFixed + k * upVar - above; // the least it has to move down
  const hi = below - (downFixed + k * downVar); // the most it may
  // a splash stretches a word upwards: when there is room to spare, the block sits low enough to leave it
  const want = fx.splashSquash * rise * (1 + grow);
  const ty = lo > hi ? (lo + hi) / 2 : Math.min(hi, Math.max(lo + Math.min(want, Math.max(0, hi - lo)), 0));
  const free = above + ty - upFixed - k * upVar; // room left over the highest word
  // the arc words come in over: as high as the room over the block, wave included
  const arc = Math.max(0, Math.min(fx.enterArc * u, free));
  const squash = Math.max(0, Math.min(fx.splashSquash, free / Math.max(1, rise * (1 + grow))));
  return { k, ty, arc, squash };
}

/** True when two colours are far enough apart to be told from each other in the frame checks' fill mask. */
const apart = (a: string, b: string): boolean => {
  const p = parseHex(a);
  const q = parseHex(b);
  return Math.max(Math.abs(p[0] - q[0]), Math.abs(p[1] - q[1]), Math.abs(p[2] - q[2])) > FAR;
};

/**
 * How far a word standing at (x, baseline y) is washed away: right and down by the parameters, but its
 * foot stays inside the caption box, so it is never on screen with nothing to see.
 */
function washed(c: Ctx | LabelCtx, fx: FX, u: number, k: number, x: number, y: number): { x: number; y: number } {
  return {
    x: Math.max(0, Math.min(fx.exitX * u, c.region.x1 - FOOT - x)),
    y: Math.max(0, Math.min(fx.exitY * u, c.region.y1 - FOOT - y - fx.waveAmp * ENDS * u * k)),
  };
}

const regionClip = (c: Ctx | LabelCtx): Rect => ({ x: c.region.x0, y: c.region.y0, w: c.region.x1 - c.region.x0, h: c.region.y1 - c.region.y0 });

export default defineTemplate<P>({
  name: "surf",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = thick(P.fx, c.layout.size);
    const u = em(c);
    const { k, ty, arc, squash } = fitHeights(c, fx, u);
    const clip = regionClip(c);
    const n = c.words.length;
    const act = c.words.map((w) => pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot)));
    const shapes: Shape[] = [];
    const units: Unit[] = [];
    const span = 1 - fx.exitStagger * Math.max(0, n - 1); // share of the exit each word takes

    c.layout.lines.forEach((line, li) => {
      const words = c.words.filter((w) => w.box.line === li);
      const swells = words.map((w) => ({ x: w.cx, a: Math.max(0, act[w.index]), half: Math.max(fx.crestWidth * u, w.box.w / 2) }));
      const water = sea(fx, u, k, c.T, swells);
      const base = c.cy + ty + line.y;

      // the water line draws itself from the left with the first word and is pulled away to the right at the end
      const x0 = Math.max(c.region.x0 + fx.lineRim * u + SLACK, c.cx + line.x - fx.linePad * u);
      const x1 = Math.min(c.region.x1 - fx.lineRim * u - SLACK, c.cx + line.x + line.w + fx.linePad * u);
      const drawn = E.outCubic(prog(t, 0, P.timing.enterS + P.timing.staggerS * (words.length - 1)));
      const pulled = E.inCubic(c.out);
      shapes.push(...waterLine(`p${c.page.id}l${li}`, fx, u, water, base + fx.lineY * u, lerp(x0, x1, pulled), lerp(x0, x1, drawn), colors.stroke, colors.accent2));

      for (const w of words) {
        const since = t - w.index * P.timing.staggerS;
        if (since < 0) continue;
        const a = act[w.index];
        const lit = Math.min(1, Math.max(0, w.a));
        const uw = w.box.size;
        // in: from the left, over an arc, turning with its path
        const p = E.outCubic(prog(since, 0, P.timing.enterS));
        const half = w.box.w / 2;
        const come = arrive(p, fx.enterSlide * u, arc, half, fx.enterTilt);
        // out: washed away to the right and down, the words on the right first (nobody is run over)
        const q = E.inCubic(prog(c.out, fx.exitStagger * (n - 1 - w.index), span));
        // a shouted word splashes down when it is spoken
        const hit = w.emph && w.since > 0 ? Math.exp((-3 * w.since) / fx.splashS) * (w.since < fx.splashS * 2 ? 1 : 0) : 0;
        const wob = hit * Math.sin(2 * Math.PI * fx.splashHz * w.since);
        const ride = afloat(water, w.cx + come.x, half, fx.tiltGain);
        const gone = washed(c, fx, u, k, w.cx, c.cy + w.box.y + ty);
        // a word at the edge of the box grows and splashes only as wide as the box lets it
        const over = 1 + 0.04 * fx.activeOvershoot; // the top of the growth's overshoot
        const lean = 0.5 * (line.bottom - line.top) * Math.sin(((fx.splashDeg + LEAN) * Math.PI) / 180); // a tilted word is that much wider
        const widest = 1 + Math.max(0, Math.min(w.cx - half - c.region.x0, c.region.x1 - w.cx - half) - (fx.halo + fx.rim) * uw - SLACK - lean) / half;
        const grow = Math.min(fx.activeScale - 1, (widest - 1) / over);
        const wide = Math.max(0, Math.min(squash, widest / (1 + grow * over) - 1));
        const m = pose([w.cx, c.cy + w.box.y], {
          x: come.x + gone.x * q,
          y: ty + ride.y + come.y + gone.y * q,
          scale: lerp(fx.enterScale, 1, p) * (1 + grow * a) * lerp(1, fx.exitScale, q),
          sx: 1 + wide * wob,
          sy: 1 - squash * wob,
          rot: ride.deg + come.deg + fx.splashDeg * hit * Math.cos(2 * Math.PI * fx.splashHz * w.since) * Math.min(1, w.since * 12) + fx.exitDeg * q,
        });
        const y1 = c.cy + w.box.y;
        const y0 = y1 - c.layout.capH * (uw / c.layout.size);
        const from = w.emph ? colors.emph : colors.text;
        const top = mix(from, colors.active, w.emph ? 0 : lit);
        const low = mix(w.emph ? mix(colors.emph, colors.accent, fx.deep) : colors.accent, mix(colors.active, colors.accent, fx.deep), w.emph ? 0 : lit);
        const g = ramp(top, low, lerp(y0, y1, fx.gradTop), lerp(y0, y1, fx.gradBottom));
        units.push(
          wordUnit(c, w, {
            m,
            z: 0, // one level for all: rims under outlines under fills, so a growing word never paints over its neighbour
            clip: p < 1 || c.out > 0 ? clip : undefined,
            fg: g.fg,
            layers: letters(thick(fx, uw, lerp(fx.enterScale, 1, p) * lerp(1, fx.exitScale, q) * (1 - squash * Math.abs(wob))), uw, g.grad, colors.stroke, colors.accent2, isEmoji(w.text, w.box.font)),
          }),
        );

        // spray: drops thrown up and outwards from the top of the word, each on its own arc and in its own time
        if (w.emph && w.since > 0 && w.since < fx.dropS * (1 + fx.dropLag)) {
          const edge = fx.dropRim * u;
          const topY = y0 + ty + ride.y;
          const up = Math.min(fx.dropUp * u, Math.max(0, topY - c.region.y0 - fx.dropSize * u - edge - SLACK));
          for (let i = 0; i < fx.drops; i++) {
            const seed = `d${c.page.id}w${w.index}n${i}`;
            const life = fx.dropS * (0.65 + 0.35 * hash01(`${seed}l`));
            const f = (w.since - fx.dropS * fx.dropLag * hash01(`${seed}t`)) / life;
            if (f <= 0 || f >= 1) continue;
            const from = (hash01(`${seed}x`) - 0.5) * w.box.w * 0.85; // where on the word it leaves
            const dx = from + (from / (w.box.w / 2)) * fx.dropOut * u * (0.4 + hash01(`${seed}o`)) * f;
            const dy = -up * (0.4 + 0.6 * hash01(`${seed}h`)) * 4 * f * (1 - f); // up and back down
            const r = fx.dropSize * u * (0.5 + 0.5 * hash01(`${seed}r`)) * Math.sqrt(1 - f) * Math.min(1, f * 6);
            const cx = Math.min(c.region.x1 - r - edge - SLACK, Math.max(c.region.x0 + r + edge + SLACK, w.cx + dx));
            shapes.push(ellipse(seed, cx, topY + dy, r, r, { fill: colors.accent2, stroke: colors.stroke, strokeWidth: edge * 2, z: 3 }));
          }
        }
      }
    });
    return { units, shapes };
  },

  label: (c): Scene => {
    const { P, t, colors } = c;
    const fx = thick(P.fx, c.layout.size);
    const u = em(c);
    const { k, ty, arc } = fitHeights(c, fx, u);
    const clip = regionClip(c);
    const shapes: Shape[] = [];
    const units: Unit[] = [];
    const p = E.outCubic(prog(t, 0, P.label.enterS));
    const q = E.inCubic(c.out);
    // white letters cannot have a white rim: they take the water colour for rim and water line
    const foam = apart(colors.text, colors.accent2) ? colors.accent2 : colors.accent;
    c.layout.lines.forEach((line, li) => {
      const words = c.words.filter((w) => w.box.line === li);
      // a label is all crest: the water stands high under the whole of it
      const swells = [{ x: c.cx + line.x + line.w / 2, a: back(fx.activeOvershoot)(prog(t, 0, P.label.enterS)), half: Math.max(fx.crestWidth * u, line.w / 2) }];
      const water = sea(fx, u, k, c.T, swells);
      const x0 = Math.max(c.region.x0 + fx.lineRim * u + SLACK, c.cx + line.x - fx.linePad * u);
      const x1 = Math.min(c.region.x1 - fx.lineRim * u - SLACK, c.cx + line.x + line.w + fx.linePad * u);
      shapes.push(...waterLine(`l${c.label.id}l${li}`, fx, u, water, c.cy + ty + line.y + fx.lineY * u, lerp(x0, x1, q), lerp(x0, x1, p), colors.stroke, foam));
      for (const w of words) {
        const half = w.box.w / 2;
        const come = arrive(p, fx.enterSlide * u, arc, half, fx.enterTilt);
        const ride = afloat(water, w.cx + come.x, half, fx.tiltGain);
        const gone = washed(c, fx, u, k, w.cx, c.cy + w.box.y + ty);
        const m = pose([w.cx, c.cy + w.box.y], {
          x: come.x + gone.x * q,
          y: ty + ride.y + come.y + gone.y * q,
          scale: lerp(fx.enterScale, 1, p) * lerp(1, fx.exitScale, q),
          rot: ride.deg + come.deg + fx.exitDeg * q,
        });
        const y1 = c.cy + w.box.y;
        const y0 = y1 - c.layout.capH * (w.box.size / c.layout.size);
        const g = ramp(colors.text, mix(colors.text, colors.accent, fx.deep), lerp(y0, y1, fx.gradTop), lerp(y0, y1, fx.gradBottom));
        units.push(
          wordUnit(c, w, {
            m,
            z: 5,
            active: true,
            clip: p < 1 || c.out > 0 ? clip : undefined,
            fg: g.fg,
            layers: letters(thick(fx, w.box.size, lerp(fx.enterScale, 1, p) * lerp(1, fx.exitScale, q)), w.box.size, g.grad, colors.stroke, foam),
          }),
        );
      }
    });
    return { units, shapes };
  },
});
