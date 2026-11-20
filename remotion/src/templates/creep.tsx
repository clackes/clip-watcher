import { z } from "zod";
import { chain, pose, translate } from "../core/affine";
import { mix } from "../core/color";
import { drift, E, hash01, lerp, prog, shake, spring } from "../core/ease";
import { isEmoji } from "../core/fonts";
import { paramsSchema } from "../core/schema";
import { wordUnit, type Ctx, type Grad, type LabelCtx, type Layer, type Scene, type Unit, type WordState } from "../core/scene";
import { defineTemplate } from "../core/template";

/**
 * CREEP: horror. Dripping caps in acid green on a black outline, with a sickly glow. Every letter oozes
 * down into place from its top edge, one after the other: a word arrives as a row of short stubs hanging
 * from their tops, and letter after letter runs down past its length and
 * settles. Nothing ever holds quite still (each letter wanders on its own). The spoken word turns a
 * hotter colour and its heart beats: lub-dub, the glow swelling with it. Shouted words are the emphasis
 * colour and shudder, letter against letter. Before a pause the letters melt: they stretch downwards and
 * sink through the floor under their line, in no particular order.
 *
 * A word is drawn once per letter, each time through a window as wide as that letter (a strip of the
 * caption box), and each strip moves on its own. So letters are animated one by one while the word
 * stays one piece of ink: the outline between two letters is always whole, and the glow is laid once.
 *
 * Glow and heartbeat take what room the caption box leaves around each word, up to their parameters.
 */
const schema = paramsSchema({
  halo: z.number().min(0.07).max(0.3), // black outline, em
  drip: z.number().min(0).max(0.2), // the font's drips hang this far under the baseline, em
  glow: z.number().min(0).max(0.4), // the glow reaches this far past the outline, em
  glowAlpha: z.number().min(0).max(0.3), // opacity of each of its layers
  glowSteps: z.number().int().min(1).max(12),
  sheen: z.number().min(0).max(1), // the top of the letters goes this far towards white (they look wet)
  sheenTo: z.number().min(0.05).max(1), // and is back to the plain colour at this share of the height
  oozeFrom: z.number().min(0.5).max(1), // a letter starts this tall, hanging from its top
  oozeFreq: z.number().min(0.5).max(4), // it runs down on a spring: Hz
  oozeDamp: z.number().min(0.2).max(1), // damping (how far it runs past its length)
  letterS: z.number().min(0).max(0.2), // each letter starts this long after the one before it
  arriveS: z.number().min(0).max(1), // but the last letter of a word starts no later than this
  arriveShare: z.number().min(0.1).max(1), // and every letter has started by this share of the page's time
  tremble: z.number().min(0).max(0.05), // every letter wanders up and down by this much, the word sideways: em
  trembleHz: z.number().min(0.2).max(4),
  activeTop: z.number().min(0).max(1), // the spoken word's top goes this much further towards white
  beat: z.number().min(0).max(0.3), // heartbeat: the spoken word swells by this share
  beat2: z.number().min(0).max(1), // the second beat, as a share of the first
  beatGapS: z.number().min(0.1).max(0.6), // from the first beat to the second
  beatEveryS: z.number().min(0.4).max(3), // from one heartbeat to the next
  beatUpS: z.number().min(0.07).max(0.3), // a beat swells over this long
  beatDownS: z.number().min(0.07).max(0.5), // and lets go over this long
  beatGlow: z.number().min(0).max(1.5), // the glow grows by this share with the beat
  beatTwistDeg: z.number().min(0).max(3), // and the word twists by this much, degrees
  shudder: z.number().min(0).max(0.1), // a shouted word's letters shake by this much when it is spoken, em
  shudderHz: z.number().min(1).max(5),
  shudderS: z.number().min(0.1).max(1.5),
  meltStretch: z.number().min(1).max(3), // exit: a letter stretches to this many times its height
  meltStagger: z.number().min(0).max(0.6), // letters start melting up to this share of the exit apart
});
type P = z.infer<typeof schema>;
type FX = P["fx"];

