import { z } from "zod";
import { chain, pose } from "../core/affine";
import { mix } from "../core/color";
import { back, E, lerp, prog, pulse, shake, spring } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { dropShadow, inked, wordUnit, type Scene } from "../core/scene";
import { blockCentre, em, popLabel, wordCentre, type Template } from "../core/template";

/**
 * PUNCH: the viral standard. Fat geometric caps with a thick outline and a hard shadow; the phrase pops
 * in as one block, the spoken word punches up and takes the highlight colour, shouted words shake.
 */
const schema = paramsSchema({
  halo: z.number().min(0.02).max(0.3), // outline reach, em
  shadowY: z.number().min(0).max(0.3), // hard shadow drop, em
  enterScale: z.number().min(0.4).max(1), // the phrase pops in from this scale
  enterFreq: z.number().min(1).max(12), // pop spring: Hz
  enterDamp: z.number().min(0.2).max(1), // pop spring: damping ratio
  enterRise: z.number().min(0).max(1), // and rises this far, em
  activeScale: z.number().min(1).max(1.5), // the spoken word grows to this
  activeLift: z.number().min(0).max(0.3), // and lifts, em
  activeOvershoot: z.number().min(0).max(4),
  emphScale: z.number().min(1).max(1.6), // a shouted word grows to this while spoken
  emphShakeDeg: z.number().min(0).max(20),
  emphShakeS: z.number().min(0).max(1),
  emphShakeHz: z.number().min(1).max(30),
  exitDrop: z.number().min(0).max(1), // the phrase sinks this far while it shrinks away, em
});
type P = z.infer<typeof schema>;

export const punch: Template<P> = {
  name: "punch",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    const pop = spring(t, fx.enterFreq, fx.enterDamp);
    const rise = 1 - E.outCubic(prog(t, 0, P.timing.enterS));
    const gone = E.inCubic(c.out);
    const page = pose(blockCentre(c), {
      scale: lerp(fx.enterScale, 1, pop) * (1 - gone),
      y: rise * fx.enterRise * u + gone * fx.exitDrop * u,
    });
    const units = c.words.map((w) => {
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));
      const tint = Math.min(1, Math.max(0, w.a));
      const fill = mix(w.emph ? colors.emph : colors.text, w.emph ? colors.emph : colors.active, tint);
      const grow = (w.emph ? fx.emphScale : fx.activeScale) - 1;
      const own = pose(wordCentre(w), {
        scale: 1 + grow * a,
        y: -fx.activeLift * u * a,
        rot: w.emph ? fx.emphShakeDeg * shake(w.since, fx.emphShakeS, fx.emphShakeHz) : 0,
      });
      return wordUnit(c, w, {
        m: chain(own, page),
        z: w.phase === "active" ? 1 : 0,
        fg: [fill],
        layers: inked(fill, colors.stroke, fx.halo * u, [dropShadow(colors.shadow, fx.halo * u, 0, fx.shadowY * u)]),
      });
    });
    return { units, shapes: [] };
  },
  label: (c) => popLabel(c, { halo: c.P.fx.halo, shadowY: c.P.fx.shadowY }),
};
