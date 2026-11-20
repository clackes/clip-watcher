// Check every animation of src/core/animations.ts by sampling it: node scripts/anim_check.mjs
//
// No browser: the animations are pure functions of time, so they are compiled with esbuild and called.
// For each one: its defaults satisfy its own bounds; it is finite everywhere; it starts and ends where
// its slot says (enter ends exactly at rest, exit starts at rest and ends gone, active is at rest when the
// highlight is off, emph is at rest before and after, idle stays small); it has no discontinuity (halving
// the time step must roughly halve the largest step); sampled at 30 fps it neither jumps nor flickers by
// the frame-by-frame harness's own limits; and inside a slot every animation moves differently from every
// other. Prints one JSON object; exit code 1 when anything failed.
import { build } from "esbuild";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = await build({
  entryPoints: [path.join(root, "src/core/animations.ts")],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
  logLevel: "silent",
});
const mod = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString("base64")}`);
const { animations } = mod;

const U = 92; // one em, px: the size of the standard template
const EPS_JUMP = { x: 14, y: 14, sx: 0.1, sy: 0.1, rot: 4, skew: 6 };
const EPS_FLASH = { x: 8, y: 8, sx: 0.08, sy: 0.08, rot: 2.5, skew: 4 };
const CH = ["x", "y", "sx", "sy", "rot", "skew"];
const DIFFERENT = 0.5; // smallest distance between two animations of a slot, as shapes (see signature()): 0 = the same motion, 1.41 = unrelated

const flat = (p) => ({
  x: p.x ?? 0,
  y: p.y ?? 0,
  sx: (p.scale ?? 1) * (p.sx ?? 1),
  sy: (p.scale ?? 1) * (p.sy ?? 1),
  rot: p.rot ?? 0,
  skew: p.skewX ?? 0,
});
const REST = flat({});
const same = (a, b, tol = 1e-6) => CH.every((c) => Math.abs(a[c] - b[c]) <= tol);
const gone = (v) => Math.abs(v.sx) < 1e-6 || Math.abs(v.sy) < 1e-6;
const smooth = (x) => x * x * (3 - 2 * x);

/** The animation as a function of one clock, as a template drives it. */
function timeline(a, p) {
  const len = a.length(p, 0.22);
  if (a.slot === "enter") return { T: len + 0.3, at: (t) => a.pose({ t, dur: 0.22, u: U, i: 0, a: 0 }, p), from: 0 };
  if (a.slot === "exit") return { T: 0.25, at: (t) => a.pose({ t, dur: 0.25, u: U, i: 0, a: 0 }, p), from: -0.2 };
  if (a.slot === "emph") return { T: len + 0.3, at: (t) => a.pose({ t, dur: 0, u: U, i: 0, a: 0 }, p), from: -0.2 };
  if (a.slot === "idle") return { T: 6, at: (t) => a.pose({ t, dur: 0, u: U, i: 1, a: 0 }, p), from: 0 };
  // active: the highlight comes on over 0.1 s at t = 0, stays 0.6 s, goes off over 0.16 s
  const amount = (t) => (t < 0 ? 0 : t < 0.1 ? smooth(t / 0.1) : t < 0.7 ? 1 : t < 0.86 ? 1 - smooth((t - 0.7) / 0.16) : 0);
  return { T: 1.2, at: (t) => a.pose({ t, dur: 0, u: U, i: 0, a: amount(t) }, p), from: -0.2 };
}

function sample(tl, fps, phase = 0.5, t0 = tl.from, t1 = tl.T) {
  const outv = [];
  for (let f = Math.floor(t0 * fps); (f + phase) / fps <= t1; f++) outv.push(flat(tl.at((f + phase) / fps)));
  return outv;
}

function check(a) {
  const fails = [];
  let checks = 0;
  const ok = (cond, msg) => {
    checks++;
    if (!cond) fails.push(msg);
  };
  const schema = z.strictObject(a.shape);
  ok(schema.safeParse(a.defaults).success, "defaults do not satisfy the parameter bounds");
  ok(typeof a.about === "string" && a.about.length > 20, "no description");
  const p = a.defaults;
  const tl = timeline(a, p);
  const len = a.length(p, tl.T);

  // rest states
  if (a.slot === "enter") {
    ok(same(flat(tl.at(len)), REST), "not exactly at rest when it ends");
    ok(same(flat(tl.at(len + 1)), REST), "moves after it ended");
    ok(!same(flat(tl.at(0)), REST, 1e-3), "starts at rest: there is no entrance");
  } else if (a.slot === "exit") {
    ok(same(flat(tl.at(0)), REST, 1e-9), "does not start at rest");
    ok(gone(flat(tl.at(0.25))), "still visible at the end of the exit");
    for (const d of [0.08, 0.14, 0.4]) ok(gone(flat(a.pose({ t: d, dur: d, u: U, i: 0, a: 0 }, p))), `still visible after a ${d}s exit`);
  } else if (a.slot === "emph") {
    ok(same(flat(tl.at(-0.1)), REST) && same(flat(tl.at(0)), REST, 1e-9), "moves before the word starts");
    ok(same(flat(tl.at(len)), REST) && same(flat(tl.at(len + 0.5)), REST), "not at rest when it ends");
    ok(len > 0.1, "shorter than three frames at 30 fps");
  } else if (a.slot === "active") {
    ok(same(flat(tl.at(-0.1)), REST), "moves while the word is not spoken");
    ok(same(flat(tl.at(1.19)), REST, 1e-6), "does not return to rest when the highlight leaves");
    ok(!sample(tl, 240, 0.5, 0, 0.7).every((v) => same(v, REST, 1e-3)), "does nothing while the word is spoken");
  } else {
    const s = sample(tl, 60);
    ok(s.every((v) => Math.abs(v.y) <= 0.2 * U && Math.abs(v.x) <= 0.2 * U && Math.abs(v.rot) <= 8 && Math.abs(v.sx - 1) <= 0.08), "too large for an idle motion");
    ok(!s.every((v) => same(v, REST, 1e-4)), "does nothing");
  }

  // finite, and continuous: halving the step halves the largest change
  const fine = sample(tl, 480);
  const finer = sample(tl, 960);
  ok(fine.every((v) => CH.every((c) => Number.isFinite(v[c]))), "not a number somewhere");
  for (const c of CH) {
    const step = (s) => Math.max(0, ...s.slice(1).map((v, i) => Math.abs(v[c] - s[i][c])));
    const [m1, m2] = [step(fine), step(finer)];
    const floor = { x: 0.2, y: 0.2, sx: 0.002, sy: 0.002, rot: 0.05, skew: 0.05 }[c];
    ok(m1 <= floor || m2 <= 0.72 * m1, `${c} jumps: largest step ${m1.toFixed(4)} at 480 fps, ${m2.toFixed(4)} at 960 fps`);
  }

  // the harness's jump and flash rules at 30 fps, at four phases of the frame clock
  for (const phase of [0.1, 0.35, 0.6, 0.85]) {
    const s = sample(tl, 30, phase);
    for (let i = 1; i + 2 < s.length; i++) {
      for (const c of CH) {
        const [d1, d2, d3] = [s[i][c] - s[i - 1][c], s[i + 1][c] - s[i][c], s[i + 2][c] - s[i + 1][c]];
        const back = s[i + 2][c] - s[i][c];
        ok(Math.abs(d2) <= 5 * Math.max(Math.abs(d1), Math.abs(d3)) + EPS_JUMP[c], `${c} jumps ${d2.toFixed(3)} in one frame at 30 fps (phase ${phase})`);
        ok(
          !(Math.abs(d2) > EPS_FLASH[c] && Math.abs(d3) > EPS_FLASH[c] && Math.abs(back) < 0.25 * Math.abs(d2) && d2 * d3 < 0),
          `${c} flickers at 30 fps: out ${d2.toFixed(3)} and back ${d3.toFixed(3)} within two frames (phase ${phase})`,
        );
      }
    }
  }
  // every parameter matters: moving it to either bound changes the motion (or it is not a parameter)
  for (const key of Object.keys(a.shape)) {
    const lo = a.shape[key].minValue ?? a.shape[key]._zod?.bag?.minimum;
    const hi = a.shape[key].maxValue ?? a.shape[key]._zod?.bag?.maximum;
    ok(Number.isFinite(lo) && Number.isFinite(hi) && lo < hi, `parameter ${key} has no bounds`);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) continue;
    const moved = [lo, hi].some((v) => dist(raw(a, p), raw(a, { ...p, [key]: v })) > 1e-5);
    ok(moved, `parameter ${key} changes nothing`);
  }
  return { name: a.name, slot: a.slot, checks, fails };
}

/** 96 samples of the motion over its own length, in comparable units: what two animations are told apart by. */
function signature(a, p) {
  const tl = timeline(a, p);
  const t0 = a.slot === "idle" ? 0 : 0;
  const t1 = a.slot === "exit" ? 0.25 : a.slot === "active" ? 0.86 : a.slot === "idle" ? 4 : Math.max(0.05, a.length(p, 0.22));
  const v = [];
  for (let k = 0; k < 96; k++) {
    const q = flat(tl.at(t0 + ((k + 0.5) / 96) * (t1 - t0)));
    v.push(q.x / U, q.y / U, Math.log(Math.max(0.02, Math.abs(q.sx))), Math.log(Math.max(0.02, Math.abs(q.sy))), q.rot / 45, q.skew / 30);
  }
  return v;
}
const dist = (a, b) => Math.sqrt(a.reduce((s, v, i) => s + (v - b[i]) ** 2, 0) / a.length);
/** The motion on a fixed clock for two units (index 0 and 1): what a parameter must change to be a parameter. */
function raw(a, p) {
  const v = [];
  for (const i of [0, 1]) {
    for (let k = 0; k < 150; k++) {
      const t = (k + 0.5) / 100;
      const q = flat(a.pose({ t, dur: 0.3, u: U, i, a: Math.min(1, t * 4) }, p));
      v.push(q.x / U, q.y / U, q.sx, q.sy, q.rot / 45, q.skew / 30);
    }
  }
  return v;
}
/** Distance between two motions as shapes: each signature scaled to unit length first, so a big and a
 * small version of the same move are the same move. */
const shapeDist = (a, b) => {
  const norm = (v) => {
    const len = Math.sqrt(v.reduce((s, x) => s + x * x, 0)) || 1;
    return v.map((x) => x / len);
  };
  const [na, nb] = [norm(a), norm(b)];
  return Math.sqrt(na.reduce((s, v, i) => s + (v - nb[i]) ** 2, 0));
};

const results = Object.values(animations).map(check);
const pairs = [];
for (const slot of ["enter", "exit", "active", "emph", "idle"]) {
  const list = Object.values(animations).filter((a) => a.slot === slot);
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const d = shapeDist(signature(list[i], list[i].defaults), signature(list[j], list[j].defaults));
      pairs.push({ slot, a: list[i].name, b: list[j].name, distance: Math.round(d * 1000) / 1000 });
    }
  }
}
const tooClose = pairs.filter((p) => p.distance < DIFFERENT);
const failures = results.reduce((s, r) => s + r.fails.length, 0) + tooClose.length;
console.log(
  JSON.stringify({
    ok: failures === 0,
    animations: results.length,
    bySlot: Object.fromEntries(["enter", "exit", "active", "emph", "idle"].map((s) => [s, results.filter((r) => r.slot === s).map((r) => r.name)])),
    checks: results.reduce((s, r) => s + r.checks, 0) + pairs.length,
    failures,
    failed: results.filter((r) => r.fails.length).map((r) => ({ name: r.name, fails: r.fails.slice(0, 8) })),
    tooClose,
    closest: [...pairs].sort((a, b) => a.distance - b.distance).slice(0, 5),
  }),
);
process.exit(failures === 0 ? 0 : 1);
