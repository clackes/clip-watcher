import React, { useLayoutEffect, useRef } from "react";
import { decompose, mul, toSvg, translate } from "./affine";
import { outline, outlineFor, textAttrs } from "./measure";
import type { Grad, Rect, Scene, Shape, Unit } from "./scene";

/**
 * Draws a Scene as one SVG. Text is drawn as the font's glyph outlines (vector paths, round-joined
 * strokes): exact at any sub-pixel position, scale and rotation, and the same on every machine. Units
 * that share a z are drawn layer by layer (all outlines, then all fills), so the outline of one word
 * never covers the fill of its neighbour.
 */

export const DEBUG_TAG = "CWDBG";

type Draw = { z: number; pass: number; order: number; node: React.ReactNode };

const gradDef = (id: string, g: Grad, dx = 0, dy = 0) => (
  <linearGradient key={id} id={id} gradientUnits="userSpaceOnUse" x1={g.x1 - dx} y1={g.y1 - dy} x2={g.x2 - dx} y2={g.y2 - dy}>
    {g.stops.map((s, i) => (
      <stop key={i} offset={s.o} stopColor={s.c} />
    ))}
  </linearGradient>
);

const clipDef = (id: string, c: Rect) => (
  <clipPath key={id} id={id} clipPathUnits="userSpaceOnUse">
    <rect x={c.x} y={c.y} width={Math.max(0, c.w)} height={Math.max(0, c.h)} />
  </clipPath>
);

const clipped = (key: string, clipId: string | null, node: React.ReactNode) =>
  clipId ? (
    <g key={key} clipPath={`url(#${clipId})`}>
      {node}
    </g>
  ) : (
    node
  );

function shapeNode(s: Shape, defs: React.ReactNode[]): React.ReactNode {
  const key = `s-${s.id}`;
  let fill = s.fill ?? "none";
  if (s.grad) {
    defs.push(gradDef(`g-${s.id}`, s.grad));
    fill = `url(#g-${s.id})`;
  }
  const common = {
    fill,
    stroke: s.stroke ?? "none",
    strokeWidth: s.stroke ? (s.strokeWidth ?? 0) : 0,
    strokeLinejoin: "round" as const,
    opacity: s.alpha ?? 1,
    transform: toSvg(s.m),
    "data-s": s.id,
  };
  let node: React.ReactNode;
  if (s.kind === "rect") {
    node = <rect key={key} x={s.x} y={s.y} width={Math.max(0, s.w)} height={Math.max(0, s.h)} rx={s.r ?? 0} {...common} />;
  } else if (s.kind === "ellipse") {
    node = <ellipse key={key} cx={s.x} cy={s.y} rx={Math.max(0, s.w)} ry={Math.max(0, s.h)} {...common} />;
  } else {
    node = <path key={key} d={s.d ?? ""} {...common} />;
  }
  if (s.clip) {
    defs.push(clipDef(`c-${s.id}`, s.clip));
    return clipped(`${key}-c`, `c-${s.id}`, node);
  }
  return node;
}

/**
 * An emoji is a colour picture, not an outline: there is nothing to stroke, and a "stroke" layer would
 * paint the whole picture again. So an emoji unit is drawn once as text, and every stroke layer of its
 * stack becomes a filter step that grows the picture's own alpha into a halo of that layer's colour.
 */
