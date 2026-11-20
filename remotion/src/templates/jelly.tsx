import { z } from "zod";
import { chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, hashSigned, lerp, prog } from "../core/ease";
import { fontMetrics } from "../core/measure";
import { isEmoji, outlineFont } from "../core/fonts";
import { paramsSchema, type FontSpec } from "../core/schema";
import { letterUnits, type Ctx, type LabelCtx, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * JELLY: soft round lowercase that behaves like jelly. Every letter springs up on its own, stretching on
 * the way and squashing as it lands; a wave runs through the spoken word letter by letter and the
 * highlight colour runs through it the same way; the whole line keeps breathing. Shouted words are pink,
 * land wobbling and flash letter by letter as the wave passes. Every letter is its own blob with its own
 * complete outline, lying a little over the one before it. Before a pause the letters pop away one after another. Plum outline instead
 * of black, with a soft base under every letter: candy, not comic.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // outline, em
  haloFloor: z.number().min(0).max(10), // ...and never thinner than this on screen, px, however small the text (what the fill is read against)
  letterGap: z.number().min(0).max(0.4), // the ink of two neighbouring letters is never closer than this at rest, em: room to wobble
  haloFill: z.number().int().min(1).max(12), // the outline is laid down in this many widths, widest first: a single wide
  // stroke leaves a pinhole in the middle of a small round counter (a, e, o, %), the narrower ones close it
  drop: z.number().min(0).max(0.2), // the outline is this much deeper underneath (a soft base), em
  letterStepS: z.number().min(0).max(0.1), // letters of a word come up this long after each other,
  cascadeS: z.number().min(0).max(1), // but a long word is all up within this time
  popFrom: z.number().min(0.5).max(1), // a letter starts at this scale, standing on the baseline
  riseFrom: z.number().min(0).max(0.5), // and this far below it, em
  letterHz: z.number().min(1).max(6), // spring of a letter's entrance, Hz
  letterDamp: z.number().min(0.2).max(1), // damping ratio (lower = more wobble)
  squash: z.number().min(0).max(0.4), // stretch at full speed and squash on the rebound, share of the height
  waveLift: z.number().min(0).max(0.4), // the wave through the spoken word: each letter hops this high, em
  waveS: z.number().min(0.15).max(1), // one letter's hop lasts this long
  waveStepS: z.number().min(0).max(0.2), // and starts this long after the letter before
  waveSquash: z.number().min(0).max(0.3), // stretch on the way up, squash on the way down
  landS: z.number().min(0).max(1), // a letter does not join the wave before it has been up this long
  activeScale: z.number().min(1).max(1.3), // letters of the spoken word are this much bigger
  emphWave: z.number().min(1).max(2.5), // shouted words hop this many times higher
  emphFlash: z.number().min(0).max(1), // and flash this far towards the second accent colour at the top of the hop
  emphWobbleDeg: z.number().min(0).max(25), // shouted letters land turned by up to this much
  breathe: z.number().min(0).max(0.05), // idle breathing, share of the size
  breatheHz: z.number().min(0.1).max(2),
  breatheLag: z.number().min(0).max(2), // phase from letter to letter, radians: the swell travels along the line
  popStepS: z.number().min(0).max(0.1), // on the way out letters pop this long after each other
  popShare: z.number().min(0).max(0.9), // but all those delays together take at most this share of the exit
  popHop: z.number().min(0).max(0.3), // a letter hops this high as it pops, em
  popWiden: z.number().min(0).max(0.6), // and gets this much wider while it shrinks to nothing
});
type P = z.infer<typeof schema>;

type Look = {
  staggerS: number; // words start coming up this long after each other
  base: (w: WordState) => string; // colour of a letter at rest
  lit: (w: WordState) => string; // colour of the spoken word: it runs through the letters like the wave
  since: (w: WordState) => number; // seconds since the wave started through the word (negative: not yet)
  hold: (w: WordState) => number; // 1 while the highlight is on the word, easing to 0 after
};

// half the width of a wide lowercase letter, em: the room the last letter of a line needs when it widens
const HALF_LETTER = 0.35;
// levels between two neighbouring letters (a phrase has far fewer letters than 1 / Z_STEP)
const Z_STEP = 0.001;
// below half its size a letter is on its way in or out: the outline floor lets go
const HALF = 0.5;
// a letter this small is gone
const GONE = 0.04;

