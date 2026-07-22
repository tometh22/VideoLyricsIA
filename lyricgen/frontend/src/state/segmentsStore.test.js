/**
 * Unit tests del segmentsStore (PR E del plan del editor).
 *
 * Contrato que protegemos:
 *   - seed UNA sola vez por jobId (seedFn no re-corre si hay entrada viva)
 *   - setSegments (valor y updater) notifica suscriptores
 *   - la entrada SOBREVIVE al unmount y un remount se re-engancha
 *     (la familia de bugs "se borran los tiempos al navegar el wizard")
 *   - evict limpia; replace() swapea contenido PRESERVANDO _id de las
 *     filas sin cambios (invariante de PR D, reseedPreservingIds)
 *   - jobId falsy degrada a estado local (no persiste entre mounts)
 *   - canary [reseed-storm]: >2 seeds/replaces del mismo job en 1 s
 */
import { renderHook, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { segmentsStore, useJobSegments, useJobSegmentsValue } from "./segmentsStore";

const SEGS = [
  { start: 0, end: 2, text: "alpha" },
  { start: 2, end: 4, text: "beta" },
];

afterEach(() => {
  segmentsStore._clearAll();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("segmentsStore — useJobSegments", () => {
  it("seedea una sola vez por jobId", () => {
    const seedFn = vi.fn(() => SEGS.map((s, i) => ({ ...s, _id: i })));
    const { rerender } = renderHook(() => useJobSegments("job-a", seedFn));
    rerender();
    rerender();
    expect(seedFn).toHaveBeenCalledTimes(1);
    expect(segmentsStore.get("job-a")).toHaveLength(2);
  });

  it("setSegments acepta valor y updater fn, y el hook ve el cambio", () => {
    const { result } = renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    act(() => {
      result.current[1]((prev) =>
        prev.map((s) => (s._id === 0 ? { ...s, text: "alpha EDITED" } : s)),
      );
    });
    expect(result.current[0][0].text).toBe("alpha EDITED");
    act(() => {
      result.current[1]([{ start: 9, end: 10, text: "solo", _id: 7 }]);
    });
    expect(result.current[0]).toHaveLength(1);
    expect(segmentsStore.get("job-a")[0].text).toBe("solo");
  });

  it("la entrada sobrevive al unmount y el remount se re-engancha (NO reseedea del prop stale)", () => {
    const first = renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    act(() => {
      first.result.current[1]((prev) =>
        prev.map((s) => ({ ...s, locked: true, start: s.start + 1 })),
      );
    });
    first.unmount();

    // Remount "del wizard": seedFn trae el prop STALE original — no debe
    // correr porque la entrada sigue viva.
    const staleSeed = vi.fn(() => SEGS.map((s, i) => ({ ...s, _id: i })));
    const second = renderHook(() => useJobSegments("job-a", staleSeed));
    expect(staleSeed).not.toHaveBeenCalled();
    expect(second.result.current[0][0].locked).toBe(true);
    expect(second.result.current[0][0].start).toBe(1);
  });

  it("dos consumidores del mismo jobId ven los mismos datos en vivo", () => {
    const a = renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    const b = renderHook(() => useJobSegmentsValue("job-a"));
    expect(b.result.current).toHaveLength(2);
    act(() => {
      a.result.current[1]((prev) =>
        prev.map((s) => ({ ...s, text: `${s.text}!` })),
      );
    });
    expect(b.result.current[0].text).toBe("alpha!");
  });

  it("jobId falsy = estado local plano que NO persiste entre mounts", () => {
    const first = renderHook(() =>
      useJobSegments(null, () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    act(() => {
      first.result.current[1]((prev) =>
        prev.map((s) => ({ ...s, text: "local edit" })),
      );
    });
    expect(first.result.current[0][0].text).toBe("local edit");
    first.unmount();
    // Nada quedó en el store...
    expect(segmentsStore.get(null)).toBeUndefined();
    // ...y un mount nuevo arranca del seed (no leakea el edit anterior).
    const second = renderHook(() =>
      useJobSegments(null, () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    expect(second.result.current[0][0].text).toBe("alpha");
  });
});

describe("segmentsStore — API módulo", () => {
  it("evict limpia la entrada y el hook cae al snapshot vacío", () => {
    const { result } = renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    expect(result.current[0]).toHaveLength(2);
    act(() => {
      segmentsStore.evict("job-a");
    });
    expect(segmentsStore.get("job-a")).toBeUndefined();
    expect(result.current[0]).toHaveLength(0);
  });

  it("replace swapea contenido preservando _id de las filas sin cambios (invariante PR D)", () => {
    renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    act(() => {
      segmentsStore.replace("job-a", [
        { start: 0, end: 2, text: "alpha" }, // igual → conserva _id 0
        { start: 2, end: 4, text: "beta CAMBIADA" }, // nueva → id fresco
      ]);
    });
    const segs = segmentsStore.get("job-a");
    expect(segs[0]._id).toBe(0);
    expect(segs[0].text).toBe("alpha");
    expect(segs[1].text).toBe("beta CAMBIADA");
    expect(segs[1]._id).not.toBe(1); // id nuevo, no reciclado por índice
  });

  it("dos mounts con la MISMA key no-nula comparten estado a través del unmount (FIX 1)", () => {
    const first = renderHook(() =>
      useJobSegments("local:song.mp3:0", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    act(() => {
      first.result.current[1]((prev) =>
        prev.map((s) => (s._id === 0 ? { ...s, text: "editado" } : s)),
      );
    });
    first.unmount();
    // Segundo mount con la MISMA key sintética: se re-engancha a la entrada
    // viva (no re-seedea), aunque no haya job de backend detrás.
    const second = renderHook(() =>
      useJobSegments("local:song.mp3:0", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    expect(second.result.current[0][0].text).toBe("editado");
  });

  it("getOriginal preserva la baseline del seed a través de edits/replace/remount (F2)", () => {
    const first = renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i, start: s.start }))),
    );
    // Baseline capturada al seed.
    expect(segmentsStore.getOriginal("job-a")).toHaveLength(2);
    expect(segmentsStore.getOriginal("job-a")[0].start).toBe(0);

    // Un edit del timing NO debe tocar `original`.
    act(() => {
      first.result.current[1]((prev) =>
        prev.map((s) => (s._id === 0 ? { ...s, start: 9 } : s)),
      );
    });
    expect(segmentsStore.get("job-a")[0].start).toBe(9);
    expect(segmentsStore.getOriginal("job-a")[0].start).toBe(0);

    // Un replace() externo tampoco.
    act(() => {
      segmentsStore.replace("job-a", [{ start: 50, end: 60, text: "alpha" }]);
    });
    expect(segmentsStore.getOriginal("job-a")[0].start).toBe(0);

    // Y sobrevive al remount (misma entrada viva).
    first.unmount();
    renderHook(() =>
      useJobSegments("job-a", () => SEGS.map((s, i) => ({ ...s, _id: i }))),
    );
    expect(segmentsStore.getOriginal("job-a")[0].start).toBe(0);
  });

  it("replace notifica a los suscriptores", () => {
    const view = renderHook(() => useJobSegmentsValue("job-a"));
    expect(view.result.current).toBeNull();
    act(() => {
      segmentsStore.replace("job-a", SEGS);
    });
    expect(view.result.current).toHaveLength(2);
  });

  it("canary: >2 replaces del mismo job en 1 s emite [reseed-storm]", () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    segmentsStore.replace("job-a", SEGS);
    segmentsStore.replace("job-a", SEGS);
    expect(warn).not.toHaveBeenCalled();
    segmentsStore.replace("job-a", SEGS); // 3ra dentro de la ventana → warn
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toMatch(/^\[reseed-storm\]/);

    // Pasada la ventana de 1 s, el contador arranca de nuevo (sin warn).
    warn.mockClear();
    vi.advanceTimersByTime(1500);
    segmentsStore.replace("job-a", SEGS);
    segmentsStore.replace("job-a", SEGS);
    expect(warn).not.toHaveBeenCalled();
  });
});
