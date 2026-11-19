// One-time setup after `npm ci`: download the headless browser Remotion renders with, then build the
// bundle. After this nothing is fetched at render time.
import { ensureBrowser } from "@remotion/renderer";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
await ensureBrowser();
const r = spawnSync(process.execPath, [path.join(here, "bundle.mjs"), ...process.argv.slice(2)], { stdio: "inherit" });
process.exit(r.status ?? 1);
