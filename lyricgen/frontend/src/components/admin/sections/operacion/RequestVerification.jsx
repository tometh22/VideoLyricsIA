import { useEffect, useState } from "react";

const clock = (seconds) => {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
};

const KIND = { text: "Letra", timing: "Timing", metadata: "Datos", punctuation: "Signos", visual: "Imagen", manual: "Revisar" };

// Por qué un punto quedó ✘ (o sin comprobar), dicho para el operador.
const REASONS = {
  found_only_elsewhere: "La frase está en otra parte de la canción, no en el tiempo citado.",
  expected_text_missing: "La letra no dice lo que pide el cliente en ese tiempo.",
  current_text_still_present: "Sigue estando lo que el cliente pidió sacar.",
  same_occurrence_as_other_time: "Ese tiempo usa la misma línea que otro tiempo del pedido: falta corregir esta aparición.",
  repetitions_not_fixed: "El cliente pidió corregirlo en todas las repeticiones y queda alguna sin cambiar.",
  interval_mismatch: "El inicio o el fin de la línea no coinciden con los tiempos pedidos.",
  timing_unchanged: "La línea tiene el mismo timing que vio el cliente.",
  timing_moved_wrong_way: "El timing cambió, pero no en la dirección pedida.",
  start_differs_from_request: "La línea no empieza donde pidió el cliente.",
  end_short_of_request: "La línea termina antes de lo pedido.",
  timing_line_not_found: "No encontré la línea en ese tiempo.",
  metadata_differs: "El dato publicado no coincide con lo pedido.",
  question_marks_remain: "Siguen los signos de pregunta.",
  terminal_period_remains: "Sigue el punto final.",
  timing_without_previous_version: "No hay versión anterior para comparar el timing: revisalo en el video.",
  lyrics_unavailable: "No hay letra aprobada para comparar.",
  metadata_unavailable: "No pude leer el dato publicado.",
  unbalanced_quotes: "Comillas desparejas en el pedido: revisalo a mano.",
  structure_review: "Pide cortar o unir líneas: revisalo en el video.",
  free_prose: "Instrucción en prosa: revisala en el video.",
  unparsed_instruction: "No pude leer esta instrucción: revisala en el video.",
  visual: "Pedido de imagen: revisalo en el video.",
};

const QUICK_REASONS = ["Errata en el pedido del cliente", "Ya estaba bien; lo confirmé en el video", "El cliente aceptó dejarlo así"];

/** ¿Se puede cerrar? Cada punto ✘ necesita su justificación. */
export function verificationGate(verification, overrides = {}) {
  const failing = (verification?.points || []).filter((point) => point.status === "fail");
  const missing = failing.filter((point) => !String(overrides[point.id] || "").trim()).map((point) => point.id);
  const clean = Object.fromEntries(failing
    .filter((point) => String(overrides[point.id] || "").trim())
    .map((point) => [point.id, String(overrides[point.id]).trim()]));
  return { ready: missing.length === 0, missing, overrides: clean, failing: failing.length };
}

/**
 * Cada punto del pedido contra la letra aprobada (la que se publica): ✔ está,
 * ✘ no está, • hay que mirarlo en el video. Un ✘ sólo se cierra con una
 * justificación interna (no llega al cliente). `load` devuelve
 * `{enabled, verification, recurrences}`.
 */
