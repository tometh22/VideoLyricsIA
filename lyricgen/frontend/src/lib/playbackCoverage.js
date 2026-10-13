// Medición de QUÉ tramos del audio sonaron de verdad en el editor (evento
// `editor_audio_played`). Sólo mide: no cambia la reproducción ni la UI.
//
// `editor_seek` dice adónde saltó el operador, no qué escuchó. Esto acumula
// los intervalos que el <audio> efectivamente reprodujo: un tramo empieza al
// dar play (o al saltar mientras suena), crece con cada `timeupdate` y se
// cierra con pausa, fin, error o un salto. El tiempo en pausa no cuenta.
// Al vaciar se fusionan los tramos que se tocan o se pisan y se manda una
// lista acotada de pares [inicio_ms, fin_ms]: sólo números, nunca texto.

export const PLAYBACK_EVENT_NAME = "editor_audio_played";
// Con 64 pares el JSON del evento queda por debajo de 1.500 caracteres aun
// con posiciones de 8 cifras; el backend rechaza propiedades > 2.000.
export const MAX_PLAYED_RANGES = 64;
export const MAX_MEDIA_MS = 86_400_000;
// Dos tramos separados por menos de esto se consideran el mismo (el
// `timeupdate` llega cada ~250 ms y un seek corto hacia adelante no es un
// hueco de escucha relevante).
const MERGE_GAP_S = 0.25;
// Un `timeupdate` que retrocede o que avanza bastante más que el reloj de
// pared es un salto aunque el navegador no haya emitido `seeking` (p. ej. el
// bucle de la revisión guiada o un cambio de fuente).
const BACKWARD_TOLERANCE_S = 0.25;
const FORWARD_SLACK_S = 1.5;

const finite = (value) => Number.isFinite(Number(value));
const toMs = (seconds) => Math.min(MAX_MEDIA_MS, Math.max(0, Math.round(Number(seconds) * 1000)));

export function mergeRanges(ranges, gapS = MERGE_GAP_S) {
  const sorted = (ranges || [])
    .filter((range) => range && finite(range[0]) && finite(range[1]) && range[1] > range[0])
    .map(([start, end]) => [Number(start), Number(end)])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  for (const [start, end] of sorted) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1] + gapS) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

export function createPlaybackCoverage({ maxRanges = MAX_PLAYED_RANGES } = {}) {
  let open = null; // { start, end, wallMs } en segundos de audio / ms de pared
  let closed = []; // [[start, end]] en segundos, ya cerrados
  let playedS = 0; // tiempo de audio reproducido (con repeticiones) sin vaciar
  let rate = 1;

  const close = () => {
    if (open && open.end > open.start) {
      closed.push([open.start, open.end]);
      playedS += open.end - open.start;
    }
    open = null;
  };

  const tracker = {
    // play / playing: abre un tramo en la posición actual.
    start(positionS, wallMs = Date.now(), playbackRate = rate) {
      if (!finite(positionS)) return;
      close();
      if (finite(playbackRate) && playbackRate > 0) rate = Number(playbackRate);
      const position = Math.max(0, Number(positionS));
      open = { start: position, end: position, wallMs };
    },
    // timeupdate mientras suena: extiende el tramo o, si hubo salto, abre otro.
    advance(positionS, wallMs = Date.now(), playbackRate = rate) {
      if (!open || !finite(positionS)) return;
      if (finite(playbackRate) && playbackRate > 0) rate = Number(playbackRate);
      const position = Math.max(0, Number(positionS));
      const elapsedS = Math.max(0, (wallMs - open.wallMs) / 1000);
      const jumped = position < open.end - BACKWARD_TOLERANCE_S
        || position > open.end + elapsedS * rate + FORWARD_SLACK_S;
      if (jumped) {
        close();
        open = { start: position, end: position, wallMs };
        return;
      }
      open.end = Math.max(open.end, position);
      open.wallMs = wallMs;
    },
    // seeking: el tramo anterior termina donde estaba; si sigue sonando,
    // empieza uno nuevo en el destino.
    seek(positionS, wallMs = Date.now(), { playing = false } = {}) {
      close();
      if (playing) tracker.start(positionS, wallMs);
    },
    // pause / ended / error: cierra en la última posición escuchada.
    stop(positionS, wallMs = Date.now()) {
      if (open && finite(positionS)) tracker.advance(positionS, wallMs);
      close();
    },
    isPlaying() {
      return open !== null;
    },
    pendingCount() {
      const ranges = open && open.end > open.start ? [...closed, [open.start, open.end]] : closed;
      return mergeRanges(ranges).length;
    },
    isFull() {
      return tracker.pendingCount() >= maxRanges;
    },
    // Devuelve el payload pendiente y lo saca del acumulador. Un tramo abierto
    // se parte: lo escuchado hasta ahora se manda y el resto sigue abierto.
    drain() {
      let openPart = null;
      if (open && open.end > open.start) {
        openPart = [open.start, open.end];
        playedS += open.end - open.start;
        open = { ...open, start: open.end };
      }
      const pending = mergeRanges(openPart ? [...closed, openPart] : closed)
        .map(([start, end]) => [toMs(start), toMs(end)])
        .filter(([start, end]) => end > start);
      const playedMs = toMs(playedS);
      if (!pending.length) {
        closed = [];
        playedS = 0;
        return null;
      }
      const ranges = pending.slice(0, maxRanges);
      // Lo que no entra queda para el próximo envío: nunca se pierde ni se
      // trunca en silencio. El tiempo reproducido viaja completo en el primero.
      closed = pending.slice(maxRanges).map(([start, end]) => [start / 1000, end / 1000]);
      playedS = 0;
      return {
        ranges,
        played_ms: playedMs,
        playback_rate: Math.round(rate * 100) / 100,
      };
    },
  };
  return tracker;
}

// Propiedades del evento. `durationS` es la duración del audio cargado (para
// el % de canción escuchada); `flushReason` dice qué disparó el envío.
export function playbackEventProperties(payload, { durationS, flushReason } = {}) {
  if (!payload) return null;
  const properties = { ...payload, flush_reason: flushReason || "interval" };
  if (finite(durationS) && Number(durationS) > 0) properties.audio_duration_ms = toMs(durationS);
  return properties;
}
