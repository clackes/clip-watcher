import type { z } from "zod";
import { chain, pose, type Affine } from "./affine";
import { back, E, lerp, prog, spring } from "./ease";
import type { BaseParams, FontSpec } from "./schema";
import { dropShadow, inked, wordUnit, type Ctx, type LabelCtx, type Scene, type WordState } from "./scene";

/** A caption template: a parameter schema and two pure functions from an instant to a Scene. */
export type Template<P extends BaseParams = BaseParams> = {
  name: string;
  schema: z.ZodType<P>;
  /** Fonts besides params.font that the template draws with. */
  fonts?: (p: P) => FontSpec[];
  /** The words of one page at one instant. */
  build: (c: Ctx<P>) => Scene;
  /** A judge label (*FAIL*, *WATCHING*) at one instant. */
  label: (c: LabelCtx<P>) => Scene;
};

/** Size of one em of the fitted text in px: fx lengths are given in em so they shrink with the text. */
export const em = (c: Ctx | LabelCtx): number => c.layout.size;

/** Centre of the block, the usual pivot of page-level motion. */
export const blockCentre = (c: Ctx | LabelCtx): [number, number] => [c.cx, c.cy];

export const wordCentre = (w: WordState): [number, number] => [w.cx, w.cy];

/**
 * The label most templates share: the words pop in tilted, hold, and pop out. Colours come from the
 * label's own roles (text = the kind's colour), so the auto mode covers labels too.
 */
export function popLabel(
  c: LabelCtx,
  o: { halo: number; shadowY: number; freq?: number; damp?: number; extra?: (w: WordState, m: Affine) => Affine },
): Scene {
  const u = em(c);
  const s = spring(c.t, o.freq ?? 4.5, o.damp ?? 0.55);
  const out = E.inCubic(c.out);
  const scale = lerp(0.5, 1, s) * (1 - out);
  const rot = lerp(c.P.label.rotate * 2, c.P.label.rotate, E.outCubic(prog(c.t, 0, c.P.label.enterS)));
  const page = pose(blockCentre(c), { scale, rot });
  const units = c.words.map((w) => {
    const m = o.extra ? o.extra(w, page) : page;
    return wordUnit(c, w, {
      m,
      z: 5,
      active: true,
      fg: [c.colors.text],
      layers: inked(c.colors.text, c.colors.stroke, o.halo * u, [dropShadow(c.colors.shadow, o.halo * u, 0, o.shadowY * u)]),
    });
  });
  return { units, shapes: [] };
}

/** Overshooting 0..1 for "pop" entrances, as a function of seconds. */
export const popIn = (t: number, dur: number, overshoot = 1.9): number => back(overshoot)(prog(t, 0, dur));

export { chain, pose };
