import { z } from "zod";
import type { Pose } from "./affine";
import { back, clamp, E, lerp, prog, spring } from "./ease";

/**
 * The animation library: named motions a template can be told to use, slot by slot.
 *
 *   enter   how a unit (page, word or letter) arrives          t = seconds since it appeared
 *   exit    how it leaves                                       t = seconds since the exit began, dur = its length
 *   active  what the word being spoken does                     a = 0..1 highlight amount, t = seconds since it started
 *   emph    what a shouted word does on top of that             t = seconds since it started
 *   idle    what everything keeps doing                         t = seconds on the clip
 *
 * An animation is a pure function from an instant to a Pose (offset in px, scale, squash, rotation, skew)
 * relative to the unit at rest, about a pivot it names. Every one has its own parameters with defaults and
 * bounds, is a function of time only (so 30 and 60 fps show the same motion), ends exactly at rest (enter),
 * starts exactly at rest (exit, active, emph) or stays within its amplitude (idle), and never fades:
 * things arrive and leave by moving, turning, squashing and scaling. `scripts/anim_check.mjs` samples every
 * one of them (rest states, continuity, 30 fps flicker limits, how different they are from each other) and
 * the frame-by-frame harness renders every one.
 */

export type Slot = "enter" | "exit" | "active" | "emph" | "idle";
/** What takes its turn in an entrance or exit: the whole page at once, word after word, letter after letter. */
export type By = "page" | "word" | "letter";
export type Pivot = "centre" | "base" | "top";

export type AnimIn = {
  t: number; // seconds (see the slot)
  dur: number; // seconds the animation has (exit: all of it; enter: the template's enterS, a hint)
  u: number; // one em in px
  i: number; // index of the unit among its siblings (for alternating sides)
  a: number; // 0..1 highlight amount (active slot)
};

export type AnimOut = Pose & { pivot?: Pivot };

type Shape = z.ZodRawShape;
export type Animation<S extends Shape = Shape> = {
  name: string;
  slot: Slot;
  about: string;
  // enter and exit: the units the animation suits. A slam or a slide is for the whole block: word by
  // word the pieces would run over each other.
  units?: By[];
  shape: S;
  defaults: z.infer<z.ZodObject<S>>;
  // seconds after which the animation is over (enter, emph: back at rest), from its parameters
  length: (p: z.infer<z.ZodObject<S>>, dur: number) => number;
  pose: (x: AnimIn, p: z.infer<z.ZodObject<S>>) => AnimOut;
};

const REST: AnimOut = {};
const n = (min: number, max: number) => z.number().min(min).max(max);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const all: Animation<any>[] = [];
function define<S extends Shape>(a: Animation<S>): void {
  all.push(a);
}
const side = (i: number) => (i % 2 === 0 ? -1 : 1);
/** 1 at t = 0 falling to exactly 0 at t = d, smoothly: the envelope of everything that rings out. */
const fade = (t: number, d: number) => (t <= 0 ? 1 : t >= d ? 0 : (1 - t / d) * (1 - t / d));

// ------------------------------------------------------------------ enter

define({
  name: "pop",
  slot: "enter",
  units: ["page", "word", "letter"],
  about: "Springs up from a smaller size with a little overshoot while rising into place.",
  shape: { from: n(0.5, 1), freq: n(1, 6), damp: n(0.3, 1), rise: n(0, 1) },
  defaults: { from: 0.62, freq: 4.5, damp: 0.55, rise: 0.3 },
  length: (p) => 2.2 / (p.freq * p.damp),
  pose: ({ t, u }, p) => {
    const d = 2.2 / (p.freq * p.damp);
    if (t >= d) return REST;
    const k = 1 - (1 - spring(t, p.freq, p.damp)) * fade(t, d) ** 0.25;
    return { scale: lerp(p.from, 1, k), y: (1 - E.outCubic(prog(t, 0, d * 0.6))) * p.rise * u };
  },
});

define({
  name: "drop",
  slot: "enter",
  units: ["page", "word", "letter"],
  about: "Falls in from above, lands with a squash and bounces twice.",
  shape: { height: n(0.2, 2), fallS: n(0.08, 0.6), squash: n(0, 0.5) },
  defaults: { height: 0.8, fallS: 0.38, squash: 0.2 },
  length: (p) => p.fallS,
  pose: ({ t, u }, p) => {
    if (t >= p.fallS) return REST;
    const x = prog(t, 0, p.fallS);
    const y = -(1 - E.outBounce(x)) * p.height * u;
    // squash while it is on the floor between bounces: strongest right after the first landing
    const hit = x > 0.36 ? Math.exp(-(x - 0.36) * 9) * Math.sin(clamp((x - 0.36) / 0.64) * Math.PI) : 0;
    return { y, sx: 1 + p.squash * hit, sy: 1 - p.squash * hit, pivot: "base" };
  },
});

