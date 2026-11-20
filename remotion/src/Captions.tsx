import React, { useLayoutEffect, useMemo } from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { mix } from "./core/color";
import { E, prog, pulse } from "./core/ease";
import { useFonts } from "./core/fonts";
import { fitWords, placeY, type Layout } from "./core/layout";
import { covered, fontMetrics } from "./core/measure";
import type { BaseParams, CaptionProps, Colors, FontSpec, Label, Page } from "./core/schema";
import type { Ctx, LabelCtx, Scene, WordState } from "./core/scene";
import { DEBUG_TAG, Stage } from "./core/Stage";
import type { Template } from "./core/template";
import { getTemplate } from "./templates";

type Placed = { layout: Layout; cx: number; cy: number };
type Plan = { pages: Placed[]; labels: Placed[] };

function place(
  texts: { text: string; scale: number; font?: FontSpec }[],
  P: BaseParams,
  props: CaptionProps,
  size: number,
  maxLines: number,
  tilt: number,
): Placed {
  const r = props.region;
  const boxW = r.x1 - r.x0 - 2 * P.margin.x;
  const boxH = r.y1 - r.y0 - 2 * P.margin.y;
  const fit = (s: number) =>
    fitWords(texts, {
      font: P.font,
      emoji: props.emojiFamily,
      size: s,
      lineHeight: P.lineHeight,
      letterSpacing: P.letterSpacing,
      wordGap: P.wordGap,
      grow: P.grow,
      maxLines,
      boxW,
      boxH,
      upper: P.case === "upper",
    });
  let layout = fit(size);
  if (tilt > 0) {
    // a block turned by `tilt` degrees needs a bigger box: shrink until the turned block fits
    const sin = Math.sin((tilt * Math.PI) / 180);
    const cos = Math.cos((tilt * Math.PI) / 180);
    for (let i = 0; i < 3; i++) {
      const w = layout.right - layout.left;
      const h = layout.bottom - layout.top;
      const k = Math.min(1, boxW / (w * cos + h * sin), boxH / (w * sin + h * cos));
      if (k >= 0.999) break;
      layout = fit(layout.size * k);
    }
  }
  return {
    layout,
    cx: (r.x0 + r.x1) / 2,
    cy: placeY(layout, r.anchorY, r.y0 + P.margin.y, r.y1 - P.margin.y),
  };
}

function prepare(props: CaptionProps, P: BaseParams): Plan {
  return {
    pages: props.pages.map((p) =>
      place(
        p.words.map((w) => ({
          text: w.text,
          scale: w.emph ? P.emphScale : 1,
          font: w.emph && P.emphFont ? P.emphFont : undefined,
        })),
        P,
        props,
        P.size,
        P.paging.maxLines,
        P.tilt,
      ),
    ),
    labels: props.labels.map((l) =>
      place(
        l.text.split(/\s+/).filter(Boolean).map((text) => ({ text, scale: 1 })),
        P,
        props,
        P.label.size,
        2,
        Math.max(P.tilt, 2 * Math.abs(P.label.rotate)),
      ),
    ),
  };
}

const ROLES = ["text", "active", "emph", "stroke", "shadow", "plate", "plateText", "accent", "accent2"] as const;

/** A page's colours at local time t: eased in from the page before when the scheme changed. */
function colorsAt(to: Colors, from: Colors | null, t: number, colorS: number): Colors {
  if (!from) return to;
  const k = E.inOutQuad(prog(t, 0, colorS));
  if (k >= 1) return to;
  const out = { ...to, cycle: to.cycle.map((c, i) => mix(from.cycle[i % from.cycle.length], c, k)) };
  for (const r of ROLES) out[r] = mix(from[r], to[r], k);
  return out;
}

function pageScene(tpl: Template, P: BaseParams, props: CaptionProps, page: Page, index: number, pl: Placed, T: number, fps: number): Scene {
  const t = T - page.start;
  const dur = page.end - page.start;
  const exit = Math.min(page.exit, dur);
  const words: WordState[] = page.words.map((w, i) => {
    const start = w.start - page.start;
    const activeEnd = w.activeEnd - page.start;
    const box = pl.layout.words[i];
    return {
      index: i,
      text: w.text,
      emph: w.emph,
      start,
      end: w.end - page.start,
      activeEnd,
      phase: t < start ? "ahead" : t < activeEnd ? "active" : "done",
      a: pulse(t, start, activeEnd, P.timing.activeInS, P.timing.activeOutS),
      since: t - start,
      box,
      cx: pl.cx + box.cx,
      cy: pl.cy + box.cy,
    };
  });
  const c: Ctx = {
    P,
    t,
    T,
    dur,
    fps,
    exit,
    exitT: dur - exit,
    out: exit > 0 ? prog(t, dur - exit, exit) : 0,
    page,
    index,
    words,
    layout: pl.layout,
    cx: pl.cx,
    cy: pl.cy,
    region: props.region,
    colors: colorsAt(page.colors, page.from, t, P.timing.colorS),
    emoji: props.emojiFamily,
    width: props.width,
    height: props.height,
  };
  return tpl.build(c);
}

