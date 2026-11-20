import opentype, { type Font } from "opentype.js";
import { useEffect, useState } from "react";
import { cancelRender, continueRender, delayRender, staticFile } from "remotion";
import type { FontSpec } from "./schema";

/**
 * Fonts come only from the files in assets/fonts (served as static files of the bundle). Each file is
 * read once and used twice: registered with the browser under an alias no installed font can have
 * ("cw-Anton-Regular"), which is what text is measured with (the browser's own shaping: kerning included),
 * and parsed for its glyph outlines, which is what is drawn. A font that happens to be installed on the
 * render machine is never used instead, and a missing file fails the render rather than silently falling
 * back. Rendering waits (delayRender) until every font is loaded and usable.
 */

export const alias = (f: FontSpec): string => `cw-${f.file.replace(/\.(ttf|otf)$/i, "").replace(/[^A-Za-z0-9]/g, "-")}`;

/** font-family value: the caption font, then the system emoji font when there is one. No generic family. */
export const familyStack = (f: FontSpec, emoji: string): string => (emoji ? `"${alias(f)}", "${emoji}"` : `"${alias(f)}"`);

const pending = new Map<string, Promise<void>>();
const outlines = new Map<string, Font>();
const emojiCache = new Map<string, boolean>();

/**
 * True for text the caption font has no glyph for, so the emoji font draws it (Python only lets such
 * characters through when they are emoji).
 */
export function isEmoji(text: string, f: FontSpec): boolean {
  if (/^[\x20-\x7e]*$/.test(text)) return false;
  const key = `${alias(f)}|${text}`;
  let hit = emojiCache.get(key);
  if (hit === undefined) {
    const font = outlines.get(alias(f));
    hit = !font || Array.from(text).some((ch) => font.charToGlyphIndex(ch) === 0);
    emojiCache.set(key, hit);
  }
  return hit;
}

/** The parsed font (glyph outlines) of a loaded FontSpec. */
export function outlineFont(f: FontSpec): Font {
  const font = outlines.get(alias(f));
  if (!font) throw new Error(`font not loaded: ${f.file}`);
  return font;
}

function load(f: FontSpec): Promise<void> {
  const name = alias(f);
  let p = pending.get(name);
  if (!p) {
    p = fetch(staticFile(f.file))
      .then((r) => {
        if (!r.ok) throw new Error(`font file not found: ${f.file} (${r.status})`);
        return r.arrayBuffer();
      })
      .then(async (buf) => {
        const parsed = opentype.parse(buf);
        if (parsed.tables.fvar) throw new Error(`${f.file} is a variable font: bundle a static instance instead`);
        const face = new FontFace(name, buf, { weight: "400", style: "normal", stretch: "100%", display: "block" });
        document.fonts.add(await face.load());
        if (!document.fonts.check(`40px "${name}"`)) throw new Error(`font not usable after load: ${f.file}`);
        outlines.set(name, parsed);
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
