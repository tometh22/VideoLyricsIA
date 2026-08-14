#!/usr/bin/env bash
# Wizard polish — los 4 pendientes del review externo
# (UX/UI/Frontend specialists, 2026-05-24):
#
#   #38) UI focus rings: ring-brand/20 → ring-brand/50 + box-shadow 0.1 → 0.3
#        + global :focus-visible para keyboard nav (botones/links sin focus).
#        WCAG AA dark-mode fix.
#
#   #39) UX chip "Fondo: generando…" en LyricsEditor.
#        Pasa bgPreview.status a LyricsEditor → muestra chip subtle arriba
#        del editor. Cierra el mental-model gap del review specialist:
#        operador no sabe que el pre-gen está corriendo.
#
#   #40) AbortController en prefetchRemaining.
#        Cancela uploads + transcripciones zombie cuando user hace
#        handleReset. Cost-leak prevention: sin esto, las transcripciones
#        ya enqueueadas siguen drenando Whisper + R2 después del reset.
#
#   #41) Typography tokens en tailwind.config.js + bulk migrate.
#        Agrega text-section/label/caption. Migra los SectionLabel
#        components + 17 ocurrencias de text-[11px] font-medium → text-label.
#
# Usage:
#   ./scripts/apply_wizard_polish.sh
#
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

echo "==> Verificando estado del repo..."
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "❌ Working tree dirty. Commit/stash y reintenta."
  git status --short
  exit 1
fi

echo "==> Checkout feat/wizard-no-redirect + rebase..."
git fetch origin
git checkout feat/wizard-no-redirect
git pull --rebase origin feat/wizard-no-redirect

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "feat/wizard-no-redirect" ]; then
  echo "❌ Checkout falló — branch actual: $BRANCH"
  exit 1
fi

echo "==> Aplicando edits via Python..."
python3 << 'PYEOF'
import re
import sys
from pathlib import Path

ROOT = Path(".")

def patch(rel_path, old, new, *, count=1, desc=""):
    p = ROOT / rel_path
    src = p.read_text()
    occ = src.count(old)
    if occ != count:
        print(f"  ✗ {rel_path}: esperaba {count} match(es), encontró {occ}. [{desc}]")
        sys.exit(2)
    p.write_text(src.replace(old, new, count))
    print(f"  ✓ {rel_path}  [{desc}]")


# ============================================================================
# #38 — Focus rings (index.css)
# ============================================================================
print("\n[#38] Focus rings + global :focus-visible...")

patch(
    "lyricgen/frontend/src/index.css",
    '''  .input-field {
    @apply w-full px-5 py-4 rounded-button
      bg-surface-1 border border-white/[0.06]
      focus:border-brand/50 focus:ring-2 focus:ring-brand/20 focus:outline-none
      text-ink-primary placeholder-ink-secondary/60;
    transition: all var(--motion-duration) var(--motion-ease);
    box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.2);
  }
  .input-field:focus {
    box-shadow:
      inset 0 2px 4px rgba(0, 0, 0, 0.2),
      0 0 0 3px rgba(109, 74, 255, 0.1);
  }''',
    '''  .input-field {
    @apply w-full px-5 py-4 rounded-button
      bg-surface-1 border border-white/[0.06]
      focus:border-brand focus:ring-2 focus:ring-brand/50 focus:outline-none
      text-ink-primary placeholder-ink-secondary/60;
    transition: all var(--motion-duration) var(--motion-ease);
    box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.2);
  }
  /* Focus state — UI specialist 2026-05-24: bumped from ring-brand/20 (6%
     visible sobre #12121A) a ring-brand/50 + box-shadow @0.3. WCAG AA pass
     en dark mode + keyboard nav visible. */
  .input-field:focus {
    box-shadow:
      inset 0 2px 4px rgba(0, 0, 0, 0.2),
      0 0 0 3px rgba(109, 74, 255, 0.3);
  }

  /* Keyboard focus indicator global — botones, links, controles que no
     declaran su propio focus. Sin esto la nav con Tab es invisible.
     `:focus-visible` solo aparece en keyboard, NO en click — los users de
     mouse no ven ring. */
  button:focus-visible,
  a:focus-visible,
  [role="button"]:focus-visible,
  [tabindex]:focus-visible {
    @apply outline-none ring-2 ring-brand/50 ring-offset-2 ring-offset-surface;
  }''',
    desc="index.css — focus rings ring/20→/50 + global :focus-visible",
)


