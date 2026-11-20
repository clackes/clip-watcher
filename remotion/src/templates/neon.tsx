import { z } from "zod";
import type { Affine } from "../core/affine";
import { contrast, mix, parseHex, toHex } from "../core/color";
import { clamp, E, hash01, lerp, prog, pulse, spring, springSettle } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { rect, wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Rect, type Scene, type Shape, type WordState } from "../core/scene";
import { defineTemplate, em } from "../core/template";

/**
 * NEON: a neon sign. The phrase sits on an opaque near-black plate with a glowing rim; every letter is a
 * tube that switches on with a stutter, the spoken word runs white-hot inside a breathing coloured glow,
 * shouted words buzz in their own hue, and before a pause the tubes go dark and the plate closes.
 * Nothing is ever transparent: an unlit tube is a darker shade that still keeps its contrast on the plate,
 * and the glow is held back so that every tube keeps `glowContrast` against the light around it.
 */
const schema = paramsSchema({
  padX: z.number().min(0.1).max(1), // the plate reaches this far past the text, em
  padY: z.number().min(0.05).max(1),
  radius: z.number().min(0).max(1.2), // plate corner radius, em (a one-line sign is a pill from about 0.6)
  rim: z.number().min(0.01).max(0.15), // the tube that runs round the plate, em
  rimGlow: z.number().min(0).max(0.3), // how far the rim's halo reaches outside the plate, em
  rimGlowAlpha: z.number().min(0).max(0.5), // opacity of each of its steps
  rimGlowSteps: z.number().int().min(1).max(6), // how many steps the halo has
  rimHz: z.number().min(0).max(2), // the rim breathes this often
  rimHot: z.number().min(0).max(0.6), // and goes this far from its colour towards white-hot at the top of a breath
  rimShout: z.number().min(0).max(0.6), // how far it goes while a shouted word is spoken
  knock: z.number().min(0.07).max(0.2), // plate-coloured backing round every tube, em: what the letters are measured against
  openFrom: z.number().min(0.1).max(1), // the plate opens from this share of its width
  openHz: z.number().min(1).max(12), // opening spring: Hz
  openDamp: z.number().min(0.3).max(1), // opening spring: damping ratio
  textInset: z.number().min(0).max(0.5), // while the plate opens or closes, text shows only this far inside its ends, em
  unlitContrast: z.number().min(7).max(12), // contrast an unlit tube keeps on the plate
  unlitMost: z.number().min(0).max(0.9), // and it never goes further than this towards the plate colour
  onS: z.number().min(0.07).max(0.4), // a tube takes this long to light
  lagS: z.number().min(0).max(0.2), // the letters of a word light up to this far apart (random per letter)
  stutterAtS: z.number().min(0.14).max(1), // the stutter starts this long after a tube began to light (onS + 0.07 or more)
  stutterS: z.number().min(0.21).max(0.6), // the stutter: a third going down, a third dim, a third coming back
  stutterDip: z.number().min(0).max(1), // how far the power drops in it
  stutterVary: z.number().min(0).max(1), // how much the drop differs from letter to letter
  hotStutter: z.number().min(0).max(1), // share of the drop a tube keeps while its word is spoken
  faultyGapS: z.number().min(0.07).max(1), // one tube per sign stutters a second time, this long after the first
  restGlow: z.number().min(0).max(0.4), // glow reach of a lit tube, em
  restGlowP: z.number().min(0).max(1), // its opacity right next to the tube
  hotGlow: z.number().min(0).max(0.4), // glow reach of the spoken word, em
  hotGlowP: z.number().min(0).max(1),
  glowSteps: z.number().int().min(2).max(12),
  glowContrast: z.number().min(4.5).max(12), // every tube keeps this contrast against its own glow
  pulseHz: z.number().min(0.5).max(4), // the spoken word's glow breathes at this rate
  pulseDepth: z.number().min(0).max(0.8), // and loses this share at the low point
  emphHot: z.number().min(0).max(1), // how far a spoken shout goes from its hue to white-hot
  buzzHz: z.number().min(1).max(4.5), // a shouted word shivers at this rate
  buzzAmp: z.number().min(0).max(0.06), // by this much, em
  buzzIdle: z.number().min(0).max(1), // share of the buzz a shout keeps while it is not spoken
  buzzY: z.number().min(0).max(1), // share of the shiver that also goes up and down
  buzzYHz: z.number().min(0.5).max(1.33), // at this many times the rate (under 6 Hz in all)
  buzzFlick: z.number().min(0).max(0.8), // share of its glow that flickers with the buzz
  labelHot: z.number().min(0).max(1), // how far a label goes from its colour to white-hot
  powerDown: z.number().min(0.3).max(0.8), // share of the exit in which the tubes go dark; the plate closes in the rest
  dimMinS: z.number().min(0.14).max(1), // an exit shorter than this only closes the plate
});
type P = z.infer<typeof schema>;
type Fx = P["fx"];

/** `top` laid over `base` at opacity p, the way the browser composites it (in sRGB). */
function over(base: string, top: string, p: number): string {
  const a = parseHex(base);
  const b = parseHex(top);
  return toHex([a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p, a[2] + (b[2] - a[2]) * p]);
}

