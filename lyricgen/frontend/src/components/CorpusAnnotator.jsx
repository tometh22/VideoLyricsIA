// Herramienta de anotación del corpus (proyecto de calibración del
// validador de calidad). Página NUEVA y AISLADA: no importa nada de
// LyricsEditor.jsx ni de sus rutas — ver lyricgen/backend/corpus.py para
// el detalle de por qué está separada del editor de producción.
//
// Pensada para gente NO técnica: sin login, sin jerga, botones grandes.
// El link (`/annotate/:token`) ES la credencial — no hay usuario/contraseña.
//
// Todo el texto está hardcodeado en español a propósito (no usa el sistema
// de i18n de la app: esta página nunca se muestra en otro idioma, la usan
// dos personas puntuales elegidas a mano por el founder).
import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { useParams } from "react-router-dom";

const API = import.meta.env.VITE_API_URL || "";

const EVENT_TYPES = [
  { value: "lexical", label: "Palabra real", hint: "Se entiende lo que dice. Ej: \"te quiero\"" },
  { value: "vocalization", label: "Sonido sin palabra", hint: "No hay palabras. Ej: \"oh oh\", \"la la la\", un grito" },
  { value: "mixed", label: "Mezcla", hint: "Las dos cosas juntas. Ej: \"vamos, oh oh oh\"" },
];

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function api(path, opts) {
  const res = await fetch(`${API}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts?.headers || {}) },
  });
  return res;
}

// ---------------------------------------------------------------------------
// Waveform: canvas simple, sin dependencias. Barras de pico (0..1) + línea
// de reproducción + segmentos ya marcados coloreados encima.
// ---------------------------------------------------------------------------
function Waveform({ peaks, duration, currentTime, segments, onSeek }) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const [width, setWidth] = useState(800);
  const height = 120;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width;
      if (w) setWidth(Math.max(320, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    // jsdom's canvas stub (unit tests) doesn't implement the 2D context —
    // bail quietly instead of crashing the component under test.
    if (!ctx || typeof ctx.scale !== "function") return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    // Fondo
    ctx.fillStyle = "#151824";
    ctx.fillRect(0, 0, width, height);

    // Segmentos ya marcados (franjas de color detrás de las barras)
    if (duration > 0) {
      for (const seg of segments) {
        const x1 = (seg.start / duration) * width;
        const x2 = (seg.end / duration) * width;
        ctx.fillStyle = seg.event_type === "vocalization"
          ? "rgba(32,212,232,0.25)"
          : seg.event_type === "mixed"
            ? "rgba(255,180,80,0.25)"
            : "rgba(117,87,255,0.30)";
        ctx.fillRect(x1, 0, Math.max(1, x2 - x1), height);
      }
    }

    // Picos
    const list = Array.isArray(peaks) && peaks.length ? peaks : [];
    if (list.length) {
      const barWidth = width / list.length;
      ctx.fillStyle = "#A7ABBA";
      list.forEach((p, i) => {
        const h = Math.max(2, p * (height - 8));
        const x = i * barWidth;
        const y = (height - h) / 2;
        ctx.fillRect(x, y, Math.max(1, barWidth - 1), h);
      });
    } else {
      ctx.fillStyle = "#3a3f52";
      ctx.fillRect(0, height / 2 - 1, width, 2);
    }

    // Línea de reproducción
    if (duration > 0) {
      const px = (currentTime / duration) * width;
      ctx.fillStyle = "#20D4E8";
      ctx.fillRect(Math.max(0, px - 1), 0, 2, height);
    }
  }, [peaks, duration, currentTime, segments, width]);

  const handleClick = (e) => {
    if (!duration || !onSeek) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const ratio = (e.clientX - rect.left) / rect.width;
    onSeek(Math.max(0, Math.min(duration, ratio * duration)));
  };

  return (
    <div ref={containerRef} className="w-full">
      <canvas
        ref={canvasRef}
        onClick={handleClick}
        className="w-full rounded-card cursor-pointer"
        style={{ height }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pantalla principal
// ---------------------------------------------------------------------------
export default function CorpusAnnotator() {
  const { token } = useParams();

  const [phase, setPhase] = useState("loading"); // loading | invalid_link | load_error | songs | annotating
  const [loadAttempt, setLoadAttempt] = useState(0); // bump para forzar un reintento
  const [annotatorName, setAnnotatorName] = useState("");
  const [songs, setSongs] = useState([]);
  const [activeSong, setActiveSong] = useState(null);
  const [segments, setSegments] = useState([]);
  const [status, setStatus] = useState("draft");
  const [audioUrl, setAudioUrl] = useState(null);
  const [waveform, setWaveform] = useState({ peaks: [], duration: 0 });
  const [currentTime, setCurrentTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [markStart, setMarkStart] = useState(null);
  const [draftText, setDraftText] = useState("");
  const [draftType, setDraftType] = useState("lexical");
  const [saveState, setSaveState] = useState("idle"); // idle | saving | saved | error
  const [songError, setSongError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // true cuando esta canción arrancó con frases ya marcadas de antes (ver
  // GET /annotate/{token}/songs/{song_id} en corpus.py) — no le decimos de
  // dónde salió esa marca, solo que ya hay una primera pasada para revisar.
  const [seededFromReference, setSeededFromReference] = useState(false);

  const audioRef = useRef(null);
  const saveTimer = useRef(null);

  // --- Carga inicial: valida el link + trae la lista de canciones ---
  // OJO: envuelto en try/catch a propósito. Sin esto, cualquier falla de red
  // (un corte momentáneo, un bloqueador de anuncios, un cold-start del
  // backend) deja a la persona mirando "Cargando…" para siempre, sin error
  // ni forma de reintentar — pasó en la práctica el 24-ago con la primera
  // anotadora real. `phase="load_error"` le da una salida visible.
  useEffect(() => {
    let cancelled = false;
    setPhase("loading");
    (async () => {
      try {
        const res = await api(`/annotate/${token}`);
        if (cancelled) return;
        if (!res.ok) {
          setPhase("invalid_link");
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        setAnnotatorName(data.name);
        const listRes = await api(`/annotate/${token}/songs`);
        if (cancelled) return;
        if (!listRes.ok) {
          setPhase("invalid_link");
          return;
        }
        const listData = await listRes.json();
        if (cancelled) return;
        setSongs(listData.songs || []);
        setPhase("songs");
      } catch {
        if (!cancelled) setPhase("load_error");
      }
    })();
    return () => { cancelled = true; };
  }, [token, loadAttempt]);

  const openSong = useCallback(async (song) => {
    setSongError("");
    setActiveSong(song);
    setPhase("annotating");
    setMarkStart(null);
    setDraftText("");
    setDraftType("lexical");
    setCurrentTime(0);
    setIsPlaying(false);
    setAudioUrl(null);
    setWaveform({ peaks: [], duration: 0 });
    setSeededFromReference(false);

    let annRes, audioRes, waveRes;
    try {
      [annRes, audioRes, waveRes] = await Promise.all([
        api(`/annotate/${token}/songs/${song.id}`),
        api(`/annotate/${token}/songs/${song.id}/audio-url`),
        api(`/annotate/${token}/songs/${song.id}/waveform`),
      ]);
    } catch {
      // Mismo problema que la carga inicial: sin este catch, un corte de
      // red acá deja la canción abierta con la forma de onda vacía y sin
      // audio, sin ningún mensaje — parece "roto" en vez de "reintentá".
      setSongError("Hubo un problema de conexión cargando esta canción. Volvé a intentar.");
      return;
    }

    if (annRes.ok) {
      const data = await annRes.json();
      setSegments(data.annotation.segments || []);
      setStatus(data.annotation.status || "draft");
      setSeededFromReference(Boolean(data.annotation.seeded_from_reference));
    }
    if (audioRes.ok) {
      const data = await audioRes.json();
      setAudioUrl(data.url);
    } else {
      setSongError("No pudimos cargar el audio de esta canción. Probá de nuevo en un momento.");
    }
    if (waveRes.ok) {
      const data = await waveRes.json();
      setWaveform({ peaks: data.peaks || [], duration: data.duration || 0 });
    }
  }, [token]);

  const backToSongs = () => {
    if (audioRef.current) audioRef.current.pause();
    setPhase("songs");
    setActiveSong(null);
    // Refresca el estado ("Sin empezar" / "Borrador" / "Enviada") de la lista.
    api(`/annotate/${token}/songs`).then((r) => r.ok && r.json()).then((d) => {
      if (d) setSongs(d.songs || []);
    });
  };

  // --- Autosave: cada cambio en `segments` programa un guardado en 1.2s ---
  useEffect(() => {
    if (phase !== "annotating" || !activeSong) return;
    setSaveState("saving");
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      try {
        const res = await api(`/annotate/${token}/songs/${activeSong.id}`, {
          method: "PUT",
          body: JSON.stringify({ segments }),
        });
        setSaveState(res.ok ? "saved" : "error");
      } catch {
        setSaveState("error");
      }
    }, 1200);
    return () => clearTimeout(saveTimer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segments]);

  // --- Audio element wiring ---
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onTime = () => setCurrentTime(el.currentTime);
    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
    };
  }, [audioUrl]);

  const togglePlay = () => {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) el.play(); else el.pause();
  };

  const seek = (t) => {
    const el = audioRef.current;
    if (el) el.currentTime = t;
    setCurrentTime(t);
  };

  const handleMarkStart = () => setMarkStart(currentTime);
  const handleMarkEnd = () => {
    if (markStart == null) return;
    const start = Math.min(markStart, currentTime);
    const end = Math.max(markStart, currentTime);
    if (end - start < 0.05) return; // demasiado corto, probablemente un doble click
    setPendingEnd(end);
    setPendingStart(start);
  };

  // Guardamos el rango "en preparación" aparte del texto/tipo para que el
  // formulario de abajo (texto + tipo) se complete DESPUÉS de marcar inicio
  // y fin, sin perder el rango mientras la persona escribe.
  const [pendingStart, setPendingStart] = useState(null);
  const [pendingEnd, setPendingEnd] = useState(null);

  const hasPendingRange = pendingStart != null && pendingEnd != null;

  const addSegment = () => {
    if (!hasPendingRange) return;
    const text = draftText.trim();
    setSegments((prev) => [
      ...prev,
      { start: pendingStart, end: pendingEnd, text, event_type: draftType },
    ].sort((a, b) => a.start - b.start));
    setPendingStart(null);
    setPendingEnd(null);
    setMarkStart(null);
    setDraftText("");
    setDraftType("lexical");
  };

  const cancelPending = () => {
    setPendingStart(null);
    setPendingEnd(null);
    setMarkStart(null);
  };

  const removeSegment = (idx) => {
    setSegments((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      const res = await api(`/annotate/${token}/songs/${activeSong.id}/submit`, {
        method: "POST",
      });
      if (res.ok) {
        setStatus("submitted");
      } else {
        const data = await res.json().catch(() => ({}));
        alert(data.detail || "No se pudo enviar. Intentá de nuevo.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const sortedSegments = useMemo(
    () => [...segments].sort((a, b) => a.start - b.start),
    [segments],
  );

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------
  if (phase === "loading") {
    return (
      <Shell>
        <p className="text-ink-secondary text-body">Cargando…</p>
      </Shell>
    );
  }

  if (phase === "load_error") {
    return (
      <Shell>
        <div className="max-w-md text-center">
          <h1 className="text-h2 text-ink-primary mb-4">No se pudo cargar</h1>
          <p className="text-ink-secondary text-body mb-6">
            Hubo un problema de conexión. Puede pasar, no es nada que hayas
            hecho mal — apretá el botón para intentar de nuevo.
          </p>
          <button
            onClick={() => setLoadAttempt((n) => n + 1)}
            className="rounded-button bg-brand hover:bg-brand-light text-white px-6 py-3 font-semibold transition-colors duration-brand"
          >
            Reintentar
          </button>
        </div>
      </Shell>
    );
  }

  if (phase === "invalid_link") {
    return (
      <Shell>
        <div className="max-w-md text-center">
          <h1 className="text-h2 text-ink-primary mb-4">Este link no funciona</h1>
          <p className="text-ink-secondary text-body">
            Puede que el link esté mal copiado o que ya no esté activo.
            Pedile al equipo de Genly que te mande uno nuevo.
          </p>
        </div>
      </Shell>
    );
  }

  if (phase === "songs") {
    return (
      <Shell>
        <div className="w-full max-w-2xl">
          <h1 className="text-h2 text-ink-primary mb-2">Hola, {annotatorName} 👋</h1>
          <p className="text-ink-secondary text-body mb-6">
            Elegí una canción de la lista de abajo. Para cada una vas a ir marcando,
            frase por frase, dónde empieza y dónde termina lo que se canta.
          </p>

          <div className="bg-surface-1 rounded-card p-5 mb-8">
            <h2 className="text-ink-primary font-semibold mb-3">Cómo funciona, paso a paso</h2>
            <ol className="text-ink-secondary text-body space-y-2 list-decimal list-inside">
              <li>Escuchá la canción hasta que empiece la primera frase cantada.</li>
              <li>Apretá <b className="text-ink-primary">"Marcar inicio"</b> justo cuando arranca esa frase.</li>
              <li>Dejá que siga sonando y apretá <b className="text-ink-primary">"Marcar fin"</b> justo cuando esa frase termina.</li>
              <li>Escribí lo que se canta en ese pedacito (o dejalo vacío si es solo un sonido, sin palabras).</li>
              <li>Elegí si fue una palabra real, un sonido sin palabra, o una mezcla de las dos.</li>
              <li>Apretá <b className="text-ink-primary">"Agregar frase"</b> y repetí con la siguiente, hasta terminar la canción.</li>
              <li>Cuando termines toda la canción, apretá <b className="text-ink-primary">"Enviar canción"</b>.</li>
            </ol>
            <p className="text-ink-secondary text-ui mt-4">
              No hay apuro ni hay que hacerlo perfecto a la primera: si te equivocás en una frase,
              la podés borrar y volver a marcarla. Todo lo que vas haciendo se guarda solo — podés
              cerrar la página en cualquier momento y seguir después donde quedaste.
            </p>
          </div>

          <div className="space-y-3">
            {songs.length === 0 && (
              <p className="text-ink-secondary">Todavía no hay canciones cargadas.</p>
            )}
            {songs.map((song) => (
              <button
                key={song.id}
                onClick={() => openSong(song)}
                className="w-full flex items-center justify-between rounded-card bg-surface-2 hover:bg-surface-3 border border-white/5 px-6 py-4 text-left transition-colors duration-brand"
              >
                <div>
                  <div className="text-ink-primary font-semibold">{song.title}</div>
                  <div className="text-ink-secondary text-ui">{song.artist}</div>
                </div>
                <StatusPill status={song.my_status} count={song.my_segment_count} />
              </button>
            ))}
          </div>
        </div>
      </Shell>
    );
  }

  // phase === "annotating"
  return (
    <Shell wide>
      <div className="w-full max-w-3xl">
        <button
          onClick={backToSongs}
          className="text-ink-secondary hover:text-ink-primary text-ui mb-4 inline-flex items-center gap-1"
        >
          ← Volver a mis canciones
        </button>

        <div className="flex items-center justify-between mb-1">
          <h1 className="text-h2 text-ink-primary">{activeSong?.title}</h1>
          {status === "submitted" && (
            <span className="text-accent text-ui font-semibold">✓ Enviada</span>
          )}
        </div>
        <p className="text-ink-secondary text-body mb-6">{activeSong?.artist}</p>

        {songError && (
          <div className="rounded-card bg-red-500/10 border border-red-500/30 text-red-300 px-4 py-3 mb-4 text-ui">
            {songError}
          </div>
        )}

        {seededFromReference && (
          <div className="rounded-card bg-accent/10 border border-accent/30 text-ink-primary px-4 py-3 mb-4 text-ui">
            Esta canción ya tiene una primera marca hecha. Escuchá cada frase igual
            y corregí lo que haga falta — no des por buena una frase sin escucharla.
          </div>
        )}

        {/* Traer el audio + la forma de onda tarda unos segundos (son archivos
            de varios minutos). Sin este aviso la pantalla queda en blanco
            todo ese tiempo y parece que la página está rota — le pasó a la
            primera anotadora real el 25-ago: abandonó la canción pensando
            que no funcionaba. */}
        {!audioUrl && !songError && (
          <div className="bg-surface-1 rounded-card p-8 mb-4 flex flex-col items-center gap-3">
            <div className="w-8 h-8 rounded-full border-2 border-white/20 border-t-brand animate-spin" />
            <p className="text-ink-secondary text-body">Cargando la canción… puede tardar unos segundos.</p>
          </div>
        )}

        {audioUrl && (
          <>
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <audio ref={audioRef} src={audioUrl} preload="metadata" />

            <div className="bg-surface-1 rounded-card p-4 mb-4">
              <Waveform
                peaks={waveform.peaks}
                duration={waveform.duration}
                currentTime={currentTime}
                segments={sortedSegments}
                onSeek={seek}
              />
              <div className="flex items-center gap-4 mt-4">
                <button
                  onClick={togglePlay}
                  className="w-12 h-12 rounded-full bg-brand hover:bg-brand-light flex items-center justify-center text-white text-xl font-bold transition-colors duration-brand"
                  aria-label={isPlaying ? "Pausar" : "Reproducir"}
                >
                  {isPlaying ? "❚❚" : "▶"}
                </button>
                <span className="text-ink-secondary text-ui tabular-nums">
                  {formatTime(currentTime)} / {formatTime(waveform.duration)}
                </span>
              </div>
            </div>

            {/* Marcado de frase */}
            <div className="bg-surface-1 rounded-card p-5 mb-4">
              <h2 className="text-ink-primary font-semibold mb-2">Marcar una frase</h2>
              <p className="text-ink-secondary text-ui mb-4">
                Ejemplo: si la canción dice <b className="text-ink-primary">"te quiero"</b> y
                empieza a sonar en el segundo 12 y termina en el segundo 14 — apretás
                "Marcar inicio" en el segundo 12, dejás que siga la canción, y apretás
                "Marcar fin" en el segundo 14. Después escribís "te quiero" y elegís "Palabra real".
              </p>

              {!hasPendingRange ? (
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    onClick={handleMarkStart}
                    className="rounded-button bg-surface-3 hover:bg-surface-2 border border-white/10 text-ink-primary px-5 py-3 font-medium transition-colors duration-brand"
                  >
                    ● Marcar inicio {markStart != null && `(${formatTime(markStart)})`}
                  </button>
                  <button
                    onClick={handleMarkEnd}
                    disabled={markStart == null}
                    className="rounded-button bg-brand hover:bg-brand-light disabled:opacity-40 disabled:cursor-not-allowed text-white px-5 py-3 font-medium transition-colors duration-brand"
                  >
                    ■ Marcar fin
                  </button>
                  {markStart != null && (
                    <span className="text-ink-secondary text-ui">
                      Reproducí hasta el final de la frase y tocá "Marcar fin".
                    </span>
                  )}
                </div>
              ) : (
                <div className="space-y-4">
                  <p className="text-ink-secondary text-ui">
                    Frase de {formatTime(pendingStart)} a {formatTime(pendingEnd)}
                  </p>
                  <input
                    type="text"
                    autoFocus
                    value={draftText}
                    onChange={(e) => setDraftText(e.target.value)}
                    placeholder="Escribí lo que se canta acá (o dejalo vacío si es solo un sonido)"
                    className="w-full rounded-button bg-surface-3 border border-white/10 text-ink-primary px-4 py-3 text-body placeholder:text-ink-secondary/60 focus:outline-none focus:border-brand"
                  />
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    {EVENT_TYPES.map((opt) => (
                      <button
                        key={opt.value}
                        onClick={() => setDraftType(opt.value)}
                        className={`rounded-button border px-4 py-4 text-left transition-colors duration-brand ${
                          draftType === opt.value
                            ? "bg-brand/20 border-brand text-ink-primary"
                            : "bg-surface-3 border-white/10 text-ink-secondary hover:border-white/20"
                        }`}
                      >
                        <div className="font-semibold">{opt.label}</div>
                        <div className="text-caption opacity-80">{opt.hint}</div>
                      </button>
                    ))}
                  </div>
                  <div className="flex gap-3">
                    <button
                      onClick={addSegment}
                      className="rounded-button bg-brand hover:bg-brand-light text-white px-6 py-3 font-semibold transition-colors duration-brand"
                    >
                      Agregar frase
                    </button>
                    <button
                      onClick={cancelPending}
                      className="rounded-button bg-transparent hover:bg-surface-3 text-ink-secondary px-6 py-3 transition-colors duration-brand"
                    >
                      Cancelar
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Lista de frases marcadas */}
            <div className="bg-surface-1 rounded-card p-5 mb-4">
              <h2 className="text-ink-primary font-semibold mb-3">
                Frases marcadas ({sortedSegments.length})
              </h2>
              {sortedSegments.length === 0 ? (
                <p className="text-ink-secondary text-ui">Todavía no marcaste ninguna frase.</p>
              ) : (
                <div className="space-y-2">
                  {sortedSegments.map((seg, idx) => (
                    <div
                      key={`${seg.start}-${idx}`}
                      className="flex items-center justify-between rounded-button bg-surface-3 px-4 py-3"
                    >
                      <button
                        onClick={() => seek(seg.start)}
                        className="text-left flex-1 mr-3"
                      >
                        <span className="text-ink-secondary text-caption tabular-nums mr-3">
                          {formatTime(seg.start)}–{formatTime(seg.end)}
                        </span>
                        <span className="text-ink-primary">
                          {seg.text || <em className="text-ink-secondary">(sin texto)</em>}
                        </span>
                        <span className="ml-2 text-caption text-accent">
                          {EVENT_TYPES.find((t) => t.value === seg.event_type)?.label}
                        </span>
                      </button>
                      <button
                        onClick={() => removeSegment(idx)}
                        className="text-ink-secondary hover:text-red-400 text-ui px-2"
                        aria-label="Borrar frase"
                      >
                        Borrar
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Guardado + enviar */}
            <div className="flex items-center justify-between">
              <span className="text-ink-secondary text-caption">
                {saveState === "saving" && "Guardando…"}
                {saveState === "saved" && "✓ Guardado"}
                {saveState === "error" && "No se pudo guardar, revisá tu conexión"}
                {saveState === "idle" && ""}
              </span>
              <button
                onClick={handleSubmit}
                disabled={submitting || sortedSegments.length === 0}
                className="rounded-button bg-accent hover:bg-accent-light disabled:opacity-40 disabled:cursor-not-allowed text-surface px-8 py-3 font-semibold transition-colors duration-brand"
              >
                {status === "submitted" ? "Actualizar envío" : "Enviar canción"}
              </button>
            </div>
          </>
        )}
      </div>
    </Shell>
  );
}

function StatusPill({ status, count }) {
  if (status === "submitted") {
    return <span className="text-caption font-semibold text-accent">✓ Enviada</span>;
  }
  if (status === "draft") {
    return (
      <span className="text-caption font-semibold text-ink-secondary">
        Borrador · {count} {count === 1 ? "frase" : "frases"}
      </span>
    );
  }
  return <span className="text-caption text-ink-secondary/60">Sin empezar</span>;
}

function Shell({ children, wide }) {
  return (
    <div className="min-h-screen bg-surface flex flex-col items-center px-4 py-10 sm:py-16">
      <div className={`w-full ${wide ? "max-w-3xl" : "max-w-xl"} flex flex-col items-center`}>
        {children}
      </div>
    </div>
  );
}
