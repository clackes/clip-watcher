import { z } from "zod";
import { chain, pose, translate, type Affine } from "../core/affine";
import { mix } from "../core/color";
import { clamp, E, lerp, prog, pulse } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { fontMetrics } from "../core/measure";
import { paramsSchema } from "../core/schema";
import { rect, wordUnit, type Ctx, type LabelCtx, type Layer, type Rect, type Scene, type Shape, type Unit, type WordState } from "../core/scene";
import { defineTemplate, em, wordCentre } from "../core/template";

/**
 * VOGUE: editorial. A high-contrast serif in cream on a soft dark outline. Each line rises out of its own
 * mask, one after the other, word by word; a thin underline draws itself under the spoken word and glides
 * to the next; shouted words are italic, larger and in the accent colour. Lines sink back into their masks
 * to leave. Nothing bounces: every move is one long deceleration.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.2), // dark outline around the letters, em
  weight: z.number().min(0).max(0.04), // extra weight on the letters (a stroke in their own colour), em
  hairPx: z.number().min(0).max(2), // and never less than this many pixels, so hairlines hold at small sizes
  soft: z.number().min(0).max(0.4), // soft shadow: how far it reaches past the letters, em
  softAlpha: z.number().min(0).max(0.3), // opacity of each of its steps
  softSteps: z.number().int().min(1).max(8), // steps it fades out in
  softY: z.number().min(0).max(0.2), // it sits this much lower than the letters, em
  lineLagS: z.number().min(0).max(0.5), // each line starts rising this long after the one above
  wordLagS: z.number().min(0).max(0.2), // each word of a line this long after the word before
  sinkLag: z.number().min(0).max(0.6), // exit: the last line sinks first, the first one waits this share of the exit
  lineY: z.number().min(0).max(0.4), // underline: its top, below the baseline, em of the word
  lineH: z.number().min(0.02).max(0.15), // its thickness, em
  lineRim: z.number().min(0.07).max(0.2), // dark rim around it, em: a full outline, it is drawn like a letter
  lineInset: z.number().min(-0.2).max(0.2), // it stops this far short of each end of the word, em
  lineTaper: z.number().min(0.05).max(1), // shorter than this it thins towards nothing, em
  calmS: z.number().min(0.1).max(1), // a word spoken at least this long gets the timings below in full; a shorter one speeds them up
  paceMin: z.number().min(0.2).max(1), // but never to less than this share (fast talk must not leave two words marked at once)
  drawS: z.number().min(0.05).max(0.8), // it draws itself under the first word in this time
  glideS: z.number().min(0.05).max(0.8), // and takes this long to glide to the next word
  stretch: z.number().min(0).max(1), // 0 = glides stiffly, 1 = its leading end arrives well before the trailing one
  undrawS: z.number().min(0.05).max(0.8), // after the last word it runs out to the right in this time
  emphLine: z.number().min(1).max(3), // thickness factor under a shouted word
  emphSwell: z.number().min(1).max(1.2), // a shouted word swells to this while it is spoken
  swellS: z.number().min(0.05).max(0.8), // and takes this long to swell and to settle
  ruleGap: z.number().min(0).max(0.5), // label: hairlines above and below, this far from the letters, em
  ruleSpan: z.number().min(0.2).max(1.5), // their length, as a share of the label's width
  ruleDrawS: z.number().min(0.05).max(1), // they grow from the centre in this time
});
type P = z.infer<typeof schema>;
type C = Ctx<P> | LabelCtx<P>;

/** Room the outline, the soft shadow and a swelling word need around a line, px. */
const reach = (c: C): number => {
  const fx = c.P.fx;
  return (fx.halo + fx.weight + fx.soft + fx.softY + (fx.emphSwell - 1)) * em(c) + fx.hairPx + 2;
};

/** The mask of each line: as wide as the caption box, as tall as the line with everything around it. */
const masks = (c: C): Rect[] =>
  c.layout.lines.map((L) => {
    const top = Math.max(c.region.y0, c.cy + L.top - reach(c));
    const bottom = Math.min(c.region.y1, c.cy + L.bottom + reach(c));
    return { x: c.region.x0, y: top, w: c.region.x1 - c.region.x0, h: bottom - top };
  });

/** Height of a word's ink above the baseline, em: x-height for a word without tall letters. */
function tall(w: WordState): number {
  const fm = fontMetrics(w.box.font);
  if (isEmoji(w.text, w.box.font)) return Math.max(fm.asc, 0.95);
  return /^[acemnorsuvwxz]+$/.test(w.text) ? fm.xH : Math.max(fm.capH, fm.asc);
}

/**
 * How far below its place a word of `line` (the k-th of that line) is, and whether it is on the move.
 * It starts just hidden: the top of its outline and shadow at the bottom edge of its line's mask, so it
 * shows from the first instant it moves and not before. It leaves the same way: the last line first.
 */
