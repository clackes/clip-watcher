import { z } from "zod";
import { apply, chain, pose, translate, type Affine } from "../core/affine";
import { back, clamp, E, hash01, lerp, prog, pulse, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { rect, wordUnit, type Layer, type Rect, type Scene, type Shape, type Unit } from "../core/scene";
import { defineTemplate, em } from "../core/template";

/**
 * CHIPS: every word is a chip, a rounded plate in its own candy colour with dark letters, a dark rim and a
 * hard shadow that gives it thickness. Chips fly in from the two sides and settle on a spring. The spoken
 * chip turns over like a card (it squashes to a sliver and opens again on its other face): dark plate,
 * letters and an inset line in the chip's colour; it flips back when the next word is spoken. Shouted chips
 * are bigger and sit tilted. At the end the chips scatter outwards and drop out of the box.
 *
 * Contrast: each face is its own unit with fixed colours (dark on the colour, the colour on dark); the two
 * are never mixed, so there is no grey frame in the flip. Letters are `plateText`, rims and shadows `stroke`
 * / `shadow`, which are different darks, so a rim never counts as part of a letter.
 */
const schema = paramsSchema({
  padX: z.number().min(0.05).max(0.8), // the plate reaches this far past the word, em
  padY: z.number().min(0.05).max(0.6),
  radius: z.number().min(0).max(0.8), // corner radius, em
  rim: z.number().min(0.02).max(0.2), // dark line around a chip, em
  halo: z.number().min(0.02).max(0.2), // clear plate around the letters that they are measured against, em
  shadowY: z.number().min(0).max(0.3), // the hard shadow under a chip (its thickness), em
  inset: z.number().min(0.03).max(0.3), // flipped face: a line in the chip's colour this far inside the edge, em
  insetLine: z.number().min(0.01).max(0.1), // and this thick, em
  flyX: z.number().min(0).max(4), // a chip flies in from this far to the side, em
  flyY: z.number().min(0).max(2), // or, in the middle of a line, from this far above, em
  enterScale: z.number().min(0.5).max(1), // and grows from this scale
  enterTurnDeg: z.number().min(0).max(40), // turned this far, straightening as it lands
  enterFreq: z.number().min(1).max(6), // the spring it settles on: Hz
  enterDamp: z.number().min(0.3).max(1),
  flipS: z.number().min(0.1).max(0.6), // the turn onto the other face takes this long
  flipHoldS: z.number().min(0.1).max(1), // shortest time a chip stays turned over
  flipBackLagS: z.number().min(0).max(0.3), // it turns back this long after the next word started, so the two turns follow each other
  flipThin: z.number().min(0.02).max(0.3), // the narrowest it gets mid-turn, share of its width
  activeScale: z.number().min(1).max(1.3), // the spoken chip bumps up to this
  bumpReach: z.number().min(0).max(0.5), // but its ends never move out by more than this, em (a long chip bumps less)
  activeLift: z.number().min(0).max(0.3), // and lifts off its shadow, em
  activeOvershoot: z.number().min(0).max(4),
  emphTiltDeg: z.number().min(0).max(12), // shouted chips are tilted by up to this
  emphRise: z.number().min(0).max(0.3), // but their ends never rise more than this, em (long chips tilt less)
  swing: z.number().min(0).max(2), // nor do a chip's ends swing further than this off the line as it flies in or drops, em
  scatterX: z.number().min(0).max(3), // on the way out a chip drifts this far outwards, em
  scatterTurnDeg: z.number().min(0).max(60), // and turns this far as it drops
  scatterSpread: z.number().min(0).max(0.6), // share of the exit over which the chips of a line let go one after another
  scatterLineLag: z.number().min(0).max(0.3), // an upper line lets go this share of the exit later than the line under it
  labelGap: z.number().min(0).max(1.5), // em between the words of a label (they share one chip)
  labelScale: z.number().min(0.5).max(1), // the label chip opens from this scale
  labelFreq: z.number().min(1).max(6), // on this spring: Hz
  labelDamp: z.number().min(0.3).max(1),
});
type P = z.infer<typeof schema>;

type Box = { l: number; r: number; t: number; b: number };

// the picture of a system emoji in its em box: how tall it is and where its middle sits above the baseline, em
const EMOJI_H = 1.03;
const EMOJI_MID = 0.35;
// a face is turned to only when it will then be seen for at least this long, seconds (two frames at
// 30 fps): shown for less, before the page goes, it is a flash and not a turn
const SEEN = 0.07;

/** The chip: shadow, plate with its rim, and on the flipped face the inset line. */
function chip(id: string, b: Box, fx: P["fx"], u: number, o: { plate: string; rim: string; shadow: string; line: string | null; m: Affine; under: Affine; clip?: Rect }): Shape[] {
  const w = b.r - b.l;
  const h = b.b - b.t;
  const out: Shape[] = [
    rect(`${id}h`, b.l, b.t, w, h, { role: "decor", fill: o.shadow, stroke: o.shadow, strokeWidth: fx.rim * u, r: fx.radius * u, m: o.under, z: -1, clip: o.clip }),
    rect(`${id}s`, b.l, b.t, w, h, { fill: o.plate, stroke: o.rim, strokeWidth: fx.rim * u, r: fx.radius * u, m: o.m, z: 0, clip: o.clip }),
  ];
  if (o.line) {
    const d = fx.inset * u;
    out.push(
      rect(`${id}i`, b.l + d, b.t + d, w - 2 * d, h - 2 * d, {
        role: "decor",
        fill: null,
        stroke: o.line,
        strokeWidth: fx.insetLine * u,
        r: Math.max(0, fx.radius * u - d),
        m: o.m,
        z: 0,
        clip: o.clip,
      }),
    );
  }
  return out;
}

const face = (plate: string, ink: string, halo: number): Layer[] => [
  { kind: "stroke", color: plate, width: halo },
  { kind: "fill", color: ink },
];

/** Largest sideways (x) and upward (y) reach of a w x h box scaled by s and turned by rot degrees. */
function reach(w: number, h: number, s: number, rot: number): [number, number] {
  const cos = Math.abs(Math.cos((rot * Math.PI) / 180));
  const sin = Math.abs(Math.sin((rot * Math.PI) / 180));
  return [(s * (w * cos + h * sin)) / 2, (s * (w * sin + h * cos)) / 2];
}

export default defineTemplate<P>({
  name: "chips",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const capEm = c.layout.capH / c.layout.size;
    const reg = c.region;
    const window: Rect = { x: reg.x0, y: reg.y0, w: reg.x1 - reg.x0, h: reg.y1 - reg.y0 };
    const clip = c.out > 0 ? window : undefined;
    const lines = c.layout.lines;
    const lastLine = lines.length - 1;
    const lagMax = (lastLine > 0 ? fx.scatterLineLag : 0) + fx.scatterSpread;
    const units: Unit[] = [];
    const shapes: Shape[] = [];
    for (const w of c.words) {
      const s = t - Math.min(w.index * P.timing.staggerS, Math.max(0, w.start)); // up by the time it is spoken
      if (s < 0) continue;
      const u = w.box.size; // one em of this word: a shouted chip is bigger all over
      const colour = w.emph ? colors.emph : colors.cycle[(w.index + c.index) % colors.cycle.length];
      const base = c.cy + w.box.y;
      const b: Box = {
        l: c.cx + w.box.x - fx.padX * u,
        r: c.cx + w.box.x + w.box.w + fx.padX * u,
        t: base - capEm * u - fx.padY * u,
        b: base + fx.padY * u,
      };
      const W = b.r - b.l;
      const H = b.b - b.t;
      const pivot: [number, number] = [(b.l + b.r) / 2, (b.t + b.b) / 2];
      const edge = (fx.rim * u) / 2 + 1;
      const odd = (w.index + c.index) % 2 === 1;

      // where it comes from: the left chip of a line from the left, the right one from the right, one in
      // the middle from above (it would have to cross its neighbours), a chip alone on its line alternates
      const row = lines[w.box.line].words;
      const at = row.indexOf(w.index);
      const side = row.length === 1 ? ((w.box.line + c.index) % 2 === 0 ? -1 : 1) : at === 0 ? -1 : at === row.length - 1 ? 1 : 0;
      const away = 1 - spring(s, fx.enterFreq, fx.enterDamp);
      const turned = (deg: number, rise: number) => Math.min(deg, (Math.atan2(rise * u, W / 2) * 180) / Math.PI);
      const tilt = w.emph ? (odd ? 1 : -1) * turned(fx.emphTiltDeg, fx.emphRise) : 0;

      // the flip: over when the word starts, back when the next one does (but never back at once). A
      // turn is made only for a face that will then be seen: the new face shows from the middle of the
      // turn, and the page must keep it for the rest of the turn (and never under SEEN) before it goes,
      // whether it leaves or the next page replaces it with a cut. So the last chip of fast talk is not
      // turned over for the one frame before the cut, and a chip is not turned back to a front nobody
      // gets to read: each stays on the face it has. Which face shows is settled when the exit begins: a
      // chip on its way out is never swapped for its other face for a frame or two.
      const leaves = c.exit > 0 ? c.exitT : c.dur;
      const seen = fx.flipS / 2 + Math.max(fx.flipS / 2, SEEN); // from the start of a turn until its face has been seen
      const turns = w.start + seen <= leaves;
      const back0 = Math.max(w.activeEnd + fx.flipBackLagS, w.start + fx.flipS + fx.flipHoldS);
      const stays = back0 + seen > leaves;
      const turnAt = (at: number) => (turns ? prog(at, w.start, fx.flipS) - (stays ? 0 : prog(at, back0, fx.flipS)) : 0);
      const turn = turnAt(t);
      const flipped = turnAt(c.exit > 0 ? Math.min(t, c.exitT) : t) > 0.5;
      const sx = Math.max(fx.flipThin, Math.abs(Math.cos(Math.PI * turn)));
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));

      // the bump: less on a long chip, and never wider than the box (a long word already fills it)
      const bump = Math.min(fx.activeScale - 1, (2 * fx.bumpReach * u) / W);
      const widest = (reg.x1 - reg.x0 - 2 * edge) / (2 * reach(W, H, 1, tilt)[0]);
      const scale = Math.min(widest, lerp(1, fx.enterScale, clamp(away)) * (1 + bump * a));
      let rot = tilt + (side === 0 ? (odd ? 1 : -1) : side) * turned(fx.enterTurnDeg, fx.swing) * away;
      const lift = fx.activeLift * u * a;
      let x = side * fx.flyX * u * away;
      let y = (side === 0 ? -fx.flyY * u * away : 0) - lift;

      // the exit: lets go, drifts outwards, turns and drops out through the bottom of the box
      let gone = 0;
      if (c.out > 0) {
        const lag = (lastLine > 0 ? (fx.scatterLineLag * (lastLine - w.box.line)) / lastLine : 0) + fx.scatterSpread * hash01(`${c.page.id}:${w.index}`);
        gone = clamp((c.out - lag) / Math.max(0.05, 1 - lagMax));
        const dir = Math.abs(pivot[0] - c.cx) < u ? (odd ? 1 : -1) : Math.sign(pivot[0] - c.cx);
        const spin = turned(fx.scatterTurnDeg, fx.swing);
        const most = reach(W, H, scale, spin)[1];
        x += dir * fx.scatterX * u * E.outCubic(gone);
        y += (reg.y1 - pivot[1] + most + edge) * E.inQuad(gone);
        rot += dir * spin * gone;
      }
      // never past the sides of the box however far it flies, nor (with its shadow) past the top or the bottom
      const [rx, ry] = reach(W, H, scale, rot);
      x = clamp(x, Math.min(0, reg.x0 + rx + edge - pivot[0]), Math.max(0, reg.x1 - rx - edge - pivot[0]));
      if (c.out <= 0) {
        const lo = reg.y0 + ry + edge - pivot[1];
        const hi = reg.y1 - ry - edge - fx.shadowY * u - lift - pivot[1];
        y = lo > hi ? (lo + hi) / 2 : clamp(y, lo, hi);
      }

      const m = pose(pivot, { scale, sx, rot, x, y });
      if (clip) {
        // the highest point of the plate (the top of one of its round corners) has left the box: it is gone
        const r0 = Math.min(fx.radius * u, W / 2, H / 2);
        const ys = [apply(m, b.l + r0, b.t + r0), apply(m, b.r - r0, b.t + r0), apply(m, b.l + r0, b.b - r0), apply(m, b.r - r0, b.b - r0)];
        if (Math.min(...ys.map((p) => p[1])) - r0 * scale * Math.min(1, sx) + 1 >= reg.y1) continue;
      }
      const under = pose(pivot, { scale, sx, rot, x, y: y + fx.shadowY * u + lift });
      shapes.push(
        ...chip(`p${c.page.id}w${w.index}`, b, fx, u, {
          plate: flipped ? colors.plate : colour,
          rim: colors.stroke,
          shadow: colors.shadow,
          line: flipped ? colour : null,
          m,
          under,
          clip,
        }),
      );
      if (isEmoji(w.text, w.box.font)) {
        // an emoji is a picture as tall as the chip: drawn smaller, in the middle of the plate, no outline
        const mid: [number, number] = [c.cx + w.box.x + w.box.w / 2, base - EMOJI_MID * u];
        const fit = pose(mid, { scale: (capEm + fx.padY) / EMOJI_H, x: pivot[0] - mid[0], y: pivot[1] - mid[1] });
        units.push(wordUnit(c, w, { m: chain(fit, m), z: 1, clip, key: flipped ? "b" : undefined, fg: [colors.plateText], layers: [{ kind: "fill", color: colors.plateText }] }));
        continue;
      }
      units.push(
        wordUnit(c, w, {
          m,
          z: 1,
          clip,
          key: flipped ? "b" : undefined,
          fg: [flipped ? colour : colors.plateText],
          layers: flipped ? face(colors.plate, colour, fx.halo * u) : face(colour, colors.plateText, fx.halo * u),
        }),
      );
    }
    return { units, shapes };
  },
  // the label: one big tilted chip in the label's colour that opens like a turning card and closes again
  label: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    // the words of a label share one chip: the room the layout leaves between two chips is closed up
    const close = Math.max(0, P.wordGap - fx.labelGap) * u;
    const nudge = (w: { index: number; box: { line: number } }) => {
      const row = c.layout.lines[w.box.line].words;
      return -close * (row.indexOf(w.index) - (row.length - 1) / 2);
    };
    const wide = Math.max(...c.layout.lines.map((ln) => ln.w - close * (ln.words.length - 1)));
    const b: Box = {
      l: c.cx - wide / 2 - fx.padX * u,
      r: c.cx + wide / 2 + fx.padX * u,
      t: c.cy + c.layout.top - fx.padY * u,
      b: c.cy + c.layout.bottom + fx.padY * u,
    };
    const pivot: [number, number] = [(b.l + b.r) / 2, (b.t + b.b) / 2];
    const open = spring(t, fx.labelFreq, fx.labelDamp);
    const scale = lerp(fx.labelScale, 1, clamp(open));
    const rot = lerp(2 * P.label.rotate, P.label.rotate, E.outCubic(prog(t, 0, P.label.enterS)));
    // it opens a little past its width and settles, but never wider than the box
    const widest = (c.region.x1 - c.region.x0 - fx.rim * u - 2) / (2 * reach(b.r - b.l, b.b - b.t, scale, rot)[0]);
    const sx = Math.max(fx.flipThin, Math.min(widest, open) * Math.cos((Math.PI / 2) * E.inCubic(c.out)));
    // the tilted chip and its shadow stay inside the box: moved up or down once, for the label's whole life
    const tall = reach(b.r - b.l, b.b - b.t, 1, 2 * P.label.rotate)[1] + (fx.rim * u) / 2 + 1;
    const y = clamp(0, c.region.y0 + tall - pivot[1], Math.max(c.region.y0 + tall, c.region.y1 - tall - fx.shadowY * u) - pivot[1]);
    const m = pose(pivot, { scale, sx, rot, y });
    const under = pose(pivot, { scale, sx, rot, y: y + fx.shadowY * u });
    return {
      shapes: chip(`l${c.label.id}`, b, fx, u, { plate: colors.text, rim: colors.stroke, shadow: colors.shadow, line: null, m, under }),
      units: c.words.map((w) =>
        wordUnit(c, w, {
          m: chain(translate(nudge(w), 0), m),
          z: 1,
          active: true,
          fg: [colors.plateText],
          layers: face(colors.text, colors.plateText, fx.halo * u),
        }),
      ),
    };
  },
});