const SLACK = 1.5; // px kept free at the box edge (anti-aliasing)
const RAMP = 3; // segments the sheen gradient is drawn with
const EMOJI_PAST = 0.2; // an emoji picture stands this far out of the letters' cap-height box, em
const LEAST = 4.3; // px of outline a fill needs next to it at any size and scale: the frame checks read it against the 2 to 3 px around it, corners included
const JUDGED = 0.5; // the scale down to which a letter is still read (and measured)
const FIRST = 0.24; // share of a word's width its first strip is at least
const SMALL = 48; // px: text this small does not hang short at all (a heavy outline squashed that far breaks up inside small letters)
const FULL = 80; // px: from this size on letters hang as short as `oozeFrom` says

/**
 * The parameters for text of size `u` drawn at `scale` (a letter hanging short is that much thinner):
 * the outline is never under `LEAST` px on screen. In small text, and on a letter that has not run down
 * yet, it is that many px instead of its width in em.
 */
const thick = (fx: FX, u: number, scale = 1): FX => ({ ...fx, halo: Math.max(fx.halo, LEAST / (Math.max(JUDGED, Math.min(1, scale)) * u)) });

/** One heartbeat (0..1 and back) starting at x = 0 seconds. */
const thump = (x: number, up: number, down: number): number =>
  x <= 0 ? 0 : x < up ? E.outCubic(x / up) : x < up + down ? 1 - E.inOutQuad((x - up) / down) : 0;

/** Lub-dub, every `fx.beatEveryS`, as 0..1 from seconds since the word was first spoken. */
function heart(fx: FX, since: number): number {
  if (since <= 0) return 0;
  const x = since % fx.beatEveryS;
  return Math.max(thump(x, fx.beatUpS, fx.beatDownS), fx.beat2 * thump(x - fx.beatGapS, fx.beatUpS, fx.beatDownS));
}

/** How far a word's ink goes past its cap-height box, em: the font's drips, or an emoji picture's overhang. */
const pastOf = (w: WordState, fx: FX): number => (isEmoji(w.text, w.box.font) ? EMOJI_PAST : fx.drip);

/**
 * How much something of half-size (hx, hy) about (cx, cy) may swell with the heartbeat, and how far it
 * may glow (px, at rest), inside the caption box. `past`: em its ink reaches beyond that size.
 */
function roomFor(c: Ctx | LabelCtx, fx: FX, u: number, cx: number, cy: number, hx: number, hy: number, past: number): { beat: number; glow: number } {
  const rx = Math.min(cx - hx - c.region.x0, c.region.x1 - (cx + hx)) - SLACK;
  const ry = Math.min(cy - hy - c.region.y0, c.region.y1 - (cy + hy)) - SLACK;
  const fixed = (fx.halo + past + fx.tremble + fx.shudder) * u; // outline, drips, tremble and shudder: these stay
  const beat = Math.max(0, Math.min(fx.beat, (rx - fixed) / (hx + fixed), (ry - fixed) / (hy + fixed)));
  const spare = Math.min((hx + rx) / (1 + beat) - hx, (hy + ry) / (1 + beat) - hy) - fixed;
  return { beat, glow: Math.max(0, Math.min(fx.glow * u, spare / (1 + fx.beatGlow))) };
}

/**
 * How much faster than written the letters have to come so that all of them have started within
 * `fx.arriveShare` of the time the page (or label) is up: in fast speech a page lasts a quarter of a second.
 */
const hurry = (c: Ctx | LabelCtx, fx: FX): number =>
  Math.min(1, (fx.arriveShare * c.dur) / Math.max(1e-6, c.P.timing.staggerS * (c.words.length - 1) + fx.arriveS));

/**
 * Glow and heartbeat want room under the text, and a caption box usually has more of it over the text:
 * the block moves up by what is missing below, as far as the room above allows. Returns the context to
 * lay things out in (the box as seen from the unmoved block) and the move in px.
 */
function raised<C extends Ctx | LabelCtx>(c: C, fx: FX): { c: C; up: number } {
  const u = c.layout.size * Math.max(1, c.P.emphScale); // as for a shouted word, on every page: pages stand level
  const edge = (fx.halo + fx.drip + fx.tremble + fx.shudder + fx.glow * (1 + fx.beatGlow)) * u + SLACK;
  const want = edge * (1 + fx.beat) + (fx.beat * (c.layout.bottom - c.layout.top)) / 2;
  const below = c.region.y1 - (c.cy + c.layout.bottom);
  const above = c.cy + c.layout.top - c.region.y0;
  const up = Math.max(0, Math.min(want - below, above - want));
  return { c: { ...c, region: { ...c.region, y0: c.region.y0 + up, y1: c.region.y1 + up } }, up };
}

