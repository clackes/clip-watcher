/** The part of opentype.js 2 this project uses (the package ships no types). */
declare module "opentype.js" {
  export interface Path {
    commands: { type: string; x?: number; y?: number; x1?: number; y1?: number; x2?: number; y2?: number }[];
    toPathData(decimals?: number): string;
  }
  export interface Glyph {
    index: number;
    advanceWidth: number;
    getPath(x: number, y: number, fontSize: number): Path;
  }
  export interface Font {
    unitsPerEm: number;
    ascender: number;
    descender: number;
    tables: Record<string, unknown>;
    charToGlyph(ch: string): Glyph;
    charToGlyphIndex(ch: string): number;
  }
  export function parse(buffer: ArrayBuffer): Font;
  const opentype: { parse: typeof parse };
  export default opentype;
}