function travel(c: C, mask: Rect[], w: WordState, line: number, k: number, enterS: number): { y: number; moving: boolean } {
  const fx = c.P.fx;
  const n = mask.length;
  const u = em(c);
  const above = Math.max(fx.halo * u + Math.max(fx.weight * u, fx.hairPx), (fx.soft - fx.softY) * u); // outline and shadow over the letters
  const dist = mask[line].y + mask[line].h - (c.cy + w.box.y - tall(w) * w.box.size - above);
  const t0 = line * fx.lineLagS + k * fx.wordLagS;
  const up = E.outExpo(prog(c.t, t0, enterS));
  const wait = n > 1 ? (fx.sinkLag * (n - 1 - line)) / (n - 1) : 0;
  const down = c.exit > 0 ? E.inCubic(clamp((c.out - wait) / (1 - (n > 1 ? fx.sinkLag : 0)))) : 0;
  return { y: (1 - up + down) * dist, moving: c.t < t0 + enterS || c.out > 0 };
}

/**
 * A word: its letters on their outline, and under them, as a unit of its own, its soft shadow. The
 * shadow is a separate unit so that the underline can lie between the two: on the shadows, under the
 * outlines (a descender's outline then interrupts the underline, the shadows never shade it). The shadow
 * unit's own letters are in the outline colour and stay hidden under the outline; an emoji has no shadow.
 */
function word(c: C, w: WordState, o: { m: Affine; clip?: Rect; fill: string; z: number; active?: boolean }): Unit[] {
  const fx = c.P.fx;
  const u = em(c);
  if (isEmoji(w.text, w.box.font)) {
    const layers: Layer[] = [{ kind: "stroke", color: c.colors.stroke, width: fx.halo * u }, { kind: "fill", color: o.fill }];
    return [wordUnit(c, w, { m: o.m, z: o.z, clip: o.clip, active: o.active, fg: [o.fill], layers })];
  }
  // the hairlines of a display serif are thinner than a pixel at caption sizes: the letters get a little
  // weight in their own colour, never less than `hairPx`, and the outline starts outside that
  const bold = Math.max(fx.weight * u, fx.hairPx);
  const top: Layer[] = [
    { kind: "stroke", color: c.colors.stroke, width: fx.halo * u + bold },
    { kind: "stroke", color: o.fill, width: bold },
    { kind: "fill", color: o.fill },
  ];
  const main = wordUnit(c, w, { m: o.m, z: o.z, clip: o.clip, active: o.active, fg: [o.fill], layers: top });
  if (fx.soft <= 0 || fx.softAlpha <= 0) return [main];
  const under: Layer[] = [
    { kind: "glow", color: c.colors.shadow, width: fx.soft * u, alpha: fx.softAlpha, steps: fx.softSteps, dy: fx.softY * u },
    { kind: "fill", color: c.colors.stroke },
  ];
  return [wordUnit(c, w, { m: o.m, z: o.z - 2, clip: o.clip, active: o.active, key: "s", fg: [], layers: under }), main];
}

/**
 * A thin bar with a dark rim: `l`..`r` and `y`..`y + h` are the coloured part. A bar shorter than
 * `lineTaper` thins towards nothing (the rim over the last half of that), so it starts and ends as a
 * stroke of a pen and never as a dot.
 */
function bar(c: C, id: string, l: number, r: number, y: number, h: number, o: { dy: number; clip?: Rect; z: number; fill: string }): Shape | null {
  if (r - l < 0.5) return null;
  const fx = c.P.fx;
  const k = clamp((r - l) / (fx.lineTaper * em(c)));
  const rim = fx.lineRim * em(c) * clamp(2 * k);
  const hk = h * k;
  return rect(id, l - rim / 2, y + (h - hk) / 2 - rim / 2, r - l + rim, hk + rim, {
    role: "decor",
    fill: o.fill,
    stroke: c.colors.stroke,
    strokeWidth: rim,
    r: (hk + rim) / 2,
    m: translate(0, o.dy),
    clip: o.clip,
    z: o.z,
  });
}

type Bar = { l: number; r: number; y: number; h: number; line: number };