/** How one word looks and moves as a whole right now. */
type Look = {
  fill: string;
  top: string;
  glow: number; // px past the outline right now
  reach: number; // px the glow reaches at its largest
  beat: number; // share the word swells by at a full heartbeat
  swell: number; // 0..1 of that right now
  pivot: [number, number]; // what it swells about
  rattle: number; // 1: a shouted word, it shudders when spoken
};

/**
 * The letters of one word, each a strip of the whole word. `born`: when its first letter starts, seconds
 * on the page clock; `pace`: the share of the written delays that is used; `up`: how far the block is
 * raised (`c` is the context from `raised`).
 */
function ooze(c: Ctx | LabelCtx, w: WordState, fx: FX, born: number, pace: number, look: Look, up: number, z: number): Unit[] {
  const u = w.box.size;
  const capH = c.layout.capH * (u / c.layout.size);
  const base = c.cy + w.box.y;
  const top = base - capH;
  const line = c.layout.lines[w.box.line];
  const reach = (fx.halo + pastOf(w, fx)) * u + look.reach;
  const floor = Math.min(c.region.y1, c.cy + line.bottom + reach + SLACK); // under the glow of this line
  // A letter may run this long before it leaves the box: outline and glow stretch with it, and the
  // heartbeat swells the word about its middle on top of that.
  const lowest = look.pivot[1] + (c.region.y1 - SLACK - look.pivot[1]) / (1 + look.beat);
  const longest = Math.max(1, (lowest - top - fx.tremble * u) / (capH + reach));
  // the sheen: from the wet top colour down to the plain one, in steps the frame checks can follow
  const fg = Array.from({ length: RAMP + 1 }, (_, k) => mix(look.top, look.fill, k / RAMP));
  const grad: Grad = { x1: 0, y1: top, x2: 0, y2: lerp(top, base, fx.sheenTo), stops: fg.map((col, k) => ({ o: k / RAMP, c: col })) };
  const label = "label" in c;
  const id = `${label ? `l${(c as LabelCtx).label.id}` : `p${(c as Ctx).page.id}`}w${w.index}`;
  const glyphs = w.box.glyphs;
  const each = Math.min(fx.letterS, fx.arriveS / Math.max(1, glyphs.length - 1)) * pace;
  // the word as a whole: its heartbeat, and a wander (and a shouted word's shake) from side to side
  const scale = 1 + look.beat * look.swell;
  const sway = fx.tremble * u * drift(c.T, fx.trembleHz, `${id}x`) + look.rattle * fx.shudder * u * shake(w.since, fx.shudderS, fx.shudderHz);
  const whole = chain(pose(look.pivot, { scale, x: sway, rot: fx.beatTwistDeg * look.swell }), translate(0, -up));
  const from = lerp(1, fx.oozeFrom, Math.min(1, Math.max(0, (u - SMALL) / (FULL - SMALL)))); // how short a letter of this size hangs
  // where a place on the resting word is on screen, across: the strips are cut there, on whole pixels
  const left = c.cx + w.box.x;
  const across = (x: number) => Math.round(look.pivot[0] + (x - look.pivot[0]) * scale + sway);
  const units: Unit[] = [];
  if (c.t < born) return units;
  // The strips start at these letters: one a letter, but the first letters of a long word share one,
  // FIRST of the word wide. One piece of every word is then more than a fifth of it, which is what the
  // frame checks count a word as shown by.
  const starts = glyphs.map((_, k) => k).filter((k) => k === 0 || glyphs[k].x >= FIRST * w.box.w);
  starts.forEach((k, n) => {
    const g = glyphs[k];
    const seed = `${id}c${k}`;
    // the whole word is there at once, every letter hanging short; then they run down one after the other
    const run = spring(c.t - born - n * each, fx.oozeFreq, fx.oozeDamp);
    // exit: melting, each letter in its own time
    const melt = E.inQuad(prog(c.out, hash01(`${seed}m`) * fx.meltStagger, 1 - fx.meltStagger));
    const tall = Math.min(longest, lerp(from, 1, run));
    const halo = thick(fx, u, tall).halo * u; // never under its least px on screen, however short the letter hangs
    const layers: Layer[] = [
      { kind: "glow", color: look.fill, width: halo + look.glow, alpha: fx.glowAlpha, steps: fx.glowSteps },
      { kind: "stroke", color: c.colors.stroke, width: halo },
      { kind: "fill", color: look.top, grad },
    ];
    const own = pose([w.cx, top], {
      sy: tall * (1 + (fx.meltStretch - 1) * melt),
      y:
        E.inCubic(melt) * (floor - top + SLACK) +
        fx.tremble * u * drift(c.T, fx.trembleHz, `${seed}y`) +
        look.rattle * fx.shudder * u * shake(w.since, fx.shudderS, fx.shudderHz, hash01(`${seed}p`) * 2 * Math.PI),
    });
    // the strip: from this letter's place to the next strip's, the first and the last out to the sides
    const x0 = n === 0 ? 0 : across(left + g.x);
    const x1 = n === starts.length - 1 ? c.width : across(left + glyphs[starts[n + 1]].x);
    units.push(
      wordUnit(c, w, {
        key: `c${k}`,
        m: chain(own, whole),
        z,
        layers,
        fg,
        clip: { x: x0, y: 0, w: Math.max(0, x1 - x0), h: c.out > 0 ? Math.max(0, floor - up) : c.height },
        active: label ? true : undefined,
      }),
    );
  });
  return units;
}