function labelScene(tpl: Template, P: BaseParams, props: CaptionProps, label: Label, index: number, pl: Placed, T: number, fps: number): Scene {
  const t = T - label.start;
  const dur = label.end - label.start;
  const exit = Math.min(label.exit, dur);
  const words: WordState[] = pl.layout.words.map((box, i) => ({
    index: i,
    text: box.text,
    emph: false,
    start: 0,
    end: dur,
    activeEnd: dur,
    phase: "active",
    a: 1,
    since: t,
    box,
    cx: pl.cx + box.cx,
    cy: pl.cy + box.cy,
  }));
  const c: LabelCtx = {
    P,
    t,
    T,
    dur,
    fps,
    exit,
    exitT: dur - exit,
    out: exit > 0 ? prog(t, dur - exit, exit) : 0,
    label,
    index,
    words,
    layout: pl.layout,
    cx: pl.cx,
    cy: pl.cy,
    region: props.region,
    colors: label.colors,
    emoji: props.emojiFamily,
    width: props.width,
    height: props.height,
  };
  return tpl.label(c);
}

/** Everything on screen at time T (seconds). Pure: the same props and T always give the same scene. */
export function sceneAt(tpl: Template, P: BaseParams, props: CaptionProps, plan: Plan, T: number, fps: number): Scene {
  const scene: Scene = { units: [], shapes: [] };
  props.pages.forEach((p, i) => {
    if (T >= p.start && T < p.end) {
      const s = pageScene(tpl, P, props, p, i, plan.pages[i], T, fps);
      scene.units.push(...s.units);
      scene.shapes.push(...s.shapes);
    }
  });
  props.labels.forEach((l, i) => {
    if (T >= l.start && T < l.end) {
      const s = labelScene(tpl, P, props, l, i, plan.labels[i], T, fps);
      scene.units.push(...s.units);
      scene.shapes.push(...s.shapes);
    }
  });
  return scene;
}

export const Captions: React.FC<CaptionProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const tpl = getTemplate(props.template);
  const P = useMemo(() => tpl.schema.parse(props.params) as BaseParams, [tpl, props.params]);
  const fonts = useMemo(() => [P.font, ...(P.emphFont ? [P.emphFont] : []), ...(tpl.fonts?.(P) ?? [])], [P, tpl]);
  const ready = useFonts(fonts);
  const plan = useMemo(() => (ready ? prepare(props, P) : null), [ready, props, P]);

  useLayoutEffect(() => {
    if (!props.debug || !plan) return;
    // once per browser tab: which fonts drew the text, and how every page was laid out
    const texts = [...props.pages.flatMap((p) => p.words.map((w) => w.text)), ...props.labels.map((l) => l.text)];
    const missing = texts.filter((t) => !covered(t, P.font, props.emojiFamily) || (P.emphFont && !covered(t, P.emphFont, props.emojiFamily)));
    console.log(
      `${DEBUG_TAG}INIT${JSON.stringify({
        fonts: fonts.map((f) => f.file),
        emoji: props.emojiFamily,
        metrics: fontMetrics(P.font),
        missing,
        pages: plan.pages.map((pl, i) => ({
          id: props.pages[i].id,
          size: Math.round(pl.layout.size * 100) / 100,
          fit: Math.round(pl.layout.fit * 1000) / 1000,
          cx: pl.cx,
          cy: Math.round(pl.cy * 100) / 100,
          words: pl.layout.words.map((w) => ({
            t: w.text,
            x: Math.round(w.x * 100) / 100,
            w: Math.round(w.w * 100) / 100,
            g: w.glyphs.map((g) => Math.round(g.x * 100) / 100),
          })),
          lines: pl.layout.lines.map((l) => ({ w: Math.round(l.w * 100) / 100, y: Math.round(l.y * 100) / 100, n: l.words.length })),
        })),
      })}`,
    );
  }, [plan, props, P, fonts]);

  if (!plan) return null;
  // the middle of the frame's time slot: a word that starts anywhere inside the frame is on in that frame
  const T = (frame + 0.5) / fps;
  const scene = sceneAt(tpl, P, props, plan, T, fps);
  return (
    <AbsoluteFill>
      <Stage scene={scene} width={width} height={height} emoji={props.emojiFamily} debug={props.debug} frame={frame} time={T} />
    </AbsoluteFill>
  );
};
