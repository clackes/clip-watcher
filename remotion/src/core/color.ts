/** Colour maths: sRGB hex <-> OKLab, mixing in OKLab (no muddy midpoints), WCAG luminance. Pure. */

export type RGB = [number, number, number]; // 0..255

export function parseHex(hex: string): RGB {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export function toHex([r, g, b]: RGB): string {
  const c = (v: number) =>
    Math.round(Math.min(255, Math.max(0, v)))
      .toString(16)
      .padStart(2, "0");
  return `#${c(r)}${c(g)}${c(b)}`.toUpperCase();
}

const toLinear = (v: number): number => {
  const s = v / 255;
  return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
};
const fromLinear = (l: number): number => {
  const c = Math.min(1, Math.max(0, l));
  return 255 * (c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055);
};

export function toOklab([r, g, b]: RGB): [number, number, number] {
  const lr = toLinear(r);
  const lg = toLinear(g);
  const lb = toLinear(b);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

export function fromOklab([L, a, b]: [number, number, number]): RGB {
  const l = Math.pow(L + 0.3963377774 * a + 0.2158037573 * b, 3);
  const m = Math.pow(L - 0.1055613458 * a - 0.0638541728 * b, 3);
  const s = Math.pow(L - 0.0894841775 * a - 1.291485548 * b, 3);
  return [
    fromLinear(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    fromLinear(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    fromLinear(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ];
}

const mixCache = new Map<string, string>();

/** Colour between a and b (t = 0..1), interpolated in OKLab. Returns #RRGGBB. */
export function mix(a: string, b: string, t: number): string {
  if (t <= 0 || a === b) return a.toUpperCase();
  if (t >= 1) return b.toUpperCase();
  const q = Math.round(t * 255); // 256 steps are finer than an 8-bit channel can show
  const key = `${a}${b}${q}`;
  const hit = mixCache.get(key);
  if (hit) return hit;
  const A = toOklab(parseHex(a));
  const B = toOklab(parseHex(b));
  const k = q / 255;
  const out = toHex(fromOklab([A[0] + (B[0] - A[0]) * k, A[1] + (B[1] - A[1]) * k, A[2] + (B[2] - A[2]) * k]));
  if (mixCache.size < 20000) mixCache.set(key, out);
  return out;
}

/** WCAG 2.x relative luminance of an sRGB colour. */
export function luminance(hex: string): number {
  const [r, g, b] = parseHex(hex);
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

/** WCAG 2.x contrast ratio, 1..21. */
export function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Lighter (k > 0) or darker (k < 0) version of a colour: OKLab lightness shifted by k. */
export function shade(hex: string, k: number): string {
  const [L, a, b] = toOklab(parseHex(hex));
  return toHex(fromOklab([Math.min(1, Math.max(0, L + k)), a, b]));
}

/** Same colour with its hue turned by `deg` degrees in OKLCh. */
export function rotateHue(hex: string, deg: number): string {
  const [L, a, b] = toOklab(parseHex(hex));
  const c = Math.hypot(a, b);
  const h = Math.atan2(b, a) + (deg * Math.PI) / 180;
  return toHex(fromOklab([L, c * Math.cos(h), c * Math.sin(h)]));
}
