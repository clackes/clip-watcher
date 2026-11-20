/** Time-based animation maths. Everything is a function of seconds, never of frame numbers, so 30 and
 * 60 fps renders show the same motion sampled at different instants. All functions are pure. */

export const clamp = (x: number, lo = 0, hi = 1): number => Math.min(hi, Math.max(lo, x));
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Progress 0..1 of an interval that starts at t0 and lasts dur seconds. */
export const prog = (t: number, t0: number, dur: number): number =>
  dur <= 1e-9 ? (t >= t0 ? 1 : 0) : clamp((t - t0) / dur);

export type Ease = (x: number) => number;

const c1 = 1.70158;
export const E = {
  linear: (x: number) => x,
  inQuad: (x: number) => x * x,
  outQuad: (x: number) => 1 - (1 - x) * (1 - x),
  inOutQuad: (x: number) => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2),
  inCubic: (x: number) => x * x * x,
  outCubic: (x: number) => 1 - Math.pow(1 - x, 3),
  inOutCubic: (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
  outQuart: (x: number) => 1 - Math.pow(1 - x, 4),
  inQuart: (x: number) => x * x * x * x,
  outExpo: (x: number) => (x >= 1 ? 1 : 1 - Math.pow(2, -10 * x)),
  inExpo: (x: number) => (x <= 0 ? 0 : Math.pow(2, 10 * x - 10)),
  inOutSine: (x: number) => -(Math.cos(Math.PI * x) - 1) / 2,
  outSine: (x: number) => Math.sin((x * Math.PI) / 2),
  inSine: (x: number) => 1 - Math.cos((x * Math.PI) / 2),
  /** Overshoots past 1 and settles: the classic pop. */
  outBack: (x: number) => 1 + (c1 + 1) * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2),
  inBack: (x: number) => (c1 + 1) * x * x * x - c1 * x * x,
  outElastic: (x: number) =>
    x <= 0 ? 0 : x >= 1 ? 1 : Math.pow(2, -10 * x) * Math.sin(((x * 10 - 0.75) * (2 * Math.PI)) / 3) + 1,
  outBounce: (x: number): number => {
    const n = 7.5625;
    const d = 2.75;
    if (x < 1 / d) return n * x * x;
    if (x < 2 / d) return n * (x -= 1.5 / d) * x + 0.75;
    if (x < 2.5 / d) return n * (x -= 2.25 / d) * x + 0.9375;
    return n * (x -= 2.625 / d) * x + 0.984375;
  },
} satisfies Record<string, Ease>;

/** outBack with a chosen overshoot (0 = none, 1.70158 = the usual 10 %). */
export const back =
  (k: number): Ease =>
  (x: number) =>
    1 + (k + 1) * Math.pow(x - 1, 3) + k * Math.pow(x - 1, 2);

/**
 * Step response of a damped spring, analytic: 0 at t = 0, settles at 1. `freq` is the undamped natural
 * frequency in Hz, `damp` the damping ratio (1 = no overshoot, 0.4 = lively). Exact at any t, so it
 * cannot drift between frame rates the way a simulated spring does.
 */
export function spring(t: number, freq = 5, damp = 0.5): number {
  if (t <= 0) return 0;
  const w = 2 * Math.PI * freq;
  if (damp >= 1) {
    const e = Math.exp(-w * t);
    return 1 - e * (1 + w * t);
  }
  const wd = w * Math.sqrt(1 - damp * damp);
  const e = Math.exp(-damp * w * t);
  return 1 - e * (Math.cos(wd * t) + ((damp * w) / wd) * Math.sin(wd * t));
}

/** Seconds after which spring(t) stays within `eps` of 1. */
export function springSettle(freq = 5, damp = 0.5, eps = 0.002): number {
  const w = 2 * Math.PI * freq;
  const d = Math.min(damp, 1);
  return Math.log(1 / (eps * Math.sqrt(Math.max(1e-6, 1 - Math.min(d, 0.999) ** 2)))) / (d * w);
}

/** A decaying shake: amplitude 1 at t = 0, gone after `dur`; `freq` in Hz. Returns -1..1. */
export function shake(t: number, dur: number, freq: number, phase = 0): number {
  if (t <= 0 || t >= dur) return 0;
  const k = 1 - t / dur;
  return Math.sin(2 * Math.PI * freq * t + phase) * k * k;
}

/** Deterministic 0..1 from an integer or string seed (no Math.random anywhere: renders must repeat). */
export function hash01(seed: number | string): number {
  const s = typeof seed === "number" ? `n${seed}` : seed;
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  h ^= h >>> 13;
  h = Math.imul(h, 0x5bd1e995);
  h ^= h >>> 15;
  return (h >>> 0) / 4294967296;
}

/** -1..1 from a seed. */
export const hashSigned = (seed: number | string): number => hash01(seed) * 2 - 1;

/** Smooth, endless wobble in -1..1: three sines with unrelated periods, phase from the seed. */
export function drift(t: number, freq: number, seed: number | string = 0): number {
  const p = hash01(seed) * Math.PI * 2;
  const w = 2 * Math.PI * freq * t;
  return (Math.sin(w + p) + 0.5 * Math.sin(w * 1.618 + p * 2.3) + 0.25 * Math.sin(w * 2.414 + p * 4.1)) / 1.75;
}

/** 0..1 "how active is the word": eases in after `start` and out after `end`. */
export function pulse(t: number, start: number, end: number, inS: number, outS: number, ease: Ease = E.outCubic): number {
  if (t < start) return 0;
  const up = ease(prog(t, start, inS));
  if (t < end) return up;
  // leave from wherever the rise had got to, so a very short word does not jump
  const top = ease(prog(end, start, inS));
  return top * (1 - E.inOutQuad(prog(t, end, outS)));
}
