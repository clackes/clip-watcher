import { z } from "zod";
import { apply, chain, pose, type Affine } from "../core/affine";
import { mix } from "../core/color";
import { back, clamp, E, hash01, hashSigned, lerp, prog, pulse, spring } from "../core/ease";
import { outlineFont } from "../core/fonts";
import { fontMetrics } from "../core/measure";
import { paramsSchema, type FontSpec } from "../core/schema";
import { letterUnits, rect, type Ctx, type LabelCtx, type Rect, type Scene, type Shape, type Unit, type WordState } from "../core/scene";
import { blockCentre, defineTemplate } from "../core/template";

/**
 * RANSOM: a ransom note. Every letter sits on its own scrap of paper, cut to that letter, turned a few
 * degrees and nudged off the line; the scraps are slapped on one after another. The spoken word's scraps
 * lift off the page, straighten into a neat row and turn highlighter colour; shouted words are cut bigger,
 * all in the emphasis colour, and rattle. At the end the scraps let go one by one and fall off, turning.
 *
 * Contrast: a letter is only ever `plateText` on a light scrap or `text` on a dark one, and neither colour
 * is used for a scrap, its rim or its shadow. Around every letter there is `halo` of clear paper (drawn
 * again as an outline in the paper's colour, so the frame checks measure the letter against its scrap).
 */
const schema = paramsSchema({
  pad: z.number().min(0.04).max(0.3), // paper around the ink of a letter, em
  rim: z.number().min(0.01).max(0.12), // dark line around a scrap, em (half of it lies on the paper)
  halo: z.number().min(0.02).max(0.2), // clear paper around the ink that the letter is measured against, em
  minW: z.number().min(0.1).max(0.8), // narrowest scrap (! and '), em
  ragged: z.number().min(0).max(1), // 0 = every scrap as tall as a capital, 1 = small letters may be cut down to their own height
  tiltDeg: z.number().min(0).max(12), // a scrap is turned up to this far at rest
  nudge: z.number().min(0).max(0.15), // and sits up to this far above or below the line, em
  stepS: z.number().min(0.005).max(0.2), // seconds between two scraps slapping on
  slapScale: z.number().min(0.5).max(1), // a scrap arrives at this scale
  slapFreq: z.number().min(1).max(6), // and settles on this spring: Hz
  slapDamp: z.number().min(0.3).max(1), // damping ratio (the overshoot)
  slapTurnDeg: z.number().min(0).max(30), // it arrives turned this much further
  slapTurnS: z.number().min(0.04).max(0.6), // and straightens to its resting tilt in this time
  slapDrop: z.number().min(0).max(0.4), // it comes down onto the page from this far above, em
  lift: z.number().min(0).max(0.3), // the spoken word's scraps rise, em
  straighten: z.number().min(0).max(1), // share of their tilt and nudge they lose while spoken
  activeScale: z.number().min(1).max(1.2), // and they grow to this
  activeOvershoot: z.number().min(0).max(4), // overshoot of the lift
  shadowX: z.number().min(0).max(0.2), // paper shadow at rest, em
  shadowY: z.number().min(0).max(0.2),
  shadowLift: z.number().min(0).max(0.2), // the shadow moves this much further away under a lifted scrap, em
  shadowAlpha: z.number().min(0).max(1),
  emphShakeDeg: z.number().min(0).max(15), // shouted scraps rattle by this much when the word starts
  emphShakeS: z.number().min(0).max(1),
  emphShakeHz: z.number().min(1).max(6),
  emphPale: z.number().min(0).max(0.8), // every other scrap of a shouted word is this far towards the plain paper colour
  fallTurnDeg: z.number().min(0).max(90), // a scrap turns this far as it falls off
  fallShrink: z.number().min(0.2).max(1), // and is this small when it is gone
  fallSpread: z.number().min(0).max(0.6), // share of the exit over which the scraps of a line let go one after another
  fallLineLag: z.number().min(0).max(0.3), // an upper line lets go this share of the exit later than the line under it
});
type P = z.infer<typeof schema>;

type Ink = { x1: number; x2: number; y1: number; y2: number }; // em, y up from the baseline
const inks = new Map<string, Ink | null>();

/** Ink box of one character in em (null for an emoji: it has no outline in the caption font). */
function inkOf(font: FontSpec, ch: string): Ink | null {
  const key = `${font.file}|${ch}`;
  let v = inks.get(key);
  if (v === undefined) {
    const f = outlineFont(font);
    const g = f.charToGlyph(ch) as unknown as { index: number; getBoundingBox(): Ink };
    const b = g.index === 0 ? null : g.getBoundingBox();
    const k = f.unitsPerEm;
    v = b && b.x2 > b.x1 && b.y2 > b.y1 ? { x1: b.x1 / k, x2: b.x2 / k, y1: b.y1 / k, y2: b.y2 / k } : null;
    inks.set(key, v);
  }
  return v;
}

