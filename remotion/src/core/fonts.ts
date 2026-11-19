import { useEffect, useState } from "react";
import { cancelRender, continueRender, delayRender, staticFile } from "remotion";
import type { FontSpec } from "./schema";

/**
 * Fonts come only from the files in assets/fonts (served as static files of the bundle). Each file is
 * registered under an alias no installed font can have ("cw-Anton-Regular"), so a font that happens to be
 * installed on the render machine is never used instead, and a missing file fails the render rather than
 * silently falling back. Rendering waits (delayRender) until every face is loaded and usable.
 */

export const alias = (f: FontSpec): string => `cw-${f.file.replace(/\.(ttf|otf)$/i, "").replace(/[^A-Za-z0-9]/g, "-")}`;

/** CSS font-weight to ask for: the wght axis of a variable font, else the single face (400). */
export const cssWeight = (f: FontSpec): number => f.variations.wght ?? 400;
export const cssStretch = (f: FontSpec): string => (f.variations.wdth ? `${f.variations.wdth}%` : "100%");

/** font-family value: the caption font, then the system emoji font when there is one. No generic family. */
export const familyStack = (f: FontSpec, emoji: string): string => (emoji ? `"${alias(f)}", "${emoji}"` : `"${alias(f)}"`);

const emojiCache = new Map<string, boolean>();

/**
 * True for text the caption font cannot draw, so the emoji font draws it (Python only lets such characters
 * through when they are emoji). Found by measuring with two different last-resort families: text the
 * caption font covers has the same width with both.
 */
export function isEmoji(text: string, f: FontSpec): boolean {
  if (/^[\x20-\x7e]*$/.test(text)) return false;
  const key = `${alias(f)}|${text}`;
  let hit = emojiCache.get(key);
  if (hit === undefined) {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) return false;
    ctx.font = `${cssWeight(f)} 64px "${alias(f)}", monospace`;
    const a = ctx.measureText(text).width;
    ctx.font = `${cssWeight(f)} 64px "${alias(f)}", serif`;
    const b = ctx.measureText(text).width;
    ctx.font = `${cssWeight(f)} 64px "${alias(f)}", cursive`;
    const c = ctx.measureText(text).width;
    hit = Math.abs(a - b) > 0.01 || Math.abs(a - c) > 0.01 || /\p{Extended_Pictographic}/u.test(text);
    emojiCache.set(key, hit);
  }
  return hit;
}

const pending = new Map<string, Promise<void>>();

function load(f: FontSpec): Promise<void> {
  const name = alias(f);
  let p = pending.get(name);
  if (!p) {
    const variable = Object.keys(f.variations).length > 0;
    const face = new FontFace(name, `url("${staticFile(f.file)}") format("truetype")`, {
      weight: variable && f.variations.wght !== undefined ? "1 1000" : "400",
      stretch: variable && f.variations.wdth !== undefined ? "50% 200%" : "100%",
      style: "normal",
      display: "block",
    });
    p = face.load().then((loaded) => {
      document.fonts.add(loaded);
      const probe = `${cssWeight(f)} 40px "${name}"`;
      if (!document.fonts.check(probe)) throw new Error(`font not usable after load: ${f.file}`);
    });
    pending.set(name, p);
  }
  return p;
}

/** True once every font is loaded. Holds the render until then; a font that cannot load cancels it. */
export function useFonts(fonts: FontSpec[]): boolean {
  const key = fonts.map(alias).join("|");
  const [ready, setReady] = useState<string | null>(null);
  const [handle] = useState(() => delayRender(`fonts ${key}`, { timeoutInMilliseconds: 20000 }));
  useEffect(() => {
    let live = true;
    Promise.all(fonts.map(load))
      .then(() => document.fonts.ready)
      .then(() => {
        if (live) {
          setReady(key);
          continueRender(handle);
        }
      })
      .catch((err) => cancelRender(err));
    return () => {
      live = false;
    };
    // fonts is rebuilt every render; its key says whether anything changed
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, handle]);
  return ready === key;
}