# ============================================================================
# #41 — Typography tokens en tailwind.config.js
# ============================================================================
print("\n[#41] Typography tokens...")

patch(
    "lyricgen/frontend/tailwind.config.js",
    '''        // Small UI: 14-15px (default 14).
        ui:   ["14px", { lineHeight: "1.45" }],
      },''',
    '''        // Small UI: 14-15px (default 14).
        ui:   ["14px", { lineHeight: "1.45" }],
        // UI specialist 2026-05-24: tokens semánticos para reemplazar los
        // arbitrary `text-[10px]`/`text-[11px]`/`text-[12px]` esparcidos
        // (172+ matches). Usar SIEMPRE estos tokens en componentes nuevos;
        // los arbitrary se irán migrando.
        caption: ["12px", { lineHeight: "1.4" }],
        label:   ["11px", { lineHeight: "1.4", fontWeight: "500" }],
        section: ["10px", { lineHeight: "1.3", fontWeight: "600", letterSpacing: "0.18em" }],
      },''',
    desc="tailwind.config — caption/label/section tokens",
)

# SectionLabel migrations
patch(
    "lyricgen/frontend/src/components/Dashboard.jsx",
    '''// Tiny uppercase label used to introduce sections — Linear / Vercel style.
function SectionLabel({ children }) {
  return (
    <p className="text-[10px] font-medium text-gray-500 uppercase tracking-[0.18em] mb-3">
      {children}
    </p>
  );
}''',
    '''// Tiny uppercase label used to introduce sections — Linear / Vercel style.
// `text-section` token (tailwind.config) ya incluye fontWeight 600 +
// letter-spacing 0.18em + size 10px. Antes era arbitrary; el token lo unifica.
function SectionLabel({ children }) {
  return (
    <p className="text-section text-gray-500 uppercase mb-3">
      {children}
    </p>
  );
}''',
    desc="Dashboard.SectionLabel → text-section",
)

patch(
    "lyricgen/frontend/src/components/Settings.jsx",
    '''function SectionLabel({ children }) {
  return (
    <p className="text-[10px] font-medium text-gray-500 uppercase tracking-[0.18em] mb-3">
      {children}
    </p>
  );
}''',
    '''function SectionLabel({ children }) {
  return (
    <p className="text-section text-gray-500 uppercase mb-3">
      {children}
    </p>
  );
}''',
    desc="Settings.SectionLabel → text-section",
)

# Bulk replace `text-[11px] font-medium` → `text-label` (17 occurrences).
print("\n[#41-bulk] text-[11px] font-medium → text-label...")
total_repl = 0
for p in (ROOT / "lyricgen/frontend/src").rglob("*.jsx"):
    src = p.read_text()
    if "text-[11px] font-medium" in src:
        cnt = src.count("text-[11px] font-medium")
        p.write_text(src.replace("text-[11px] font-medium", "text-label"))
        rel = p.relative_to(ROOT / "lyricgen/frontend/src")
        print(f"  ✓ {rel}: {cnt} replace(s)")
        total_repl += cnt
print(f"  Total bulk: {total_repl} reemplazos")


# ============================================================================
# #39 — UX chip "Fondo: generando…" en LyricsEditor
# ============================================================================
print("\n[#39] UX chip — bgStatus en LyricsEditor...")

