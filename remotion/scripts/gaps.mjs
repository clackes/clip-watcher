// No speck of video inside the caption: closeGaps(png) -> { png, px } | null
//
// Two outlines that almost meet wall in a gap a few pixels across, with the video showing through: between
// two letters that move on their own, between a box and the word beside it, in a counter while its word is
// still small. Such gaps come and go with every frame of every motion, between any two shapes, so there is
// no list of shapes to correct: the rule is applied where a gap exists, on the finished frame, before it
// is written. A gap is a see-through region (alpha under one half, the line the frame checks draw) that
//   - no path of see-through pixels joins to the outside of the drawing (up, down, left, right), and
//   - has no room for a 5 x 5 px square: the counter of a big letter is not a gap.
// It is painted in the colour of the ink that walls it in, and the soft edge pixels of that ink around it
// become opaque. No other pixel is touched: a frame without a gap is returned as null and written as the
// browser made it. Pure functions of the pixels, so two renders stay identical.
//
// gapPool(n) runs the same on n worker threads (the render does not wait for it on its own thread).
// As a command, for the tests: node scripts/gaps.mjs <in.png> <out.png> prints {"px": n}
import fs from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { isMainThread, parentPort, Worker } from "node:worker_threads";
import zlib from "node:zlib";

const SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const OPENING = 5; // px: a see-through region with room for a square this size is an opening, not a gap
const HALF = 128; // alpha from here on is ink
const UP = 2; // the PNG row filter the browser uses: every byte against the one above it