const memo = new Map<string, number>();
function cached(key: string, f: () => number): number {
  let v = memo.get(key);
  if (v === undefined) {
    v = f();
    if (memo.size > 20000) memo.clear();
    memo.set(key, v);
  }
  return v;
}

/** The largest x in 0..most for which ok(x) holds (ok is true at 0 and falls away once). */
function most(max: number, ok: (x: number) => boolean): number {
  if (ok(max)) return max;
  let lo = 0;
  let hi = max;
  for (let i = 0; i < 12; i++) {
    const mid = (lo + hi) / 2;
    if (ok(mid)) lo = mid;
    else hi = mid;
  }
  return lo;
}

/** An unlit tube: its colour taken towards the plate, as far as it can go and keep `keep`:1 on it. */
function unlit(color: string, plate: string, fx: Fx): string {
  const k = cached(`u${color}${plate}${fx.unlitContrast}|${fx.unlitMost}`, () =>
    most(fx.unlitMost, (x) => contrast(mix(color, plate, x), plate) >= fx.unlitContrast),
  );
  return mix(color, plate, k);
}

/** The most a glow may cover the plate right next to a tube and leave the tube `keep`:1 against it. */
function glowCap(fill: string, glow: string, plate: string, keep: number): number {
  return cached(`g${fill}${glow}${plate}${keep}`, () => most(1, (x) => contrast(fill, over(plate, glow, x)) >= keep));
}

/** Power 0..1 of a tube `t` seconds after it was switched on: up, a stutter, up again. */
function power(t: number, fx: Fx, depth: number, twice: boolean): number {
  if (t <= 0) return 0;
  const third = fx.stutterS / 3;
  const dip = (at: number) => E.inOutSine(prog(t, at, third)) - E.inOutSine(prog(t, at + 2 * third, third));
  let p = 1 - depth * dip(fx.stutterAtS);
  if (twice) p *= 1 - depth * dip(fx.stutterAtS + fx.stutterS + fx.faultyGapS);
  return E.inOutSine(prog(t, 0, fx.onS)) * p;
}

type Tube = {
  w: WordState;
  on: number; // seconds since this word was switched on
  rest: string; // the tube lit
  hot: string; // the tube while its word is spoken
  restGlow: string;
  hotGlow: string;
  heat: number; // 0..1 how far from rest to hot
  buzz: number; // 0..1
  seed: string;
};