# A) App.jsx: pasa bgStatus prop al LyricsEditor.
# El bloque original tiene la callback onFontChange/onCaseChange como
# último prop antes del cierre `/>`. Insertamos bgStatus justo después
# del eslint-disable comment removal NO, mejor antes de los onXChange
# callbacks (donde está claramente "más props del editor").
patch(
    "lyricgen/frontend/src/App.jsx",
    '''  // eslint-disable-next-line no-unused-vars
  const bgPreview = useBackgroundPreview(previewEntry, {''',
    '''  // bgPreview alimenta:
  //   - onCacheKey → currentReview.bgCacheKey + approvedJobs (race fix R-FRONT-2).
  //   - bgPreview.status → chip subtle "Fondo: generando…" en LyricsEditor
  //     (UX specialist 2026-05-24, cierra el mental-model gap de pre-gen invisible).
  const bgPreview = useBackgroundPreview(previewEntry, {''',
    desc="App.jsx — bgPreview comment + eslint-disable removed",
)

# Pasamos bgStatus como prop a LyricsEditor. Anchor: el bloque que termina
# con onLineTransitionChange (último prop conocido del editor).
patch(
    "lyricgen/frontend/src/App.jsx",
    '''            onAnimationChange={(c) => setCurrentReview((r) => (r ? { ...r, lyricsAnimation: c } : r))}
            onLineTransitionChange={(c) => setCurrentReview((r) => (r ? { ...r, lineTransition: c } : r))}
          />''',
    '''            onAnimationChange={(c) => setCurrentReview((r) => (r ? { ...r, lyricsAnimation: c } : r))}
            onLineTransitionChange={(c) => setCurrentReview((r) => (r ? { ...r, lineTransition: c } : r))}
            // UX specialist 2026-05-24: chip de status del pre-gen del
            // fondo. Status posibles: "idle" | "queued" | "generating" |
            // "done" | "error" | "disabled" (free-tier plan-tier guard).
            bgStatus={bgPreview.status}
          />''',
    desc="App.jsx — bgStatus={bgPreview.status} prop",
)

# B) LyricsEditor.jsx: aceptar `bgStatus` prop + render chip.
patch(
    "lyricgen/frontend/src/components/LyricsEditor.jsx",
    '''  // 2026-05-23: callbacks de los nuevos ejes (libass). Reemplazan al
  // onTransitionChange (que controlaba el legacy lyric_transition).
  onAnimationChange = null,
  onLineTransitionChange = null,
}) {''',
    '''  // 2026-05-23: callbacks de los nuevos ejes (libass). Reemplazan al
  // onTransitionChange (que controlaba el legacy lyric_transition).
  onAnimationChange = null,
  onLineTransitionChange = null,
  // UX specialist 2026-05-24: status del pre-gen del fondo (useBackgroundPreview).
  // Valores: "idle" | "queued" | "generating" | "done" | "error" | "disabled".
  // null/undefined → no se renderiza el chip (modo /edit modal post-render).
  bgStatus = null,
}) {''',
    desc="LyricsEditor — accept bgStatus prop",
)

