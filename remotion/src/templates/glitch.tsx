import { z } from "zod";
import { pose } from "../core/affine";
import { mix } from "../core/color";
import { clamp, drift, E, hash01, hashSigned, lerp, prog, shake } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type LabelCtx, type Layer, type Rect, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate, wordCentre } from "../core/template";

/**
 * GLITCH: RGB split. White caps on a dark outline with a cyan and a pink copy behind them. A word that
 * tears is drawn as three horizontal bands; on entrance the bands start out of register and the copies far
 * apart, and everything snaps home. The spoken word takes the highlight with a short split pulse, shouted words
 * tear again and lean, and before a pause the copies fly apart while the word collapses to a line.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // dark outline, em (the colour copies are the same silhouette, shifted)
  split: z.number().min(0).max(0.2), // the copies sit this far left and right of the letters at rest, em
  splitDy: z.number().min(-1).max(1), // share of the split that also goes up (first copy) and down (second)
  breathe: z.number().min(0).max(1), // the rest split wanders by this share of itself
  breatheHz: z.number().min(0.1).max(2.4),
  enterSplit: z.number().min(0).max(0.6), // on entrance the copies start this far apart, em
  tearX: z.number().min(0).max(0.23), // and the bands this far out of register, em (under half the word gap: bands never reach the next word)
  tearS: z.number().min(0.1).max(0.8), // everything is home after this long
  cutA: z.number().min(0.15).max(0.5), // the first cut, as a share of the cap height from the top
  cutB: z.number().min(0.5).max(0.85), // the second cut
  cutVary: z.number().min(0).max(0.15), // each word moves its cuts by up to this share
  bandC: z.number().min(-1).max(1), // the bottom band moves this share of the way the top band does (the middle one goes the other way)
  tearVary: z.number().min(0).max(1), // each band of each word tears up to this share less far
  attack: z.number().min(0.05).max(0.9), // share of a pulse or a tear spent rising
  pulseSplit: z.number().min(0).max(0.4), // split pulse of the spoken word, em
  pulseS: z.number().min(0.1).max(1),
  emphPulse: z.number().min(1).max(3), // a shouted word's pulse is this many times as wide
  emphTear: z.number().min(0).max(0.23), // a shouted word's bands tear again this far when it is spoken, em
  emphTearS: z.number().min(0.1).max(1),
  emphSkew: z.number().min(0).max(20), // and it leans this far while it is spoken, degrees
  emphGrow: z.number().min(1).max(1.3), // and stretches this much taller
  emphShakeS: z.number().min(0).max(1), // the lean rattles for this long
  emphShakeHz: z.number().min(1).max(6),
  emphHot: z.number().min(0).max(1), // a spoken shout goes this far from its colour towards the base colour
  exitSplit: z.number().min(0).max(0.6), // exit: the copies fly this far apart, em
  exitThin: z.number().min(0.02).max(0.45), // and the word collapses to this share of its height
  exitHalo: z.number().min(1).max(4), // while it is squashed its outline is made up to this many times as wide, so it stays as tall
});
type P = z.infer<typeof schema>;

/** 0 -> 1 -> 0 over x = 0..1, the top at `peak`: a quick attack and a longer release. */
const bump = (x: number, peak: number): number =>
  x <= 0 || x >= 1 ? 0 : x < peak ? E.inOutSine(x / peak) : 1 - E.inOutSine((x - peak) / (1 - peak));

type Torn = {
  w: WordState;
  born: number; // seconds since the word appeared (negative: not yet)
  since: number; // seconds since it was spoken
  fill: string;
  shout: boolean;
  lean: number; // 0..1 share of the shout's lean that holds now
  seed: string;
};