/** The sign at one instant: plate, rim and one unit per word (per-letter power as hard gradient stops). */
function sign(c: Ctx<P> | LabelCtx<P>, tubes: Tube[], id: string, shout: number): Scene {
  const fx = c.P.fx;
  const u = em(c);
  const { colors, t } = c;
  const turn = 2 * Math.PI;

  // exit: the tubes go dark, then the plate closes on its centre
  const dims = c.exit >= fx.dimMinS;
  const dark = dims ? E.inOutSine(prog(c.out, 0, fx.powerDown)) : 0;
  const close = E.inCubic(dims ? prog(c.out, fx.powerDown, 1 - fx.powerDown) : c.out);

  const open = lerp(fx.openFrom, 1, spring(t, fx.openHz, fx.openDamp)) * (1 - close);
  const half = ((c.layout.right - c.layout.left) / 2 + fx.padX * u) * open;
  const top = c.cy + c.layout.top - fx.padY * u;
  const height = c.layout.bottom - c.layout.top + 2 * fx.padY * u;
  const rimW = fx.rim * u;
  const r = Math.min(fx.radius * u, height / 2, half);
  // the rim is its own tube (accent): it breathes towards white-hot, flares on a shout and goes dark with the rest
  const breath = 0.5 - 0.5 * Math.cos(turn * fx.rimHz * c.T);
  const flare = Math.max(fx.rimHot * breath, fx.rimShout * shout);
  const rimNow = mix(mix(colors.accent, colors.plateText, flare), colors.plate, dark * fx.unlitMost);
  const shapes: Shape[] = [];
  for (let k = fx.rimGlowSteps; k >= 1; k--) {
    const out = rimW / 2 + (fx.rimGlow * u * k) / fx.rimGlowSteps;
    shapes.push(
      rect(`${id}halo${k}`, c.cx - half - out, top - out, 2 * (half + out), height + 2 * out, {
        fill: rimNow,
        role: "decor",
        alpha: fx.rimGlowAlpha * (1 - dark) * lerp(1 - fx.rimHot, 1, Math.max(breath, shout)),
        r: r + out,
        z: -6,
      }),
    );
  }
  shapes.push(rect(`${id}plate`, c.cx - half, top, 2 * half, height, { fill: colors.plate, stroke: rimNow, strokeWidth: rimW, r, z: -5 }));

  // while the plate moves the text shows only inside it
  const moving = close > 0 || t < springSettle(fx.openHz, fx.openDamp);
  const inset = rimW / 2 + fx.textInset * u;
  const clip: Rect | undefined = moving ? { x: c.cx - half + inset, y: top, w: Math.max(0, 2 * half - 2 * inset), h: height } : undefined;

  // one tube of the sign is faulty: it stutters twice
  const total = tubes.reduce((n, tb) => n + tb.w.box.glyphs.length, 0);
  const faulty = Math.floor(hash01(`${id}faulty`) * total);
  let seen = 0;

  const units = tubes.map((tb) => {
    const w = tb.w;
    const lit = mix(tb.rest, tb.hot, tb.heat);
    const off = unlit(tb.rest, colors.plate, fx);
    let low = 1;
    let sum = 0;
    const fills = w.box.glyphs.map((_, k) => {
      const seed = `${tb.seed}c${k}`;
      const twice = seen++ === faulty;
      const depth = fx.stutterDip * (twice ? 1 : lerp(1 - fx.stutterVary, 1, hash01(seed))) * lerp(1, fx.hotStutter, tb.heat);
      const p = power(tb.on - hash01(`${seed}lag`) * fx.lagS, fx, depth, twice) * (1 - dark);
      low = Math.min(low, p);
      sum += p;
      return mix(off, lit, p);
    });
    const mean = sum / Math.max(1, fills.length);

    // the glow: as strong as asked, never so strong that the dimmest tube of the word loses its contrast
    const hue = mix(tb.restGlow, tb.hotGlow, tb.heat);
    const breath = 1 - fx.pulseDepth * (0.5 - 0.5 * Math.cos(turn * fx.pulseHz * Math.max(0, w.since)));
    const flick = 1 - fx.buzzFlick * tb.buzz * (0.5 + 0.5 * Math.sin(turn * fx.buzzHz * c.T));
    const want = lerp(fx.restGlowP, fx.hotGlowP * breath, tb.heat) * mean * mean * flick;
    const peak = Math.min(want, glowCap(mix(off, lit, low), hue, colors.plate, fx.glowContrast));
    const steps = fx.glowSteps;

    const flat = fills.every((f) => f === fills[0]);
    const x0 = c.cx + w.box.x;
    const grad: Grad | undefined = flat
      ? undefined
      : {
          x1: x0,
          y1: 0,
          x2: x0 + w.box.w,
          y2: 0,
          stops: w.box.glyphs.flatMap((g, k) => [
            { o: clamp(g.x / w.box.w), c: fills[k] },
            { o: clamp((g.x + g.w) / w.box.w), c: fills[k] },
          ]),
        };
    const layers: Layer[] = [
      { kind: "stroke", color: colors.plate, width: fx.knock * u },
      { kind: "glow", color: hue, width: lerp(fx.restGlow, fx.hotGlow, tb.heat) * u, alpha: 1 - Math.pow(1 - peak, 1 / steps), steps },
      { kind: "fill", color: fills[0], grad },
    ];
    const amp = fx.buzzAmp * u * tb.buzz;
    const phase = turn * hash01(tb.seed);
    const m: Affine = [1, 0, 0, 1, amp * Math.sin(turn * fx.buzzHz * c.T + phase), fx.buzzY * amp * Math.sin(turn * fx.buzzHz * fx.buzzYHz * c.T + 2 * phase)];
    return wordUnit(c, w, { m, clip, layers, fg: [...new Set(fills)] });
  });
  return { units, shapes };
}

export default defineTemplate<P>({
  name: "neon",
  schema,
  build: (c): Scene => {
    const { colors, P } = c;
    const fx = P.fx;
    const hotShout = mix(colors.emph, colors.plateText, fx.emphHot);
    let shout = 0;
    const tubes = c.words.map((w): Tube => {
      // a tube heats up evenly over the time the highlight takes. (On the shared curve, which is most of
      // the way there after a third of that time, a tube went from its colour to nearly white in one frame
      // at 30 fps whenever its word began just after a frame, and then hardly changed: a jump.)
      const heat = clamp(pulse(c.t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, E.inOutSine));
      if (w.emph) shout = Math.max(shout, heat);
      return {
        w,
        on: c.t - w.index * P.timing.staggerS,
        rest: w.emph ? colors.emph : colors.text,
        hot: w.emph ? hotShout : colors.plateText,
        restGlow: w.emph ? colors.emph : colors.text,
        hotGlow: w.emph ? colors.emph : colors.active,
        heat,
        buzz: w.emph ? lerp(fx.buzzIdle, 1, heat) : 0,
        seed: `neon${c.page.id}w${w.index}`,
      };
    });
    return sign(c, tubes, `p${c.page.id}`, shout);
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    const hue = c.colors.text; // the label kind's colour
    const hot = mix(hue, c.colors.plateText, fx.labelHot);
    const tubes = c.words.map(
      (w): Tube => ({
        w,
        on: c.t - w.index * c.P.timing.staggerS,
        rest: hue,
        hot,
        restGlow: hue,
        hotGlow: hue,
        heat: 1,
        buzz: c.label.kind === "action" ? 0 : 1,
        seed: `neonl${c.label.id}w${w.index}`,
      }),
    );
    return sign(c, tubes, `l${c.label.id}`, c.label.kind === "action" ? 0 : 1);
  },
});