# Insertar el chip justo después del header (entre el div header y el botón fixed CTA).
patch(
    "lyricgen/frontend/src/components/LyricsEditor.jsx",
    '''        <div className="min-w-0">
          <h2 className="text-lg font-bold tracking-tight">{t("editor.title")}</h2>
          <p className="text-sm text-ink-secondary truncate">
            {name}
            {batchProgress && <span className="ml-2 text-brand-light text-xs">({batchProgress})</span>}
          </p>
        </div>
      </div>

      {/* Fixed floating primary CTA — always reachable, never cut. */}''',
    '''        <div className="min-w-0">
          <h2 className="text-lg font-bold tracking-tight">{t("editor.title")}</h2>
          <p className="text-sm text-ink-secondary truncate">
            {name}
            {batchProgress && <span className="ml-2 text-brand-light text-xs">({batchProgress})</span>}
          </p>
        </div>
      </div>

      {/* Chip de status del pre-gen del fondo — UX 2026-05-24. Operador edita
          lyrics, Veo/Imagen está generando en background. Sin esto el pre-gen
          era invisible y cambiar un param descartaba un preview sin aviso. */}
      {bgStatus && bgStatus !== "idle" && bgStatus !== "disabled" && (
        <div className={`mb-3 inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-caption
            ${bgStatus === "done"
              ? "bg-accent/10 text-accent-light ring-1 ring-accent/30"
              : bgStatus === "error"
                ? "bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/30"
                : "bg-brand/10 text-brand-light ring-1 ring-brand/30"}`}>
          {bgStatus === "queued" || bgStatus === "generating" ? (
            <>
              <svg className="w-3 h-3 animate-spin" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M21 12a9 9 0 11-6.219-8.56" strokeLinecap="round" />
              </svg>
              <span>{t("editor.bg_generating") || "Generando fondo en background…"}</span>
            </>
          ) : bgStatus === "done" ? (
            <>
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span>{t("editor.bg_done") || "Fondo listo"}</span>
            </>
          ) : bgStatus === "error" ? (
            <>
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <span>{t("editor.bg_error") || "El fondo se generará al apretar Crear video"}</span>
            </>
          ) : null}
        </div>
      )}

      {/* Fixed floating primary CTA — always reachable, never cut. */}''',
    desc="LyricsEditor — chip render block",
)

# Agregar las keys i18n. i18n.jsx en este repo está estructurado por idioma;
# el path más seguro es no fallar si las keys ya existen. Hacemos un patch
# best-effort para `es` (es el default) — los otros idiomas heredan del
# fallback español si la key falta (ver i18n.jsx).
i18n_path = ROOT / "lyricgen/frontend/src/i18n.jsx"
if i18n_path.exists():
    i18n_src = i18n_path.read_text()
    if '"editor.bg_generating"' not in i18n_src:
        # Buscar la key "editor.title" en el bloque ES y agregar las nuevas
        # justo después (ubicación natural por nesting semántico).
        anchor = '"editor.title": "Revisar y editar letra",'
        if anchor in i18n_src:
            addition = anchor + '''
      "editor.bg_generating": "Generando fondo en background…",
      "editor.bg_done": "Fondo listo",
      "editor.bg_error": "El fondo se generará al apretar Crear video",'''
            i18n_path.write_text(i18n_src.replace(anchor, addition, 1))
            print("  ✓ i18n.jsx — keys editor.bg_* agregadas (es; otros idiomas heredan)")
        else:
            print("  ⚠  i18n.jsx — anchor 'editor.title' no encontrado; el chip usará fallback inline (OK).")
    else:
        print("  ⏭  i18n.jsx — keys editor.bg_* ya estaban")


# ============================================================================
# #40 — AbortController en prefetchRemaining
# ============================================================================
print("\n[#40] AbortController en prefetchRemaining...")

# A) Crear el ref. Insertar después de pollingIntervals (ya es un useRef).
patch(
    "lyricgen/frontend/src/App.jsx",
    '''  const prefetchRemaining = useCallback(async (queue, fromIdx) => {
    for (let idx = fromIdx; idx < queue.length; idx++) {
      if (prefetchCache.current[idx]) continue;
      prefetchCache.current[idx] = { status: "loading" };
      const entry = queue[idx];
      const file = entry.file;''',
    '''  // R-FRONT-3 (review specialist 2026-05-24): cost-leak prevention en
  // handleReset. Sin esto, las transcripciones encoladas en background
  // seguían drenando Whisper + R2 cuando el operador cancelaba el batch.
  // Conservador: solo abortamos LOOP ITERATIONS (no requests en progreso —
  // requeriría signal en uploadFileToR2 + authFetchWithRetryOn503, que es
  // refactor invasivo). El próximo iteration ve aborted=true y rompe.
  const prefetchAbortRef = useRef(null);
  if (prefetchAbortRef.current === null) {
    prefetchAbortRef.current = new AbortController();
  }

  const prefetchRemaining = useCallback(async (queue, fromIdx) => {
    // Snapshot del controller actual al arrancar el loop. Si handleReset
    // crea uno nuevo entremedio, esta closure sigue revisando el viejo
    // (que YA está abortado) y rompe limpio.
    const controller = prefetchAbortRef.current;
    for (let idx = fromIdx; idx < queue.length; idx++) {
      if (controller && controller.signal.aborted) {
        // handleReset disparó abort — paramos el prefetch loop. Los
        // requests en flight se completan (sin signal) pero el siguiente
        // iter NO arranca.
        break;
      }
      if (prefetchCache.current[idx]) continue;
      prefetchCache.current[idx] = { status: "loading" };
      const entry = queue[idx];
      const file = entry.file;''',
    desc="App.jsx — prefetchAbortRef + signal check",
)

