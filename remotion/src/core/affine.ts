/** 2D affine transforms as [a, b, c, d, e, f] (the SVG matrix() order): x' = a x + c y + e, y' = b x + d y + f. */

export type Affine = [number, number, number, number, number, number];

export const IDENTITY: Affine = [1, 0, 0, 1, 0, 0];

/** m1 then m2 is mul(m2, m1): the right-hand matrix is applied first. */
export function mul(m2: Affine, m1: Affine): Affine {
  return [
    m2[0] * m1[0] + m2[2] * m1[1],
    m2[1] * m1[0] + m2[3] * m1[1],
    m2[0] * m1[2] + m2[2] * m1[3],
    m2[1] * m1[2] + m2[3] * m1[3],
    m2[0] * m1[4] + m2[2] * m1[5] + m2[4],
    m2[1] * m1[4] + m2[3] * m1[5] + m2[5],
  ];
}

export const translate = (x: number, y: number): Affine => [1, 0, 0, 1, x, y];

export type Pose = {
  x?: number; // translation, px
  y?: number;
  scale?: number; // uniform scale
  sx?: number; // extra horizontal / vertical scale (squash and stretch)
  sy?: number;
  rot?: number; // degrees, clockwise on screen
  skewX?: number; // degrees
};

/**
 * Scale, skew and rotate about `origin`, then translate. This is the one way templates move things, so
 * an animation always has an explicit pivot.
 */
export function pose(origin: [number, number], p: Pose): Affine {
  const s = p.scale ?? 1;
  const sx = s * (p.sx ?? 1);
  const sy = s * (p.sy ?? 1);
  const r = ((p.rot ?? 0) * Math.PI) / 180;
  const k = Math.tan(((p.skewX ?? 0) * Math.PI) / 180);
  const cos = Math.cos(r);
  const sin = Math.sin(r);
  // R * Skew * Scale
  const a = cos * sx;
  const b = sin * sx;
  const c = (cos * k - sin) * sy;
  const d = (sin * k + cos) * sy;
  const [ox, oy] = origin;
  return [a, b, c, d, ox - a * ox - c * oy + (p.x ?? 0), oy - b * ox - d * oy + (p.y ?? 0)];
}

/** Several transforms, applied left to right (first argument first). */
export function chain(...ms: Affine[]): Affine {
  let out = IDENTITY;
  for (const m of ms) out = mul(m, out);
  return out;
}

export const apply = (m: Affine, x: number, y: number): [number, number] => [
  m[0] * x + m[2] * y + m[4],
  m[1] * x + m[3] * y + m[5],
];

const r5 = (v: number) => Math.round(v * 1e5) / 1e5;
export const toSvg = (m: Affine): string => `matrix(${m.map(r5).join(" ")})`;

/** Uniform scale and rotation (degrees) a matrix carries: for the frame-by-frame checks. */
export function decompose(m: Affine): { scale: number; rot: number } {
  const det = m[0] * m[3] - m[1] * m[2];
  return { scale: Math.sqrt(Math.abs(det)), rot: (Math.atan2(m[1], m[0]) * 180) / Math.PI };
}
