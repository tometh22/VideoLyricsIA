#!/usr/bin/env node
// Falla si una key usada no existe en los 3 idiomas o si un idioma
// declara la misma key más de una vez.
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
import { parse } from "@babel/parser";

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
const RAW_TEXT_ENFORCED = new Set([
  "components/Dashboard.jsx",
  "components/LyricsTimeline.jsx",
  "components/SearchPalette.jsx",
  "components/Sidebar.jsx",
  "components/OnboardingTour.jsx",
  "components/HistoryView.jsx",
  "components/Settings.jsx",
  "components/UploadZone.jsx",
]);
const RAW_TEXT_ALLOWLIST = new Set(["A", "Pro", "ID", "USD", "@handle", "esc", "ESC", "click", "⌘K", "px/s", "— MP4, MOV, JPG, PNG", "soporte@genly.pro"]);

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

function languageSection(lang) {
  const start = i18nSource.indexOf(`  ${lang}: {`);
  if (start < 0) return "";
  const next = lang === "es" ? "\n  en: {" : lang === "en" ? "\n  pt: {" : "\n  },\n};";
  const end = i18nSource.indexOf(next, start + 1);
  return i18nSource.slice(start, end < 0 ? i18nSource.length : end);
}

const duplicates = [];
for (const lang of LANGS) {
  const counts = new Map();
  for (const match of languageSection(lang).matchAll(/^\s+"([^"]+)"\s*:/gm)) {
    counts.set(match[1], (counts.get(match[1]) || 0) + 1);
  }
  for (const [key, count] of counts) {
    if (count > 1) duplicates.push({ lang, key, count });
  }
}

const usedKeys = new Set();
const dynamicPatterns = [];
const rawText = [];
for (const f of walk(SRC)) {
  if (f === I18N_PATH) continue;
  const src = readFileSync(f, "utf8");
  let ast;
  try {
    ast = parse(src, { sourceType: "module", plugins: ["jsx", "typescript"], errorRecovery: true });
  } catch (error) {
    console.error(`✗ i18n: no se pudo analizar ${f}: ${error.message}`);
    process.exit(1);
  }
  const relative = f.slice(SRC.length + 1);
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.type === "CallExpression" && node.callee?.type === "Identifier" && node.callee.name === "t") {
      const arg = node.arguments?.[0];
      if (arg?.type === "StringLiteral" && /^[a-z][a-z_]*(?:\.[a-z][a-z_0-9]*)+$/.test(arg.value)) {
        usedKeys.add(arg.value);
      } else if (arg?.type === "TemplateLiteral" && arg.expressions.length > 0) {
        const source = arg.quasis.map((q) => q.value.cooked.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".+");
        dynamicPatterns.push({ relative, line: node.loc?.start.line, regex: new RegExp(`^${source}$`) });
      }
    }
    if (RAW_TEXT_ENFORCED.has(relative)) {
      if (node.type === "JSXText") {
        const value = node.value.replace(/\s+/g, " ").trim();
        if (/[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]/.test(value) && !RAW_TEXT_ALLOWLIST.has(value)) {
          rawText.push({ relative, line: node.loc?.start.line, value });
        }
      }
      if (node.type === "JSXAttribute" && ["title", "placeholder", "aria-label"].includes(node.name?.name)
          && node.value?.type === "StringLiteral" && /[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]/.test(node.value.value)
          && !RAW_TEXT_ALLOWLIST.has(node.value.value)) {
        rawText.push({ relative, line: node.loc?.start.line, value: node.value.value });
      }
    }
    for (const [key, value] of Object.entries(node)) {
      if (["loc", "start", "end", "extra"].includes(key)) continue;
      if (Array.isArray(value)) value.forEach(visit);
      else if (value && typeof value === "object" && typeof value.type === "string") visit(value);
    }
  };
  visit(ast.program);
}

const languageKeys = Object.fromEntries(LANGS.map((lang) => [
  lang,
  new Set([...languageSection(lang).matchAll(/^\s+"([^"]+)"\s*:/gm)].map((m) => m[1])),
]));
const dynamicMissing = [];
for (const pattern of dynamicPatterns) {
  const matches = Object.fromEntries(LANGS.map((lang) => [
    lang,
    [...languageKeys[lang]].filter((key) => pattern.regex.test(key)).sort(),
  ]));
  const canonical = JSON.stringify(matches.es);
  if (matches.es.length === 0 || LANGS.some((lang) => JSON.stringify(matches[lang]) !== canonical)) {
    dynamicMissing.push({ ...pattern, matches });
  }
}

// Dos buckets:
//   missingAll: la clave no existe en NINGÚN idioma. En prod cualquier
//     usuario ve la key literal. Es el bug crítico → falla CI.
//   missingSome: existe en al menos uno pero no en todos. También falla CI:
//     una interfaz multilingüe no puede aceptar cobertura parcial.
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
  console.error(`✗ i18n: ${missingSome.length} claves traducidas parcialmente:`);
  for (const { key, absent } of missingSome) {
    console.error(`    ${key}  (falta en: ${absent.join(", ")})`);
  }
  console.error("");
}

if (duplicates.length > 0) {
  console.error(`✗ i18n: ${duplicates.length} claves duplicadas:`);
  for (const { lang, key, count } of duplicates) console.error(`    ${lang}.${key}  (${count} veces)`);
  console.error("");
}

if (dynamicMissing.length > 0) {
  console.error(`✗ i18n: ${dynamicMissing.length} patrones dinámicos sin cobertura equivalente:`);
  for (const item of dynamicMissing) console.error(`    ${item.relative}:${item.line}  ${item.regex}`);
  console.error("");
}

if (rawText.length > 0) {
  console.error(`✗ i18n: ${rawText.length} textos JSX hardcodeados en superficies protegidas:`);
  for (const item of rawText) console.error(`    ${item.relative}:${item.line}  ${JSON.stringify(item.value)}`);
  console.error("");
}

if (missingAll.length === 0 && missingSome.length === 0 && duplicates.length === 0 && dynamicMissing.length === 0 && rawText.length === 0) {
  console.log(`✓ i18n: ${usedKeys.size} claves usadas · 0 parciales · 0 duplicadas.`);
  process.exit(0);
}

if (missingAll.length > 0) {
  console.error(`✗ i18n: ${missingAll.length} claves usadas pero ausentes en TODOS los idiomas:\n`);
  for (const k of missingAll) console.error(`    ${k}`);
  console.error(`\nEsto significa que cualquier usuario ve la key literal en lugar del texto. Agregala a los 3 idiomas de src/i18n.jsx. Si la clave usa \${vars}, sumala a IGNORED_KEYS con el motivo.`);
}
process.exit(1);