const spans = new Map<string, [number, number] | null>();

/** Where the ink of a glyph begins and ends, from its origin, in em (null: no outline, an emoji). */
function inkSpan(font: FontSpec, ch: string): [number, number] | null {
  const key = `${font.file}|${ch}`;
  let span = spans.get(key);
  if (span === undefined) {
    const glyph = outlineFont(font).charToGlyph(ch);
    let lo = Infinity;
    let hi = -Infinity;
    for (const cmd of glyph.index === 0 ? [] : glyph.getPath(0, 0, 1).commands) {
      for (const v of [cmd.x, cmd.x1, cmd.x2]) {
        if (v !== undefined) {
          lo = Math.min(lo, v);
          hi = Math.max(hi, v);
        }
      }
    }
    span = lo <= hi ? [lo, hi] : null;
    spans.set(key, span);
  }
  return span;
}

/** How far each letter of a word is moved sideways so that no two neighbours' ink is closer than `gap`
 * px (a t's bar or a j's tail reaches under its neighbour otherwise). The word grows evenly to both sides. */
function nudges(w: WordState, gap: number): number[] {
  const u = w.box.size;
  const g = w.box.glyphs;
  const ink = g.map((q) => inkSpan(w.box.font, q.ch) ?? [0, q.w / u]);
  const out = [0];
  for (let k = 0; k + 1 < g.length; k++) {
    const free = g[k + 1].x + ink[k + 1][0] * u - (g[k].x + ink[k][1] * u);
    out.push(out[k] + Math.max(0, gap - free));
  }
  const spread = out[out.length - 1];
  return out.map((v) => v - spread / 2);
}

/** Step response of a damped spring and its speed as a share of the top speed (analytic, so exact at any t). */
function boing(t: number, hz: number, damp: number): { s: number; v: number; over: number } {
  const zeta = Math.min(damp, 0.999);
  const k = Math.sqrt(1 - zeta * zeta);
  const over = Math.exp((-Math.PI * zeta) / k); // how far it overshoots
  if (t <= 0) return { s: 0, v: 0, over };
  const w = 2 * Math.PI * hz;
  const e = Math.exp(-zeta * w * t);
  const s = 1 - e * (Math.cos(w * k * t) + (zeta / k) * Math.sin(w * k * t));
  const v = (e * Math.sin(w * k * t)) / (k * Math.exp((-zeta * Math.acos(zeta)) / k));
  return { s, v, over };
}

