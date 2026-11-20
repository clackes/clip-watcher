import { z } from "zod";
import { chain, pose } from "../core/affine";
import { mix } from "../core/color";
import { back, clamp, E, lerp, prog, spring } from "../core/ease";
import { paramsSchema } from "../core/schema";
import { letterUnits, path, rect, type Ctx, type LabelCtx, type Layer, type Scene, type Shape, type Unit, type WordState } from "../core/scene";
import { defineTemplate, em } from "../core/template";

/**
 * TYPER: a terminal line. An opaque dark plate with a prompt mark and a block caret; every word is typed
 * letter by letter while it is spoken, the caret running ahead of each key and blinking (a slow colour
 * swell, never on/off) when nothing is typed. The spoken word is bright, typed words are the terminal
 * colour, shouted words are in inverse video. The plate switches on and off like a CRT.
 */
const schema = paramsSchema({
  padX: z.number().min(0.1).max(1), // plate past the prompt on the left and the caret on the right, em
  padY: z.number().min(0.05).max(1), // plate above and below the line, em
  radius: z.number().min(0).max(0.6), // plate corners, em
  rim: z.number().min(0).max(0.1), // plate border, em
  rimMix: z.number().min(0).max(1), // the border is the terminal colour taken this far towards the plate colour
  promptW: z.number().min(0.1).max(1), // the prompt mark (a chevron): width, em
  promptH: z.number().min(0.1).max(1), // its height, em
  promptUp: z.number().min(0).max(0.8), // its middle above the baseline, em
  promptStroke: z.number().min(0.03).max(0.3), // its line width, em
  promptGap: z.number().min(0.1).max(1.5), // between the prompt and the first letter, em
  promptS: z.number().min(0.1).max(1), // the prompt flares this long when a new word starts
  promptAttack: z.number().min(0.05).max(0.9), // share of that spent rising
  caretW: z.number().min(0.1).max(1), // block caret, em
  caretGap: z.number().min(0.12).max(0.6), // between the last letter and the caret, em (more than blockPadX and knock)
  caretUp: z.number().min(0.3).max(1.2), // the caret's top above the baseline, em
  caretDown: z.number().min(0).max(0.4), // and its bottom below it, em
  caretS: z.number().min(0.02).max(0.3), // the caret glides to its next cell in this long, just before the key lands
  idleS: z.number().min(0).max(2), // the caret starts to blink this long after the last key
  blinkHz: z.number().min(0.3).max(3), // one blink is a slow swell of colour at this rate
  blinkDepth: z.number().min(0).max(1), // how far towards the plate colour the caret goes
  typeShare: z.number().min(0.1).max(1), // a word's letters land across this share of the time it is spoken
  keyS: z.number().min(0.01).max(0.3), // and never further apart than this
  typeTailS: z.number().min(0).max(0.5), // the last letter of a word is up at least this long before the word is over
  keyFrom: z.number().min(0.5).max(1), // a typed letter pops in from this scale
  keyHz: z.number().min(2).max(14), // pop spring: Hz
  keyDamp: z.number().min(0.3).max(1), // pop spring: damping ratio
  knock: z.number().min(0.07).max(0.11), // backing in the colour behind every letter, em: what the letters are measured against
  blockPadX: z.number().min(0.11).max(0.5), // the inverse block of a shouted word reaches this far past it, em
  blockPadY: z.number().min(0).max(0.5),
  blockRadius: z.number().min(0).max(0.3), // em
  blockHot: z.number().min(0).max(1), // while the shout is spoken its block goes this far towards the bright colour
  openS: z.number().min(0.07).max(1), // switching on: the picture opens vertically in this long
  openXS: z.number().min(0.03).max(1), // and horizontally in this long
  openThin: z.number().min(0.02).max(0.5), // from this share of its height
  openFromX: z.number().min(0.05).max(1), // and this share of its width
  openOver: z.number().min(0).max(3), // overshoot of the vertical opening
  offLine: z.number().min(0.3).max(0.9), // switching off: share of the exit in which the picture collapses to a line
  offThin: z.number().min(0.02).max(0.2), // the line, as a share of the plate height
  offFlash: z.number().min(0).max(1), // how far the plate goes towards the terminal colour while it is a line
  flashBelow: z.number().min(0.06).max(0.24), // it only lights up while the picture is flatter than this share of its height: too flat to read
  labelTypeS: z.number().min(0).max(1), // a label is typed in this long
});
type P = z.infer<typeof schema>;

