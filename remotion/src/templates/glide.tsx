import { z } from "zod";
import { chain, pose, apply } from "../core/affine";
import { E, lerp, prog, spring } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { dropShadow, inked, rect, wordUnit, type Scene, type Shape, type Unit } from "../core/scene";
import { blockCentre, defineTemplate, em, popLabel } from "../core/template";

/**
 * GLIDE: a rounded highlight box sits behind the spoken word and glides to the next one, stretching as it
 * goes. Words outside the box are white with an outline; whatever part of a word is inside the box is
 * dark on the box colour. Both versions are always drawn in full contrast: the box has a dark rim, so a
 * word it only half covers is clean on both sides of the edge.
 */
const schema = paramsSchema({
  halo: z.number().min(0.02).max(0.3), // outline of the words outside the box, em
  shadowY: z.number().min(0).max(0.3),
  padX: z.number().min(0.02).max(0.6), // box reaches this far past the word, em
  padY: z.number().min(0.02).max(0.6),
  radius: z.number().min(0).max(0.6), // box corner radius, em
  rim: z.number().min(0.04).max(0.2), // dark rim of the box, em
  glideS: z.number().min(0.04).max(0.6), // the box takes this long to reach the next word
  stretch: z.number().min(0).max(1), // 0 = moves stiffly, 1 = leading edge arrives well before the trailing one
  enterScale: z.number().min(0.4).max(1),
  enterFreq: z.number().min(1).max(10),
  enterDamp: z.number().min(0.2).max(1),
  enterRise: z.number().min(0).max(1), // em
  emphGrow: z.number().min(0).max(0.5), // extra box padding on a shouted word, em
  exitDrop: z.number().min(0).max(1), // em
});
type P = z.infer<typeof schema>;

export default defineTemplate<P>({
  name: "glide",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    const pop = spring(t, fx.enterFreq, fx.enterDamp);
    const gone = E.inCubic(c.out);
    const page = pose(blockCentre(c), {
      scale: lerp(fx.enterScale, 1, pop) * (1 - gone),
      y: (1 - E.outCubic(prog(t, 0, P.timing.enterS))) * fx.enterRise * u + gone * fx.exitDrop * u,
    });

    // the box around word i, in block coordinates (before the page transform)
    const boxOf = (i: number) => {
      const w = c.words[i];
      const grow = w.emph ? fx.emphGrow * u : 0;
      const capH = c.layout.capH * (w.box.size / c.layout.size);
      return {
        l: c.cx + w.box.x - fx.padX * u - grow,
        r: c.cx + w.box.x + w.box.w + fx.padX * u + grow,
        t: c.cy + w.box.y - capH - fx.padY * u - grow,
        b: c.cy + w.box.y + fx.padY * u + grow,
      };
    };
    // which word the box is on, and where it came from
    let k = 0;
    for (const w of c.words) if (t >= w.start) k = w.index;
    const to = boxOf(k);
    const from = k > 0 ? boxOf(k - 1) : { l: (to.l + to.r) / 2, r: (to.l + to.r) / 2, t: to.t, b: to.b };
    const p = prog(t, c.words[k].start, fx.glideS);
    const lead = E.outCubic(Math.min(1, p * (1 + fx.stretch)));
    const trail = E.inOutCubic(p);
    const right = to.l >= from.l;
    const box = {
      l: lerp(from.l, to.l, right ? trail : lead),
      r: lerp(from.r, to.r, right ? lead : trail),
      t: lerp(from.t, to.t, E.inOutCubic(p)),
      b: lerp(from.b, to.b, E.inOutCubic(p)),
    };
    const rim = fx.rim * u;
    const colour = c.words[k].emph ? colors.emph : colors.active;
    const shapes: Shape[] = [
      rect(`p${c.page.id}box`, box.l, box.t, box.r - box.l, box.b - box.t, {
        fill: colour,
        stroke: colors.stroke,
        strokeWidth: rim * 2,
        r: fx.radius * u,
        m: page,
        z: 1,
      }),
    ];
    // the window the dark words show through: the inside of the box, as it is on screen
    const [wx0, wy0] = apply(page, box.l + rim, box.t + rim);
    const [wx1, wy1] = apply(page, box.r - rim, box.b - rim);
    const window = { x: wx0, y: wy0, w: Math.max(0, wx1 - wx0), h: Math.max(0, wy1 - wy0) };

    const units: Unit[] = [];
    for (const w of c.words) {
      const m = chain(page);
      units.push(
        wordUnit(c, w, {
          m,
          z: 0,
          fg: [colors.text],
          layers: inked(colors.text, colors.stroke, fx.halo * u, [dropShadow(colors.shadow, fx.halo * u, 0, fx.shadowY * u)]),
        }),
      );
      const near = c.cx + w.box.x < box.r && c.cx + w.box.x + w.box.w > box.l && Math.abs(w.box.line - c.words[k].box.line) <= 1;
      if (near && window.w > 1 && window.h > 1) {
        units.push(
          wordUnit(c, w, { m, z: 2, key: "in", clip: window, fg: [colors.plateText], layers: [{ kind: "fill", color: colors.plateText }] }),
        );
      }
    }
    return { units, shapes };
  },
  label: (c) => popLabel(c, { halo: c.P.fx.halo, shadowY: c.P.fx.shadowY }),
});