export default defineTemplate<P>({
  name: "creep",
  schema,
  build: (page): Scene => {
    const { P, colors } = page;
    const fx = thick(P.fx, page.layout.size);
    const { c, up } = raised(page, fx);
    const units: Unit[] = [];
    const pace = hurry(c, fx);
    for (const w of c.words) {
      const u = w.box.size;
      const capH = c.layout.capH * (u / c.layout.size);
      const room = roomFor(c, fx, u, w.cx, w.cy, w.box.w / 2, capH / 2, pastOf(w, fx));
      const lit = Math.min(1, Math.max(0, w.a));
      const hb = heart(fx, w.since) * lit;
      const fill = w.emph ? colors.emph : mix(colors.text, colors.active, lit);
      const look: Look = {
        fill,
        top: mix(fill, colors.accent2, fx.sheen + fx.activeTop * lit),
        glow: room.glow * (1 + fx.beatGlow * hb),
        reach: room.glow * (1 + fx.beatGlow),
        beat: room.beat,
        swell: hb,
        pivot: [w.cx, w.cy],
        rattle: w.emph ? 1 : 0,
      };
      // a word is there by the time it is spoken, however early that is
      const born = Math.min(w.index * P.timing.staggerS * pace, w.start);
      units.push(...ooze(c, w, fx, born, pace, look, up, w.phase === "active" ? 1 : 0));
    }
    return { units, shapes: [] };
  },

  label: (lab): Scene => {
    const fx = thick(lab.P.fx, lab.layout.size);
    const { c, up } = raised(lab, fx);
    const u = c.layout.size;
    // a label's heart beats for as long as it is up; the whole label swells about its middle
    const mid = (c.layout.top + c.layout.bottom) / 2;
    const room = roomFor(c, fx, u, c.cx, c.cy + mid, Math.max(-c.layout.left, c.layout.right), (c.layout.bottom - c.layout.top) / 2, fx.drip);
    const hb = heart(fx, c.t);
    const pace = hurry(c, fx);
    const look: Look = {
      fill: c.colors.text,
      top: mix(c.colors.text, c.colors.accent2, fx.sheen),
      glow: room.glow * (1 + fx.beatGlow * hb),
      reach: room.glow * (1 + fx.beatGlow),
      beat: room.beat,
      swell: hb,
      pivot: [c.cx, c.cy + mid],
      rattle: 0,
    };
    const units: Unit[] = [];
    c.words.forEach((w, i) => units.push(...ooze(c, w, fx, i * c.P.timing.staggerS * pace, pace, look, up, 5)));
    return { units, shapes: [] };
  },
});
