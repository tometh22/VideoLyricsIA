import { describe, expect, it } from "vitest";
import { campaignCounts, campaignNextStep, doneCount, formatDuration, resolveView, stagesFor, totalOf } from "./campaignPipeline";

const params = (query) => new URLSearchParams(query);

describe("campaign pipeline vocabulary", () => {
  it("art tracks skip the lyric stages", () => {
    expect(stagesFor("lyric_video").map((stage) => stage.key)).toEqual(["audio", "lyrics", "ready", "rendering", "qc", "approved", "delivered"]);
    expect(stagesFor("art_track").map((stage) => stage.key)).toEqual(["audio", "rendering", "qc", "approved", "delivered"]);
  });

  it("prefers backend pipeline counts and maps legacy phase counters conservatively", () => {
    expect(campaignCounts({ pipeline: { counts: { lyrics: 3, delivered: 2 } } })).toMatchObject({ lyrics: 3, delivered: 2, qc: 0 });
    const legacy = campaignCounts({ counters: { transcribing: 2, uploading: 1, lyrics_ready: 4, final_review: 1, done: 5, failed: 1, discarded: 2 } });
    expect(legacy).toMatchObject({ audio: 3, lyrics: 4, qc: 1, approved: 5, delivered: 0, attention: 1, discarded: 2 });
    expect(totalOf(legacy)).toBe(14);
    expect(totalOf(legacy, { includeDiscarded: true })).toBe(16);
    expect(doneCount({ approved: 2, delivered: 5 })).toBe(7);
  });

  it("orders the next step by human work first and never proposes lyrics for art tracks", () => {
    const counts = { audio: 1, lyrics: 2, ready: 3, rendering: 0, qc: 4, approved: 5, delivered: 0, attention: 1, discarded: 0 };
    expect(campaignNextStep(counts)).toEqual({ stage: "lyrics", label: "Revisar 2 letras" });
    expect(campaignNextStep({ ...counts, lyrics: 0 })).toEqual({ stage: "qc", label: "Revisar 4 videos" });
    expect(campaignNextStep({ ...counts, lyrics: 0, qc: 0 })).toEqual({ stage: "ready", label: "Generar 3 videos" });
    expect(campaignNextStep({ ...counts, lyrics: 0, qc: 0, ready: 0 })).toEqual({ stage: "approved", label: "Enviar 5 aprobadas" });
    expect(campaignNextStep({ ...counts, lyrics: 0, qc: 0, ready: 0, approved: 0 })).toEqual({ stage: "attention", label: "Resolver 1 problema" });
    expect(campaignNextStep({ audio: 2 })).toMatchObject({ passive: true });
    expect(campaignNextStep({ delivered: 3 })).toBeNull();
    expect(campaignNextStep({ lyrics: 5, qc: 1 }, "art_track")).toEqual({ stage: "qc", label: "Revisar 1 video" });
    expect(campaignNextStep({ approved: 4, attention: 1 }, "art_track", { portalSends: false })).toEqual({ stage: "attention", label: "Resolver 1 problema" });
  });

  it("keeps old links and editor return paths valid", () => {
    expect(resolveView(params(""))).toBe("all");
    expect(resolveView(params("view=qc"))).toBe("qc");
    expect(resolveView(params("view=review"))).toBe("lyrics");
    expect(resolveView(params("view=creative"))).toBe("ready");
    expect(resolveView(params("view=history&video_state=review"))).toBe("qc");
    expect(resolveView(params("view=deliveries"))).toBe("approved");
    expect(resolveView(params("view=contract"))).toBe("config");
    expect(resolveView(params("tab=drafts"))).toBe("lyrics");
    expect(resolveView(params("tab=approved&stage=final"))).toBe("all");
    expect(resolveView(params("tab=discarded"))).toBe("discarded");
    expect(resolveView(params("tab=pending"), "art_track")).toBe("qc");
    expect(resolveView(params("view=bogus"))).toBe("all");
  });

  it("formats durations without inventing values", () => {
    expect(formatDuration(149.6)).toBe("2:30");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0)).toBe("—");
  });
});