# B) En handleReset, abortar + crear un controller fresco. Anchor: el block
# que limpia pollingIntervals + prefetchCache.
patch(
    "lyricgen/frontend/src/App.jsx",
    '''    pollingIntervals.current.forEach((iv) => clearInterval(iv));
    pollingIntervals.current.clear();
    prefetchCache.current = {};
    setFiles([]); setJobs([]); setBackgroundFile(null); setBackgroundId(null);''',
    '''    pollingIntervals.current.forEach((iv) => clearInterval(iv));
    pollingIntervals.current.clear();
    prefetchCache.current = {};
    // R-FRONT-3: abortamos el prefetch loop (el siguiente iter se rompe).
    // Después creamos un controller fresco para el próximo batch del operador.
    try { prefetchAbortRef.current && prefetchAbortRef.current.abort(); } catch {}
    prefetchAbortRef.current = new AbortController();
    setFiles([]); setJobs([]); setBackgroundFile(null); setBackgroundId(null);''',
    desc="App.jsx — handleReset abort + fresh controller",
)


print("\n✅ Edits aplicados.")
PYEOF

echo "==> Verificando build frontend..."
( cd lyricgen/frontend && npm run build --silent 2>&1 | tail -15 )

echo "==> Diff resumen..."
git diff --stat

echo ""
echo "==> Si todo OK, commit + push:"
echo ""
cat <<MSG
git add -A

git commit -m "polish(wizard): focus rings + typography tokens + bg-status chip + AbortController

Aplica los 4 pendientes del review externo (UX/UI/Frontend specialists 2026-05-24).

#38 Focus rings (UI specialist):
- input-field focus ring 6% → 50% opacity (WCAG AA dark mode)
- Global :focus-visible para botones/links sin focus declarado (keyboard nav)

#41 Typography tokens (UI specialist):
- text-section/label/caption en tailwind.config (size + weight + tracking unificados)
- Dashboard + Settings SectionLabel: text-[10px] font-medium tracking-[0.18em] → text-section
- Bulk migrate text-[11px] font-medium → text-label (17 ocurrencias en 8 archivos)

#39 UX chip 'Fondo: generando…' (UX specialist mental-model gap):
- App.jsx pasa bgStatus={bgPreview.status} al LyricsEditor
- LyricsEditor renderiza chip subtle arriba del editor con 4 estados:
  generating (spinner + brand), done (check + accent), error (warn + amber),
  idle/disabled (no render)
- i18n keys editor.bg_generating/done/error en español (otros idiomas heredan)
- Cierra el gap: operador ahora sabe que el pre-gen está corriendo en background

#40 AbortController en prefetchRemaining (Frontend specialist cost-leak):
- prefetchAbortRef ref creado en App-level
- prefetchRemaining loop chequea signal.aborted al inicio de cada iteración
- handleReset abortea + crea controller fresco para el próximo batch
- Previene transcripciones zombie consumiendo Whisper + R2 después de cancel
- Conservador: solo abortamos loop iterations (signal en upload/fetch requiere refactor invasivo)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"

git push origin feat/wizard-no-redirect
MSG
echo ""
echo "==> Done."