/** 0 -> 1 -> 0 over x = 0..1, the top at `peak`. */
const bump = (x: number, peak: number): number =>
  x <= 0 || x >= 1 ? 0 : x < peak ? E.inOutSine(x / peak) : 1 - E.inOutSine((x - peak) / (1 - peak));

type Entry = {
  w: WordState;
  times: number[]; // when each letter lands, seconds on the page clock
  inverse: boolean; // dark letters on a block
  ink: string; // letter colour
  block: string; // block colour (inverse only)
};

/** When the letters of a word land: across the first part of the time it is spoken. */
function keys(n: number, start: number, dur: number, fx: P["fx"]): number[] {
  const window = Math.max(0, Math.min(fx.typeShare * dur, dur - fx.typeTailS));
  const step = n > 1 ? Math.min(fx.keyS, window / (n - 1)) : 0;
  return Array.from({ length: n }, (_, k) => start + k * step);
}

function terminal(c: Ctx<P> | LabelCtx<P>, id: string, entries: Entry[], flare: number): Scene {
  const fx = c.P.fx;
  const u = em(c);
  const { colors, t } = c;
  const line = c.layout.lines[0];
  const base = c.cy + line.y;

  // the plate, around the prompt, the whole line and the caret's last cell
  const left = c.cx + c.layout.left - (fx.promptGap + fx.promptW + fx.padX) * u;
  const right = c.cx + c.layout.right + (fx.caretGap + fx.caretW + fx.padX) * u;
  const top = c.cy + c.layout.top - fx.padY * u;
  const bottom = c.cy + c.layout.bottom + fx.padY * u;

  // the picture switches on (a line that opens) and off (collapses to a line, the line to a dot)
  const off = E.inOutSine(prog(c.out, 0, fx.offLine));
  const dot = E.inCubic(prog(c.out, fx.offLine, 1 - fx.offLine));
  const sy = lerp(fx.openThin, 1, back(fx.openOver)(prog(t, 0, fx.openS))) * lerp(1, fx.offThin, off);
  const sx = lerp(fx.openFromX, 1, E.outCubic(prog(t, 0, fx.openXS))) * (1 - dot);
  const screen = pose([(left + right) / 2, (top + bottom) / 2], { sx, sy });
  const thin = Math.min(fx.openThin, fx.offThin);
  const flash = fx.offFlash * clamp((fx.flashBelow - sy) / (fx.flashBelow - thin));

  const shapes: Shape[] = [
    rect(`${id}plate`, left, top, right - left, bottom - top, {
      fill: mix(colors.plate, colors.text, flash),
      stroke: mix(colors.text, colors.plate, fx.rimMix),
      strokeWidth: fx.rim * u,
      r: fx.radius * u,
      m: screen,
      z: -5,
    }),
  ];

  // the prompt mark
  const px = c.cx + c.layout.left - (fx.promptGap + fx.promptW) * u;
  const py = base - fx.promptUp * u;
  const ph = (fx.promptH * u) / 2;
  shapes.push(
    path(`${id}prompt`, `M${px} ${py - ph}L${px + fx.promptW * u} ${py}L${px} ${py + ph}`, {
      fill: null,
      stroke: mix(colors.accent, colors.active, flare),
      strokeWidth: fx.promptStroke * u,
      m: screen,
      z: -2,
    }),
  );

  // every cell in the order it is typed: when its key lands and where the caret stands after it
  const cells: { at: number; x: number; entry: number }[] = [];
  entries.forEach((e, i) => e.w.box.glyphs.forEach((g, k) => cells.push({ at: e.times[k], x: c.cx + e.w.box.x + g.x + g.w, entry: i })));
  let last = -1;
  for (let i = 0; i < cells.length; i++) if (t >= cells[i].at) last = i;
  const home = entries.length ? c.cx + entries[0].w.box.x : c.cx;
  const here = last >= 0 ? cells[last].x : home;
  const since = last >= 0 ? t - cells[last].at : t; // seconds since the last key
  let caret = here;
  let moving = false;
  const next = cells[last + 1];
  if (next) {
    // the caret is in its new cell by the time the key lands: the letter never appears under it
    const glide = Math.min(fx.caretS, next.at - (last >= 0 ? cells[last].at : 0));
    const p = prog(t, next.at - glide, glide);
    moving = p > 0;
    caret = lerp(here, next.x, E.inOutQuad(p));
  }

  const units: Unit[] = [];
  entries.forEach((e, i) => {
    const w = e.w;
    if (e.inverse) {
      // the block is typed too: it reaches as far as the caret has come inside this word
      const from = c.cx + w.box.x;
      const to = Math.min(Math.max(caret, from), from + w.box.w);
      if (to > from && t >= e.times[0]) {
        shapes.push(
          rect(`${id}block${i}`, from - fx.blockPadX * u, c.cy + line.top - fx.blockPadY * u, to - from + 2 * fx.blockPadX * u, line.bottom - line.top + 2 * fx.blockPadY * u, {
            fill: e.block,
            r: fx.blockRadius * u,
            m: screen,
            z: -3,
          }),
        );
      }
    }
    const behind = e.inverse ? e.block : colors.plate;
    units.push(
      ...letterUnits(c, w, (_g, k, _n, gx) => {
        if (t < e.times[k]) return null;
        const pop = lerp(fx.keyFrom, 1, spring(t - e.times[k], fx.keyHz, fx.keyDamp));
        const layers: Layer[] = [
          { kind: "stroke", color: behind, width: fx.knock * u },
          { kind: "fill", color: e.ink },
        ];
        return { m: chain(pose([gx, c.cy + w.box.y], { scale: pop }), screen), layers, fg: [e.ink] };
      }),
    );
  });

  // the caret: solid while keys land, a slow swell towards the plate colour when idle
  const idle = moving ? 0 : Math.max(0, since - fx.idleS);
  const swell = fx.blinkDepth * (0.5 - 0.5 * Math.cos(2 * Math.PI * fx.blinkHz * idle));
  shapes.push(
    rect(`${id}caret`, caret + fx.caretGap * u, base - fx.caretUp * u, fx.caretW * u, (fx.caretUp + fx.caretDown) * u, {
      fill: mix(colors.text, colors.plate, swell),
      role: "decor",
      m: screen,
      z: -2,
    }),
  );
  return { units, shapes };
}

