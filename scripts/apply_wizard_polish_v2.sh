#!/usr/bin/env bash
# Wizard polish v2 — los 4 pendientes restantes:
#
#   #42) isMountedRef en pollJob (Frontend specialist RIESGO 5)
#        Previene setState on unmounted component warnings + memory leak
#        cuando user navega away durante SSE/polling.
#
#   #43) AbortController signal end-to-end (Frontend specialist cost-leak)
#        prefetchRemaining ahora pasa signal a uploadFileToR2 + authFetchWithRetryOn503.
#        Cancela requests EN FLIGHT (no solo loop iterations).
#
#   #44) Skeleton component + Dashboard loading state polish
#        Reemplaza el spinner genérico del Dashboard history-loading
#        por 6 skeleton VideoCards. UI specialist medium-effort-high-impact.
#
#   #45) Bulk migrate text-[12px] standalone → text-caption
#        Conservative: solo standalone (sin font-weight). Las que tienen
#        font-medium/font-semibold quedan (mantienen weight explícito).
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

echo "==> Checkout feat/wizard-no-redirect + pull..."
git fetch origin
git checkout feat/wizard-no-redirect
git pull --rebase origin feat/wizard-no-redirect

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "feat/wizard-no-redirect" ]; then
  echo "❌ Checkout falló — branch actual: $BRANCH"
  exit 1
fi


# ============================================================================
# #44a — Crear Skeleton.jsx component
# ============================================================================
echo "==> #44a — Creando Skeleton.jsx..."

cat > lyricgen/frontend/src/components/Skeleton.jsx << 'JSX'
// Skeleton loading primitives — reemplazan los spinners genéricos en
// pantallas donde sabemos el shape del contenido final. UX gain: la
// app "feels faster" porque el operador YA ve la estructura visual
// antes de que llegue la data. UI specialist 2026-05-24.
//
// Uso:
//   <Skeleton className="h-4 w-32 rounded-md" />
//   <SkeletonVideoCard />

export function Skeleton({ className = "" }) {
  return (
    <div
      className={`bg-surface-2/60 animate-pulse ${className}`}
      aria-hidden="true"
    />
  );
}

// Replica visual de VideoCard (Dashboard.jsx) — mismo aspect-ratio + footer
// para que el "swap" de skeleton→card no tenga reflow visible.
export function SkeletonVideoCard() {
  return (
    <div
      className="rounded-card overflow-hidden bg-surface-2/40 ring-1 ring-white/[0.04]"
      aria-hidden="true"
    >
      <div className="aspect-video bg-surface-2/70 animate-pulse" />
      <div className="px-3.5 py-3 space-y-2">
        <Skeleton className="h-3.5 w-3/4 rounded" />
        <Skeleton className="h-3 w-1/2 rounded" />
      </div>
    </div>
  );
}

// Replica visual de ProcessingRow (Dashboard.jsx).
export function SkeletonProcessingRow() {
  return (
    <div
      className="w-full flex items-center gap-3 px-3 py-2.5"
      aria-hidden="true"
    >
      <Skeleton className="w-2 h-2 rounded-full" />
      <Skeleton className="h-4 flex-1 rounded" />
      <Skeleton className="h-3 w-16 rounded" />
    </div>
  );
}

export default Skeleton;
JSX

echo "  ✓ lyricgen/frontend/src/components/Skeleton.jsx (creado)"


# ============================================================================
# Resto via Python (anchor-based patches)
# ============================================================================
echo "==> Aplicando edits via Python..."

python3 << 'PYEOF'
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
# #42 — isMountedRef en pollJob
# ============================================================================
print("\n[#42] isMountedRef en pollJob...")

