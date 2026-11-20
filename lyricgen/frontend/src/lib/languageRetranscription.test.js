import { describe, expect, it, vi } from "vitest";
import { fetchLanguageRetranscription, startLanguageRetranscription } from "./languageRetranscription";

const json = (status, body) => ({ ok: status < 400, status, json: async () => body });

describe("languageRetranscription", () => {
  it("posts the language and the saved revision", async () => {
    const request = vi.fn().mockResolvedValue(json(202, { status: "queued", language: "es" }));
    expect(await startLanguageRetranscription(request, "job1", "es", 3))
      .toEqual({ ok: true, status: "queued", language: "es" });
    expect(request).toHaveBeenCalledWith("/jobs/job1/retranscribe-language", expect.objectContaining({
      method: "POST", body: JSON.stringify({ language: "es", base_revision: 3 }),
    }));
  });

  it("requires a saved revision and reports server codes", async () => {
    const request = vi.fn().mockResolvedValue(json(409, { code: "stale_revision", message: "cambió" }));
    expect((await startLanguageRetranscription(request, "job1", "es", undefined)).reason).toBe("save_required");
    expect(request).not.toHaveBeenCalled();
    expect(await startLanguageRetranscription(request, "job1", "es", 2))
      .toEqual({ ok: false, reason: "stale_revision", message: "cambió" });
    const offline = vi.fn().mockRejectedValue(new Error("offline"));
    expect((await startLanguageRetranscription(offline, "job1", "es", 2)).reason).toBe("network");
  });

  it("polls the status endpoint", async () => {
    const request = vi.fn().mockResolvedValue(json(200, { status: "running" }));
    expect(await fetchLanguageRetranscription(request, "job1")).toEqual({ ok: true, status: "running" });
    expect(request).toHaveBeenCalledWith("/jobs/job1/retranscribe-language");
  });
});
