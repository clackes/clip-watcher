import type { Template } from "../core/template";
import { punch } from "./punch";

/** Every caption template, by name. The name is also the file name of its parameters: templates/<name>.json. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const all: Template<any>[] = [punch];

export const templates: Record<string, Template> = Object.fromEntries(all.map((t) => [t.name, t as Template]));

export function getTemplate(name: string): Template {
  const t = templates[name];
  if (!t) throw new Error(`unknown caption template "${name}" (have: ${Object.keys(templates).join(", ")})`);
  return t;
}