define({
  name: "slide",
  slot: "enter",
  units: ["page"],
  about: "Shoots in from the side, leaning into the motion, and straightens as it stops.",
  shape: { distance: n(0.3, 6), slideS: n(0.1, 0.8), lean: n(0, 30), alternate: n(0, 1) },
  defaults: { distance: 2.2, slideS: 0.32, lean: 14, alternate: 0 },
  length: (p) => p.slideS,
  pose: ({ t, u, i }, p) => {
    if (t >= p.slideS) return REST;
    const x = prog(t, 0, p.slideS);
    const dir = p.alternate >= 0.5 ? side(i) : -1;
    const left = 1 - E.outQuart(x);
    return { x: dir * left * p.distance * u, skewX: dir * p.lean * left * (1 - x), scale: lerp(0.86, 1, E.outCubic(x)) };
  },
});

define({
  name: "twirl",
  slot: "enter",
  units: ["page", "word", "letter"],
  about: "Spins in from a tilt while growing, overshoots the upright and settles.",
  shape: { angle: n(-180, 180), from: n(0.5, 1), twirlS: n(0.12, 0.8), overshoot: n(0, 3) },
  defaults: { angle: -70, from: 0.5, twirlS: 0.36, overshoot: 1.6 },
  length: (p) => p.twirlS,
  pose: ({ t }, p) => {
    if (t >= p.twirlS) return REST;
    const k = back(p.overshoot)(prog(t, 0, p.twirlS));
    return { rot: p.angle * (1 - k), scale: lerp(p.from, 1, E.outCubic(prog(t, 0, p.twirlS * 0.8))) };
  },
});

define({
  name: "unfold",
  slot: "enter",
  units: ["page", "word"],
  about: "Stands up from flat on the baseline like a pop-up card, rubbery.",
  shape: { freq: n(1, 6), damp: n(0.25, 1), widen: n(0, 0.6) },
  defaults: { freq: 3.4, damp: 0.42, widen: 0.25 },
  length: (p) => 2.4 / (p.freq * p.damp),
  pose: ({ t }, p) => {
    const d = 2.4 / (p.freq * p.damp);
    if (t >= d) return REST;
    const k = 1 - (1 - spring(t, p.freq, p.damp)) * fade(t, d) ** 0.25;
    const sy = Math.max(0.04, k);
    return { sy, sx: 1 + p.widen * (1 - clamp(k)), pivot: "base" };
  },
});

define({
  name: "slam",
  slot: "enter",
  units: ["page"],
  about: "Comes down from far too big, hits its size hard and shudders.",
  shape: { from: n(1.2, 3), slamS: n(0.06, 0.4), shudder: n(0, 0.2), shudderHz: n(1, 6), settleS: n(0.05, 0.6) },
  defaults: { from: 1.9, slamS: 0.13, shudder: 0.07, shudderHz: 5, settleS: 0.3 },
  length: (p) => p.slamS + p.settleS,
  pose: ({ t }, p) => {
    if (t >= p.slamS + p.settleS) return REST;
    if (t < p.slamS) return { scale: lerp(p.from, 1, E.inCubic(prog(t, 0, p.slamS))) };
    const s = t - p.slamS;
    const ring = Math.sin(2 * Math.PI * p.shudderHz * s) * fade(s, p.settleS);
    return { sx: 1 + p.shudder * ring, sy: 1 - p.shudder * ring };
  },
});

define({
  name: "swing",
  slot: "enter",
  units: ["page", "word"],
  about: "Swings down on a hinge like a sign and rocks until it hangs still.",
  shape: { angle: n(5, 90), swingHz: n(0.5, 4), swingS: n(0.2, 1.5) },
  defaults: { angle: 38, swingHz: 2.2, swingS: 0.75 },
  length: (p) => p.swingS,
  pose: ({ t, i }, p) => {
    if (t >= p.swingS) return REST;
    return { rot: side(i) * p.angle * Math.cos(2 * Math.PI * p.swingHz * t) * fade(t, p.swingS), pivot: "top" };
  },
});

// ------------------------------------------------------------------ exit (must be gone at t = dur)

