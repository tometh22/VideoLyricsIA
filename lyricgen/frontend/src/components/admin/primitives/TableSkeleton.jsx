// Skeleton de tabla para estados de carga — el operador ve la estructura
// antes de que llegue la data (mismo principio que SkeletonVideoCard).
import { Skeleton } from "../../Skeleton";

export default function TableSkeleton({ rows = 6, cols = 5 }) {
  return (
    <div className="space-y-2 py-2" aria-hidden="true">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-3 px-2 py-1.5">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton
              key={c}
              className={`h-3.5 rounded ${c === 0 ? "w-40" : "flex-1"}`}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
