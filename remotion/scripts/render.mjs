// Render a caption overlay: node scripts/render.mjs --props props.json --out <dir> [options]
//
// Writes one transparent PNG per frame of the clip into <dir> as 000000.png, 000001.png, ... (lossless,
// straight alpha: ffmpeg composites them exactly). Frames on which nothing is on screen are not rendered:
// they are hard links to one blank frame. Prints one JSON line with the result on stdout.
// The last step of every frame, before it is written: gaps of video that the drawing walls in are closed
// (scripts/gaps.mjs; gapFrames and gapPx in the result say how many).
//
//   --bundle <dir>       the bundle from scripts/bundle.mjs (default: ./build)
//   --concurrency <n>    browser tabs rendering in parallel (default: all cores)
//   --debug <file>       also write every frame's scene (positions, scales, colours) as JSON lines
//   --all-frames         render blank frames too (the frame-by-frame harness checks them)
//   --scene-only         no pictures: every frame is laid out and its scene written (--debug), nothing is
//                        drawn (the frame checks' phase sweep reads positions, scales and colours only)
//   --raw                leave every frame as the browser drew it: no gap closing (scripts/gaps.mjs). The
//                        frame checks use it to show that they see a gap when one is left
//   --gl <backend>       Chromium GL backend (angle, swangle, ...), default: Remotion's
import { openBrowser, renderFrames, selectComposition } from "@remotion/renderer";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gapPool } from "./gaps.mjs";

const DEBUG_TAG = "CWDBG";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : fallback;
};
const flag = (name) => args.includes(`--${name}`);

function fail(message, code = 1) {
  console.log(JSON.stringify({ ok: false, error: String(message).slice(0, 4000) }));
  process.exit(code);
}

const propsFile = opt("props");
const outDir = opt("out");
if (!propsFile || !outDir) fail("usage: render.mjs --props <file> --out <dir>", 2);
const bundleDir = path.resolve(opt("bundle", path.join(root, "build")));
if (!fs.existsSync(path.join(bundleDir, "index.html"))) fail(`no bundle at ${bundleDir}: run scripts/bundle.mjs`, 3);

let inputProps;
try {
  inputProps = JSON.parse(fs.readFileSync(propsFile, "utf8"));
} catch (err) {
  fail(`cannot read props: ${err.message}`, 2);
}
const concurrency = Math.max(1, Math.min(os.cpus().length, Number(opt("concurrency", os.cpus().length))));
const debugFile = opt("debug", null);
const sceneOnly = flag("scene-only");
const raw = flag("raw");
if (sceneOnly && !debugFile) fail("--scene-only needs --debug <file>", 2);
const gl = opt("gl", null);
const chromiumOptions = gl ? { gl } : {};
const name = (f) => `${String(f).padStart(6, "0")}.png`;

const started = Date.now();
fs.mkdirSync(outDir, { recursive: true });
const debug = debugFile ? fs.createWriteStream(debugFile) : null;
let browser = null;
let pool = null;
try {
  browser = await openBrowser("chrome", { chromiumOptions });
  const composition = await selectComposition({
    serveUrl: bundleDir,
    id: "Captions",
    inputProps,
    puppeteerInstance: browser,
    logLevel: "error",
  });
  const total = composition.durationInFrames;
  const fps = composition.fps;

  // a frame shows time (f + 0.5) / fps; it is blank when no page and no label covers that instant
  const spans = [...(inputProps.pages ?? []), ...(inputProps.labels ?? [])].map((p) => [p.start, p.end]);
  const busy = (f) => {
    const t = (f + 0.5) / fps;
    return spans.some(([a, b]) => t >= a && t < b);
  };
  const frames = [];
  let firstBlank = -1;
  for (let f = 0; f < total; f++) {
    if (flag("all-frames") || sceneOnly || busy(f)) frames.push(f);
    else if (firstBlank < 0) firstBlank = f;
  }
  if (firstBlank >= 0) frames.push(firstBlank);
  frames.sort((a, b) => a - b);

  const written = new Set();
  const writes = [];
  // the last step of a frame: gaps of video that the drawing walls in are closed (see gaps.mjs), on
  // threads of their own so that the browser tabs are not kept waiting
  const gaps = { frames: 0, px: 0, ms: 0, failed: 0 };
  pool = raw || sceneOnly ? null : gapPool(Math.max(1, Math.min(4, concurrency >> 1)));
  const finished = async (buffer) => {
    if (!pool) return buffer;
    try {
      const done = await pool.run(buffer);
      gaps.ms += done.ms;
      if (done.px > 0) {
        gaps.frames++;
        gaps.px += done.px;
      }
      return done.png;
    } catch {
      gaps.failed++; // an unreadable frame is written as it came: the render itself is not at stake
      return buffer;
    }
  };
  const opened = Date.now();
  if (frames.length > 0) {
    await renderFrames({
      composition,
      serveUrl: bundleDir,
      inputProps,
      outputDir: null,
      frames,
      imageFormat: sceneOnly ? "none" : "png",
      concurrency: Math.min(concurrency, frames.length),
      puppeteerInstance: browser,
      chromiumOptions,
      logLevel: "error",
      onStart: () => {},
      onFrameUpdate: () => {},
      onFrameBuffer: sceneOnly
        ? undefined
        : (buffer, frame) => {
            written.add(frame);
            writes.push(finished(buffer).then((png) => fs.promises.writeFile(path.join(outDir, name(frame)), png)));
          },
      onBrowserLog: (log) => {
        if (debug && log.text.startsWith(DEBUG_TAG)) debug.write(`${log.text.slice(DEBUG_TAG.length)}\n`);
      },
    });
  }
  await Promise.all(writes);
  if (!sceneOnly) for (const f of frames) if (!written.has(f)) throw new Error(`frame ${f} was not rendered`);
  let blank = 0;
  if (firstBlank >= 0 && !sceneOnly) {
    const src = path.join(outDir, name(firstBlank));
    for (let f = 0; f < total; f++) {
      if (written.has(f)) continue;
      const dst = path.join(outDir, name(f));
      fs.rmSync(dst, { force: true });
      try {
        fs.linkSync(src, dst);
      } catch {
        fs.copyFileSync(src, dst);
      }
      blank++;
    }
  }
  if (debug) await new Promise((resolve) => debug.end(resolve));
  if (pool) await pool.close();
  console.log(
    JSON.stringify({
      ok: true,
      out: path.resolve(outDir),
      pattern: "%06d.png",
      frames: total,
      rendered: frames.length,
      blank,
      fps,
      width: composition.width,
      height: composition.height,
      concurrency,
      gapFrames: gaps.frames,
      gapPx: gaps.px,
      gapSeconds: Math.round(gaps.ms) / 1000,
      gapFailed: gaps.failed,
      seconds: (Date.now() - started) / 1000,
      renderSeconds: (Date.now() - opened) / 1000,
    }),
  );
} catch (err) {
  if (debug) debug.end();
  if (browser) await browser.close({ silent: true }).catch(() => {});
  fail(err && err.stack ? err.stack : err, 1);
}
await browser.close({ silent: true }).catch(() => {});
process.exit(0);