export default function RequestVerification({ load, refreshKey, onChange, onSeek }) {
  const [state, setState] = useState({ loading: true, error: "", data: null });
  const [overrides, setOverrides] = useState({});

  useEffect(() => {
    let alive = true;
    setState({ loading: true, error: "", data: null });
    load()
      .then((data) => { if (alive) setState({ loading: false, error: "", data }); })
      .catch((error) => { if (alive) setState({ loading: false, error: error?.message || "No pude verificar el pedido.", data: null }); });
    return () => { alive = false; };
  }, [load, refreshKey]);

  const verification = state.data?.enabled ? state.data.verification : null;
  const gate = verificationGate(verification, overrides);
  useEffect(() => {
    // Mientras carga, o si falló la consulta, no se traba la UI: el servidor
    // verifica igual al cerrar y responde 409 con los puntos ✘.
    onChange?.({ ready: state.loading || gate.ready, overrides: gate.overrides, loading: state.loading });
  }, [state.loading, gate.ready, JSON.stringify(gate.overrides)]); // eslint-disable-line react-hooks/exhaustive-deps

  const recurrences = state.data?.recurrences || [];
  if (state.loading) {
    return <p aria-live="polite" className="text-label text-gray-400">Verificando cada punto contra la letra…</p>;
  }
  if (state.error) {
    return <p role="alert" className="text-label text-amber-200">No pude verificar el pedido: {state.error}. Revisalo en el video.</p>;
  }
  if (!verification && !recurrences.length) return null;
  const points = verification?.points || [];
  const counts = verification?.counts || {};
  return (
    <section aria-label="Verificación automática del pedido"
      className="rounded-2xl bg-white/[0.025] p-4 ring-1 ring-white/[0.07]">
      {recurrences.length > 0 && (
        <p role="note" className="mb-3 rounded-xl bg-red-500/10 p-2.5 text-label text-red-100 ring-1 ring-red-400/25">
          <span className="font-semibold">Reincidencia:</span> repite {recurrences.length === 1 ? "un punto" : `${recurrences.length} puntos`} del pedido
          {" "}#{recurrences[0].request_id}, que ya se había cerrado. Verificá que esta vez quede completo.
        </p>
      )}
      {verification && <>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-label font-semibold uppercase tracking-[0.16em] text-gray-400">Verificación contra la letra</p>
          <span className="text-label text-gray-400">
            <span className="text-emerald-300">{counts.ok || 0} ✔</span> · <span className="text-red-300">{counts.fail || 0} ✘</span>
            {" "}· {counts.manual || 0} a revisar en el video
          </span>
        </div>
        <p className="mt-1 text-label text-gray-500">
          Letra aprobada{verification.revision != null ? ` (revisión ${verification.revision})` : ""}. Un ✘ bloquea el cierre hasta que lo corrijas o escribas por qué está bien; la justificación queda interna.
        </p>
        <ol className="mt-3 space-y-2">
          {points.map((point) => {
            const failed = point.status === "fail";
            const icon = failed ? "✘" : point.status === "ok" ? "✔" : "•";
            const tone = failed ? "text-red-300" : point.status === "ok" ? "text-emerald-300" : "text-gray-400";
            const line = point.line;
            return (
              <li key={point.id} className={`rounded-xl p-3 ${failed ? "bg-red-500/[0.06] ring-1 ring-red-400/20" : "bg-black/20"}`}
                aria-label={`${failed ? "No verificado" : point.status === "ok" ? "Verificado" : "Revisar"}: ${point.excerpt}`}>
                <div className="flex items-start gap-2">
                  <span aria-hidden="true" className={`mt-0.5 font-bold ${tone}`}>{icon}</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-caption text-gray-100 break-words">
                      <span className="mr-1.5 text-[10px] uppercase tracking-wider text-gray-500">{KIND[point.kind] || point.kind}</span>
                      {point.excerpt}
                    </p>
                    {point.status !== "ok" && REASONS[point.reason] && (
                      <p className={`mt-1 text-label ${failed ? "text-red-200" : "text-gray-400"}`}>{REASONS[point.reason]}</p>
                    )}
                    {line && (
                      <p className="mt-1 text-label text-gray-300">
                        <button type="button" onClick={() => onSeek?.(line.start)} className="mr-2 font-mono text-brand-light hover:text-white">
                          {clock(line.start)}
                        </button>
                        {line.text}
                      </p>
                    )}
                    {point.kind === "metadata" && point.current_value && (
                      <p className="mt-1 text-label text-gray-300">Publicado: {point.current_value}</p>
                    )}
                    {Array.isArray(point.repetitions_pending) && point.repetitions_pending.length > 0 && (
                      <p className="mt-1 text-label text-amber-200">
                        La misma línea se repite sin corregir en {point.repetitions_pending.map((row) => clock(row.start)).join(", ")}.
                      </p>
                    )}
                    {failed && (
                      <div className="mt-2 space-y-1.5">
                        <div className="flex flex-wrap gap-1">
                          {QUICK_REASONS.map((text) => (
                            <button key={text} type="button"
                              onClick={() => setOverrides((old) => ({ ...old, [point.id]: text }))}
                              className="rounded-full bg-white/[0.05] px-2 py-0.5 text-[11px] text-gray-300 ring-1 ring-white/10 hover:text-white">
                              {text}
                            </button>
                          ))}
                        </div>
                        <textarea
                          aria-label={`Justificación interna: ${point.excerpt}`}
                          placeholder="Por qué está bien así (interno, el cliente no lo ve)"
                          value={overrides[point.id] || ""}
                          onChange={(event) => { const { value } = event.target; setOverrides((old) => ({ ...old, [point.id]: value })); }}
                          maxLength={1000}
                          rows={2}
                          className="w-full rounded-lg bg-surface-3/40 px-2.5 py-1.5 text-caption text-white ring-1 ring-white/[0.06] placeholder:text-gray-600"
                        />
                      </div>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
        {gate.failing > 0 && !gate.ready && (
          <p aria-live="polite" className="mt-3 text-label text-red-200">
            Faltan {gate.missing.length} de {gate.failing} justificaciones para poder cerrar. Lo mejor es corregirlo y volver a publicar.
          </p>
        )}
      </>}
    </section>
  );
}
