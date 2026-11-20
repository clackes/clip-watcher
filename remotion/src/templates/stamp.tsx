import { z } from "zod";
import { chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, prog, shake } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { rect, wordUnit, type Ctx, type LabelCtx, type Layer, type Rect, type Scene, type Shape, type WordState } from "../core/scene";
import { defineTemplate, em, wordCentre } from "../core/template";

/**
 * STAMP: a rubber stamp. Heavy condensed italics in a frame, the whole thing turned a few degrees, on its
 * own dark plate. It comes down from too big with a hard stop, turns a little as it settles and lands a
 * second, lighter time; the ink flashes on every landing and a print of the frame spreads and fades. The
 * spoken word takes the highlight colour; for a shouted word the whole stamp is lifted and brought down
 * twice. To leave, it lifts and slides off.
 */
const schema = paramsSchema({
  rest: z.number().min(-15).max(15), // resting rotation of the stamp, degrees (`tilt` must cover this plus settleDeg)
  halo: z.number().min(0.07).max(0.12), // outline in the plate's own colour around every letter (not visible), em: what the frame checks measure the letters against
  padX: z.number().min(0.1).max(0.8), // from the letters to the middle of the frame line, em
  padY: z.number().min(0.1).max(0.8),
  frameW: z.number().min(0.02).max(0.2), // frame line, em
  frameShade: z.number().min(0.1).max(0.5), // the frame is the letters' ink mixed this far towards the plate
  rim: z.number().min(0.07).max(0.2), // dark rim outside the frame, em
  headroom: z.number().min(1).max(1.5), // in a box too low for the stamp it shrinks until it can still be lifted by this factor
  innerGap: z.number().min(0).max(0.3), // a thin second line inside the frame: its distance from the frame, em
  innerW: z.number().min(0).max(0.1), // and its width, em (0 = no second line)
  innerMinPx: z.number().min(0.5).max(4), // the second line is drawn only where it is at least this many pixels wide
  radius: z.number().min(0).max(0.5), // frame corner radius, em
  fromScale: z.number().min(1).max(2), // it comes down from this size (from less where the box has no room)
  bounce: z.number().min(0).max(0.3), // the second, lighter landing: it lifts by this much first
  liftS: z.number().min(0.05).max(0.5), // a lift takes this long up
  slamS: z.number().min(0.05).max(0.5), // and this long down
  settleDeg: z.number().min(0).max(6), // it turns up to this much as it settles after a landing, degrees
  settleS: z.number().min(0.05).max(1), // for this long
  settleHz: z.number().min(1).max(6), // at this rate
  flash: z.number().min(0).max(1), // on a landing the ink flashes this far towards the accent colour
  flashInS: z.number().min(0.07).max(0.3), // rising over this time
  flashOutS: z.number().min(0.07).max(1), // and fading over this
  ghost: z.number().min(0).max(0.5), // on a landing a print of the frame spreads by this much
  ghostS: z.number().min(0.1).max(1), // over this time, fading
  ghostAlpha: z.number().min(0).max(1), // from this opacity
  press: z.number().min(0).max(0.09), // the spoken word is pressed: it swells by this much
  pressS: z.number().min(0.1).max(0.6), // for this long
  emphLift: z.number().min(0).max(0.4), // a shouted word: the whole stamp lifts by this much and lands
  emphSecond: z.number().min(0).max(1), // and again, by this share of the first
  exitLift: z.number().min(0).max(0.3), // exit: it lifts by this much
  exitLiftShare: z.number().min(0.1).max(0.9), // over this share of the exit, then it slides off
});
type P = z.infer<typeof schema>;
type C = Ctx<P> | LabelCtx<P>;

type Hit = { at: number; h: number; power: number }; // a lift starting at `at`, `h` high, and how hard it lands

/**
 * The stamp at this instant: frame, plate and letters. `ink` is its colour, `words` the letters with the
 * colour each would have without the flash, `hits` the lifts after the first landing.
 */
