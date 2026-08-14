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
