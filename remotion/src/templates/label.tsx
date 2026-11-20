import { z } from "zod";
import { apply, chain, IDENTITY, pose, translate, type Affine } from "../core/affine";
import { mix } from "../core/color";
import { back, clamp, E, prog, pulse } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { rect, wordUnit, type Ctx, type LabelCtx, type Layer, type Rect, type Scene, type Shape, type Unit } from "../core/scene";
import { blockCentre, defineTemplate, em, wordCentre } from "../core/template";

/**
 * LABEL: the social "label" look. Every line sits on its own tight light plate, dark letters, no
 * outline. Plates wipe open from the left and bring their text with them; the part of the plate under the
 * spoken word opens in the highlight colour and lifts off the plate with its word; shouted words have
 * their own tilted plate in the emphasis colour. Plates close from the right to leave.
 *
 * No outline that can be seen: every letter does carry one, in the colour of the plate it lies on, so
 * that the frame checks have a ring to measure the letters against (`halo`). Where a word lies partly on
 * the white plate and partly on its coloured part, it is drawn twice, once for each, as glide does.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.12), // outline in the plate's own colour around every letter (not visible), em: less than every pad below
  padX: z.number().min(0.08).max(0.6), // plate reaches this far past the line, em
  padY: z.number().min(0.07).max(0.4), // and this far above and below it, em
  radius: z.number().min(0).max(0.5), // plate corner radius, em
  lineLagS: z.number().min(0).max(0.5), // each plate starts opening this long after the one above
  openOvershoot: z.number().min(0).max(2), // how far the opening edge shoots past its place (0 = not at all)
  slide: z.number().min(0).max(1.5), // the text slides in this far from the left while its plate opens, em
  closeLag: z.number().min(0).max(0.6), // exit: the last plate starts closing this share of the exit after the first
  segPadX: z.number().min(0.02).max(0.3), // the coloured part under a word reaches this far past the word, em
  segInset: z.number().min(0).max(0.2), // and stays this far inside the plate's top and bottom, em
  segRadius: z.number().min(0).max(0.5), // its corner radius, em
  segOvershoot: z.number().min(0).max(4), // it opens from its middle and overshoots this much
  nudge: z.number().min(0).max(0.3), // the spoken word lifts this far with its part of the plate, em
  depth: z.number().min(0).max(1), // shade of the edge a lifted part leaves under it: 0 = its own colour, 1 = the text colour
  emphTilt: z.number().min(0).max(10), // a shouted word and its plate are turned this much, alternately left and right, degrees
  emphPop: z.number().min(1).max(1.3), // a shouted word grows to this while spoken
  labelPop: z.number().min(1).max(1.3), // a judge label's plate swells to this as it lands
});
type P = z.infer<typeof schema>;
type C = Ctx<P> | LabelCtx<P>;

type Plate = { x0: number; x1: number; y0: number; y1: number; right: number; moving: boolean };

/**
 * The plate of every line at this instant: its full box and where its right edge is now (it opens from
 * the left edge, overshoots a little, and closes back to it on the way out).
 */
function plates(c: C, enterS: number): Plate[] {
  const fx = c.P.fx;
  const u = em(c);
  const n = c.layout.lines.length;
  return c.layout.lines.map((L, i) => {
    const x0 = c.cx + L.x - fx.padX * u;
    const x1 = c.cx + L.x + L.w + fx.padX * u;
    const t0 = i * fx.lineLagS;
    const open = back(fx.openOvershoot)(prog(c.t, t0, enterS));
    const wait = n > 1 ? (fx.closeLag * i) / (n - 1) : 0;
    const shut = c.exit > 0 ? E.inCubic(clamp((c.out - wait) / (1 - (n > 1 ? fx.closeLag : 0)))) : 0;
    return {
      x0,
      x1,
      y0: c.cy + L.top - fx.padY * u,
      y1: c.cy + L.bottom + fx.padY * u,
      right: x0 + (x1 - x0) * open * (1 - shut),
      moving: c.t < t0 + enterS || c.out > 0,
    };
  });
}

const plateShape = (c: C, id: string, p: Plate, fill: string, m: Affine, z: number): Shape | null =>
  p.right - p.x0 < 0.5 ? null : rect(id, p.x0, p.y0, p.right - p.x0, p.y1 - p.y0, { fill, r: c.P.fx.radius * em(c), m, z });

/**
 * The window a line's text shows through while its plate moves: from the plate's left edge to its right
 * edge as they are on screen (the edges of a turned plate lean, so the window stops at their near ends),
 * a little inside both, so a letter is never drawn without plate around it.
 */
const both = (a: Rect, b: Rect): Rect => {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  return { x, y, w: Math.max(0, Math.min(a.x + a.w, b.x + b.w) - x), h: Math.max(0, Math.min(a.y + a.h, b.y + b.h) - y) };
};

/** Dark letters with the plate's colour around them. */
const letters = (c: C, under: string): Layer[] => [
  { kind: "stroke", color: under, width: c.P.fx.halo * em(c) },
  { kind: "fill", color: c.colors.plateText },
];