function stamp(
  c: C,
  o: { id: string; enterS: number; z: number; ink: string; hits: Hit[]; fill: (w: WordState, ink: string) => string; press: (w: WordState) => number },
): Scene {
  const fx = c.P.fx;
  const u = em(c);
  const { t } = c;
  const x0 = c.cx + c.layout.left - fx.padX * u;
  const x1 = c.cx + c.layout.right + fx.padX * u;
  const y0 = c.cy + c.layout.top - fx.padY * u;
  const y1 = c.cy + c.layout.bottom + fx.padY * u;
  const fw = fx.frameW * u;

  // half the size of the turned stamp, frame and rim included, at its largest turn
  const hw = (x1 - x0) / 2 + fw / 2 + fx.rim * u;
  const hh = (y1 - y0) / 2 + fw / 2 + fx.rim * u;
  const worst = ((Math.abs(fx.rest) + fx.settleDeg) * Math.PI) / 180;
  const [bx0, bx1, by0, by1] = [c.region.x0 + 2, c.region.x1 - 2, c.region.y0 + 2, c.region.y1 - 2];
  const wide = hw * Math.cos(worst) + hh * Math.sin(worst);
  const high = hw * Math.sin(worst) + hh * Math.cos(worst);
  // the frame is the template's own, the text was fitted without it: where the box is too low (or narrow) for
  // the whole stamp, the stamp is made smaller, until it can still be lifted by `headroom`
  const fit = Math.min(1, (bx1 - bx0) / (2 * wide * fx.headroom), (by1 - by0) / (2 * high * fx.headroom));
  const ex = wide * fit;
  const ey = high * fit;
  // where it rests: on the text's own place, moved only as far as the frame needs to stay in the caption box
  const inside = (v: number, e: number, lo: number, hi: number): number => (lo + e <= hi - e ? Math.min(hi - e, Math.max(lo + e, v)) : (lo + hi) / 2);
  const mid: [number, number] = [inside((x0 + x1) / 2, ex, bx0, bx1), inside((y0 + y1) / 2, ey, by0, by1)];
  const place = translate(mid[0] - (x0 + x1) / 2, mid[1] - (y0 + y1) / 2);
  // how much bigger than at rest it may get before it leaves the box, and the point it grows from so that it
  // stays inside at that size: its own middle where there is room, nearer the tight side where there is not
  const room = Math.max(1, Math.min((bx1 - bx0) / (2 * ex), (by1 - by0) / (2 * ey)));
  const most = (h: number): number => Math.min(1 + h, room) - 1;
  const top = 1 + Math.max(most(fx.fromScale - 1), most(fx.emphLift), most(fx.exitLift), most(fx.ghost), most(fx.bounce));
  const pin = (v: number, e: number, lo: number, hi: number): number =>
    top > 1.0001 ? Math.min((top * (v - e) - lo) / (top - 1), Math.max((top * (v + e) - hi) / (top - 1), v)) : v;
  const pivot: [number, number] = [pin(mid[0], ex, bx0, bx1), pin(mid[1], ey, by0, by1)];

  // height above the paper (as extra scale): the first fall, then every lift
  let height = t < o.enterS ? most(fx.fromScale - 1) * (1 - E.inCubic(t / o.enterS)) : 0;
  const landings: { at: number; power: number }[] = [{ at: o.enterS, power: 1 }];
  for (const k of o.hits) {
    const x = t - k.at;
    landings.push({ at: k.at + fx.liftS + fx.slamS, power: k.power });
    if (x <= 0 || x >= fx.liftS + fx.slamS) continue;
    const v = x < fx.liftS ? E.outQuad(x / fx.liftS) : 1 - E.inCubic((x - fx.liftS) / fx.slamS);
    height = Math.max(height, most(k.h) * v);
  }
  let rot = fx.rest;
  let flash = 0;
  for (const l of landings) {
    const x = t - l.at;
    if (x <= 0) continue;
    rot += l.power * fx.settleDeg * shake(x, fx.settleS, fx.settleHz);
    const f = x < fx.flashInS ? E.outQuad(x / fx.flashInS) : (1 - clamp((x - fx.flashInS) / fx.flashOutS)) ** 2;
    flash = Math.max(flash, l.power * f);
  }
  const ink = mix(o.ink, c.colors.accent, fx.flash * flash);
  // the frame is a shade deeper than the letters: the same ink pressed harder at the edge of the rubber, and
  // far enough from every letter colour that the frame checks never take a piece of frame for a letter
  const edge = mix(ink, c.colors.plate, fx.frameShade);

  // exit: lifted, then off along its own slope through a window the size of its footprint
  const lifted = most(fx.exitLift) * E.outQuad(prog(c.out, 0, fx.exitLiftShare));
  const gone = E.inCubic(prog(c.out, fx.exitLiftShare, 1 - fx.exitLiftShare));
  const reach = 2 * ex * (1 + most(fx.exitLift)) + 4;
  const dir = (fx.rest * Math.PI) / 180;
  const off = translate(Math.cos(dir) * reach * gone, Math.sin(dir) * reach * gone);
  const k = 1 + most(fx.exitLift);
  const cx0 = Math.max(c.region.x0, pivot[0] + k * (mid[0] - ex - pivot[0]) - 2);
  const cy0 = Math.max(c.region.y0, pivot[1] + k * (mid[1] - ey - pivot[1]) - 2);
  const clip: Rect | undefined =
    c.out > 0
      ? {
          x: cx0,
          y: cy0,
          w: Math.min(c.region.x1, pivot[0] + k * (mid[0] + ex - pivot[0]) + 2) - cx0,
          h: Math.min(c.region.y1, pivot[1] + k * (mid[1] + ey - pivot[1]) + 2) - cy0,
        }
      : undefined;
  // to its resting place, turned about its own middle, grown from the pivot, and off on the way out
  const pose2 = (scale: number) => chain(place, pose(mid, { rot, scale: fit }), pose(pivot, { scale }), off);
  const body = pose2(1 + height + lifted);

  const shapes: Shape[] = [];
  const box = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  const r = fx.radius * u;
  // a print of the frame spreads from every landing and fades
  landings.forEach((l, i) => {
    const x = (t - l.at) / fx.ghostS;
    if (x <= 0 || x >= 1 || fx.ghost <= 0) return;
    shapes.push(
      rect(`${o.id}g${i}`, box.x, box.y, box.w, box.h, {
        role: "decor",
        fill: null,
        stroke: mix(o.ink, c.colors.plate, fx.frameShade),
        strokeWidth: fw * (1 - x),
        alpha: fx.ghostAlpha * l.power * (1 - x) * (1 - x),
        r,
        m: pose2(1 + most(fx.ghost) * E.outCubic(x)),
        z: o.z - 6,
        clip,
      }),
    );
  });
  shapes.push(
    // the rim is a larger plate under the frame, not a wide line: its box then holds all of it
    rect(`${o.id}rim`, box.x - fw / 2 - fx.rim * u, box.y - fw / 2 - fx.rim * u, box.w + fw + 2 * fx.rim * u, box.h + fw + 2 * fx.rim * u, {
      fill: c.colors.stroke,
      r: r + fw / 2 + fx.rim * u,
      m: body,
      z: o.z - 5,
      clip,
    }),
    rect(`${o.id}fr`, box.x, box.y, box.w, box.h, { fill: c.colors.plate, stroke: edge, strokeWidth: fw, r, m: body, z: o.z - 4, clip }),
  );
  // the second line is a detail for sizes that have the pixels for it: thinner than `innerMinPx`, or closer
  // than three of them to the frame, it would only be a blur along the frame, so it is left out
  if (fx.innerW > 0 && fx.innerW * u * fit >= fx.innerMinPx && fx.innerGap * u * fit >= 3 * fx.innerMinPx) {
    const d = fw / 2 + fx.innerGap * u + (fx.innerW * u) / 2;
    shapes.push(
      rect(`${o.id}in`, box.x + d, box.y + d, box.w - 2 * d, box.h - 2 * d, {
        role: "decor",
        fill: null,
        stroke: edge,
        strokeWidth: fx.innerW * u,
        r: Math.max(0, r - d),
        m: body,
        z: o.z - 3,
        clip,
      }),
    );
  }
  const units = c.words.map((w) => {
    const fill = o.fill(w, ink);
    const own = pose(wordCentre(w), { scale: 1 + o.press(w) });
    const layers: Layer[] = [
      { kind: "stroke", color: c.colors.plate, width: fx.halo * u },
      { kind: "fill", color: fill },
    ];
    return wordUnit(c, w, { m: chain(own, body), z: o.z, fg: [fill], clip, layers });
  });
  return { units, shapes };
}