# A) Crear el ref + cleanup en useEffect global. Lo insertamos donde ya
# vive el cleanup de pollingIntervals.
patch(
    "lyricgen/frontend/src/App.jsx",
    '''  const pollingIntervals = useRef(new Set());''',
    '''  const pollingIntervals = useRef(new Set());
  // R-FRONT-5 (Frontend specialist 2026-05-24): isMountedRef previene
  // setState-on-unmounted warnings + memory leaks cuando el operador
  // navega away durante un SSE/polling en curso. Cada callback async
  // chequea esto antes de tocar state.
  const isMountedRef = useRef(true);''',
    desc="App.jsx — isMountedRef declaration",
)

# B) Guard en SSE onmessage callback.
patch(
    "lyricgen/frontend/src/App.jsx",
    '''      if (es) {
        const cleanup = () => { es.close(); pollingIntervals.current.delete(es); };
        pollingIntervals.current.add(es);
        es.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data);
            setJobs((prev) => prev.map((j) =>''',
    '''      if (es) {
        const cleanup = () => { es.close(); pollingIntervals.current.delete(es); };
        pollingIntervals.current.add(es);
        es.onmessage = (e) => {
          if (!isMountedRef.current) { cleanup(); return; }
          try {
            const data = JSON.parse(e.data);
            setJobs((prev) => prev.map((j) =>''',
    desc="App.jsx — guard SSE onmessage",
)

# C) Guard en SSE onmessage TERMINAL branch (fetchHistory + resolve).
patch(
    "lyricgen/frontend/src/App.jsx",
    '''            if (TERMINAL.has(data.status)) {
              cleanup();
              fetchHistory();
              resolve(data.status);
            }
          } catch {}
        };
        es.onerror = () => {''',
    '''            if (TERMINAL.has(data.status)) {
              cleanup();
              if (isMountedRef.current) fetchHistory();
              resolve(data.status);
            }
          } catch {}
        };
        es.onerror = () => {''',
    desc="App.jsx — guard SSE TERMINAL fetchHistory",
)

# D) Guard en polling interval callback (clearInterval branches).
patch(
    "lyricgen/frontend/src/App.jsx",
    '''        const iv = setInterval(async () => {
          if (typeof document !== "undefined" && document.hidden) return;
          if (!getToken()) {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            resolve("aborted");
            return;
          }''',
    '''        const iv = setInterval(async () => {
          if (typeof document !== "undefined" && document.hidden) return;
          if (!isMountedRef.current) {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            resolve("aborted");
            return;
          }
          if (!getToken()) {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            resolve("aborted");
            return;
          }''',
    desc="App.jsx — guard polling unmount check",
)

# E) Guard en polling setJobs + terminal branch.
patch(
    "lyricgen/frontend/src/App.jsx",
    '''            if (!res.ok) return;
            const data = await res.json();
            setJobs((prev) => prev.map((j) =>
              j.job_id === jobId
                ? { ...j, status: data.status, current_step: data.current_step,
                    progress: data.progress, error: data.error,
                    created_at: data.created_at ?? j.created_at,
                    completed_at: data.completed_at ?? j.completed_at }
                : j
            ));
            if (TERMINAL.has(data.status)) {
              clearInterval(iv);
              pollingIntervals.current.delete(iv);
              fetchHistory();
              resolve(data.status);
            }''',
    '''            if (!res.ok) return;
            const data = await res.json();
            if (!isMountedRef.current) {
              clearInterval(iv);
              pollingIntervals.current.delete(iv);
              resolve("aborted");
              return;
            }
            setJobs((prev) => prev.map((j) =>
              j.job_id === jobId
                ? { ...j, status: data.status, current_step: data.current_step,
                    progress: data.progress, error: data.error,
                    created_at: data.created_at ?? j.created_at,
                    completed_at: data.completed_at ?? j.completed_at }
                : j
            ));
            if (TERMINAL.has(data.status)) {
              clearInterval(iv);
              pollingIntervals.current.delete(iv);
              if (isMountedRef.current) fetchHistory();
              resolve(data.status);
            }''',
    desc="App.jsx — guard polling setJobs + terminal",
)