function textWindow(c: C, p: Plate, m: Affine): Rect {
  const inset = c.P.fx.radius * em(c) * 0.5 + 3;
  const left = Math.max(apply(m, p.x0, p.y0)[0], apply(m, p.x0, p.y1)[0]) + inset;
  const right = Math.min(apply(m, p.right, p.y0)[0], apply(m, p.right, p.y1)[0]) - inset;
  return { x: left, y: c.region.y0, w: Math.max(0, right - left), h: c.region.y1 - c.region.y0 };
}

export default defineTemplate<P>({
  name: "label",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    const ps = plates(c, P.timing.enterS);
    const flat: Affine = IDENTITY;
    const shapes: Shape[] = [];
    const units: Unit[] = [];
    ps.forEach((p, i) => {
      const s = plateShape(c, `p${c.page.id}pl${i}`, p, colors.plate, flat, -6);
      if (s) shapes.push(s);
    });
    for (const w of c.words) {
      const p = ps[w.box.line];
      const window = p.moving ? textWindow(c, p, flat) : undefined;
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.segOvershoot));
      const lift = -fx.nudge * u * a;
      const tilt = w.emph ? (w.index % 2 === 0 ? -1 : 1) * fx.emphTilt : 0;
      const own = pose(wordCentre(w), { rot: tilt, scale: w.emph ? 1 + (fx.emphPop - 1) * a : 1, y: lift });
      const t0 = w.box.line * fx.lineLagS;
      const slideX = -(1 - E.outExpo(prog(t, t0, P.timing.enterS))) * fx.slide * u;
      const slide = translate(slideX, 0);
      const fg = [colors.plateText];

      // the word's own part of the plate: always there for a shouted word, opening from its middle for the spoken one
      const width = w.emph ? 1 : a;
      const colour = w.emph ? colors.emph : colors.active;
      const half = (w.box.w / 2 + fx.segPadX * u) * width; // half its width right now
      const sy0 = p.y0 + fx.segInset * u;
      const sh0 = p.y1 - p.y0 - 2 * fx.segInset * u;
      if (width > 0.02) {
        // where it sits on the plate, and where it is now; the edge it leaves behind when it lifts is a darker
        // shade, drawn a pixel inside the part that covers it, so that no dark fringe shows around it
        const rest = chain(pose(wordCentre(w), { rot: tilt }), slide);
        const id = `p${c.page.id}sg${w.index}`;
        const r = Math.min(fx.segRadius * u, half);
        if (half > 2) {
          shapes.push(
            rect(`${id}d`, w.cx - half + 1, sy0 + 1, 2 * half - 2, sh0 - 2, {
              role: "decor",
              fill: mix(colour, colors.plateText, fx.depth),
              r: Math.max(0, r - 1),
              m: rest,
              z: -4,
              clip: window,
            }),
          );
        }
        shapes.push(rect(id, w.cx - half, sy0, 2 * half, sh0, { fill: colour, r, m: chain(own, slide), z: -3, clip: window }));
      }
      const m = chain(own, slide);
      if (w.emph) {
        // on its own plate, which is always whole
        units.push(wordUnit(c, w, { m, z: 1, fg, clip: window, layers: letters(c, colors.emph) }));
        continue;
      }
      // on the white plate, under any coloured part...
      units.push(wordUnit(c, w, { m, z: -5, fg, clip: window, layers: letters(c, colors.plate) }));
      if (width > 0.02) {
        // ...and again on its coloured part, through a window the size that part has right now, widened to
        // whole pixels: a window edge that covered part of a pixel would leave a tinted seam down a stem
        const [wl, wr] = [Math.floor(w.cx + slideX - half), Math.ceil(w.cx + slideX + half)];
        const [wt, wb] = [Math.floor(sy0 + lift), Math.ceil(sy0 + lift + sh0)];
        const inside: Rect = { x: wl, y: wt, w: wr - wl, h: wb - wt };
        units.push(wordUnit(c, w, { m, z: 1, key: "in", fg, clip: window ? both(inside, window) : inside, layers: letters(c, colour) }));
      }
    }
    return { units, shapes };
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    const u = em(c);
    const ps = plates(c, c.P.label.enterS);
    // lands with a small swell, a little turned; the plate is the label's own colour
    const swell = Math.sin(Math.PI * prog(c.t, c.P.label.enterS * 0.6, c.P.label.enterS)) * (fx.labelPop - 1);
    const turn = pose(blockCentre(c), { rot: c.P.label.rotate, scale: 1 + swell });
    const shapes: Shape[] = [];
    ps.forEach((p, i) => {
      const s = plateShape(c, `l${c.label.id}pl${i}`, p, c.colors.text, turn, 4);
      if (s) shapes.push(s);
    });
    const units = c.words.map((w) => {
      const p = ps[w.box.line];
      const slide = translate(-(1 - E.outExpo(prog(c.t, w.box.line * fx.lineLagS, c.P.label.enterS))) * fx.slide * u, 0);
      return wordUnit(c, w, {
        m: chain(slide, turn),
        z: 5,
        active: true,
        fg: [c.colors.plateText],
        clip: p.moving ? textWindow(c, p, turn) : undefined,
        layers: letters(c, c.colors.text),
      });
    });
    return { units, shapes };
  },
});
