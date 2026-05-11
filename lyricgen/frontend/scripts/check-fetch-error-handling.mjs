#!/usr/bin/env node
// Falla con exit code 1 si encuentra `await fetch(...)` en frontend/src/
// que NO chequea res.ok dentro de las ~12 líneas siguientes.
//
// Motivación: el bug clásico en AdminPanel.jsx y JobDetail.jsx fue
// hacer `await fetch(...)` y asumir que el server respondió 200. Si
// el server rechaza con 4xx/5xx, el código sigue al `success path`
// y el operador cree que cumplió una acción que en realidad falló
// (delete, toggle, plan change, etc.).
//
// Heurística (intencionalmente simple):
//   - Para cada `await fetch(...)` (multi-line aware), busca en las
//     N líneas siguientes:
//       * `if (!res.ok)` o `if (res.ok)` → OK
//       * `.then(` chain → OK (la asume manejada, el código asincr
//         con then suele tener `.catch()`)
//       * `assert(res.ok)` (en tests) → OK
//   - Sin match → flag como warning.
//
// Severity:
//   - Métodos destructivos (DELETE / PATCH / PUT / POST) sin check → ERROR
//   - GET sin check → WARNING (no rompe state, solo UX)
//
// Excepciones: usar EXCLUDED_FILES o un comment `// fetch-no-check-ok`
// inmediatamente antes del fetch (raras vez justificable).

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = join(__dirname, "..", "src");

// Archivos exentos (raras vez necesario — preferir el comment exempt)
const EXCLUDED_FILES = new Set([
  // tests/ no aplica si introducimos vitest en el futuro
]);

// Cuántas líneas después del fetch buscamos el chequeo
const LOOKAHEAD = 12;

// Métodos que se consideran destructivos (errores) vs no-destructivos (warnings)
const DESTRUCTIVE_METHODS = new Set(["DELETE", "PATCH", "PUT", "POST"]);

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const s = statSync(p);
    if (s.isDirectory()) walk(p, files);
    else if (/\.(jsx?|tsx?)$/.test(name)) files.push(p);
  }
  return files;
}

// Detecta el `method:` del fetch (busca en el mismo block + N líneas
// siguientes). Default GET.
function detectMethod(lines, startIdx) {
  const block = lines.slice(startIdx, startIdx + 8).join("\n");
  const match = block.match(/method:\s*['"]([A-Z]+)['"]/i);
  return match ? match[1].toUpperCase() : "GET";
}

function isCheckedAfter(lines, fetchIdx) {
  const slice = lines.slice(fetchIdx, fetchIdx + LOOKAHEAD).join("\n");
  // Aceptamos varios patrones de check:
  return (
    /if\s*\(\s*!res\.ok/.test(slice) ||
    /if\s*\(\s*res\.ok/.test(slice) ||
    /res\.ok\s*[?&|]/.test(slice) ||           // ternaries / coalesce
    /assert(?:Equal|True|False)?\([^)]*res\.ok/.test(slice) ||
    /expect\([^)]*res\.ok/.test(slice) ||      // jest/vitest
    /throwIfNotOk\(/.test(slice) ||            // custom helper si existe
    /\.then\(/.test(slice)                      // chain promise
  );
}

const errors = [];
const warnings = [];

for (const file of walk(SRC)) {
  if (EXCLUDED_FILES.has(relative(SRC, file))) continue;
  const src = readFileSync(file, "utf8");
  const lines = src.split("\n");

  for (let i = 0; i < lines.length; i++) {
    if (!/\bawait\s+fetch\s*\(/.test(lines[i])) continue;

    // Comment exempt en la línea anterior
    const prev = (lines[i - 1] || "").trim();
    if (prev.includes("fetch-no-check-ok")) continue;

    if (isCheckedAfter(lines, i)) continue;

    const method = detectMethod(lines, i);
    const rel = relative(SRC, file);
    const finding = {
      file: rel,
      line: i + 1,
      method,
      snippet: lines[i].trim().slice(0, 100),
    };
    if (DESTRUCTIVE_METHODS.has(method)) {
      errors.push(finding);
    } else {
      warnings.push(finding);
    }
  }
}

if (warnings.length > 0) {
  console.warn(`⚠ fetch: ${warnings.length} GET sin chequeo de res.ok (deuda, no bloqueante):`);
  for (const w of warnings) {
    console.warn(`    ${w.file}:${w.line}  ${w.method}`);
  }
  console.warn("");
}

if (errors.length === 0) {
  console.log(`✓ fetch: 0 fetch destructivos (DELETE/PATCH/PUT/POST) sin chequeo de res.ok.`);
  process.exit(0);
}

console.error(`✗ fetch: ${errors.length} fetch DESTRUCTIVOS sin chequeo de res.ok:\n`);
for (const e of errors) {
  console.error(`    ${e.file}:${e.line}  ${e.method}  ${e.snippet}`);
}
console.error(
  `\nUn fetch destructivo (DELETE/PATCH/PUT/POST) que no verifica res.ok hace que el operador ` +
  `crea que su acción se cumplió cuando el server rechazó. Patrón correcto:\n\n` +
  `  const res = await fetch(url, { method: "DELETE", headers: ... });\n` +
  `  if (!res.ok) {\n` +
  `    const data = await res.json().catch(() => ({}));\n` +
  `    throw new Error(data.detail || \`Error \${res.status}\`);\n` +
  `  }\n\n` +
  `Si tenés un caso excepcional, agregá un comment \`// fetch-no-check-ok\` ` +
  `inmediatamente arriba del fetch con la razón.`
);
process.exit(1);
