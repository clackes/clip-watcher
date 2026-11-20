// Render a caption overlay: node scripts/render.mjs --props props.json --out <dir> [options]
//
// Writes one transparent PNG per frame of the clip into <dir> as 000000.png, 000001.png, ... (lossless,
// straight alpha: ffmpeg composites them exactly). Frames on which nothing is on screen are not rendered:
// they are hard links to one blank frame. Prints one JSON line with the result on stdout.
//
//   --bundle <dir>       the bundle from scripts/bundle.mjs (default: ./build)
//   --concurrency <n>    browser tabs rendering in parallel (default: all cores)
//   --debug <file>       also write every frame's scene (positions, scales, colours) as JSON lines
//   --all-frames         render blank frames too (the frame-by-frame harness checks them)
//   --scene-only         no pictures: every frame is laid out and its scene written (--debug), nothing is
//                        drawn (the frame checks' phase sweep reads positions, scales and colours only)
//   --gl <backend>       Chromium GL backend (angle, swangle, ...), default: Remotion's
import { openBrowser, renderFrames, selectComposition } from "@remotion/renderer";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
if (sceneOnly && !debugFile) fail("--scene-only needs --debug <file>", 2);
const gl = opt("gl", null);
const chromiumOptions = gl ? { gl } : {};
const name = (f) => `${String(f).padStart(6, "0")}.png`;

const started = Date.now();
fs.mkdirSync(outDir, { recursive: true });
const debug = debugFile ? fs.createWriteStream(debugFile) : null;
let browser = null;
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
            writes.push(fs.promises.writeFile(path.join(outDir, name(frame)), buffer));
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
