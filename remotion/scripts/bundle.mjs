// Build the Remotion bundle once: node scripts/bundle.mjs [--out build] [--stamp <hash>]
// Renders reuse it, so a clip pays for rendering only. The stamp (a hash of the sources, computed by the
// caller) is stored next to the bundle; clipwatcher rebuilds when the sources no longer match it.
import { bundle } from "@remotion/bundler";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : fallback;
};
const outDir = path.resolve(opt("out", path.join(root, "build")));
const stamp = opt("stamp", "");

const started = Date.now();
const tmp = `${outDir}.tmp-${process.pid}`;
fs.rmSync(tmp, { recursive: true, force: true });
await bundle({
  entryPoint: path.join(root, "src", "index.ts"),
  outDir: tmp,
  publicDir: path.join(root, "..", "assets", "fonts"),
  enableCaching: false,
});
const pkg = JSON.parse(fs.readFileSync(path.join(root, "node_modules", "remotion", "package.json"), "utf8"));
fs.writeFileSync(
  path.join(tmp, "clipwatcher-bundle.json"),
  JSON.stringify({ stamp, remotion: pkg.version, built: new Date().toISOString() }, null, 2),
);
// swap in whole: a render never sees half a bundle
const old = `${outDir}.old-${process.pid}`;
if (fs.existsSync(outDir)) fs.renameSync(outDir, old);
fs.renameSync(tmp, outDir);
fs.rmSync(old, { recursive: true, force: true });
console.log(JSON.stringify({ ok: true, out: outDir, stamp, seconds: (Date.now() - started) / 1000 }));
process.exit(0);
