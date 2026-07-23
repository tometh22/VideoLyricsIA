import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchSse, SseUnauthorizedError } from "./fetchSse";

function streamResponse(chunks, init = {}) {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
  return new Response(body, { status: 200, ...init });
}

describe("fetchSse", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("uses Bearer auth and incrementally parses message and named events", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(streamResponse([
      "data: {\"status\":\"process",
      "ing\"}\n\nevent: unauthorized\ndata: {\"reason\":\"revoked\"}\n\n",
    ]));
    const messages = [];
    const events = [];

    await fetchSse("/events/job-1", {
      token: "access-secret",
      onMessage: (data) => messages.push(data),
      onEvent: (name, data) => events.push([name, data]),
      maxRetries: 0,
    });

    expect(fetchMock).toHaveBeenCalledWith("/events/job-1", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer access-secret" }),
    }));
    expect(fetchMock.mock.calls[0][0]).not.toContain("access-secret");
    expect(messages).toEqual([{ status: "processing" }]);
    expect(events).toEqual([["unauthorized", { reason: "revoked" }]]);
  });

  it("surfaces an authentication failure without retrying", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("unauthorized", { status: 401 }),
    );
    await expect(fetchSse("/events/job-1", {
      token: "expired",
      maxRetries: 2,
    })).rejects.toBeInstanceOf(SseUnauthorizedError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("re-arms the watchdog after data and aborts a stream that later freezes", async () => {
    vi.useFakeTimers();
    const encoder = new TextEncoder();
    let reads = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (_url, { signal }) => ({
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: () => {
              reads += 1;
              if (reads === 1) {
                return Promise.resolve({
                  value: encoder.encode('data: {"status":"processing"}\n\n'),
                  done: false,
                });
              }
              return new Promise((_resolve, reject) => {
                signal.addEventListener("abort", () => {
                  reject(new DOMException("Aborted", "AbortError"));
                }, { once: true });
              });
            },
          }),
        },
      }),
    );
    const messages = [];
    const pending = fetchSse("/events/job-1", {
      token: "access-secret",
      onMessage: (data) => messages.push(data),
      watchdogMs: 100,
      maxRetries: 0,
    });
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });

    await vi.advanceTimersByTimeAsync(1);
    expect(messages).toEqual([{ status: "processing" }]);
    await vi.advanceTimersByTimeAsync(101);
    await rejected;
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
