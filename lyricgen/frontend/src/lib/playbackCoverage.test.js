import { describe, expect, it } from "vitest";

import {
  MAX_PLAYED_RANGES,
  createPlaybackCoverage,
  mergeRanges,
  playbackEventProperties,
} from "./playbackCoverage";

// Simula `timeupdate` cada 250 ms entre dos posiciones a velocidad 1.
const playThrough = (tracker, from, to, wallStart = 0) => {
  let wall = wallStart;
  for (let t = from + 0.25; t <= to + 1e-9; t += 0.25) {
    wall += 250;
    tracker.advance(t, wall);
  }
  return wall;
};

describe("mergeRanges", () => {
  it("fusiona tramos que se pisan o se tocan y ordena", () => {
    expect(mergeRanges([[10, 12], [0, 2], [1.5, 4], [4.1, 5]])).toEqual([[0, 5], [10, 12]]);
  });

  it("descarta tramos vacíos, invertidos o no numéricos", () => {
    expect(mergeRanges([[3, 3], [5, 4], [NaN, 2], null, [1, 2]])).toEqual([[1, 2]]);
  });
});

describe("createPlaybackCoverage", () => {
  it("acumula lo que sonó y no cuenta el tiempo en pausa", () => {
    const tracker = createPlaybackCoverage();
    tracker.start(0, 0);
    const wall = playThrough(tracker, 0, 10);
    tracker.stop(10, wall);
    // 20 s de pausa: no avanza nada.
    tracker.start(10, wall + 20_000);
    const wall2 = playThrough(tracker, 10, 15, wall + 20_000);
    tracker.stop(15, wall2);
    expect(tracker.drain()).toEqual({ ranges: [[0, 15_000]], played_ms: 15_000, playback_rate: 1 });
    expect(tracker.drain()).toBeNull();
  });

  it("un seek mientras suena cierra el tramo y empieza otro", () => {
    const tracker = createPlaybackCoverage();
    tracker.start(0, 0);
    const wall = playThrough(tracker, 0, 5);
    tracker.seek(60, wall, { playing: true });
    const wall2 = playThrough(tracker, 60, 64, wall);
    tracker.stop(64, wall2);
    expect(tracker.drain().ranges).toEqual([[0, 5_000], [60_000, 64_000]]);
  });

  it("un seek en pausa no inventa escucha", () => {
    const tracker = createPlaybackCoverage();
    tracker.seek(30, 0, { playing: false });
    tracker.advance(31, 1000);
    expect(tracker.drain()).toBeNull();
  });

  it("detecta un salto sin `seeking` (bucle de revisión guiada)", () => {
    const tracker = createPlaybackCoverage();
    tracker.start(20, 0);
    let wall = playThrough(tracker, 20, 22);
    // El bucle vuelve a 20 sin pausa: es una nueva escucha del mismo tramo.
    tracker.advance(20, wall + 250);
    wall = playThrough(tracker, 20, 22, wall + 250);
    tracker.stop(22, wall);
    const payload = tracker.drain();
    expect(payload.ranges).toEqual([[20_000, 22_000]]);
    // El tiempo reproducido sí cuenta la repetición.
    expect(payload.played_ms).toBe(4_000);
  });

  it("un avance mucho mayor que el reloj de pared se trata como salto", () => {
    const tracker = createPlaybackCoverage();
    tracker.start(0, 0);
    tracker.advance(0.25, 250);
    tracker.advance(90, 500);
    tracker.advance(90.25, 750);
    tracker.stop(90.25, 750);
    expect(tracker.drain().ranges).toEqual([[0, 250], [90_000, 90_250]]);
  });

  it("vaciar con un tramo abierto manda lo escuchado y sigue midiendo", () => {
    const tracker = createPlaybackCoverage();
    tracker.start(0, 0);
    const wall = playThrough(tracker, 0, 30);
    expect(tracker.drain()).toEqual({ ranges: [[0, 30_000]], played_ms: 30_000, playback_rate: 1 });
    const wall2 = playThrough(tracker, 30, 40, wall);
    tracker.stop(40, wall2);
    expect(tracker.drain().ranges).toEqual([[30_000, 40_000]]);
  });

  it("respeta la velocidad de reproducción al decidir si hubo salto", () => {
    const tracker = createPlaybackCoverage();
    tracker.start(0, 0, 2);
    tracker.advance(2, 1000, 2);
    tracker.advance(4, 2000, 2);
    tracker.stop(4, 2000);
    expect(tracker.drain()).toEqual({ ranges: [[0, 4_000]], played_ms: 4_000, playback_rate: 2 });
  });

  it("acota la lista y guarda el excedente para el próximo envío", () => {
    const tracker = createPlaybackCoverage({ maxRanges: 3 });
    for (let i = 0; i < 5; i += 1) {
      tracker.start(i * 10, i * 1000);
      tracker.advance(i * 10 + 0.5, i * 1000 + 500);
      tracker.stop(i * 10 + 0.5, i * 1000 + 500);
    }
    expect(tracker.isFull()).toBe(true);
    const first = tracker.drain();
    expect(first.ranges).toHaveLength(3);
    expect(first.played_ms).toBe(2_500);
    const second = tracker.drain();
    expect(second.ranges).toEqual([[30_000, 30_500], [40_000, 40_500]]);
    expect(second.played_ms).toBe(0);
    expect(tracker.drain()).toBeNull();
  });

  it("el máximo por defecto mantiene el payload por debajo del límite del backend", () => {
    const tracker = createPlaybackCoverage();
    for (let i = 0; i < MAX_PLAYED_RANGES + 5; i += 1) {
      const start = 86_000 + i * 2;
      tracker.start(start, i * 10_000);
      tracker.advance(start + 0.5, i * 10_000 + 500);
      tracker.stop(start + 0.5, i * 10_000 + 500);
    }
    const properties = playbackEventProperties(tracker.drain(), { durationS: 86_399, flushReason: "full" });
    expect(properties.ranges).toHaveLength(MAX_PLAYED_RANGES);
    const json = JSON.stringify({ ...properties, session_id: "019abcde-1234-4567-8901-abcdefabcdef" });
    expect(json.length).toBeLessThan(2000);
  });
});

describe("playbackEventProperties", () => {
  it("sólo números y enums: nunca texto de la letra", () => {
    const properties = playbackEventProperties(
      { ranges: [[0, 1000]], played_ms: 1000, playback_rate: 1 },
      { durationS: 180.4, flushReason: "pause" },
    );
    expect(properties).toEqual({
      ranges: [[0, 1000]], played_ms: 1000, playback_rate: 1,
      flush_reason: "pause", audio_duration_ms: 180_400,
    });
  });

  it("omite la duración si el audio todavía no la conoce", () => {
    const properties = playbackEventProperties(
      { ranges: [[0, 1000]], played_ms: 1000, playback_rate: 1 },
      { durationS: NaN, flushReason: "approve" },
    );
    expect(properties.audio_duration_ms).toBeUndefined();
    expect(playbackEventProperties(null)).toBeNull();
  });
});