/** 0 → 1 → 0 over `dur` seconds from 0. */
const hump = (x: number, dur: number): number => (x <= 0 || x >= dur ? 0 : Math.sin((Math.PI * x) / dur));

export default defineTemplate<P>({
  name: "stamp",
  schema,
  build: (c): Scene => {
    const { P, colors } = c;
    const fx = P.fx;
    const enterS = P.timing.enterS;
    const beat = fx.liftS + fx.slamS;
    // the second, lighter landing; then two more for every shouted word, as soon as the stamp is down
    const hits: Hit[] = [{ at: enterS, h: fx.bounce, power: 0.4 }];
    let free = enterS + beat;
    for (const w of c.words) {
      if (!w.emph) continue;
      const at = Math.max(w.start, free);
      hits.push({ at, h: fx.emphLift, power: 0.8 }, { at: at + beat, h: fx.emphLift * fx.emphSecond, power: 0.5 });
      free = at + 2 * beat;
    }
    return stamp(c, {
      id: `p${c.page.id}`,
      enterS,
      z: 0,
      ink: colors.text,
      hits,
      fill: (w, ink) => mix(ink, w.emph ? colors.emph : colors.active, clamp(w.a)),
      // an emoji is a picture drawn on whole pixels: it steps when it changes size slowly, so it is held pressed instead
      press: (w) => fx.press * (isEmoji(w.text, w.box.font) ? clamp(w.a) : hump(w.since, fx.pressS)),
    });
  },
  label: (c): Scene =>
    stamp(c, {
      id: `l${c.label.id}`,
      enterS: c.P.label.enterS,
      z: 6,
      ink: c.colors.text,
      hits: [{ at: c.P.label.enterS, h: c.P.fx.bounce, power: 0.4 }],
      fill: (_w, ink) => ink,
      press: () => 0,
    }),
});
