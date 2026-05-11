#!/usr/bin/env node
// Falla con exit code 1 si encuentra keys t("foo.bar") en el código
// que NO existen en los 3 idiomas (es/en/pt) de i18n.jsx.
//
// El bug que motivó esto: si una key no está en i18n, t() devuelve
// la key como string (truthy), y el pattern `t("foo.bar") || "fallback"`
// nunca dispara el fallback. Resultado: la UI le muestra
// "detail.error_title" al usuario en lugar del texto.
//
// Convención de excepciones: keys con templating dinámico (interpolación
// con ${n}) no se pueden traducir como strings simples — saltearlas via
// la lista IGNORED_KEYS de abajo.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = join(__dirname, "..", "src");
const I18N_PATH = join(SRC, "i18n.jsx");

// Keys templated o intencionalmente no traducidas — agregar acá con
// motivo. NO usar para tapar bugs.
const IGNORED_KEYS = new Set([
  "batch.approval_sub",       // interpola ${n} de ${total} videos…
  "batch.celebration_batch",  // interpola ${total} videos aprobados!
]);

const LANGS = ["es", "en", "pt"];

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const s = statSync(p);
    if (s.isDirectory()) walk(p, files);
    else if (/\.(jsx?|tsx?)$/.test(name)) files.push(p);
  }
  return files;
}

const i18nSource = readFileSync(I18N_PATH, "utf8");

// Cuenta apariciones de "<key>": en el archivo i18n por idioma. No
// parsea el JSX completo (overkill) — usa búsqueda lineal por la
// estructura: cada bloque empieza con `  es: {` `  en: {` `  pt: {`.
function keyPresence(key) {
  const langSections = {};
  for (const lang of LANGS) {
    const re = new RegExp(`^\\s+${lang}:\\s*\\{`, "m");
    const start = i18nSource.search(re);
    if (start < 0) { langSections[lang] = ""; continue; }
    const after = i18nSource.slice(start + 8);
    const endRel = after.search(/^  [a-z]+:\s*\{|^};/m);
    const end = endRel < 0 ? i18nSource.length : start + 8 + endRel;
    langSections[lang] = i18nSource.slice(start, end);
  }
  const keyRe = new RegExp(`"${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"\\s*:`);
  return LANGS.filter((l) => keyRe.test(langSections[l]));
}

// Extrae todas las keys "foo.bar" usadas como t("...") en src/
const keyUsageRe = /\bt\(\s*"([a-z][a-z_]*\.[a-z][a-z_0-9]*)"\s*\)/g;
const usedKeys = new Set();
for (const f of walk(SRC)) {
  if (f === I18N_PATH) continue;
  const src = readFileSync(f, "utf8");
  let m;
  while ((m = keyUsageRe.exec(src)) !== null) {
    usedKeys.add(m[1]);
  }
}

// Dos buckets:
//   missingAll: la clave no existe en NINGÚN idioma. En prod cualquier
//     usuario ve la key literal. Es el bug crítico → falla CI.
//   missingSome: existe en al menos uno (típicamente es) pero no en
//     todos. Solo afecta usuarios en idiomas sin la traducción. Es
//     deuda técnica → warning, no falla CI.
const missingAll = [];
const missingSome = [];
for (const key of usedKeys) {
  if (IGNORED_KEYS.has(key)) continue;
  const present = keyPresence(key);
  if (present.length === 0) missingAll.push(key);
  else if (present.length < LANGS.length) {
    const absent = LANGS.filter((l) => !present.includes(l));
    missingSome.push({ key, absent });
  }
}
missingAll.sort();
missingSome.sort((a, b) => a.key.localeCompare(b.key));

if (missingSome.length > 0) {
  console.warn(`⚠ i18n: ${missingSome.length} claves traducidas parcialmente (deuda, no bloquea):`);
  for (const { key, absent } of missingSome) {
    console.warn(`    ${key}  (falta en: ${absent.join(", ")})`);
  }
  console.warn("");
}

if (missingAll.length === 0) {
  console.log(`✓ i18n: ${usedKeys.size} claves usadas, ninguna falta en TODOS los idiomas.`);
  process.exit(0);
}

console.error(`✗ i18n: ${missingAll.length} claves usadas pero ausentes en TODOS los idiomas:\n`);
for (const k of missingAll) console.error(`    ${k}`);
console.error(`\nEsto significa que cualquier usuario ve la key literal en lugar del texto. Agregalas a src/i18n.jsx en al menos un idioma. Si la clave usa \${vars}, sumala a IGNORED_KEYS con el motivo.`);
process.exit(1);