# F) Setear isMountedRef.current = false en el cleanup del useEffect global.
patch(
    "lyricgen/frontend/src/App.jsx",
    '''  useEffect(() => () => {
    pollingIntervals.current.forEach((handle) => {
      if (handle && typeof handle.close === "function") handle.close();
      else clearInterval(handle);
    });
  }, []);''',
    '''  useEffect(() => () => {
    // R-FRONT-5: marca unmounted ANTES de cerrar handles para que
    // cualquier callback async en flight (SSE messages bufferadas, polls
    // ya disparados) salga temprano vía el guard sin tocar state.
    isMountedRef.current = false;
    pollingIntervals.current.forEach((handle) => {
      if (handle && typeof handle.close === "function") handle.close();
      else clearInterval(handle);
    });
  }, []);''',
    desc="App.jsx — set isMountedRef=false on unmount",
)


# ============================================================================
# #43 — AbortController signal end-to-end en prefetchRemaining
# ============================================================================
print("\n[#43] AbortController signal end-to-end...")

# Pasar signal: controller.signal a uploadFileToR2 + al fetch de
# /transcribe-uploaded dentro del loop. También manejar AbortError en catch
# para break clean.
patch(
    "lyricgen/frontend/src/App.jsx",
    '''      try {
        setRowStatus(file, "uploading");
        const { jobId } = await uploadFileToR2(file, {
          meta: { artist: entry.artist || "", title: (entry.songTitle || "").trim() },
        });
        setRowStatus(file, "queued");
        const res = await authFetchWithRetryOn503(`${API}/transcribe-uploaded`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: jobId,
            language: entry.language || "",
            artist: entry.artist || "",
            title: (entry.songTitle || "").trim(),
          }),
        }, { maxRetries: 3 });''',
    '''      try {
        setRowStatus(file, "uploading");
        const { jobId } = await uploadFileToR2(file, {
          meta: { artist: entry.artist || "", title: (entry.songTitle || "").trim() },
          // R-FRONT-3 end-to-end: si handleReset abort, la upload se corta
          // en la mitad del multipart en vez de seguir hasta terminar.
          signal: controller && controller.signal,
        });
        setRowStatus(file, "queued");
        const res = await authFetchWithRetryOn503(`${API}/transcribe-uploaded`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: jobId,
            language: entry.language || "",
            artist: entry.artist || "",
            title: (entry.songTitle || "").trim(),
          }),
          signal: controller && controller.signal,
        }, { maxRetries: 3 });''',
    desc="App.jsx — pass signal to upload + fetch",
)

# Manejar AbortError en el catch del loop (sin esto, el aborted upload
# tira una excepción que termina el prefetch loop pero deja prefetchCache
# en "loading" forever).
patch(
    "lyricgen/frontend/src/App.jsx",
    '''      } catch (err) {
        prefetchCache.current[idx] = { status: "error" };
        setRowStatus(file, "error");
        continue;
      }''',
    '''      } catch (err) {
        // Aborted (handleReset disparó abort): el iter actual se rompe y
        // el siguiente check de signal.aborted al top corta el loop.
        if (err && (err.name === "AbortError" || (controller && controller.signal.aborted))) {
          prefetchCache.current[idx] = { status: "aborted" };
          break;
        }
        prefetchCache.current[idx] = { status: "error" };
        setRowStatus(file, "error");
        continue;
      }''',
    desc="App.jsx — handle AbortError in prefetch catch",
)


# ============================================================================
# #44b — Dashboard loading state polish con SkeletonVideoCard
# ============================================================================
print("\n[#44b] Dashboard loading state → skeleton...")

# Import Skeleton en Dashboard.
patch(
    "lyricgen/frontend/src/components/Dashboard.jsx",
    '''import { DashboardTour } from "./OnboardingTour";
import ProResBadge from "./ProResBadge";''',
    '''import { DashboardTour } from "./OnboardingTour";
import ProResBadge from "./ProResBadge";
import { SkeletonVideoCard, SkeletonProcessingRow } from "./Skeleton";''',
    desc="Dashboard — import Skeleton",
)

