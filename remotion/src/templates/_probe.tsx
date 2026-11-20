import { z } from "zod";
import { pose } from "../core/affine";
import { paramsSchema } from "../core/schema";
import { inked, wordUnit, type Scene } from "../core/scene";
import { blockCentre, em, defineTemplate, popLabel } from "../core/template";

/** _PROBE (not a caption style; names starting with _ are not offered): a word drifting at a constant
 * sub-pixel speed, turning and growing, to measure how smoothly motion is drawn (tests/test_remotion_frames.py). */
const schema = paramsSchema({
  halo: z.number(),
  vx: z.number(), // px per second
  vy: z.number(),
  rot: z.number(), // degrees
  spin: z.number(), // degrees per second
  growth: z.number(), // scale per second
});
type P = z.infer<typeof schema>;

export default defineTemplate<P>({
  name: "_probe",
  schema,
  build: (c): Scene => {
    const fx = c.P.fx;
    const m = pose(blockCentre(c), { x: fx.vx * c.t, y: fx.vy * c.t, rot: fx.rot + fx.spin * c.t, scale: 1 + fx.growth * c.t });
    return {
      shapes: [],
      units: c.words.map((w) =>
        wordUnit(c, w, { m, fg: [c.colors.text], layers: inked(c.colors.text, c.colors.stroke, fx.halo * em(c)) }),
      ),
    };
  },
  label: (c) => popLabel(c, { halo: c.P.fx.halo, shadowY: 0 }),
});