// the picture of a system emoji in its em box: how tall it is and where its middle sits above the baseline, em
const EMOJI_H = 1.03;
const EMOJI_MID = 0.35;

type Paper = { plate: string; ink: string };
type Opts = {
  paper: (w: WordState, j: number) => Paper; // colours of letter j of the page
  lift: (w: WordState) => number; // 0..1 how far the word is lifted off the page
  block: Affine; // transform of the whole note
};

function note(c: Ctx<P> | LabelCtx<P>, o: Opts): Scene {
  const { P, t, colors } = c;
  const fx = P.fx;
  const tag = "label" in c ? `l${c.label.id}` : `p${c.page.id}`;
  const capEm = c.layout.capH / c.layout.size;
  const xEm = fontMetrics(P.font).xH;
  const counts = c.words.map((w) => w.box.glyphs.filter((g) => g.ch.trim()).length);
  const total = counts.reduce((a, b) => a + b, 0);
  const step = Math.min(fx.stepS, P.timing.enterS / Math.max(1, total));
  const lastLine = c.layout.lines.length - 1;
  const lagMax = (lastLine > 0 ? fx.fallLineLag : 0) + fx.fallSpread;
  const window: Rect = { x: c.region.x0, y: c.region.y0, w: c.region.x1 - c.region.x0, h: c.region.y1 - c.region.y0 };
  const clip = c.out > 0 ? window : undefined;
  const units: Unit[] = [];
  const shapes: Shape[] = [];
  let before = 0;
  for (const w of c.words) {
    const first = before;
    before += counts[w.index];
    const u = w.box.size; // one em of this word: a shouted word has bigger scraps
    const pad = fx.pad * u;
    const rim = fx.rim * u;
    const halo = Math.min(fx.halo * u, pad - rim / 2);
    const base = c.cy + w.box.y;
    const a = o.lift(w);
    const t0 = Math.min(first * step, Math.max(0, w.start)); // on the page by the time it is spoken
    const shift = (P.letterSpacing * u) / 2; // the spacing after the last letter is not part of the word
    const fallLag = lastLine > 0 ? (fx.fallLineLag * (lastLine - w.box.line)) / lastLine : 0;
    let n = 0;
    units.push(
      ...letterUnits(c, w, (g, k) => {
        const j = first + n;
        const s = t - (t0 + n * step);
        n += 1;
        if (s < 0) return null;
        const seed = `${tag}:${w.index}:${k}`;
        // the scrap: cut around the letter's own ink
        const x0 = c.cx + w.box.x + g.x;
        const ink = inkOf(w.box.font, g.ch);
        let l: number;
        let r: number;
        let top: number;
        let bottom: number;
        const cap = capEm * u;
        let fit: Affine | null = null; // an emoji is a picture: it is scaled to sit on a scrap as tall as a capital's
        if (ink) {
          const tall = Math.max(cap, ink.y2 * u);
          const low = Math.max(ink.y2 * u, xEm * u); // a small letter may be cut down to the x-height, never lower
          const small = ink.y2 < (xEm + capEm) / 2;
          l = x0 + ink.x1 * u - pad;
          r = x0 + ink.x2 * u + pad;
          top = base - (small ? lerp(tall, low, fx.ragged * hash01(`${seed}h`)) : tall) - pad;
          bottom = base + Math.max(0, -ink.y1 * u) + pad;
        } else {
          const adv = g.w - P.letterSpacing * u;
          const k = (cap + pad) / (EMOJI_H * u);
          const mid = x0 + adv / 2;
          l = mid - (adv * k) / 2 - pad / 2;
          r = mid + (adv * k) / 2 + pad / 2;
          top = base - cap - pad;
          bottom = base + pad;
          fit = pose([mid, base - EMOJI_MID * u], { scale: k, y: (top + bottom) / 2 - (base - EMOJI_MID * u) });
        }
        if (r - l < fx.minW * u) {
          const mid = (l + r) / 2;
          l = mid - (fx.minW * u) / 2;
          r = mid + (fx.minW * u) / 2;
        }
        const pivot: [number, number] = [(l + r) / 2, (top + bottom) / 2];

        // at rest: turned and nudged; spoken: lifted, straight, a little bigger
        const loose = 1 - fx.straighten * clamp(a);
        const side = hash01(`${seed}s`) < 0.5 ? -1 : 1;
        const pop = spring(s, fx.slapFreq, fx.slapDamp);
        const arrive = 1 - E.outCubic(prog(s, 0, fx.slapTurnS));
        let scale = lerp(fx.slapScale, 1, pop) * (1 + (fx.activeScale - 1) * a);
        let rot = hashSigned(`${seed}r`) * fx.tiltDeg * loose + side * fx.slapTurnDeg * arrive;
        let y = hashSigned(`${seed}y`) * fx.nudge * u * loose - fx.lift * u * a - fx.slapDrop * u * arrive;
        if (w.emph && w.since > 0 && w.since < fx.emphShakeS) {
          // a rattle that swells in and dies away, every scrap at its own phase
          const life = w.since / fx.emphShakeS;
          const swell = Math.sin(Math.PI * Math.sqrt(life)) * (1 - life);
          rot += fx.emphShakeDeg * swell * Math.sin(2 * Math.PI * (fx.emphShakeHz * w.since + hash01(`${seed}p`)));
        }
        // the exit: lets go, falls, turns, shrinks, out through the bottom of the box
        let gone = 0;
        if (c.out > 0) {
          const lag = fallLag + fx.fallSpread * hash01(`${seed}f`);
          gone = clamp((c.out - lag) / Math.max(0.05, 1 - lagMax));
          if (gone >= 1) return null;
          y += (c.region.y1 - top + rim + 2) * E.inQuad(gone);
          rot += side * fx.fallTurnDeg * gone * gone;
          scale *= lerp(1, fx.fallShrink, gone);
        }
        const m = chain(pose(pivot, { scale, rot, x: shift, y }), o.block);
        if (clip) {
          // the highest corner of the paper has left the box: it is gone (never drawn without a pixel to show)
          const ys = [apply(m, l, top), apply(m, r, top), apply(m, l, bottom), apply(m, r, bottom)];
          if (Math.min(...ys.map((p) => p[1])) + 1 >= c.region.y1) return null;
        }
        const paper = ink ? o.paper(w, j) : { plate: colors.plate, ink: colors.text }; // an emoji always on dark paper
        const sx = (fx.shadowX + fx.shadowLift * clamp(a)) * u;
        const sy = (fx.shadowY + fx.shadowLift * clamp(a)) * u;
        shapes.push(
          rect(`${tag}w${w.index}c${k}h`, l, top, r - l, bottom - top, {
            role: "decor",
            fill: colors.shadow,
            alpha: fx.shadowAlpha * (1 - gone),
            m: chain(pose(pivot, { scale, rot, x: shift + sx, y: y + sy }), o.block),
            z: -1,
            clip,
          }),
          rect(`${tag}w${w.index}c${k}s`, l, top, r - l, bottom - top, {
            fill: paper.plate,
            stroke: colors.stroke,
            strokeWidth: rim,
            m,
            z: 0,
            clip,
          }),
        );
        if (fit) return { m: chain(fit, m), z: 1, clip, fg: [paper.ink], layers: [{ kind: "fill", color: paper.ink }] };
        return {
          m,
          z: 1,
          clip,
          fg: [paper.ink],
          layers: [
            { kind: "stroke", color: paper.plate, width: halo },
            { kind: "fill", color: paper.ink },
          ],
        };
      }),
    );
  }
  return { units, shapes };
}

export default defineTemplate<P>({
  name: "ransom",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const cycle = colors.cycle;
    return note(c, {
      block: pose(blockCentre(c), {}),
      lift: (w) => pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS, back(fx.activeOvershoot)),
      paper: (w, j) => {
        const lit = clamp(w.a);
        const q = j + c.index; // the pattern starts somewhere else on every page
        if (w.emph) return { plate: q % 2 === 0 ? colors.emph : mix(colors.emph, colors.accent, fx.emphPale), ink: colors.plateText };
        if (q % 2 === 0) return { plate: mix(cycle[(q / 2) % cycle.length], colors.active, lit), ink: colors.plateText };
        if (((q - 1) / 2) % 2 === 0) return { plate: mix(colors.accent, colors.active, lit), ink: colors.plateText };
        return { plate: colors.plate, ink: colors.text };
      },
    });
  },
  // the label: the same note, all cut from the label's colour, tilted as one
  label: (c): Scene =>
    note(c, {
      block: pose(blockCentre(c), { rot: c.P.label.rotate }),
      lift: () => 0,
      paper: () => ({ plate: c.colors.text, ink: c.colors.plateText }),
    }),
});