define({
  name: "shrink",
  slot: "exit",
  units: ["page", "word", "letter"],
  about: "Winds up a touch, then shrinks away to nothing while sinking.",
  shape: { windup: n(0, 3), drop: n(0, 1.5) },
  defaults: { windup: 1.4, drop: 0.35 },
  length: (_p, dur) => dur,
  pose: ({ t, dur, u }, p) => {
    const x = prog(t, 0, dur);
    // an "in-back" curve: dips below zero first (the wind-up: a touch bigger), then runs to 1 (gone)
    // no wind-up in an exit too short to show one: under a tenth of a second it would be a one-frame twitch
    const w = p.windup * clamp((dur - 0.1) / 0.15);
    const k = (w + 1) * x * x * x - w * x * x;
    return { scale: Math.max(0, 1 - k), y: E.inCubic(x) * p.drop * u };
  },
});

define({
  name: "melt",
  slot: "exit",
  units: ["page", "word"],
  about: "Sags into the floor: flattens downwards and spreads until it is a line, then gone.",
  shape: { spread: n(0, 1), sag: n(0, 1) },
  defaults: { spread: 0.35, sag: 0.25 },
  length: (_p, dur) => dur,
  pose: ({ t, dur, u }, p) => {
    const x = E.inQuad(prog(t, 0, dur));
    return { sy: 1 - x, sx: 1 + p.spread * x, y: p.sag * x * u, pivot: "base" };
  },
});

define({
  name: "fling",
  slot: "exit",
  units: ["page"],
  about: "Leans back and is thrown out sideways, thinning as it goes.",
  shape: { distance: n(0.3, 6), lean: n(0, 35), alternate: n(0, 1) },
  defaults: { distance: 2.4, lean: 18, alternate: 0 },
  length: (_p, dur) => dur,
  pose: ({ t, dur, u, i }, p) => {
    const x = prog(t, 0, dur);
    const dir = p.alternate >= 0.5 ? -side(i) : 1;
    const go = E.inCubic(x);
    return { x: dir * go * p.distance * u, skewX: -dir * p.lean * E.outCubic(x), sx: 1 - go, scale: 1 - 0.2 * go };
  },
});

define({
  name: "topple",
  slot: "exit",
  units: ["page", "word"],
  about: "Tips over on its base like a felled tree, falls, and is gone only once it is down.",
  shape: { angle: n(20, 120), fall: n(0, 2), goneFrom: n(0.3, 0.9) },
  defaults: { angle: 82, fall: 0.6, goneFrom: 0.6 },
  length: (_p, dur) => dur,
  pose: ({ t, dur, u, i }, p) => {
    const x = prog(t, 0, dur);
    const down = E.inQuart(clamp((x - p.goneFrom) / (1 - p.goneFrom)));
    return { rot: side(i) * p.angle * E.inQuad(x), y: p.fall * E.inCubic(x) * u, scale: 1 - down, pivot: "base" };
  },
});

define({
  name: "blink",
  slot: "exit",
  units: ["page", "word"],
  about: "Closes like an eye: squeezes shut to a horizontal line around its middle, then the line snaps away.",
  shape: { widen: n(0, 0.6), shutAt: n(0.3, 0.9) },
  defaults: { widen: 0.2, shutAt: 0.7 },
  length: (_p, dur) => dur,
  pose: ({ t, dur }, p) => {
    const x = prog(t, 0, dur);
    const shut = E.inOutCubic(clamp(x / p.shutAt));
    const snap = E.inCubic(clamp((x - p.shutAt) / (1 - p.shutAt)));
    return { sy: Math.max(0, (1 - 0.93 * shut) * (1 - snap)), sx: (1 + p.widen * shut) * (1 - snap) };
  },
});

// ------------------------------------------------------------------ active (identity at a = 0)

define({
  name: "punch",
  slot: "active",
  about: "Punches up in size and lifts while it is spoken.",
  shape: { scale: n(1, 1.5), lift: n(0, 0.3) },
  defaults: { scale: 1.14, lift: 0.05 },
  length: () => 0,
  pose: ({ a, u }, p) => ({ scale: 1 + (p.scale - 1) * a, y: -p.lift * u * a }),
});

define({
  name: "hop",
  slot: "active",
  about: "Jumps once when its turn comes and lands with a squash.",
  shape: { height: n(0.05, 0.8), hopS: n(0.12, 0.6), squash: n(0, 0.4) },
  defaults: { height: 0.28, hopS: 0.3, squash: 0.14 },
  length: (p) => p.hopS * 1.5,
  pose: ({ t, u }, p) => {
    if (t <= 0 || t >= p.hopS * 1.5) return REST;
    if (t < p.hopS) return { y: -Math.sin((t / p.hopS) * Math.PI) * p.height * u, pivot: "base" };
    const land = Math.sin(((t - p.hopS) / (p.hopS * 0.5)) * Math.PI);
    return { sx: 1 + p.squash * land, sy: 1 - p.squash * land, pivot: "base" };
  },
});

