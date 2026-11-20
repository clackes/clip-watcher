import type { Template } from "../core/template";

/**
 * Every caption template, by name. A template is a file in this folder whose default export is a
 * Template; its parameters are templates/<name>.json. Nothing to register: adding the two files is enough.
 */
declare const require: {
  context: (dir: string, deep: boolean, filter: RegExp) => { keys: () => string[]; (key: string): { default?: Template } };
};

const modules = require.context("./", false, /^\.\/(?!index)[a-z0-9_]+\.tsx$/);

export const templates: Record<string, Template> = {};
for (const key of modules.keys()) {
  const t = modules(key).default;
  if (!t) throw new Error(`templates/${key}: no default export`);
  if (`./${t.name}.tsx` !== key) throw new Error(`templates/${key}: exports a template named "${t.name}"`);
  templates[t.name] = t;
}

export function getTemplate(name: string): Template {
  const t = templates[name];
  if (!t) throw new Error(`unknown caption template "${name}" (have: ${Object.keys(templates).join(", ")})`);
  return t;
}
