import { z } from "zod";
import { chain, pose } from "../core/affine";
import { mix } from "../core/color";
import { clamp, lerp, prog } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { letterUnits, type Ctx, type LabelCtx, type Layer, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * ARCADE: 8-bit. Pixel letters with a one-pixel outline and a hard, stepped extrusion in a second colour.
 * Every word is put on a whole-pixel grid (one font pixel is a whole number of screen pixels, positions
 * and motion are whole pixels), so it is drawn like a sprite: no soft edges anywhere. Letters drop in one
 * by one, growing as they fall, and bounce; the spoken word hops letter by letter and runs between two
 * colours; shouted words are bigger and blink; before a pause the letters fall off one by one.
 */
const schema = paramsSchema({
  px: z.number().min(0.05).max(0.25), // one pixel of the font's grid, em (Press Start 2P: an eighth)
  depth: z.number().int().min(0).max(4), // extrusion, font pixels: one hard step each
  letterS: z.number().min(0).max(0.2), // letters start to drop this far apart...
  cascadeS: z.number().min(0).max(1), // ...but the last letter of a page starts within this long
  dropH: z.number().min(0.1).max(1.5), // they drop from this far above their place, em
  dropFrom: z.number().min(0.5).max(1), // starting at this scale and growing to full size as they land
  dropS: z.number().min(0.05).max(0.6), // the fall takes this long
  bounce: z.number().min(0).max(0.7), // each bounce leaves the floor this many times as fast as the letter hit it
  bounces: z.number().int().min(0).max(4),
  hopH: z.number().min(0).max(1), // the spoken word hops this high, em
  hopS: z.number().min(0.1).max(1), // one hop
  hopSpreadS: z.number().min(0).max(0.5), // its letters hop one after another, the last this long after the first
  cycleHz: z.number().min(0.2).max(4), // the spoken word runs between its two colours this often
  emphHop: z.number().min(1).max(3), // a spoken shout hops this many times as high
  blinkHz: z.number().min(0.5).max(3.5), // shouted words and labels blink between their two colours at this rate
  waveH: z.number().min(0).max(0.6), // a label's letters ride a travelling wave this high, em
  waveS: z.number().min(0.3).max(3), // one wave passes a letter in this long
  waveLag: z.number().min(0).max(0.5), // each letter is this share of a wave behind the one before
  fallS: z.number().min(0.07).max(1), // exit: a letter takes this long to fall away
  fallH: z.number().min(0).max(0.5), // it drops this far while it shrinks to nothing, em
});
type P = z.infer<typeof schema>;
type Fx = P["fx"];

/** Height 1..0 above the floor of something dropped at t = 0: it falls in `fallS`, then bounces. */
function dropped(t: number, fallS: number, bounce: number, bounces: number): number {
  if (t <= 0) return 1;
  if (t < fallS) return 1 - (t / fallS) ** 2;
  let t0 = fallS;
  let v = bounce; // speed off the floor, as a share of the speed of the first impact
  for (let i = 0; i < bounces; i++) {
    const dur = 2 * v * fallS;
    if (t < t0 + dur) {
      const x = (t - t0) / dur;
      return v * v * 4 * x * (1 - x);
    }
    t0 += dur;
    v *= bounce;
  }
  return 0;
}

const CORNERS: [number, number][] = [[-1, -1], [1, -1], [-1, 1], [1, 1]]; // prettier-ignore

/**
 * A sprite letter, back to front: a dark far edge, `depth` steps of extrusion, a one-pixel outline, the
 * face. Everything is the glyph itself shifted by whole pixels `q`, so every corner stays square and the
 * extrusion is a staircase; step k only needs the five copies that stick out past step k - 1. `q` is in
 * the unit's own coordinates (before its transform).
 */
function sprite(face: string, edge: string, side: string, q: number, depth: number): Layer[] {
  const out: Layer[] = [];
  const at = (color: string, i: number, j: number) => out.push({ kind: "stroke", color, width: 0, dx: i * q, dy: j * q });
  const step = (color: string, k: number) => {
    at(color, k, k - 2);
    at(color, k - 2, k);
    at(color, k, k - 1);
    at(color, k - 1, k);
    at(color, k, k);
  };
  step(edge, depth + 2);
  for (let k = depth + 1; k >= 2; k--) step(side, k);
  out.push({ kind: "stroke", color: edge, width: q }); // the outline: a stroke one pixel wide (the halo the checks measure)...
  for (const [i, j] of CORNERS) at(edge, i, j); // ...and four copies that square its corners
  out.push({ kind: "fill", color: face });
  return out;
}

/** An emoji is a picture: the same stack as three square halos. */
function emojiSprite(edge: string, side: string, q: number): Layer[] {
  return [
    { kind: "stroke", color: edge, width: q, dx: 2 * q, dy: 2 * q },
    { kind: "stroke", color: side, width: q, dx: q, dy: q },
    { kind: "stroke", color: edge, width: q },
    { kind: "fill", color: edge },
  ];
}

type Line = {
  w: WordState;
  face: string;
  since: number; // seconds since the word started to hop (negative: not yet)
  hop: number; // hop height, em
  wave: number; // 0..1 share of the label wave
};

function settled(fx: Fx): number {
  let total = fx.dropS;
  let v = fx.bounce;
  for (let i = 0; i < fx.bounces; i++) {
    total += 2 * v * fx.dropS;
    v *= fx.bounce;
  }
  return total;
}

function sprites(c: Ctx<P> | LabelCtx<P>, lines: Line[]): Scene {
  const fx = c.P.fx;
  const { colors, t } = c;
  const count = lines.reduce((n, l) => n + l.w.box.glyphs.length, 0);
  const lag = count > 1 ? Math.min(fx.letterS, fx.cascadeS / (count - 1)) : 0;
  const rest = settled(fx);
  // exit: one letter after another, the last one out of sight as the page ends
  const fallS = Math.min(fx.fallS, c.exit);
  const fallLag = count > 1 ? (c.exit - fallS) / (count - 1) : 0;
  const units: Unit[] = [];
  let seen = 0;
  for (const l of lines) {
    const w = l.w;
    // the word's own pixel grid: q screen pixels to a font pixel, the word scaled by s to fit it
    const q = Math.max(1, Math.floor(fx.px * w.box.size + 0.02));
    const s = q / (fx.px * w.box.size);
    const e = q / fx.px; // one em on that grid, screen pixels
    const left = Math.round(c.cx + w.box.x + (w.box.w * (1 - s)) / 2);
    const base = Math.round(c.cy + w.box.y);
    const n = w.box.glyphs.length;
    const hopLag = n > 1 ? fx.hopSpreadS / (n - 1) : 0;
    const first = seen;
    seen += n;
    units.push(
      ...letterUnits(c, w, (glyph, k) => {
        const g = first + k;
        const born = g * lag;
        if (t < born) return null;
        const x = prog(l.since - k * hopLag, 0, fx.hopS);
        // a letter still in the air from its drop does not hop on top of that: the higher of the two, so a
        // word spoken as it lands never rises above the room a hop alone needs
        let y = -e * Math.max(fx.dropH * dropped(t - born, fx.dropS, fx.bounce, fx.bounces), l.hop * 4 * x * (1 - x));
        y -= l.wave * fx.waveH * e * (0.5 - 0.5 * Math.cos(2 * Math.PI * Math.max(0, (t - rest) / fx.waveS - g * fx.waveLag)));
        // it grows from dropFrom to full size while it falls, and shrinks to nothing as it falls off
        let size = lerp(fx.dropFrom, 1, prog(t, born, fx.dropS) ** 2);
        if (c.exit > 0 && t >= c.exitT) {
          const gone = prog(t, c.exitT + g * fallLag, fallS);
          if (gone >= 1) return null;
          y += fx.fallH * e * gone * gone;
          size *= 1 - gone * gone;
        }
        // the letter's own origin (baseline-left) goes to a whole pixel, and at full size it moves in whole pixels
        const ox = c.cx + w.box.x + glyph.x;
        const oy = c.cy + w.box.y;
        const tx = left + Math.round(glyph.x * s);
        const grid = pose([ox, oy], { scale: s, x: tx - ox, y: base - oy });
        const m = chain(grid, pose([tx + (glyph.w * s) / 2, base], { scale: size, y: size === 1 ? Math.round(y) : y }));
        const layers = isEmoji(glyph.ch, w.box.font) ? emojiSprite(colors.stroke, colors.shadow, q / s) : sprite(l.face, colors.stroke, colors.shadow, q / s, fx.depth);
        return { m, fg: [l.face], layers };
      }),
    );
  }
  return { units, shapes: [] };
}

export default defineTemplate<P>({
  name: "arcade",
  schema,
  build: (c): Scene => {
    const { colors, P } = c;
    const fx = P.fx;
    const turn = 2 * Math.PI;
    const blink = mix(colors.emph, colors.accent2, 0.5 - 0.5 * Math.cos(turn * fx.blinkHz * c.T));
    return sprites(
      c,
      c.words.map((w): Line => {
        const run = mix(colors.active, colors.accent, 0.5 - 0.5 * Math.cos(turn * fx.cycleHz * Math.max(0, w.since)));
        return {
          w,
          face: w.emph ? blink : mix(colors.text, run, clamp(w.a)),
          since: w.since,
          hop: fx.hopH * (w.emph ? fx.emphHop : 1),
          wave: 0,
        };
      }),
    );
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    const face = mix(c.colors.text, c.colors.accent2, 0.5 - 0.5 * Math.cos(2 * Math.PI * fx.blinkHz * c.t));
    return sprites(
      c,
      c.words.map((w): Line => ({ w, face, since: -1, hop: 0, wave: 1 })),
    );
  },
});