define({
  name: "rock",
  slot: "active",
  about: "Rocks from side to side for as long as it is spoken.",
  shape: { angle: n(0.5, 15), hz: n(0.3, 3), scale: n(1, 1.3) },
  defaults: { angle: 3.5, hz: 1.6, scale: 1.04 },
  length: () => 0,
  pose: ({ t, a }, p) => ({ rot: p.angle * a * Math.sin(2 * Math.PI * p.hz * Math.max(0, t)), scale: 1 + (p.scale - 1) * a }),
});

define({
  name: "tall",
  slot: "active",
  about: "Stretches tall and narrow from the baseline while spoken, like taking a breath.",
  shape: { stretch: n(0, 0.6), narrow: n(0, 0.3) },
  defaults: { stretch: 0.2, narrow: 0.07 },
  length: () => 0,
  pose: ({ a }, p) => ({ sy: 1 + p.stretch * a, sx: 1 - p.narrow * a, pivot: "base" }),
});

define({
  name: "lean",
  slot: "active",
  about: "Leans forward into italics and edges ahead while spoken.",
  shape: { skew: n(0, 25), nudge: n(0, 0.3) },
  defaults: { skew: 11, nudge: 0.06 },
  length: () => 0,
  pose: ({ a, u }, p) => ({ skewX: -p.skew * a, x: p.nudge * u * a, pivot: "base" }),
});

// ------------------------------------------------------------------ emph (identity at t <= 0 and t >= length)

define({
  name: "shake",
  slot: "emph",
  about: "A quick angry shake that dies away.",
  shape: { angle: n(0.5, 20), hz: n(1, 6), shakeS: n(0.1, 1.5) },
  defaults: { angle: 5, hz: 5.5, shakeS: 0.45 },
  length: (p) => p.shakeS,
  pose: ({ t }, p) => (t <= 0 || t >= p.shakeS ? REST : { rot: p.angle * Math.sin(2 * Math.PI * p.hz * t) * fade(t, p.shakeS) }),
});

define({
  name: "quake",
  slot: "emph",
  about: "Rattles in place, up-down and left-right at different speeds, like the ground moving.",
  shape: { amount: n(0.01, 0.3), hz: n(1, 6), quakeS: n(0.1, 1.5) },
  defaults: { amount: 0.07, hz: 5, quakeS: 0.5 },
  length: (p) => p.quakeS,
  pose: ({ t, u }, p) => {
    if (t <= 0 || t >= p.quakeS) return REST;
    const k = fade(t, p.quakeS) * p.amount * u;
    return { x: k * Math.sin(2 * Math.PI * p.hz * t), y: -k * Math.sin(2 * Math.PI * p.hz * 0.73 * t) };
  },
});

define({
  name: "throb",
  slot: "emph",
  about: "Two heartbeats: swells, relaxes, swells again a little less.",
  shape: { swell: n(0.02, 0.5), beatS: n(0.12, 0.6) },
  defaults: { swell: 0.2, beatS: 0.26 },
  length: (p) => p.beatS * 2,
  pose: ({ t }, p) => {
    if (t <= 0 || t >= p.beatS * 2) return REST;
    const beat = Math.floor(t / p.beatS);
    const x = (t - beat * p.beatS) / p.beatS;
    return { scale: 1 + p.swell * (beat === 0 ? 1 : 0.6) * Math.sin(x * Math.PI) ** 2 };
  },
});

define({
  name: "wobble",
  slot: "emph",
  about: "Wobbles like jelly: wide then tall, a few times, settling.",
  shape: { amount: n(0.02, 0.5), hz: n(1, 6), wobbleS: n(0.15, 1.5) },
  defaults: { amount: 0.18, hz: 3.5, wobbleS: 0.6 },
  length: (p) => p.wobbleS,
  pose: ({ t }, p) => {
    if (t <= 0 || t >= p.wobbleS) return REST;
    const w = p.amount * Math.sin(2 * Math.PI * p.hz * t) * fade(t, p.wobbleS);
    return { sx: 1 + w, sy: 1 - w, pivot: "base" };
  },
});

