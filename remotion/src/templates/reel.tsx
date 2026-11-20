import { z } from "zod";
import { apply, chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, lerp, prog, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { dropShadow, rect, wordUnit, type Ctx, type Grad, type LabelCtx, type Rect, type Scene, type Shape, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * REEL: a slot machine. One word at a time shows in a window between two rails. Each word rolls in from
 * above, stretched by its speed, and locks with a little bounce while the word before rolls out below: the
 * two move as one strip (the old page's exit and the new page's entrance run on the same clock and the
 * same spring, `paging.overlapS` long). A word arrives white-hot and cools to gold from the bottom up; a
 * shouted word cools to the emphasis colour, swells as it locks and makes the rails flash. Before a pause
 * the word rolls out and the rails close; after one they open again.
 *
 * The window and the rails are the same for every word of a clip: they depend on the caption box and the
 * nominal size only, never on the word (long words are set smaller inside the same window).
 */
const schema = paramsSchema({
  halo: z.number().min(0.04).max(0.2), // outline of the letters, em
  shadowY: z.number().min(0).max(0.2), // hard shadow under them, em
  padY: z.number().min(0.05).max(0.5), // the window is this much taller than a capital, above and below, em
  railH: z.number().min(0.02).max(0.2), // thickness of a rail, em
  railRim: z.number().min(0.01).max(0.1), // dark line around a rail, em
  railSpan: z.number().min(0.2).max(1), // length of the rails, share of the caption box's width
  railStart: z.number().min(0.02).max(1), // share of their length the rails start at when they open (they take paging.overlapS)
  rollMinS: z.number().min(0.05).max(0.3), // a word rolls in in paging.overlapS, and never faster than this
  lockDamp: z.number().min(0.3).max(0.95), // damping of the stop: lower = more bounce when the word locks
  stretch: z.number().min(0).max(0.6), // vertical stretch at full speed
  thin: z.number().min(0).max(0.3), // and how much narrower the word gets with it
  runAhead: z.number().min(0).max(1), // a word rolling out travels this much further than it has to, em
  exitRoll: z.number().min(0.2).max(1), // share of an exit before a pause in which the word rolls out (the rails start closing halfway through it)
  coolAt: z.number().min(0).max(1.5), // a word starts to cool this far into its roll (1 = as it locks); it takes timing.colorS
  bandTop: z.number().min(0).max(0.5), // the fill of a spoken word: the light colour from the top down to here (share of the cap height)
  bodyFrom: z.number().min(0.1).max(1), // and the body colour from here down
  restBand: z.number().min(0).max(0.5), // the same two once the word is no longer spoken (more body colour)
  restBody: z.number().min(0.05).max(1),
  restScale: z.number().min(0.8).max(1), // and the word settles back to this scale
  emphPunch: z.number().min(0).max(0.3), // a shouted word swells by this as it locks
  emphPunchS: z.number().min(0.1).max(1), // for this long
  flashInS: z.number().min(0.07).max(0.3), // the rails take this long to flash to the emphasis colour
  flashOutS: z.number().min(0.1).max(1.5), // and this long to come back
  flashGrow: z.number().min(0).max(2), // they are this much thicker at the peak of the flash
});
type P = z.infer<typeof schema>;

// the picture of a system emoji in its em box: how tall it is and where its middle sits above the baseline, em
const EMOJI_H = 1.03;
const EMOJI_MID = 0.35;

// rails are drawn between z 1 and 2, a later page's under an earlier page's: this many pages before the order repeats
const RAIL_LEVELS = 4096;

/** spring(t, lockAt(d) / T, d) first reaches 1 exactly at t = T: the moment the word locks. */
const lockAt = (d: number): number => (Math.PI - Math.acos(d)) / (2 * Math.PI * Math.sqrt(1 - d * d));

/** Speed of spring(t, f, d) as a share of its top speed (negative on the way back from the overshoot). */
function speed(t: number, f: number, d: number): number {
  if (t <= 0) return 0;
  const w = 2 * Math.PI * f;
  const q = Math.sqrt(1 - d * d);
  return (Math.exp(-d * w * t) * Math.sin(w * q * t)) / (q * Math.exp((-d * Math.acos(d)) / q));
}

/** A hump 0 -> 1 -> 0 over p = 0..1 that rises fast and dies away slowly. */
const swell = (p: number): number => (p <= 0 || p >= 1 ? 0 : Math.sin(Math.PI * Math.sqrt(p)) * (1 - p));

type Look = {
  top: string; // light end of the fill
  body: (w: WordState) => string; // body colour of a word
  hot: (w: WordState) => boolean; // this word swells and flashes the rails when it locks
  rail: string;
  flash: string; // what the rails flash to
};

function reel(c: Ctx<P> | LabelCtx<P>, content: number, look: Look): Scene {
  const { P, t, colors, region: reg } = c;
  const fx = P.fx;
  const capEm = c.layout.capH / c.layout.size;
  const boxH = reg.y1 - reg.y0;
  const boxW = reg.x1 - reg.x0;

  // the window: a capital of the largest size this box allows, plus padY; the rails sit right outside it
  const rails = 2 * fx.railH * (1 + fx.flashGrow / 2) + fx.railRim * (1 + fx.flashGrow); // em above and below the window, at the peak of a flash
  let u = Math.min(P.size, (boxH - 2 * P.margin.y) / capEm, (boxH - 2) / (capEm + 2 * fx.padY + rails));
  const inner = Math.max(capEm * u, content);
  if (inner + (2 * fx.padY + rails) * u > boxH - 2) u = Math.max(1, (boxH - 2 - inner) / (2 * fx.padY + rails));
  const hw = inner / 2 + fx.padY * u;
  const half = hw + (rails / 2) * u + 1;
  const wy = clamp(reg.anchorY, reg.y0 + half, Math.max(reg.y0 + half, reg.y1 - half));
  const window: Rect = { x: reg.x0, y: wy - hw, w: boxW, h: 2 * hw };
  const eu = Math.max(u, c.layout.size); // em of the letters' outline and shadow
  const fit = Math.min(1, inner / Math.max(c.layout.capH, content)); // a word too tall for the window is scaled into it
  // How far this word travels: in from where its lower edge is just above the window (so it shows from
  // its first frame, however small it is), out until its upper edge, at full stretch, is below it and
  // then `runAhead` further, so the word leaving keeps clear of the one coming in behind it.
  const comeFrom = hw + c.layout.bottom * fit + (fx.halo + fx.shadowY) * eu + 2;
  const leaveTo = hw + (-c.layout.top * fit + fx.halo * eu) * (1 + fx.stretch) + fx.runAhead * u + 2;

  // What a page cannot see is its neighbours, so both ends are worked out from the planner's rules:
  // - a page that hands over to the next one (continuous speech) gets an exit of at most paging.overlapS
  //   that starts the moment its highlight ends; an exit before a pause starts later or is longer;
  // - if a page is still rolling out while this one rolls in, it is gone after `shared` seconds.
  const last = c.words[c.words.length - 1];
  const handover = c.exit > 0 && c.exit <= P.paging.overlapS + 1e-4 && Math.abs(c.exitT - last.activeEnd) <= 2e-4;
  const shared = Math.min(P.paging.overlapS, last.activeEnd);

  // the roll in: same clock and same spring as the exit of the page before, so the two move as one strip
  const d = fx.lockDamp;
  const rollS = Math.max(shared, fx.rollMinS);
  const fIn = lockAt(d) / rollS;
  const into = spring(t, fIn, d);
  let speedNow = Math.abs(speed(t, fIn, d));
  // the roll out: over the next page's entrance, or in the first part of an exit before a pause
  let outOf = 0;
  if (c.exit > 0 && t >= c.exitT) {
    const fOut = lockAt(d) / (handover ? c.exit : c.exit * fx.exitRoll);
    outOf = spring(t - c.exitT, fOut, d);
    speedNow = outOf >= 1 ? 0 : Math.max(speedNow, Math.abs(speed(t - c.exitT, fOut, d)));
  }
  const y = wy - c.cy + lerp(comeFrom * (into - 1), leaveTo, Math.min(1, outOf));

  const hot = c.words.some(look.hot);
  const punch = hot ? fx.emphPunch * swell(prog(t, rollS, fx.emphPunchS)) : 0;
  const rest = E.inOutQuad(prog(t, last.activeEnd, P.timing.activeOutS));
  const widest = (boxW - 2 * fx.halo * eu - 4) / Math.max(1, c.layout.right - c.layout.left);
  const scale = Math.min(widest, fit * lerp(1, fx.restScale, rest) * (1 + punch));
  const sy = 1 + fx.stretch * speedNow;
  const m = pose([c.cx, c.cy], { scale, sx: 1 - fx.thin * speedNow, sy, y });

  const units: Unit[] = [];
  const top = c.cy + y + c.layout.top * sy * scale - fx.halo * eu;
  const bottom = c.cy + y + c.layout.bottom * sy * scale + (fx.halo + fx.shadowY) * eu;
  if (outOf < 1 && bottom > window.y + 0.5 && top < window.y + window.h - 0.5) {
    const cool = E.inOutQuad(prog(t, rollS * fx.coolAt, P.timing.colorS)); // white-hot as it rolls in, then the body colour rises
    for (const w of c.words) {
      const body = look.body(w);
      const mid = mix(look.top, body, 0.5); // a stop of its own, so the fill is never far from a colour it reports
      const base = c.cy + w.box.y;
      if (isEmoji(w.text, w.box.font)) {
        // An emoji is a picture taller than the window: drawn as tall as a capital, on the cap line, with
        // no outline of its own. The browser draws it as text, on whole pixel rows: its baseline is put on
        // one, so that what the scene says and what is drawn move at the same instant.
        const at: [number, number] = [c.cx + w.box.x + w.box.w / 2, base - EMOJI_MID * w.box.size];
        const fitted = chain(pose(at, { scale: capEm / EMOJI_H, y: base - (capEm * w.box.size) / 2 - at[1] }), m);
        const row = apply(fitted, c.cx + w.box.x, base)[1];
        const own = chain(fitted, translate(0, Math.round(row) - row));
        units.push(wordUnit(c, w, { m: own, z: 1, clip: window, fg: [body], layers: [{ kind: "fill", color: body }] }));
        continue;
      }
      const band = lerp(lerp(1, fx.bandTop, cool), fx.restBand, rest);
      const from = Math.max(band, lerp(lerp(1, fx.bodyFrom, cool), fx.restBody, rest));
      const grad: Grad = {
        x1: 0,
        y1: base - capEm * w.box.size,
        x2: 0,
        y2: base,
        stops: [
          { o: 0, c: look.top },
          { o: band, c: look.top },
          { o: (band + from) / 2, c: mid },
          { o: from, c: body },
          { o: 1, c: body },
        ],
      };
      units.push(
        wordUnit(c, w, {
          m,
          z: 1,
          clip: window,
          fg: [look.top, mid, body],
          layers: [
            dropShadow(colors.shadow, fx.halo * eu, 0, fx.shadowY * eu),
            { kind: "stroke", color: colors.stroke, width: fx.halo * eu },
            { kind: "fill", color: body, grad },
          ],
        }),
      );
    }
  }

  // The rails open from the middle on every page and are fully open after `shared` seconds. When a page
  // is rolling out, its rails are still there, full length, drawn over these (a later page's rails lie
  // under an earlier page's), so nothing is seen of the opening; they go at the moment these are open.
  // They close in an exit that nothing follows.
  let grow = lerp(fx.railStart, 1, E.outCubic(prog(t, 0, shared)));
  if (c.exit > 0 && !handover) {
    const from = c.exitT + (c.exit * fx.exitRoll) / 2;
    grow *= 1 - E.inCubic(prog(t, from, c.exitT + c.exit - from));
  }
  // the flash starts as the word locks (the rails of a page before are gone by then) and is over before
  // this page's own exit, so two pages never show the rails in two colours
  let flash = 0;
  if (hot) {
    flash = E.inOutQuad(prog(t, rollS, fx.flashInS)) * (1 - E.inOutQuad(prog(t, rollS + fx.flashInS, fx.flashOutS)));
    if (c.exit > 0) flash *= 1 - E.inOutQuad(prog(t, c.exitT - fx.flashInS, fx.flashInS));
  }
  const tag = "label" in c ? `l${c.label.id}` : `p${c.page.id}`;
  const span = boxW * fx.railSpan * grow;
  const thick = fx.railH * u;
  const fill = mix(look.rail, look.flash, flash);
  const shapes: Shape[] = [];
  if (span > 0.5) {
    for (const [name, ry] of [
      ["a", wy - hw - thick],
      ["b", wy + hw],
    ] as const) {
      shapes.push(
        rect(`${tag}rail${name}`, c.cx - span / 2, ry, span, thick, {
          role: "decor",
          fill,
          stroke: colors.stroke,
          strokeWidth: fx.railRim * u,
          r: thick / 2,
          m: pose([c.cx, ry + thick / 2], { sy: 1 + fx.flashGrow * flash }),
          z: 2 - (c.index % RAIL_LEVELS) / RAIL_LEVELS,
        }),
      );
    }
  }
  return { units, shapes };
}

export default defineTemplate<P>({
  name: "reel",
  schema,
  build: (c): Scene =>
    reel(c, 0, {
      top: c.colors.text,
      body: (w) => (w.emph ? c.colors.emph : c.colors.active),
      hot: (w) => w.emph,
      rail: c.colors.accent,
      flash: c.colors.emph,
    }),
  // the label is the jackpot: it rolls into the same window in the label's colour and the rails flash
  label: (c): Scene =>
    reel(c, c.layout.bottom - c.layout.top, {
      top: c.colors.accent2,
      body: () => c.colors.text,
      hot: () => true,
      rail: c.colors.text,
      flash: c.colors.accent2,
    }),
});
