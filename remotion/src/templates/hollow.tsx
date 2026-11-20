import { z } from "zod";
import { apply, pose, type Affine } from "../core/affine";
import { mix, parseHex } from "../core/color";
import { back, clamp, E, lerp, prog, pulse, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Rect, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * HOLLOW: outline letters that fill up. Slab caps drawn as glass: dark inside, a light line around the
 * letter shape, a dark outline around that. When a word is spoken a liquid rises inside it from the
 * bottom, a lighter head of foam on top, the surface tipping to and fro as it settles; the word stays
 * filled. Where the liquid stands, the light line gives way to the dark outline: a full word is a word
 * in the liquid's colour on dark, an empty one a light line on dark. Shouted words fill with the
 * emphasis colour and slosh: the liquid runs up to the brim and falls back. The lines slide in from
 * opposite sides through the edges of the block; before a pause the liquid drains and the lines slide
 * on and out.
 *
 * A word is drawn twice, through two windows that meet at the level of the liquid: the empty part above
 * (with the light line), the full part below (without). A glass floats a little higher the fuller it is
 * and rocks against its liquid, so whenever the liquid moves the word moves too.
 */
const schema = paramsSchema({
  outer: z.number().min(0.07).max(0.3), // dark outline outside the light line, em
  inline: z.number().min(0.02).max(0.15), // the light line around the letter shape of an empty glass, em
  level: z.number().min(0.3).max(1), // how full a letter ends up, as a share of its height
  foam: z.number().min(0).max(0.3), // the head on the liquid, as a share of the letter height
  foamTint: z.number().min(0.2).max(0.8), // its colour: the liquid's, this far towards the light line's (never the line's own)
  fillS: z.number().min(0.07).max(1.5), // the liquid takes this long to rise (less when the word is shorter)
  fillMinS: z.number().min(0.07).max(0.5), // but never less than this
  fillOvershoot: z.number().min(0).max(3), // it runs a little past its level and falls back
  fillLift: z.number().min(0.005).max(0.1), // a full word floats this much higher than an empty one, em
  sloshDeg: z.number().min(0).max(20), // the surface tips by this much while it rises
  sloshHz: z.number().min(0.5).max(4), // to and fro this often
  sloshS: z.number().min(0.1).max(3), // and is still after this long
  rock: z.number().min(0.02).max(0.5), // the word rocks the other way by this share of the tip
  emphFreq: z.number().min(0.5).max(4), // a shouted word's liquid rises on a spring: Hz
  emphDamp: z.number().min(0.15).max(1), // low: it runs up to the brim and falls back
  emphSloshDeg: z.number().min(0).max(25),
  activeScale: z.number().min(1).max(1.4), // the spoken word grows to this
  activeLift: z.number().min(0).max(0.2), // and lifts, em
  activeOvershoot: z.number().min(0).max(4),
  slitPad: z.number().min(0).max(1), // the edges the lines slide through stand this far outside the block, em
  enterOvershoot: z.number().min(0).max(2), // a line slides a little past its place and back
  drainShare: z.number().min(0.1).max(1), // share of the exit in which the liquid drains
  slideFrom: z.number().min(0).max(0.9), // the lines start sliding out at this share of the exit
});
type P = z.infer<typeof schema>;
type FX = P["fx"];

const HEAD = 0.04; // the liquid's box reaches this share of the cap height past top and bottom (round letters overshoot)
const DARK = 4.4; // px of dark outline kept (the frame checks read a fill against the 2 to 4 px around it)
const LINE = 2.2; // px of light line kept: thinner, it is neither a line nor gone
const FAR = 48; // 8-bit levels two colours must differ by to count as different

/**
 * Reach (px) of the two outlines of a word of size `u`, from the letter shape outwards: the dark one all
 * the way, the light line of an empty glass inside it. In very small text both keep a width in pixels
 * (the outline is then fatter for its size).
 */
function outlines(fx: FX, u: number): { dark: number; light: number } {
  const dark = Math.max((fx.outer + fx.inline) * u, DARK + LINE);
  return { dark, light: Math.max(LINE, Math.min(fx.inline * u, dark - DARK)) };
}

/** True when two colours are clearly different ones (a line in the liquid's own colour would read as liquid). */
const apart = (a: string, b: string): boolean => {
  const p = parseHex(a);
  const q = parseHex(b);
  return Math.max(Math.abs(p[0] - q[0]), Math.abs(p[1] - q[1]), Math.abs(p[2] - q[2])) > FAR;
};

/** What is in the glass: how full (0..1 of the letter height), the tip of the surface in degrees, the colours. */
type Fill = { level: number; tip: number; colour: string; light: string };

/** A to-and-fro that dies out: 0 at the start, `deg` at most, still after `dur`. */
const tip = (since: number, deg: number, hz: number, dur: number): number =>
  since <= 0 || since >= dur ? 0 : deg * Math.sin(2 * Math.PI * hz * since) * (1 - since / dur) ** 2;

/**
 * A word as glass, as one or two units. The inside of the letters is one hard-edged gradient (dark above
 * the surface, a band of foam, the liquid below) whose axis is tipped with the surface, so the liquid
 * really tilts inside the letters. Around them: above the level of the liquid the light line of the
 * empty glass, below it the dark outline alone.
 *
 * The full part is the fill there is to read: its `fg` is the liquid and its foam (a tint of the liquid,
 * so the light line is never taken for fill). The empty part has no fill of its own (`fg` is empty): its
 * letter shapes are carried by the light line, 21:1 on the dark around it.
 */
function glass(c: Ctx | LabelCtx, w: WordState, fx: FX, f: Fill, m: Affine, z: number, win: Rect, active?: boolean): Unit[] {
  const u = w.box.size;
  const dark = c.colors.stroke;
  const wide = outlines(fx, u);
  const edge: Layer = { kind: "stroke", color: dark, width: wide.dark };
  // An emoji is a picture, not glass: it gets the dark outline alone (the Stage grows an emoji's halos
  // from the picture's alpha, faint corner pixels and all, and one halo shows them least).
  if (isEmoji(w.text, w.box.font)) return [wordUnit(c, w, { m, z, clip: win, active, fg: [], layers: [edge, { kind: "fill", color: dark }] })];
  const capH = c.layout.capH * (u / c.layout.size);
  const base = c.cy + w.box.y + HEAD * capH;
  const height = capH * (1 + 2 * HEAD);
  const level = clamp(f.level);
  const head = mix(f.colour, f.light, fx.foamTint);
  let inside: Layer = { kind: "fill", color: dark };
  if (level > 0.001) {
    const rad = (f.tip * Math.PI) / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    const mid: [number, number] = [w.cx, base - height / 2];
    const half = (w.box.w / 2) * Math.abs(sin) + (height / 2) * cos + 2; // the letters' reach along the axis
    const at = (y: number) => clamp(0.5 + ((y - mid[1]) * cos) / (2 * half)); // a height on the word's centre line, on the axis
    const surface = base - level * height;
    const s1 = at(surface);
    const s0 = Math.min(s1, at(surface - Math.min(fx.foam, level) * height));
    const grad: Grad = {
      x1: mid[0] + sin * half,
      y1: mid[1] - cos * half,
      x2: mid[0] - sin * half,
      y2: mid[1] + cos * half,
      stops: [
        { o: 0, c: dark },
        { o: s0, c: dark },
        { o: s0, c: head },
        { o: s1, c: head },
        { o: s1, c: f.colour },
        { o: 1, c: f.colour },
      ],
    };
    inside = { kind: "fill", color: f.colour, grad };
  }
  // where the two parts meet on screen: from under the outline (empty) to over it (full), on a whole pixel
  const meet = Math.round(apply(m, w.cx, lerp(base + wide.dark + 1, base - height - wide.dark - 1, level))[1]);
  const top = win.y;
  const bottom = win.y + win.h;
  const cut = Math.min(bottom, Math.max(top, meet));
  return [
    wordUnit(c, w, { m, z, active, clip: { ...win, y: cut, h: bottom - cut }, fg: [f.colour, head], layers: [edge, inside] }),
    wordUnit(c, w, {
      key: "e",
      m,
      z,
      active,
      clip: { ...win, h: cut - top },
      fg: [],
      layers: [edge, { kind: "stroke", color: f.light, width: wide.light }, inside],
    }),
  ];
}

/** The slits a block's lines slide through: its own box, `pad` wider, as tall as the caption box. */
function slits(c: Ctx | LabelCtx, pad: number): Rect {
  const l = Math.max(c.region.x0, c.cx + c.layout.left - pad);
  const r = Math.min(c.region.x1, c.cx + c.layout.right + pad);
  return { x: l, y: c.region.y0, w: r - l, h: c.region.y1 - c.region.y0 };
}

/**
 * Sideways offset of line `li`: in from one side (odd lines from the other), on and out the far side at
 * the exit. It starts behind its edge with only its outline showing, and leaves until only its outline
 * is left at the other edge: it is never on screen with nothing to see.
 */
function slide(c: Ctx | LabelCtx, fx: FX, li: number, win: Rect, enterS: number, staggerS: number): number {
  const words = c.words.filter((w) => w.box.line === li);
  // 1: travels to the right. The first line comes in from the right, so the word that is spoken first leads.
  const dir = li % 2 === 0 ? -1 : 1;
  const l = Math.min(...words.map((w) => w.cx - w.box.w / 2)); // the ink of the line
  const r = Math.max(...words.map((w) => w.cx + w.box.w / 2));
  const hidden = dir > 0 ? win.x - r : win.x + win.w - l;
  const gone = dir > 0 ? win.x + win.w - l : win.x - r;
  const p = back(fx.enterOvershoot)(prog(c.t, li * staggerS, enterS));
  return hidden * (1 - p) + gone * E.inCubic(prog(c.out, fx.slideFrom, 1 - fx.slideFrom));
}

/** The window words are seen through: the slits while a line is sliding, else the whole caption box. */
const windowOf = (c: Ctx | LabelCtx, sliding: boolean, slit: Rect): Rect =>
  sliding ? slit : { x: c.region.x0, y: c.region.y0, w: c.region.x1 - c.region.x0, h: c.region.y1 - c.region.y0 };

export default defineTemplate<P>({
  name: "hollow",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const slit = slits(c, fx.slitPad * c.layout.size);
    const drained = 1 - E.inQuad(prog(c.out, 0, fx.drainShare));
    const win = windowOf(c, t < P.timing.enterS + P.timing.staggerS * (c.layout.lines.length - 1) || c.out > 0, slit);
    const units = c.words.flatMap((w) => {
      const u = w.box.size;
      const dx = slide(c, fx, w.box.line, slit, P.timing.enterS, P.timing.staggerS);
      const a = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot));
      // the liquid rises while the word is spoken, in the word's own time when that is short
      const dur = Math.max(fx.fillMinS, Math.min(fx.fillS, w.end - w.start));
      const risen = w.emph ? spring(w.since, fx.emphFreq, fx.emphDamp) : back(fx.fillOvershoot)(E.inOutQuad(prog(w.since, 0, dur)));
      const fill: Fill = {
        level: Math.min(1, fx.level * risen) * drained,
        tip: tip(w.since, w.emph ? fx.emphSloshDeg : fx.sloshDeg, fx.sloshHz, fx.sloshS) + tip(t - c.exitT, fx.sloshDeg, fx.sloshHz, c.exit * 2),
        colour: w.emph ? colors.emph : colors.active,
        light: colors.text,
      };
      const m = pose([w.cx, w.cy], {
        x: dx,
        y: -(fx.activeLift * a + (fx.fillLift * fill.level) / fx.level) * u,
        scale: 1 + (fx.activeScale - 1) * a,
        rot: -fx.rock * fill.tip,
      });
      return glass(c, w, fx, fill, m, w.phase === "active" ? 1 : 0, win);
    });
    return { units, shapes: [] };
  },

  label: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const slit = slits(c, fx.slitPad * c.layout.size);
    const drained = 1 - E.inQuad(prog(c.out, 0, fx.drainShare));
    const win = windowOf(c, t < P.label.enterS + P.timing.staggerS * (c.layout.lines.length - 1) || c.out > 0, slit);
    const units = c.words.flatMap((w) => {
      const u = w.box.size;
      const dx = slide(c, fx, w.box.line, slit, P.label.enterS, P.timing.staggerS);
      // a label is poured as it arrives, in its kind's colour, and slops like a shouted word
      const since = t - P.label.enterS / 2;
      const fill: Fill = {
        level: Math.min(1, fx.level * spring(since, fx.emphFreq, fx.emphDamp)) * drained,
        tip: tip(since, fx.emphSloshDeg, fx.sloshHz, fx.sloshS) + tip(t - c.exitT, fx.sloshDeg, fx.sloshHz, c.exit * 2),
        colour: colors.text,
        light: apart(colors.text, colors.plateText) ? colors.plateText : colors.accent,
      };
      const m = pose([w.cx, w.cy], { x: dx, y: (-fx.fillLift * fill.level * u) / fx.level, rot: -fx.rock * fill.tip });
      return glass(c, w, fx, fill, m, 5, win, true);
    });
    return { units, shapes: [] };
  },
});