function emojiDraw(u: Unit, order: number, emoji: string, defs: React.ReactNode[], clipId: string | null): Draw[] {
  const attrs = textAttrs(u.font, emoji, u.size, 0);
  const id = `e-${u.id}`;
  const halos = u.layers.filter((l) => l.kind === "stroke");
  const pad = Math.max(8, ...halos.map((l) => l.width + Math.hypot(l.dx ?? 0, l.dy ?? 0))) + 4;
  defs.push(
    <filter
      key={id}
      id={id}
      filterUnits="userSpaceOnUse"
      x={u.x - pad}
      y={u.y - u.size * 1.2 - pad}
      width={u.w + 2 * pad}
      height={u.size * 1.7 + 2 * pad}
      colorInterpolationFilters="sRGB"
    >
      {/* the picture's own alpha, without the stray near-transparent pixels at the corners of the font's
          bitmaps: grown into a halo they would show as faint squares */}
      <feComponentTransfer in="SourceAlpha" result="solid">
        <feFuncA type="linear" slope={3} intercept={-0.3} />
      </feComponentTransfer>
      <feComponentTransfer in="SourceGraphic" result="pic">
        <feFuncA type="linear" slope={1.08} intercept={-0.07} />
      </feComponentTransfer>
      {halos.map((l, i) => (
        <React.Fragment key={i}>
          <feMorphology in="solid" operator="dilate" radius={Math.max(0, l.width)} result={`d${i}`} />
          <feOffset in={`d${i}`} dx={l.dx ?? 0} dy={l.dy ?? 0} result={`o${i}`} />
          <feFlood floodColor={l.color} result={`f${i}`} />
          <feComposite in={`f${i}`} in2={`o${i}`} operator="in" result={`h${i}`} />
        </React.Fragment>
      ))}
      <feMerge>
        {halos.map((_, i) => (
          <feMergeNode key={i} in={`h${i}`} />
        ))}
        <feMergeNode in="pic" />
      </feMerge>
    </filter>,
  );
  const node = (
    <g key={`u-${u.id}`} transform={toSvg(u.m)}>
      <g filter={`url(#${id})`}>
        <text x={u.x} y={u.y} fill="#000000" {...attrs} data-u={u.id}>
          {u.text}
        </text>
      </g>
    </g>
  );
  return [{ z: u.z, pass: -1, order, node: clipped(`u-${u.id}-c`, clipId, node) }];
}

