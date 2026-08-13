import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createSaveQueue } from "./saveQueue";

// Deferred: promesa cuya resolución controlamos desde el test, para simular
// un POST en vuelo y probar la serialización/coalescing.
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const SEG = (t) => [{ start: 0, end: 1, text: t }];

describe("saveQueue", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("schedule dispara UN save tras el debounce, con los segmentos más frescos", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const q = createSaveQueue(persist, { debounceMs: 3000 });
    let segs = SEG("v1");
    q.schedule("job", () => segs);
    segs = SEG("v2"); // el provider se evalúa AL disparar → debe mandar v2
    expect(persist).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(3000);
    expect(persist).toHaveBeenCalledTimes(1);
    expect(persist.mock.calls[0][1]).toEqual(SEG("v2"));
  });

  it("re-schedule resetea el debounce (no dispara dos veces)", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const q = createSaveQueue(persist, { debounceMs: 3000 });
    q.schedule("job", () => SEG("a"));
    await vi.advanceTimersByTimeAsync(2000);
    q.schedule("job", () => SEG("b")); // resetea
    await vi.advanceTimersByTimeAsync(2000); // total 4s pero solo 2s desde el reset
    expect(persist).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(persist).toHaveBeenCalledTimes(1);
    expect(persist.mock.calls[0][1]).toEqual(SEG("b"));
  });

  it("flush dispara ya, sin esperar el debounce", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const q = createSaveQueue(persist, { debounceMs: 3000 });
    q.flush("job", { provider: () => SEG("now") });
    expect(persist).toHaveBeenCalledTimes(1);
    expect(persist.mock.calls[0][1]).toEqual(SEG("now"));
  });

  it("serializa: con un POST en vuelo, los pedidos se COALESCEN en un solo trailing con lo más fresco", async () => {
    const d1 = deferred();
    const persist = vi.fn()
      .mockReturnValueOnce(d1.promise)              // primer POST (en vuelo)
      .mockResolvedValue({ ok: true });             // trailing
    const q = createSaveQueue(persist, { debounceMs: 3000 });

    let segs = SEG("v1");
    q.flush("job", { provider: () => segs });        // arranca POST #1 (v1, en vuelo)
    expect(persist).toHaveBeenCalledTimes(1);

    // Mientras #1 está en vuelo, llegan 3 pedidos más → deben coalescer en 1
    segs = SEG("v2"); q.flush("job", { provider: () => segs });
    segs = SEG("v3"); q.flush("job", { provider: () => segs });
    segs = SEG("v4"); q.flush("job", { provider: () => segs });
    expect(persist).toHaveBeenCalledTimes(1); // ninguno arrancó todavía

    d1.resolve({ ok: true });                        // termina #1
    await vi.advanceTimersByTimeAsync(0);
    // se dispara UN solo trailing, con los segmentos MÁS frescos (v4)
    expect(persist).toHaveBeenCalledTimes(2);
    expect(persist.mock.calls[1][1]).toEqual(SEG("v4"));
  });

  it("status: idle → saving → saved, y saved se desvanece a idle", async () => {
    const d = deferred();
    const persist = vi.fn().mockReturnValue(d.promise);
    const q = createSaveQueue(persist, { debounceMs: 3000, savedFadeMs: 2000 });
    const seen = [];
    q.subscribe("job", (s) => seen.push(s.status));

    expect(q.getStatus("job").status).toBe("idle");
    q.flush("job", { provider: () => SEG("x") });
    expect(q.getStatus("job").status).toBe("saving");
    d.resolve({ ok: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(q.getStatus("job").status).toBe("saved");
    await vi.advanceTimersByTimeAsync(2000);
    expect(q.getStatus("job").status).toBe("idle");
    expect(seen).toEqual(["saving", "saved", "idle"]);
  });

  it("error: setea status=error con la categoría del clasificador", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: false, reason: "http-401", status: 401 });
    const categorize = (r) => (r.status === 401 ? "session" : "server");
    const q = createSaveQueue(persist, { categorize });
    q.flush("job", { provider: () => SEG("x") });
    await vi.advanceTimersByTimeAsync(0);
    expect(q.getStatus("job")).toEqual({ status: "error", reason: "session" });
  });

  it("rebasea automáticamente un 409 legacy y reintenta con la revisión actual", async () => {
    const persist = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "stale-revision", status: 409, currentRevision: 3 })
      .mockResolvedValueOnce({ ok: true, revision: 4 });
    const q = createSaveQueue(persist);

    q.flush("job", { provider: () => SEG("local") });
    await vi.advanceTimersByTimeAsync(0);

    expect(persist).toHaveBeenCalledTimes(2);
    expect(persist.mock.calls[0][2].baseRevision).toBe(0);
    expect(persist.mock.calls[1][2].baseRevision).toBe(3);
    expect(q.getStatus("job")).toEqual({ status: "saved", reason: null });
  });

  it("reinicia el presupuesto de rebase para un nuevo edit después de agotar los reintentos", async () => {
    const persist = vi
      .fn()
      .mockResolvedValue({
        ok: false,
        reason: "stale-revision",
        status: 409,
        currentRevision: 7,
      });
    const q = createSaveQueue(persist);

    q.flush("job", { provider: () => SEG("first") });
    await vi.advanceTimersByTimeAsync(0);
    expect(persist).toHaveBeenCalledTimes(4); // initial + 3 automatic rebases
    expect(q.getStatus("job").status).toBe("error");

    persist.mockResolvedValueOnce({ ok: true, revision: 8 });
    q.flush("job", { provider: () => SEG("second") });
    await vi.advanceTimersByTimeAsync(0);
    expect(persist).toHaveBeenCalledTimes(5);
    expect(persist.mock.calls.at(-1)[1]).toEqual(SEG("second"));
    expect(persist.mock.calls.at(-1)[2].baseRevision).toBe(7);
    expect(q.getStatus("job")).toEqual({ status: "saved", reason: null });
  });

  it("un fetch que tira (reject) cae a status=error reason=network", async () => {
    const persist = vi.fn().mockRejectedValue(new Error("boom"));
    const categorize = (r) => r.reason || "server";
    const q = createSaveQueue(persist, { categorize });
    q.flush("job", { provider: () => SEG("x") });
    await vi.advanceTimersByTimeAsync(0);
    expect(q.getStatus("job")).toEqual({ status: "error", reason: "network" });
  });

  it("el resultado del trailing coalescido gana (serialización, no contador)", async () => {
    const d1 = deferred();
    const persist = vi.fn()
      .mockReturnValueOnce(d1.promise)          // POST #1 (resuelve OK tarde)
      .mockResolvedValue({ ok: false, reason: "http-500", status: 500 }); // trailing error
    const categorize = (r) => (r.status === 500 ? "server" : "network");
    const q = createSaveQueue(persist, { categorize });

    q.flush("job", { provider: () => SEG("v1") }); // #1 en vuelo
    q.flush("job", { provider: () => SEG("v2") }); // coalescido (pending)
    // #1 resuelve OK → settle (saved) → dispara el trailing (error). Como el
    // trailing arranca DESPUÉS del settle de #1 (serialización estricta), su
    // resultado es el último y gana. Sin necesidad de contador de secuencia.
    d1.resolve({ ok: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(q.getStatus("job").status).toBe("error");
    expect(q.getStatus("job").reason).toBe("server");
  });

  it("nunca hay DOS POST reales en vuelo a la vez (single-flight estricto)", async () => {
    const d1 = deferred();
    let inflight = 0, maxInflight = 0;
    const persist = vi.fn().mockImplementation(() => {
      inflight++; maxInflight = Math.max(maxInflight, inflight);
      const p = persist.mock.calls.length === 1 ? d1.promise : Promise.resolve({ ok: true });
      return p.then((r) => { inflight--; return r; });
    });
    const q = createSaveQueue(persist);
    q.flush("job", { provider: () => SEG("a") });   // #1 en vuelo
    q.flush("job", { provider: () => SEG("b") });   // coalesce
    q.flush("job", { provider: () => SEG("c") });   // coalesce
    d1.resolve({ ok: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(maxInflight).toBe(1); // jamás 2 concurrentes
  });

  it("evict + recrear el MISMO jobId con un POST viejo en vuelo NO corrompe la instancia nueva", async () => {
    const d1 = deferred(); // POST viejo
    const d2 = deferred(); // POST de la instancia nueva
    const persist = vi.fn()
      .mockReturnValueOnce(d1.promise)
      .mockReturnValueOnce(d2.promise);
    const q = createSaveQueue(persist);

    q.flush("job", { provider: () => SEG("old") });  // POST viejo en vuelo
    expect(q.getStatus("job").status).toBe("saving");
    q.evict("job");                                   // se descarta la instancia
    // nueva instancia del MISMO id, nuevo POST en vuelo
    q.flush("job", { provider: () => SEG("new") });
    expect(q.getStatus("job").status).toBe("saving");
    const jNew = q._peek("job");

    // el POST VIEJO resuelve ahora (con error): su settle debe caer en la
    // instancia vieja (ya evictada) → dropear en silencio, sin tocar la nueva.
    d1.resolve({ ok: false, reason: "http-500", status: 500 });
    await vi.advanceTimersByTimeAsync(0);
    // la instancia nueva sigue EN VUELO (inflight intacto), status sin pisar
    expect(q._peek("job")).toBe(jNew);
    expect(jNew.inflight).toBe(true);
    expect(q.getStatus("job").status).toBe("saving"); // NO "error" del viejo

    // el POST nuevo resuelve OK → refleja SU propio resultado
    d2.resolve({ ok: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(q.getStatus("job").status).toBe("saved");
  });

  it("keepalive: dispara aunque haya uno en vuelo y NO toca el status", async () => {
    const d1 = deferred();
    const persist = vi.fn()
      .mockReturnValueOnce(d1.promise)   // POST normal en vuelo
      .mockResolvedValue({ ok: true });  // keepalive
    const q = createSaveQueue(persist);
    q.flush("job", { provider: () => SEG("v1") });      // normal, en vuelo
    expect(q.getStatus("job").status).toBe("saving");
    const unload = await q.flush("job", { keepalive: true, provider: () => SEG("v2") });
    // CAS no permite dos snapshots con la misma base en paralelo: el draft
    // queda local y el trailing se drena cuando A termina.
    expect(unload).toMatchObject({ ok: false, reason: "inflight" });
    expect(persist).toHaveBeenCalledTimes(1);
    expect(q.getStatus("job").status).toBe("saving");
    d1.resolve({ ok: true, revision: 1 });
    await vi.advanceTimersByTimeAsync(0);
  });

  it("no emite saved para A si B quedó debounced durante el POST", async () => {
    const d1 = deferred();
    const d2 = deferred();
    const persist = vi.fn().mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);
    const q = createSaveQueue(persist, { debounceMs: 3000 });
    const seen = [];
    q.subscribe("job", (state) => seen.push(state.status));
    let value = SEG("A");
    q.flush("job", { provider: () => value });
    value = SEG("B");
    q.schedule("job", () => value);
    d1.resolve({ ok: true, revision: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(persist).toHaveBeenCalledTimes(2);
    expect(persist.mock.calls[1][1]).toEqual(SEG("B"));
    expect(seen).not.toContain("saved");
    d2.resolve({ ok: true, revision: 2 });
    await vi.advanceTimersByTimeAsync(0);
    expect(seen.at(-1)).toBe("saved");
  });

  it("no dispara si no hay segmentos (o lista vacía)", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const q = createSaveQueue(persist);
    q.flush("job", { provider: () => [] });
    q.flush("job", { provider: () => null });
    q.schedule("job", () => []);
    await vi.advanceTimersByTimeAsync(3000);
    expect(persist).not.toHaveBeenCalled();
  });

  it("evict cancela el debounce pendiente y limpia el estado", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const q = createSaveQueue(persist, { debounceMs: 3000 });
    q.schedule("job", () => SEG("x"));
    q.evict("job");
    await vi.advanceTimersByTimeAsync(3000);
    expect(persist).not.toHaveBeenCalled();
    expect(q.getStatus("job").status).toBe("idle");
  });

  it("dos jobs distintos no interfieren entre sí", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const q = createSaveQueue(persist, { debounceMs: 3000 });
    q.schedule("A", () => SEG("a"));
    q.schedule("B", () => SEG("b"));
    await vi.advanceTimersByTimeAsync(3000);
    expect(persist).toHaveBeenCalledTimes(2);
    const jobsSaved = persist.mock.calls.map((c) => c[0]).sort();
    expect(jobsSaved).toEqual(["A", "B"]);
  });

  it("flush retorna una Promise que espera también el trailing coalescido", async () => {
    const d1 = deferred();
    const d2 = deferred();
    const persist = vi.fn()
      .mockReturnValueOnce(d1.promise)
      .mockReturnValueOnce(d2.promise);
    const q = createSaveQueue(persist);
    let segs = SEG("v1");
    const first = q.flush("job", { provider: () => segs });
    segs = SEG("v2");
    const second = q.flush("job", { provider: () => segs });
    let drained = false;
    second.then(() => { drained = true; });
    d1.resolve({ ok: true, revision: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(drained).toBe(false);
    expect(persist.mock.calls[1][2].baseRevision).toBe(1);
    d2.resolve({ ok: true, revision: 2 });
    await vi.advanceTimersByTimeAsync(0);
    await expect(first).resolves.toMatchObject({ ok: true, revision: 2 });
    await expect(second).resolves.toMatchObject({ ok: true, revision: 2 });
  });

  it("flushAll drena todos los jobs y retire espera antes de descartar", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true, revision: 1 });
    const q = createSaveQueue(persist);
    q.schedule("A", () => SEG("a"));
    q.schedule("B", () => SEG("b"));
    await q.flushAll();
    expect(persist).toHaveBeenCalledTimes(2);
    await expect(q.retire("A", { provider: () => SEG("final") })).resolves.toMatchObject({ ok: true });
    expect(q._peek("A")).toBeUndefined();
    expect(q._peek("B")).toBeDefined();
  });
});