function torn(c: Ctx<P> | LabelCtx<P>, items: Torn[]): Scene {
  const fx = c.P.fx;
  const { colors } = c;
  const fly = E.inCubic(c.out);
  const units: Unit[] = [];
  for (const it of items) {
    if (it.born < 0) continue;
    const w = it.w;
    const u = w.box.size;
    const home = 1 - E.outExpo(prog(it.born, 0, fx.tearS)); // 1 -> 0: how far everything still is from its place
    const hit = bump(prog(it.since, 0, fx.pulseS), fx.attack);
    const rip = it.shout ? bump(prog(it.since, 0, fx.emphTearS), fx.attack) : 0;
    const wander = 1 + fx.breathe * drift(c.T, fx.breatheHz, it.seed);
    const d = u * (fx.split * wander + fx.enterSplit * home + fx.pulseSplit * hit * (it.shout ? fx.emphPulse : 1) + fx.exitSplit * fly);
    const squash = lerp(1, fx.exitThin, fly);
    const halo = fx.halo * u * Math.min(fx.exitHalo, 1 / squash);
    const layers: Layer[] = [
      { kind: "stroke", color: colors.accent, width: halo, dx: -d, dy: -d * fx.splitDy },
      { kind: "stroke", color: colors.accent2, width: halo, dx: d, dy: d * fx.splitDy },
      { kind: "stroke", color: colors.stroke, width: halo },
      { kind: "fill", color: it.fill },
    ];
    const skew = it.shout ? -fx.emphSkew * (it.lean + shake(it.since, fx.emphShakeS, fx.emphShakeHz)) : 0;
    // While a word is torn it is three bands; the cuts sit on whole pixel rows, so the bands meet without a
    // seam. In register it is drawn once, whole: the first band's unit without a window, the other two
    // behind an empty one (they keep their place in the scene, so a tear never starts from nothing).
    const torn = home > 0 || rip > 0;
    const capH = c.layout.capH * (u / c.layout.size);
    const top = c.cy + w.box.y - capH;
    const y1 = Math.round(top + capH * (fx.cutA + fx.cutVary * hashSigned(`${it.seed}a`)));
    const y2 = Math.max(y1 + 2, Math.round(top + capH * (fx.cutB + fx.cutVary * hashSigned(`${it.seed}b`))));
    const shut: Rect = { x: 0, y: 0, w: 0, h: 0 };
    const windows: (Rect | undefined)[] = torn
      ? [
          { x: 0, y: 0, w: c.width, h: y1 },
          { x: 0, y: y1, w: c.width, h: y2 - y1 },
          { x: 0, y: y2, w: c.width, h: Math.max(0, c.height - y2) },
        ]
      : [undefined, shut, shut];
    const bands: [string, number][] = [["a", 1], ["b", -1], ["c", fx.bandC]]; // prettier-ignore
    bands.forEach(([key, dir], k) => {
      const reach = lerp(1 - fx.tearVary, 1, hash01(`${it.seed}${key}`));
      const m = pose(wordCentre(w), {
        x: dir * reach * u * Math.max(fx.tearX * home, fx.emphTear * rip), // never both: a band stays clear of the next word
        skewX: skew,
        sy: squash * (it.shout ? lerp(1, fx.emphGrow, it.lean) : 1),
      });
      // an emoji is drawn in one go, copies and all: under the words, so its copies never cover a neighbour
      units.push(wordUnit(c, w, { key, clip: windows[k], m, layers, fg: [it.fill], z: isEmoji(w.text, w.box.font) ? -1 : 0 }));
    });
  }
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "glitch",
  schema,
  build: (c): Scene => {
    const { colors, P } = c;
    const fx = P.fx;
    return torn(
      c,
      c.words.map((w): Torn => {
        const a = clamp(w.a);
        return {
          w,
          born: c.t - w.index * P.timing.staggerS,
          since: w.since,
          fill: w.emph ? mix(colors.emph, colors.text, fx.emphHot * a) : mix(colors.text, colors.active, a),
          shout: w.emph,
          lean: a,
          seed: `glitch${c.page.id}w${w.index}`,
        };
      }),
    );
  },
  label: (c): Scene =>
    torn(
      c,
      c.words.map((w): Torn => {
        const born = c.t - w.index * c.P.timing.staggerS;
        return { w, born, since: born, fill: c.colors.text, shout: c.label.kind !== "action", lean: 0, seed: `glitchl${c.label.id}w${w.index}` };
      }),
    ),
});