# Reemplazar el spinner del initial-load por 6 SkeletonVideoCards en grid.
patch(
    "lyricgen/frontend/src/components/Dashboard.jsx",
    '''      {history.length === 0 && !historyLoaded && !historyError && (
        <div className="rounded-card p-14 text-center bg-surface-2/30 ring-1 ring-white/[0.04]">
          <div className="w-10 h-10 mx-auto mb-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-ink-secondary">
            {t("history.loading") || "Cargando historial…"}
          </p>
        </div>
      )}''',
    '''      {history.length === 0 && !historyLoaded && !historyError && (
        /* Skeleton screen — UI specialist 2026-05-24: reemplazó el spinner
           genérico. El operador YA ve la estructura del Dashboard (6 cards
           en grid) antes de que llegue /jobs; el swap no tiene reflow. */
        <div>
          <div className="mb-4">
            <div className="h-3 w-32 rounded bg-surface-2/60 animate-pulse mb-3" />
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <SkeletonVideoCard key={i} />
            ))}
          </div>
        </div>
      )}''',
    desc="Dashboard — spinner → skeleton grid",
)


# ============================================================================
# #45 — Bulk migrate text-[12px] standalone → text-caption
# ============================================================================
print("\n[#45] Bulk migrate text-[12px] standalone → text-caption...")

# IMPORTANT: solo standalone (sin font-medium/semibold/bold después).
# Approach: regex que matchee `text-[12px]` que NO esté seguido inmediato
# por space + font-{medium|semibold|bold}.
import re

PATTERN = re.compile(r'text-\[12px\](?! font-(?:medium|semibold|bold))')
total = 0
for p in (ROOT / "lyricgen/frontend/src").rglob("*.jsx"):
    src = p.read_text()
    matches = PATTERN.findall(src)
    if not matches:
        continue
    new = PATTERN.sub("text-caption", src)
    p.write_text(new)
    rel = p.relative_to(ROOT / "lyricgen/frontend/src")
    print(f"  ✓ {rel}: {len(matches)} replace(s)")
    total += len(matches)
print(f"  Total bulk: {total} reemplazos")


print("\n✅ Edits aplicados.")
PYEOF

echo "==> Verificando build frontend..."
( cd lyricgen/frontend && npm run build --silent 2>&1 | tail -10 )

echo "==> Diff resumen..."
git diff --stat

echo ""
echo "==> Si todo OK, commit + push:"
echo ""
cat <<MSG
git add -A

git commit -m "polish(wizard): isMountedRef + AbortController e2e + Skeleton + typography v2

#42 isMountedRef en pollJob (Frontend specialist RIESGO 5):
- Ref creado a App-level + set false en useEffect cleanup
- 5 guards: SSE onmessage, SSE terminal fetchHistory, polling unmount check,
  polling setJobs, polling terminal
- Previene setState-on-unmounted warnings + memory leaks cuando user navega
  away durante SSE/polling

#43 AbortController signal end-to-end (Frontend specialist cost-leak v2):
- prefetchRemaining ahora pasa signal a uploadFileToR2 (cancela multipart
  upload en flight) Y a authFetchWithRetryOn503 (cancela el POST a
  /transcribe-uploaded)
- catch handle AbortError para break clean (sin dejar prefetchCache en
  'loading' forever)
- Antes (v1): solo loop-iter check. Ahora (v2): requests en flight TAMBIÉN
  se cancelan

#44 Skeleton component + Dashboard loading polish:
- Skeleton.jsx nuevo: <Skeleton>, <SkeletonVideoCard>, <SkeletonProcessingRow>
- Dashboard history-loading: spinner → 6 SkeletonVideoCards en grid
- El swap visual no tiene reflow porque las skeletons matchean el aspect
  ratio + dims de VideoCard

#45 Typography bulk v2:
- text-[12px] standalone (sin font-weight) → text-caption en TODA la base
- Conservative: las que tienen font-medium/font-semibold se mantienen

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"

git push origin feat/wizard-no-redirect
MSG
echo ""
echo "==> Done."
