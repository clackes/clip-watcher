import { z } from "zod";

/** Props of the Captions composition. Python (clipwatcher.render.remotion) builds them; nothing here is
 * optional, so a render either gets a complete, valid description or fails before the first frame. */

export const hex = z.string().regex(/^#[0-9a-fA-F]{6}$/, "colour must be #RRGGBB");

/** Colour roles. Every template reads its colours from these, never from literals, so the fixed scheme
 * and the auto mode can both set them. */
export const colorsSchema = z.strictObject({
  text: hex, // base fill of a word
  active: hex, // the word being spoken (or its highlight box)
  emph: hex, // shouted words
  stroke: hex, // the halo right next to the fill: what guarantees the contrast
  shadow: hex, // drop shadow / extrusion
  plate: hex, // background plate, when the template has one
  plateText: hex, // text drawn on the plate
  accent: hex, // decoration (outer border, glow, underline)
  accent2: hex, // second decoration colour
  cycle: z.array(hex).min(1).max(8), // per-word colours for templates that rotate them
});
export type Colors = z.infer<typeof colorsSchema>;

export const wordSchema = z.strictObject({
  text: z.string().min(1).max(80),
  start: z.number().min(0), // seconds on the overlay timeline
  end: z.number().min(0), // end of the spoken word
  activeEnd: z.number().min(0), // the highlight leaves the word here (next word's start, or end + linger)
  emph: z.boolean(),
});
export type Word = z.infer<typeof wordSchema>;

export const pageSchema = z.strictObject({
  id: z.number().int().min(0),
  start: z.number().min(0), // first frame the page is on screen
  end: z.number().min(0), // first frame it is gone
  exit: z.number().min(0), // seconds of exit animation that fit before `end` (0 = hard cut to the next page)
  words: z.array(wordSchema).min(1).max(12),
  colors: colorsSchema,
  from: colorsSchema.nullable(), // colours of the page before, when this page should ease in from them
});
export type Page = z.infer<typeof pageSchema>;

export const labelKinds = ["fail", "hype", "shock", "funny", "action"] as const;
export const labelSchema = z.strictObject({
  id: z.number().int().min(0),
  start: z.number().min(0),
  end: z.number().min(0),
  exit: z.number().min(0),
  text: z.string().min(1).max(40),
  kind: z.enum(labelKinds),
  colors: colorsSchema,
});
export type Label = z.infer<typeof labelSchema>;

export const fontSchema = z.strictObject({
  family: z.string().min(1),
  // path under assets/fonts (the bundle's static files): "Anton-Regular.ttf", "captions/Poppins-Black.ttf"
  file: z.string().regex(/^(captions\/)?[A-Za-z0-9._-]+\.(ttf|otf)$/),
  weight: z.number().int().min(100).max(900), // of this file (for the record: one file is one face)
  style: z.enum(["normal", "italic"]),
});
export type FontSpec = z.infer<typeof fontSchema>;

export const regionSchema = z.strictObject({
  x0: z.number(), // ink must stay inside [x0, x1] x [y0, y1], overlay pixels
  y0: z.number(),
  x1: z.number(),
  y1: z.number(),
  anchorY: z.number(), // where the block centre goes when it fits there
});
export type Region = z.infer<typeof regionSchema>;

export const propsSchema = z.strictObject({
  template: z.string().min(1),
  params: z.record(z.string(), z.unknown()), // checked against the template's own schema
  fps: z.number().min(1).max(120),
  width: z.number().int().min(16).max(4096),
  height: z.number().int().min(16).max(4096),
  durationInFrames: z.number().int().min(1),
  region: regionSchema,
  pages: z.array(pageSchema),
  labels: z.array(labelSchema),
  // CSS family of the system emoji font ("Apple Color Emoji"), or "" when the render machine has
  // none: then the text carries no emoji at all. Apple's font cannot be redistributed, so it is never
  // bundled; no other emoji set is ever used.
  emojiFamily: z.string().max(60),
  debug: z.boolean(), // log every unit's geometry and colours per frame (frame-by-frame harness)
});
export type CaptionProps = z.infer<typeof propsSchema>;

/** Parameters every template has. `fx` is the template's own set, typed by the template. */
export const autoSchema = z.strictObject({
  // role -> palette colour names the auto mode may pick for it, best first
  candidates: z.record(z.string(), z.array(z.string()).min(1)),
  // pairs of roles that sit next to each other and need the contrast floor between them
  pairs: z.array(z.tuple([z.string(), z.string()])),
  // roles that must stay away from the dominant hue of the video under them
  avoidHue: z.array(z.string()),
  // pairs of roles that must be told apart (minimum hue distance, degrees)
  distinct: z.array(z.tuple([z.string(), z.string()])),
});

export const pagingSchema = z.strictObject({
  mode: z.enum(["page", "build"]), // all words of a page at once | each word appears as it is spoken
  maxWords: z.number().int().min(1).max(8), // 1 = one word at a time
  maxLines: z.number().int().min(1).max(3),
  gapS: z.number().min(0), // a pause longer than this starts a new page
  holdS: z.number().min(0), // a page stays this long after its last word when a pause follows
  lingerS: z.number().min(0), // the last word stays highlighted this long into a pause
  minShowS: z.number().min(0), // shortest time a page is shown
  leadS: z.number().min(0).max(0.2), // captions lead the sound by this much (0 = on the word)
  overlapS: z.number().min(0).max(0.5), // a page stays this long into the next one (its exit plays over the entrance)
});

export const labelParamsSchema = z.strictObject({
  size: z.number().min(10), // font size of *FAIL* / *WATCHING* labels
  enterS: z.number().min(0),
  exitS: z.number().min(0),
  rotate: z.number().min(-20).max(20), // resting tilt, degrees (it enters from twice that)
});

export const baseParamsShape = {
  title: z.string().min(1),
  description: z.string().min(1),
  font: fontSchema,
  emphFont: fontSchema.nullable(), // shouted words are set in this font instead (null = the same font)
  size: z.number().min(10), // nominal font size, px in a 1080-wide frame
  minSize: z.number().min(6), // never shrink below this (longer text wraps or is split first)
  lineHeight: z.number().min(0.6).max(2), // em
  letterSpacing: z.number().min(-0.2).max(0.5), // em
  wordGap: z.number().min(0).max(1.5), // em between words
  case: z.enum(["upper", "lower", "title", "keep"]),
  emphScale: z.number().min(0.5).max(2), // font size factor of shouted words (layout level)
  grow: z.number().min(0).max(0.5), // room each side of a word for its own growth, as a share of its width
  tilt: z.number().min(0).max(30), // the most the whole block is ever rotated, degrees: the fit keeps the turned block inside the box
  margin: z.strictObject({ x: z.number().min(0), y: z.number().min(0) }), // room kept for stroke/shadow/overshoot
  paging: pagingSchema,
  timing: z.strictObject({
    enterS: z.number().min(0),
    exitS: z.number().min(0),
    activeInS: z.number().min(0),
    activeOutS: z.number().min(0),
    colorS: z.number().min(0),
    staggerS: z.number().min(0),
  }),
  colors: colorsSchema, // the fixed scheme
  auto: autoSchema,
  label: labelParamsSchema,
};

export const baseParamsSchema = z.strictObject({ ...baseParamsShape, fx: z.record(z.string(), z.unknown()) });
export type BaseParams = z.infer<typeof baseParamsSchema>;

/** Build a template's parameter schema: the shared parameters plus its own `fx` block. */
export function paramsSchema<T extends z.ZodRawShape>(fx: T) {
  return z.strictObject({ ...baseParamsShape, fx: z.strictObject(fx) });
}
