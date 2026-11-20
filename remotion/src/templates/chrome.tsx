import { z } from "zod";
import { chain, pose } from "../core/affine";
import { mix, parseHex, toHex } from "../core/color";
import { back, clamp, E, hash01, prog, pulse, spring, springSettle } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Rect, type Scene, type WordState } from "../core/scene";
import { defineTemplate, wordCentre } from "../core/template";

/**
 * CHROME: liquid metal. Wide caps filled with a chrome reflection (a ramp from the tint to white, a hard
 * horizon, the same ramp again) on a dark outline with a hard shadow. The horizon sloshes slowly and the
 * word floats with it. Lines are poured in from the left, leaning forward and settling; a slanted glint
 * runs across the spoken word, which takes its own tint; shouted words are gold; before a pause the
 * lines lean back and leave to the right.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // dark outline, em
  shadowX: z.number().min(0).max(0.2), // hard shadow offset, em
  shadowY: z.number().min(0).max(0.2),
  sheen: z.number().min(0.3).max(1), // the light end of the reflection: this far from the tint to white
  horizon: z.number().min(0.3).max(0.7), // the hard line of the reflection, share of the cap height from the top
  slosh: z.number().min(0).max(0.15), // it rises and falls by this share of the cap height...
  sloshHz: z.number().min(0.05).max(1), // ...this often
  bob: z.number().min(0.004).max(0.05), // and the word floats up and down with it by this much, em
  slide: z.number().min(0).max(3), // a line comes in from this far to the left, em
  enterSkew: z.number().min(0).max(30), // leaning forward by this much, degrees
  skewHz: z.number().min(1).max(10), // the lean settles like a spring: Hz
  skewDamp: z.number().min(0.2).max(1), // and damping ratio
  pourS: z.number().min(0.07).max(1), // the line is uncovered from left to right in this long
  gate: z.number().min(0.1).max(1), // the window a line comes through reaches this far past its ends, em
  punch: z.number().min(1).max(1.3), // the spoken word grows to this
  punchOver: z.number().min(0).max(4), // overshoot of that
  glintS: z.number().min(0.15).max(1.5), // a glint crosses the spoken word in this long
  glintW: z.number().min(0.05).max(1.5), // half its width, em
  glintSlant: z.number().min(-40).max(40), // degrees from upright
  glintAlpha: z.number().min(0).max(1), // how much of the letter it covers at its middle
  exitSlide: z.number().min(0).max(3), // exit: a line travels this far to the right, em
  exitSkew: z.number().min(0).max(30), // leaning back by this much, degrees
  labelTint: z.number().min(0).max(1), // a label's tint: its own colour taken this far towards the base tint
});
type P = z.infer<typeof schema>;

/** Colour a share p of the way from a to b in sRGB: the path an SVG gradient takes between two stops. */
function along(a: string, b: string, p: number): string {
  const x = parseHex(a);
  const y = parseHex(b);
  return toHex([x[0] + (y[0] - x[0]) * p, x[1] + (y[1] - x[1]) * p, x[2] + (y[2] - x[2]) * p]);
}

/** #RRGGBB with an alpha: only the glint, a highlight laid over the opaque fill, is ever translucent. */
const veil = (hex: string, a: number): string =>
  `${hex}${Math.round(clamp(a) * 255)
    .toString(16)
    .padStart(2, "0")}`;

type Metal = {
  w: WordState;
  tint: string; // the colour the reflection runs to
  grow: number; // 0..1 of the punch
  since: number; // seconds since the glint set off
  seed: string;
};