export default defineTemplate<P>({
  name: "typer",
  schema,
  build: (c): Scene => {
    const { colors, P } = c;
    const fx = P.fx;
    let flare = 0;
    const entries = c.words.map((w): Entry => {
      const a = clamp(w.a);
      flare = Math.max(flare, bump(prog(w.since, 0, fx.promptS), fx.promptAttack));
      return {
        w,
        times: keys(w.box.glyphs.length, w.start, Math.min(w.end, w.activeEnd) - w.start, fx),
        inverse: w.emph,
        ink: w.emph ? colors.stroke : mix(colors.text, colors.active, a),
        block: mix(colors.emph, colors.active, fx.blockHot * a),
      };
    });
    return terminal(c, `p${c.page.id}`, entries, flare);
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    // one run of keys across the whole label
    const count = c.words.reduce((n, w) => n + w.box.glyphs.length, 0);
    const step = count > 1 ? Math.min(fx.keyS, fx.labelTypeS / (count - 1)) : 0;
    let seen = 0;
    const entries = c.words.map((w): Entry => {
      const times = w.box.glyphs.map((_, k) => (seen + k) * step);
      seen += w.box.glyphs.length;
      return { w, times, inverse: true, ink: c.colors.stroke, block: c.colors.text };
    });
    return terminal(c, `l${c.label.id}`, entries, bump(prog(c.t, 0, fx.promptS), fx.promptAttack));
  },
});