export default defineTemplate<P>({
  name: "vogue",
  schema,
  build: (c): Scene => {
    const { P, t, colors } = c;
    const fx = P.fx;
    const u = em(c);
    const mask = masks(c);
    const place = new Map<number, number>(); // word -> its position in its line
    c.layout.lines.forEach((L) => L.words.forEach((wi, k) => place.set(wi, k)));
    const move = (i: number) => travel(c, mask, c.words[i], c.words[i].box.line, place.get(i) ?? 0, P.timing.enterS);
    // how much of the calm timings a word has time for
    const pace = (w: WordState): number => clamp((w.activeEnd - w.start) / fx.calmS, fx.paceMin, 1);

    const units = c.words.flatMap((w) => {
      const mv = move(w.index);
      const next = c.words[Math.min(w.index + 1, c.words.length - 1)];
      // (the colour comes in its usual time, which a frame rate can show; only its way out is hurried)
      const lit = pulse(t, w.start, w.activeEnd, P.timing.activeInS, P.timing.activeOutS * pace(next), E.outCubic);
      const fill = w.emph ? colors.emph : mix(colors.text, colors.active, clamp(lit));
      // (not an emoji: a picture is drawn on whole pixels and would step as it swells)
      const swell = w.emph && !isEmoji(w.text, w.box.font) ? pulse(t, w.start, w.activeEnd, fx.swellS, fx.swellS, E.inOutSine) : 0;
      const own = pose(wordCentre(w), { scale: 1 + (fx.emphSwell - 1) * swell });
      return word(c, w, { m: chain(own, translate(0, mv.y)), clip: mv.moving ? mask[w.box.line] : undefined, fill, z: 0 });
    });

    // the underline: where it rests under word i
    const under = (i: number): Bar => {
      const w = c.words[i];
      return {
        l: c.cx + w.box.x + fx.lineInset * u,
        r: c.cx + w.box.x + w.box.w - fx.lineInset * u,
        y: c.cy + w.box.y + fx.lineY * w.box.size,
        h: fx.lineH * u * (w.emph ? fx.emphLine : 1),
        line: w.box.line,
      };
    };
    // ...and where it is at `time` while word i is the spoken one: it leaves from wherever it had got to
    const at = (i: number, time: number): Bar => {
      const to = under(i);
      const w = c.words[i];
      const from = i > 0 ? at(i - 1, w.start) : null;
      if (!from || from.line !== to.line) {
        return { ...to, r: lerp(to.l, to.r, E.outCubic(prog(time, w.start, (i > 0 ? fx.glideS : fx.drawS) * pace(w)))) };
      }
      const p = prog(time, w.start, fx.glideS * pace(w));
      const lead = E.outCubic(Math.min(1, p * (1 + fx.stretch)));
      const trail = E.inOutCubic(p);
      return { l: lerp(from.l, to.l, trail), r: lerp(from.r, to.r, lead), y: lerp(from.y, to.y, trail), h: lerp(from.h, to.h, trail), line: to.line };
    };
    let k = -1;
    for (const w of c.words) if (t >= w.start) k = w.index;
    const shapes: Shape[] = [];
    if (k >= 0) {
      const last = c.words[c.words.length - 1];
      const now = at(k, t);
      const out = E.inOutCubic(prog(t, last.activeEnd, fx.undrawS)); // the last word is said: it runs out
      const mv = move(k);
      const clip = mv.moving ? mask[now.line] : undefined;
      const s = bar(c, `p${c.page.id}ul`, lerp(now.l, now.r, out), now.r, now.y, now.h, { dy: mv.y, clip, z: -1, fill: colors.accent });
      if (s) shapes.push(s);
      if (k > 0 && c.words[k - 1].box.line !== now.line) {
        // the line above keeps its underline for a moment: it runs out to the right as the new one is drawn
        const old = at(k - 1, c.words[k].start);
        const mo = move(k - 1);
        const p = E.inOutCubic(prog(t, c.words[k].start, fx.glideS * pace(c.words[k])));
        const b = bar(c, `p${c.page.id}ulo`, lerp(old.l, old.r, p), old.r, old.y, old.h, { dy: mo.y, clip: mo.moving ? mask[old.line] : undefined, z: -1, fill: colors.accent });
        if (b) shapes.push(b);
      }
    }
    return { units, shapes };
  },
  label: (c): Scene => {
    const fx = c.P.fx;
    const u = em(c);
    const mask = masks(c);
    const units = c.layout.lines.flatMap((L, li) =>
      L.words.flatMap((wi, k) => {
        const w = c.words[wi];
        const mv = travel(c, mask, w, li, k, c.P.label.enterS);
        return word(c, w, { m: translate(0, mv.y), clip: mv.moving ? mask[li] : undefined, fill: c.colors.text, z: 5, active: true });
      }),
    );
    // a hairline above and one below in the label's own colour, growing from the centre and closing again
    const lines = c.layout.lines;
    const half = ((c.layout.right - c.layout.left) / 2) * fx.ruleSpan * E.outQuart(prog(c.t, 0, fx.ruleDrawS)) * (1 - E.inCubic(c.out));
    const h = fx.lineH * u;
    const top = c.cy + lines[0].y - c.layout.capH - fx.ruleGap * u - h;
    const bottom = c.cy + lines[lines.length - 1].y + fx.ruleGap * u;
    const shapes = [
      bar(c, `l${c.label.id}ra`, c.cx - half, c.cx + half, top, h, { dy: 0, z: 4, fill: c.colors.text }),
      bar(c, `l${c.label.id}rb`, c.cx - half, c.cx + half, bottom, h, { dy: 0, z: 4, fill: c.colors.text }),
    ].filter((s): s is Shape => s !== null);
    return { units, shapes };
  },
});