function metal(c: Ctx<P> | LabelCtx<P>, items: Metal[]): Scene {
  const fx = c.P.fx;
  const { colors, t, P } = c;
  const turn = 2 * Math.PI;
  const lines = c.layout.lines.map((ln, li) => {
    const u = c.layout.size;
    const lt = t - li * P.timing.staggerS;
    const left = c.cx + ln.x - fx.gate * u;
    const width = ln.w + 2 * fx.gate * u;
    const settle = spring(lt, fx.skewHz, fx.skewDamp);
    const away = E.inCubic(c.out);
    // the window: opens from the left on entrance, closes from the left on exit
    const shut = width * away;
    const open = width * E.outCubic(prog(lt, 0, fx.pourS));
    const moving = c.out > 0 || lt < Math.max(fx.pourS, P.timing.enterS, springSettle(fx.skewHz, fx.skewDamp));
    const clip: Rect | undefined = moving ? { x: left + shut, y: 0, w: Math.max(0, open - shut), h: c.height } : undefined;
    const m = pose([c.cx + ln.x + ln.w / 2, c.cy + ln.y], {
      x: (-fx.slide * (1 - E.outQuart(prog(lt, 0, P.timing.enterS))) + fx.exitSlide * away) * u,
      skewX: -fx.enterSkew * (1 - settle) + fx.exitSkew * E.outCubic(c.out),
    });
    return { lt, clip, m };
  });

  const units = items
    .filter((it) => lines[it.w.box.line].lt >= 0)
    .map((it) => {
      const w = it.w;
      const ln = lines[w.box.line];
      const u = w.box.size;
      const capH = c.layout.capH * (u / c.layout.size);
      const base = c.cy + w.box.y;
      const white = colors.plateText;
      const light = along(it.tint, white, fx.sheen);
      // the reflection: tint -> light, a hard horizon, tint -> light again; the horizon sloshes
      const wave = Math.sin(turn * (fx.sloshHz * c.T + hash01(it.seed)));
      const h = clamp(fx.horizon + fx.slosh * wave, 0.05, 0.95);
      const fill: Grad = {
        x1: 0,
        y1: base - capH,
        x2: 0,
        y2: base,
        stops: [
          { o: 0, c: it.tint },
          { o: h, c: light },
          { o: h, c: it.tint },
          { o: 1, c: light },
        ],
      };
      // the glint: a slanted band of the glint colour that crosses the word once
      const x0 = c.cx + w.box.x - fx.glintW * u;
      const x1 = c.cx + w.box.x + w.box.w + fx.glintW * u;
      const half = (fx.glintW * u) / (x1 - x0);
      const p = prog(it.since, 0, fx.glintS);
      const on = p > 0 && p < 1;
      const at = -half + (1 + 2 * half) * E.inOutSine(p);
      const glint: Grad = {
        x1: x0,
        y1: base,
        x2: x1,
        y2: base + (x1 - x0) * Math.tan((fx.glintSlant * Math.PI) / 180),
        stops: on
          ? [
              { o: clamp(at - half), c: veil(colors.accent, 0) },
              { o: clamp(at), c: veil(colors.accent, fx.glintAlpha) },
              { o: clamp(at + half), c: veil(colors.accent, 0) },
            ]
          : [{ o: 0, c: veil(colors.accent, 0) }],
      };
      const halo = fx.halo * u;
      const layers: Layer[] = [
        { kind: "stroke", color: colors.shadow, width: halo, dx: fx.shadowX * u, dy: fx.shadowY * u },
        { kind: "stroke", color: colors.stroke, width: halo },
        { kind: "fill", color: light, grad: fill },
        { kind: "fill", color: veil(colors.accent, 0), grad: glint },
      ];
      const own = pose(wordCentre(w), { scale: 1 + (fx.punch - 1) * it.grow, y: fx.bob * u * wave });
      // every colour of the reflection, glint included, lies between white and the tint: report that line closely
      const fg = [white, along(white, it.tint, 1 / 3), along(white, it.tint, 2 / 3), it.tint];
      return wordUnit(c, w, { m: chain(own, ln.m), clip: ln.clip, layers, fg });
    });
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "chrome",
  schema,
  build: (c): Scene => {
    const { colors, P } = c;
    const fx = P.fx;
    return metal(
      c,
      c.words.map(
        (w): Metal => ({
          w,
          tint: w.emph ? colors.emph : mix(colors.text, colors.active, clamp(w.a)),
          grow: pulse(c.t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.punchOver)),
          since: w.since,
          seed: `chrome${c.page.id}w${w.index}`,
        }),
      ),
    );
  },
  label: (c): Scene =>
    metal(
      c,
      c.words.map(
        (w): Metal => ({
          w,
          tint: mix(c.colors.text, c.colors.accent2, c.P.fx.labelTint),
          grow: 0,
          since: c.t - c.P.timing.enterS,
          seed: `chromel${c.label.id}w${w.index}`,
        }),
      ),
    ),
});
