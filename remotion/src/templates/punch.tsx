import { z } from "zod";
import { chain, IDENTITY, pose, type Affine } from "../core/affine";
import { animate, animLength, animRef, type AnimOut, type AnimRef, type By } from "../core/animations";
import { mix } from "../core/color";
import { clamp } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { dropShadow, inked, letterUnits, wordUnit, type Ctx, type LabelCtx, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate, em } from "../core/template";

/**
 * PUNCH: the viral standard, and the template built entirely from the animation library. Fat geometric
 * caps with a thick outline and a hard shadow. By default the phrase pops in as one block, the spoken
 * word punches up and takes the highlight colour, shouted words shake, and the phrase shrinks away
 * before a pause. Every one of those motions is a named animation (src/core/animations.ts) chosen here
 * by name, so each can be swapped from the settings, with its own parameters, per page, word or letter:
 *   render.rm.templates.punch.fx.enter: {name: drop, by: letter, params: {height: 1.2}}
 */
const schema = paramsSchema({
  halo: z.number().min(0.02).max(0.3), // outline reach, em
  shadowY: z.number().min(0).max(0.3), // hard shadow drop, em
  // how the phrase arrives: {name, params, by}. by: as one block (page), word after word or letter after
  // letter, timing.staggerS apart; an animation only takes the units it suits
  enter: animRef("enter"),
  exit: animRef("exit"), // how it leaves before a pause, same shape
  active: animRef("active"), // what the spoken word does
  emph: animRef("emph"), // what a shouted word does on top
  emphBoost: z.number().min(1).max(2), // a shouted word's spoken motion is this much stronger
  idle: animRef("idle").nullable(), // what everything keeps doing (null: holds still)
});
type P = z.infer<typeof schema>;
type C = Ctx<P> | LabelCtx<P>;

/** The three points an animation may turn or squash about. */
type Piv = { centre: [number, number]; base: [number, number]; top: [number, number] };
const pivots = (cx: number, base: number, capH: number): Piv => ({ centre: [cx, base - capH / 2], base: [cx, base], top: [cx, base - capH] });
const at = (o: AnimOut, piv: Piv): Affine => pose(piv[o.pivot ?? "centre"], o);

/** Entrance of unit number i. */
function arrive(c: C, ref: AnimRef, i: number, piv: Piv): Affine {
  return at(animate(ref, { t: c.t - i * c.P.timing.staggerS, dur: c.P.timing.enterS, u: em(c), i, a: 0 }), piv);
}

/** Exit of unit number i of n: the stagger is squeezed so the last unit is gone when the exit ends. */
function leave(c: C, ref: AnimRef, i: number, n: number, piv: Piv): Affine {
  if (c.exit <= 0 || c.t < c.exitT) return IDENTITY;
  const step = n > 1 ? Math.min(c.P.timing.staggerS, (c.exit * 0.4) / (n - 1)) : 0;
  const t = c.t - c.exitT - i * step;
  return t <= 0 ? IDENTITY : at(animate(ref, { t, dur: c.exit - (n - 1) * step, u: em(c), i, a: 0 }), piv);
}

function draw(c: C, fillOf: (w: WordState) => string, label: boolean): Scene {
  const fx = c.P.fx;
  const u = em(c);
  const lines = c.layout.lines;
  const last = lines[lines.length - 1];
  const block = pivots(c.cx, c.cy + last.y, last.y - lines[0].y + c.layout.capH);
  const enterBy: By = fx.enter.by ?? "page";
  const exitBy: By = fx.exit.by ?? "page";
  const letters = enterBy === "letter" || exitBy === "letter";
  const nWords = c.words.length;
  const nLetters = c.words.reduce((s, w) => s + w.box.glyphs.length, 0);
  // while anything is arriving or leaving it is drawn through the caption box: a drop or a slide starts
  // outside it, and what is outside is simply not there
  const steps = enterBy === "page" ? 0 : enterBy === "word" ? nWords : nLetters;
  const arriving = c.t < animLength(fx.enter, c.P.timing.enterS) + c.P.timing.staggerS * steps;
  const clip = arriving || c.out > 0 ? { x: c.region.x0, y: c.region.y0, w: c.region.x1 - c.region.x0, h: c.region.y1 - c.region.y0 } : undefined;
  const units: Unit[] = [];
  let seen = 0;
  for (const w of c.words) {
    const fill = fillOf(w);
    const layers = inked(fill, c.colors.stroke, fx.halo * u, [dropShadow(c.colors.shadow, fx.halo * u, 0, fx.shadowY * u)]);
    const wp = pivots(w.cx, c.cy + w.box.y, c.layout.capH * (w.box.size / c.layout.size));
    const a = clamp(w.a) * (w.emph ? fx.emphBoost : 1);
    // the word's own motions: idle, then the shout, then the highlight
    const own = chain(
      at(animate(fx.idle, { t: c.T, dur: 0, u, i: w.index, a: 0 }), wp),
      w.emph && !label ? at(animate(fx.emph, { t: w.since, dur: 0, u, i: w.index, a: 0 }), wp) : IDENTITY,
      label ? IDENTITY : at(animate(fx.active, { t: w.since, dur: 0, u, i: w.index, a }), wp),
    );
    const z = !label && w.phase === "active" ? 1 : 0;
    const active = label ? true : undefined;
    // which unit of how many, about which pivots, for a page-, word- or letter-wise animation
    const pick = (mode: By, letter: number, lp: Piv): [number, number, Piv] =>
      mode === "page" ? [0, 1, block] : mode === "word" ? [w.index, nWords, wp] : [letter, nLetters, lp];
    if (!letters) {
      const [ei, , ep] = pick(enterBy, 0, wp);
      const [xi, xn, xp] = pick(exitBy, 0, wp);
      const m = chain(own, arrive(c, fx.enter, ei, ep), leave(c, fx.exit, xi, xn, xp));
      units.push(wordUnit(c, w, { m, z, fg: [fill], layers, clip, active }));
    } else {
      const first = seen;
      units.push(
        ...letterUnits(c, w, (_g, k, _n, gx) => {
          const lp = pivots(gx, c.cy + w.box.y, wp.base[1] - wp.top[1]);
          const [ei, , ep] = pick(enterBy, first + k, lp);
          const [xi, xn, xp] = pick(exitBy, first + k, lp);
          const m = chain(own, arrive(c, fx.enter, ei, ep), leave(c, fx.exit, xi, xn, xp));
          return { m, z, fg: [fill], layers, clip, active };
        }),
      );
    }
    seen += w.box.glyphs.length;
  }
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "punch",
  schema,
  build: (c) =>
    draw(c, (w) => mix(w.emph ? c.colors.emph : c.colors.text, w.emph ? c.colors.emph : c.colors.active, clamp(w.a)), false),
  // a label is the same lettering arriving and leaving the same way, in the label's colour, tilted
  label: (c) => {
    const tilt = pose([c.cx, c.cy], { rot: c.P.label.rotate });
    return { shapes: [], units: draw(c, () => c.colors.text, true).units.map((u) => ({ ...u, m: chain(u.m, tilt), z: 5 })) };
  },
});
