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
  afterEach(() => vi.restoreAllMocks());

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
});