function unitDraws(u: Unit, order: number, emoji: string, defs: React.ReactNode[]): Draw[] {
  let clipId: string | null = null;
  if (u.clip) {
    clipId = `c-${u.id}`;
    defs.push(clipDef(clipId, u.clip));
  }
  if (u.emoji) return emojiDraw(u, order, emoji, defs, clipId);
  // the glyph outlines once, with the baseline start at the origin; every layer is a <use> of them
  const pid = `o-${u.id}`;
  const shape = outline(u.font, u.glyphs, u.size);
  defs.push(<path key={pid} id={pid} d={shape} />);
  // the outline layers share the outlines without the counters they would close to a pinhole between them
  const rims = u.layers.filter((l) => l.kind !== "fill");
  const closed = outlineFor(
    u.font,
    u.glyphs,
    u.size,
    Math.max(0, ...rims.map((l) => l.width)),
    Math.max(0, ...rims.map((l) => Math.abs(l.dx ?? 0))),
    Math.max(0, ...rims.map((l) => Math.abs(l.dy ?? 0))),
  );
  const solidId = closed === shape ? pid : `${pid}-s`;
  if (closed !== shape) defs.push(<path key={solidId} id={solidId} d={closed} />);
  const lastFill = u.layers.map((l) => l.kind).lastIndexOf("fill");
  const out: Draw[] = [];
  u.layers.forEach((l, i) => {
    const key = `u-${u.id}-${i}`;
    const dx = u.x + (l.dx ?? 0);
    const dy = u.y + (l.dy ?? 0);
    const transform = toSvg(mul(u.m, translate(dx, dy)));
    let node: React.ReactNode;
    if (l.kind === "fill") {
      let fill = l.color;
      if (l.grad) {
        defs.push(gradDef(`g-${u.id}-${i}`, l.grad, dx, dy));
        fill = `url(#g-${u.id}-${i})`;
      }
      node = <use key={key} href={`#${pid}`} transform={transform} fill={fill} {...(i === lastFill ? { "data-u": u.id } : {})} />;
    } else if (l.kind === "stroke") {
      node = (
        <use
          key={key}
          href={`#${solidId}`}
          transform={transform}
          fill={l.color}
          stroke={l.color}
          strokeWidth={Math.max(0, l.width) * 2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      );
    } else {
      const n = Math.max(1, Math.round(l.steps));
      node = (
        <g key={key}>
          {Array.from({ length: n }, (_, s) => (
            <use
              key={s}
              href={`#${solidId}`}
              transform={transform}
              fill={l.color}
              stroke={l.color}
              strokeWidth={((l.width * (n - s)) / n) * 2}
              strokeLinejoin="round"
              strokeLinecap="round"
              opacity={l.alpha}
            />
          ))}
        </g>
      );
    }
    out.push({ z: u.z, pass: i - u.layers.length, order, node: clipped(`${key}-c`, clipId, node) });
  });
  return out;
}

const r2 = (v: number) => Math.round(v * 100) / 100;

export const Stage: React.FC<{
  scene: Scene;
  width: number;
  height: number;
  emoji: string;
  debug: boolean;
  frame: number;
  time: number;
}> = ({ scene, width, height, emoji, debug, frame, time }) => {
  const ref = useRef<SVGSVGElement>(null);
  const defs: React.ReactNode[] = [];
  const draws: Draw[] = [];
  scene.shapes.forEach((s, i) => draws.push({ z: s.z, pass: -1000, order: i, node: shapeNode(s, defs) }));
  scene.units.forEach((u, i) => draws.push(...unitDraws(u, i, emoji, defs)));
  draws.sort((a, b) => a.z - b.z || a.pass - b.pass || a.order - b.order);

  useLayoutEffect(() => {
    if (!debug || !ref.current) return;
    const svg = ref.current;
    const box = (el: Element | null) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return [r2(r.left), r2(r.top), r2(r.width), r2(r.height)];
    };
    const units = scene.units.map((u) => {
      const d = decompose(u.m);
      return {
        id: u.id,
        p: u.page,
        w: u.word,
        c: u.letter,
        lb: u.label ? 1 : 0,
        tx: u.text,
        a: u.active ? 1 : 0,
        e: u.emph ? 1 : 0,
        em: u.emoji ? 1 : 0,
        fg: u.emoji ? [] : u.fg,
        m: u.m.map((v) => Math.round(v * 1e4) / 1e4),
        sc: Math.round(d.scale * 1e4) / 1e4,
        rot: r2(d.rot),
        z: u.z,
        // the fill as drawn: the glyphs' own ink box (the em box for an emoji)
        box: box(svg.querySelector(`[data-u="${u.id}"]`)),
        clip: u.clip ? [r2(u.clip.x), r2(u.clip.y), r2(u.clip.w), r2(u.clip.h)] : null,
        // how far the outline reaches outside the fill, and how far any layer reaches at all (px, unscaled)
        halo: Math.max(0, ...u.layers.map((l) => (l.kind === "stroke" && !l.dx && !l.dy ? l.width : 0))),
        reach: Math.max(0, ...u.layers.map((l) => (l.kind === "fill" ? 0 : l.width) + Math.hypot(l.dx ?? 0, l.dy ?? 0))),
        // everything else that decides how the unit looks: a change here is a change of the scene
        ly: u.layers
          .map((l) => {
            const g = l.kind === "fill" && l.grad ? [l.grad.x1, l.grad.y1, l.grad.x2, l.grad.y2].map(r2).join(",") + l.grad.stops.map((t) => `${r2(t.o)}${t.c}`).join("") : "";
            return `${l.kind[0]}${l.color}${l.kind === "fill" ? "" : r2(l.width)}${r2(l.dx ?? 0)},${r2(l.dy ?? 0)}${g}`;
          })
          .join("|"),
      };
    });
    const shapes = scene.shapes.map((s) => ({
      id: s.id,
      role: s.role,
      fill: s.fill,
      alpha: s.alpha ?? 1,
      ly: `${s.stroke ?? ""}${r2(s.strokeWidth ?? 0)}${s.grad ? s.grad.stops.map((t) => `${r2(t.o)}${t.c}`).join("") : ""}`,
      box: box(svg.querySelector(`[data-s="${s.id}"]`)),
    }));
    console.log(DEBUG_TAG + JSON.stringify({ f: frame, t: Math.round(time * 1e5) / 1e5, u: units, s: shapes }));
  });

  return (
    <svg
      ref={ref}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      style={{ position: "absolute", left: 0, top: 0, overflow: "hidden" }}
      shapeRendering="geometricPrecision"
    >
      <defs>{defs}</defs>
      {draws.map((d) => d.node)}
    </svg>
  );
};