define({
  name: "kick",
  slot: "emph",
  about: "Kicks out bigger with a twist and snaps back.",
  shape: { scale: n(1, 1.8), twist: n(0, 25), kickS: n(0.15, 1) },
  defaults: { scale: 1.3, twist: 8, kickS: 0.4 },
  length: (p) => p.kickS,
  pose: ({ t, i }, p) => {
    if (t <= 0 || t >= p.kickS) return REST;
    const x = t / p.kickS;
    const k = Math.sin(x * Math.PI) ** 2 * (1 - x * 0.35);
    return { scale: 1 + (p.scale - 1) * k, rot: side(i) * p.twist * k };
  },
});

// ------------------------------------------------------------------ idle (endless, small)

define({
  name: "float",
  slot: "idle",
  about: "Bobs gently up and down, every unit a little out of step.",
  shape: { height: n(0, 0.2), hz: n(0.1, 2), step: n(0, 3) },
  defaults: { height: 0.035, hz: 0.7, step: 0.9 },
  length: () => 0,
  pose: ({ t, u, i }, p) => ({ y: p.height * u * Math.sin(2 * Math.PI * p.hz * t + i * p.step) }),
});

define({
  name: "sway",
  slot: "idle",
  about: "Tilts slowly from side to side like something hanging.",
  shape: { angle: n(0, 8), hz: n(0.1, 2), step: n(0, 3) },
  defaults: { angle: 1.6, hz: 0.5, step: 0.6 },
  length: () => 0,
  pose: ({ t, i }, p) => ({ rot: p.angle * Math.sin(2 * Math.PI * p.hz * t + i * p.step), pivot: "top" }),
});

define({
  name: "breathe",
  slot: "idle",
  about: "Grows and shrinks a hair, slowly.",
  shape: { amount: n(0, 0.08), hz: n(0.1, 2), step: n(0, 3) },
  defaults: { amount: 0.018, hz: 0.6, step: 0.5 },
  length: () => 0,
  pose: ({ t, i }, p) => ({ scale: 1 + p.amount * Math.sin(2 * Math.PI * p.hz * t + i * p.step) }),
});

// ------------------------------------------------------------------ registry

export const animations: Record<string, Animation> = Object.fromEntries(all.map((a) => [a.name, a]));
export const animationNames = (slot: Slot): string[] => all.filter((a) => a.slot === slot).map((a) => a.name);

/** What a template's JSON says: the animation by name and any of its parameters to change. */
export const animRef = (slot: Slot) =>
  z
    .strictObject({
      name: z.string(),
      params: z.record(z.string(), z.number()),
      by: z.enum(["page", "word", "letter"]).default("page"), // enter and exit only: what takes its turn
    })
    .superRefine((ref, ctx) => {
      const a = animations[ref.name];
      if (!a || a.slot !== slot) {
        ctx.addIssue({ code: "custom", message: `no ${slot} animation "${ref.name}" (have: ${animationNames(slot).join(", ")})` });
        return;
      }
      if (a.units ? !a.units.includes(ref.by) : ref.by !== "page") {
        ctx.addIssue({ code: "custom", message: `${ref.name} cannot run by ${ref.by} (it suits: ${(a.units ?? ["page"]).join(", ")})` });
      }
      const r = z.strictObject(a.shape).safeParse({ ...a.defaults, ...ref.params });
      if (!r.success) ctx.addIssue({ code: "custom", message: `${ref.name}: ${z.prettifyError(r.error)}` });
    });
export type AnimRef = { name: string; params: Record<string, number>; by?: By };

const resolved = new WeakMap<AnimRef, { a: Animation; p: Record<string, number> }>();

/** The animation a reference names, with its parameters filled in from the defaults. */
export function resolve(ref: AnimRef): { a: Animation; p: Record<string, number> } {
  let r = resolved.get(ref);
  if (!r) {
    const a = animations[ref.name];
    if (!a) throw new Error(`unknown animation "${ref.name}"`);
    r = { a, p: { ...(a.defaults as Record<string, number>), ...ref.params } };
    resolved.set(ref, r);
  }
  return r;
}

/** The pose of an animation reference at an instant. */
export function animate(ref: AnimRef | null, x: AnimIn): AnimOut {
  if (!ref) return REST;
  const { a, p } = resolve(ref);
  return a.pose(x, p);
}

/** Seconds a referenced animation needs to finish (enter, emph, hop). */
export function animLength(ref: AnimRef | null, dur: number): number {
  if (!ref) return 0;
  const { a, p } = resolve(ref);
  return a.length(p, dur);
}