function jelly(c: Ctx<P> | LabelCtx<P>, o: Look): Scene {
  const fx = c.P.fx;
  const { colors } = c;
  const L = c.layout;
  const R = c.region;
  const U = L.size;
  const fm = fontMetrics(c.P.font);
  const up = Math.max(fm.capH, fm.asc); // em above the baseline
  const { over } = boing(0, fx.letterHz, fx.letterDamp);

  // room the letters need around the ink of the block, and the scale at which all of it fits the box
  const swell = fx.activeScale - 1 + fx.breathe;
  const hopTop = fx.emphWave * Math.max(fx.waveLift, fx.waveLift / 2 + fx.waveSquash * up);
  const landTop = over * (fx.riseFrom + (1 - fx.popFrom) * up);
  const padT = (fx.halo + swell * up + Math.max(hopTop, landTop, fx.popHop)) * U;
  const padB = (fx.halo + fx.drop + (swell + fx.squash) * fm.desc + Math.max(0, fx.riseFrom - fm.desc * (1 - fx.popFrom))) * U;
  const moved = c.words.map((w) => nudges(w, fx.letterGap * w.box.size));
  const padX = (fx.halo + HALF_LETTER * (swell + fx.squash / 2 + fx.popWiden)) * U + Math.max(...moved.map((n) => n[n.length - 1]));
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

  const total = c.words.reduce((a, w) => a + w.box.glyphs.length, 0);
  const popStep = total > 1 ? Math.min(fx.popStepS, (c.exit * fx.popShare) / (total - 1)) : 0;
  const popS = c.exit - popStep * (total - 1);
  const units: Unit[] = [];
  let first = 0; // running number of the word's first letter in the phrase
  for (const w of c.words) {
    const u = w.box.size;
    const since = o.since(w);
    const hold = o.hold(w);
    const gain = w.emph ? fx.emphWave : 1;
    const from = first;
    const count = w.box.glyphs.length;
    const letterStep = count > 1 ? Math.min(fx.letterStepS, fx.cascadeS / (count - 1)) : 0;
    units.push(
      ...letterUnits(c, w, (g, k, _n, gx) => {
        const i = from + k;
        const tl = c.t - (w.index * o.staggerS + k * letterStep); // this letter's own clock
        if (tl < 0) return null;
        // an emoji is drawn as text, on whole pixels: it comes up once and then holds still (no spring tail,
        // no breathing), because a slow drift would show as steps
        const still = isEmoji(g.ch, w.box.font);
        const { s, v } = still ? { s: E.outCubic(prog(tl, 0, 1 / fx.letterHz)), v: 0 } : boing(tl, fx.letterHz, fx.letterDamp);
        // the wave: a smooth hop, stretched on the way up and squashed on the way down
        const x = prog(Math.min(since - k * fx.waveStepS, tl - fx.landS), 0, fx.waveS);
        const hop = Math.sin(Math.PI * x) ** 2;
        const stretch = fx.squash * v + fx.waveSquash * Math.sin(2 * Math.PI * x);
        const lit = E.outCubic(prog(since, k * fx.waveStepS, c.P.timing.activeInS)) * hold;
        const fill = w.emph ? mix(o.base(w), colors.accent2, fx.emphFlash * hop) : mix(o.base(w), o.lit(w), lit);
        const breathe = still ? 1 : 1 + fx.breathe * Math.sin(2 * Math.PI * fx.breatheHz * c.T - i * fx.breatheLag);
        // the way out: a little hop, wider, and gone
        const pe = c.exit > 0 ? prog(c.t, c.exitT + i * popStep, popS) : 0;
        const left = 1 - E.inCubic(pe);
        if (left < GONE) return null;
        const scale = lerp(fx.popFrom, 1, s) * breathe * (1 + (fx.activeScale - 1) * lit) * left;
        const sx = (1 - stretch / 2) * (1 + fx.popWiden * pe);
        const sy = 1 + stretch;
        const halo = Math.max(fx.halo * u, fx.haloFloor / (fit * Math.max(scale * Math.min(sx, sy), HALF)));
        const m = chain(
          pose([gx, c.cy + w.box.y], {
            x: moved[w.index][k],
            scale,
            sx,
            sy,
            rot: w.emph ? fx.emphWobbleDeg * hashSigned(`${i}${w.text}`) * (1 - s) : 0,
            y: ((1 - s) * fx.riseFrom - hop * gain * fx.waveLift - Math.sin(Math.PI * pe) * fx.popHop) * u,
          }),
          block,
        );
        return {
          m,
          // every letter on its own level, later ones on top: a letter's outline is whole even where its
          // neighbour comes close (letters of one word differ in colour and height while the wave runs)
          z: (w.phase === "active" ? 1 : 0) + i * Z_STEP,
          fg: [fill],
          layers: [
            { kind: "stroke", color: colors.shadow, width: halo, dy: fx.drop * u },
            { kind: "glow", color: colors.stroke, width: halo, alpha: 1, steps: fx.haloFill },
            { kind: "stroke", color: colors.stroke, width: halo },
            { kind: "fill", color: fill },
          ],
        };
      }),
    );
    first += w.box.glyphs.length;
  }
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "jelly",
  schema,
  build: (c): Scene => {
    const { P, colors } = c;
    return jelly(c, {
      staggerS: P.timing.staggerS,
      base: (w) => (w.emph ? colors.emph : colors.text),
      lit: () => colors.active,
      since: (w) => w.since,
      hold: (w) => (w.phase === "done" ? 1 - E.inOutQuad(prog(c.t, w.activeEnd, P.timing.activeOutS)) : 1),
    });
  },
  // a label comes up the same way, in its own colour; the wave runs through it once as it lands
  label: (c): Scene =>
    jelly(c, {
      staggerS: c.P.timing.staggerS,
      base: () => c.colors.text,
      lit: () => c.colors.text,
      since: () => c.t,
      hold: () => 1,
    }),
});