const CRC = new Uint32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  CRC[n] = c >>> 0;
}
function crc32(...parts) {
  let c = 0xffffffff;
  for (const p of parts) for (let i = 0; i < p.length; i++) c = CRC[(c ^ p[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** The chunks of a PNG and its packed pixel data; null for anything but 8-bit RGBA without interlace. */
function read(png) {
  if (png.length < 33 || !png.subarray(0, 8).equals(SIGNATURE)) return null;
  const chunks = [];
  const idat = [];
  let head = null;
  for (let p = 8; p + 12 <= png.length; ) {
    const len = png.readUInt32BE(p);
    const type = png.toString("latin1", p + 4, p + 8);
    const body = png.subarray(p + 8, p + 8 + len);
    if (type === "IHDR") head = body;
    if (type === "IDAT") idat.push(body);
    chunks.push({ type, raw: png.subarray(p, p + 12 + len) });
    p += 12 + len;
  }
  if (!head || idat.length === 0) return null;
  const width = head.readUInt32BE(0);
  const height = head.readUInt32BE(4);
  if (head[8] !== 8 || head[9] !== 6 || head[12] !== 0) return null;
  const packed = zlib.inflateSync(idat.length === 1 ? idat[0] : Buffer.concat(idat));
  const line = width * 4 + 1;
  if (packed.length !== height * line) return null;
  let up = true;
  for (let y = 0; y < height && up; y++) up = packed[y * line] === UP;
  return { width, height, chunks, packed, line, up };
}

/** One channel of the picture (0 red .. 3 alpha): PNG row filters work channel by channel. */
function channel(img, ch) {
  const { width: w, height: h, packed, line } = img;
  const out = new Uint8Array(w * h);
  if (img.up) {
    for (let x = 0, s = 1 + ch; x < w; x++, s += 4) out[x] = packed[s];
    for (let y = 1; y < h; y++) {
      const d = y * w;
      for (let x = 0, s = y * line + 1 + ch; x < w; x++, s += 4) out[d + x] = (packed[s] + out[d + x - w]) & 255;
    }
    return out;
  }
  for (let y = 0; y < h; y++) {
    const filter = packed[y * line];
    const s = y * line + 1 + ch;
    const d = y * w;
    for (let x = 0; x < w; x++) {
      const a = x ? out[d + x - 1] : 0;
      const b = y ? out[d + x - w] : 0;
      let v = packed[s + 4 * x];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const c = x && y ? out[d + x - w - 1] : 0;
        const pa = Math.abs(b - c);
        const pb = Math.abs(a - c);
        const pc = Math.abs(a + b - 2 * c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (filter !== 0) throw new Error(`png row filter ${filter}`);
      out[d + x] = v & 255;
    }
  }
  return out;
}

/**
 * The gap pixels of an alpha plane, as indices, or null when there is none. Looks only at the box around
 * the ink: everything beyond it is the outside.
 */
export function gapsOf(alpha, w, h) {
  let x0 = w;
  let x1 = -1;
  let y0 = h;
  let y1 = -1;
  for (let y = 0; y < h; y++) {
    const row = y * w;
    for (let x = 0; x < w; x++) {
      if (alpha[row + x] >= HALF) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        y1 = y;
      }
    }
  }
  if (x1 < 0) return null;
  // a border of clear pixels round the box (the outside), then the box itself: 0 ink, 1 clear, 2 reached
  const bw = x1 - x0 + 3;
  const bh = y1 - y0 + 3;
  const size = bw * bh;
  const state = new Uint8Array(size);
  let clear = 0;
  for (let y = 0; y < bh; y++) {
    const edge = y === 0 || y === bh - 1;
    const src = (y0 - 1 + y) * w + x0 - 1;
    for (let x = 0; x < bw; x++) {
      if (edge || x === 0 || x === bw - 1) state[y * bw + x] = 1;
      else if (alpha[src + x] < HALF) {
        state[y * bw + x] = 1;
        clear++;
      }
    }
  }
  if (clear === 0) return null;
  // the outside reaches every clear pixel a path of clear pixels leads to
  const stack = new Int32Array(size);
  let top = 0;
  let reached = 0;
  const flood = (from, was, now, each) => {
    state[from] = now;
    stack[top++] = from;
    while (top > 0) {
      const i = stack[--top];
      const x = i % bw;
      if (x > 0 && state[i - 1] === was) {
        state[i - 1] = now;
        stack[top++] = i - 1;
      }
      if (x < bw - 1 && state[i + 1] === was) {
        state[i + 1] = now;
        stack[top++] = i + 1;
      }
      if (i >= bw && state[i - bw] === was) {
        state[i - bw] = now;
        stack[top++] = i - bw;
      }
      if (i + bw < size && state[i + bw] === was) {
        state[i + bw] = now;
        stack[top++] = i + bw;
      }
      each(i);
    }
  };
  flood(0, 1, 2, () => reached++);
  if (reached === clear + 2 * bw + 2 * bh - 4) return null; // nothing is walled in
  // walled-in regions, one at a time (3 while looked at): one with room for the square is an opening
  const run = new Uint16Array(size); // walled-in pixels in a row, ending here
  const tall = new Uint16Array(size); // rows on end, ending here, whose run is at least the square
  const out = [];
  for (let start = bw; start < size - bw; start++) {
    if (state[start] !== 1) continue;
    const region = [];
    flood(start, 1, 3, (i) => region.push(i));
    region.sort((a, b) => a - b);
    let roomy = false;
    for (const i of region) {
      run[i] = (state[i - 1] === 3 ? run[i - 1] : 0) + 1;
      tall[i] = run[i] >= OPENING ? (state[i - bw] === 3 ? tall[i - bw] : 0) + 1 : 0;
      if (tall[i] >= OPENING) roomy = true;
    }
    for (const i of region) {
      state[i] = 4; // looked at
      if (!roomy) out.push((y0 - 1 + Math.floor(i / bw)) * w + x0 - 1 + (i % bw));
    }
  }
  return out.length ? out : null;
}

/** The picture with its gaps closed, as a PNG, and how many pixels that took; null when it has none. */
export function closeGaps(png) {
  const img = read(png);
  if (!img) return null;
  const { width: w, height: h, packed, line } = img;
  const alpha = channel(img, 3);
  const gaps = gapsOf(alpha, w, h);
  if (!gaps) return null;
  const rgba = [channel(img, 0), channel(img, 1), channel(img, 2), alpha];
  const changed = new Set(gaps);
  // A gap takes the colour of its walls: ring by ring inwards, each pixel from the most opaque of the
  // coloured pixels around it (the first of them, in a fixed order, when several are alike).
  const open = new Uint8Array(w * h);
  for (const i of gaps) open[i] = 1;
  const around = [-w, -1, 1, w, -w - 1, -w + 1, w - 1, w + 1];
  let left = gaps;
  while (left.length > 0) {
    const ring = [];
    const later = [];
    for (const i of left) {
      const x = i % w;
      let from = -1;
      let most = 0;
      for (const d of around) {
        const j = i + d;
        if (j < 0 || j >= w * h || Math.abs((j % w) - x) > 1 || open[j]) continue;
        if (alpha[j] >= HALF && alpha[j] > most) {
          most = alpha[j];
          from = j;
        }
      }
      if (from < 0) later.push(i);
      else ring.push(i, from);
    }
    if (ring.length === 0) break; // (cannot be: a gap has walls)
    for (let k = 0; k < ring.length; k += 2) for (let ch = 0; ch < 3; ch++) rgba[ch][ring[k]] = rgba[ch][ring[k + 1]];
    for (let k = 0; k < ring.length; k += 2) {
      open[ring[k]] = 0;
      alpha[ring[k]] = 255;
    }
    left = later;
  }
  // the soft edge of the walls: what a wall pixel left uncovered showed the video too
  for (const i of gaps) {
    const x = i % w;
    for (const d of around) {
      const j = i + d;
      if (j >= 0 && j < w * h && Math.abs((j % w) - x) <= 1 && alpha[j] >= HALF && alpha[j] < 255) {
        alpha[j] = 255;
        changed.add(j);
      }
    }
  }
  // back into the packed rows. Packed as the browser does it (every byte against the one above), only
  // the changed pixels and the ones under them are written again; any other packing is redone whole.
  const put = (i) => {
    const s = Math.floor(i / w) * line + 1 + 4 * (i % w);
    for (let ch = 0; ch < 4; ch++) packed[s + ch] = (rgba[ch][i] - (i >= w ? rgba[ch][i - w] : 0)) & 255;
  };
  if (img.up) {
    for (const i of changed) {
      put(i);
      if (i + w < w * h && !changed.has(i + w)) put(i + w);
    }
  } else {
    for (let y = 0; y < h; y++) packed[y * line] = UP;
    for (let i = 0; i < w * h; i++) put(i);
  }
  const data = zlib.deflateSync(packed, { level: 1 });
  const parts = [SIGNATURE];
  let written = false;
  for (const c of img.chunks) {
    if (c.type !== "IDAT") parts.push(c.raw);
    else if (!written) {
      const head = Buffer.alloc(8);
      head.writeUInt32BE(data.length, 0);
      head.write("IDAT", 4, "latin1");
      const sum = Buffer.alloc(4);
      sum.writeUInt32BE(crc32(head.subarray(4), data), 0);
      parts.push(head, data, sum);
      written = true;
    }
  }
  return { png: Buffer.concat(parts), px: gaps.length };
}

/**
 * closeGaps on `n` worker threads: run(png) resolves to { png, px, ms } (the frame to write, the same
 * bytes when it had no gap; the pixels closed; the time it took). A worker that fails takes nothing with
 * it: its frames, and every frame after, are done on the calling thread. close() ends the threads: until
 * then they keep the process alive, so that it cannot end with a frame still on its way.
 */
export function gapPool(n) {
  const jobs = new Map(); // id -> { png, resolve, reject }
  let next = 0;
  let turn = 0;
  let workers = [];
  const here = (png) => {
    const t0 = performance.now();
    const done = closeGaps(png);
    return { png: done ? done.png : png, px: done ? done.px : 0, ms: performance.now() - t0 };
  };
  const settle = (job, fn) => {
    try {
      job.resolve(fn());
    } catch (err) {
      job.reject(err);
    }
  };
  const giveUp = () => {
    const mine = workers;
    workers = [];
    for (const wk of mine) wk.terminate().catch(() => {});
    for (const [id, job] of jobs) {
      jobs.delete(id);
      settle(job, () => here(job.png));
    }
  };
  for (let k = 0; k < n; k++) {
    try {
      const wk = new Worker(fileURLToPath(import.meta.url));
      wk.on("message", ({ id, png, px, ms, error }) => {
        const job = jobs.get(id);
        if (!job) return;
        jobs.delete(id);
        if (error) job.reject(new Error(error));
        else job.resolve({ png: png ? Buffer.from(png.buffer, png.byteOffset, png.byteLength) : job.png, px, ms });
      });
      wk.on("error", giveUp);
      wk.on("exit", () => workers.includes(wk) && giveUp());
      workers.push(wk);
    } catch {
      break;
    }
  }
  return {
    threads: () => workers.length,
    run: (png) =>
      new Promise((resolve, reject) => {
        if (workers.length === 0) return settle({ resolve, reject }, () => here(png));
        const id = next++;
        jobs.set(id, { png, resolve, reject });
        workers[turn++ % workers.length].postMessage({ id, png });
      }),
    close: async () => {
      const mine = workers;
      workers = [];
      await Promise.all(mine.map((wk) => wk.terminate().catch(() => {})));
    },
  };
}

if (!isMainThread && parentPort) {
  parentPort.on("message", ({ id, png }) => {
    try {
      const t0 = performance.now();
      const done = closeGaps(Buffer.from(png.buffer, png.byteOffset, png.byteLength));
      const ms = performance.now() - t0;
      if (!done) parentPort.postMessage({ id, png: null, px: 0, ms });
      else {
        const out = new Uint8Array(done.png); // its own memory: handed over, not copied
        parentPort.postMessage({ id, png: out, px: done.px, ms }, [out.buffer]);
      }
    } catch (err) {
      parentPort.postMessage({ id, error: String(err && err.message ? err.message : err) });
    }
  });
} else if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [src, dst] = process.argv.slice(2);
  if (!src || !dst) {
    console.error("usage: gaps.mjs <in.png> <out.png>");
    process.exit(2);
  }
  const png = fs.readFileSync(src);
  const done = closeGaps(png);
  fs.writeFileSync(dst, done ? done.png : png);
  console.log(JSON.stringify({ px: done ? done.px : 0 }));
}
