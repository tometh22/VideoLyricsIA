import { describe, expect, it } from "vitest";
import { reviewAlreadyOpen, reviewJobIdFromLocation, reviewJobPath } from "./reviewJobRoute";

describe("durable review job routes", () => {
  it("builds a personalized, URL-safe review path", () => {
    expect(reviewJobPath("abc 123")).toBe("/review/abc%20123");
  });

  it("loads the job id from a personalized review URL", () => {
    expect(reviewJobIdFromLocation("/review/job%20123", "")).toBe("job 123");
  });

  it("keeps the legacy /new?resume= link working", () => {
    expect(reviewJobIdFromLocation("/new", "?resume=abc123")).toBe("abc123");
  });

  it("does not treat the legacy bare review route as a job deep-link", () => {
    expect(reviewJobIdFromLocation("/review", "")).toBeNull();
  });
});

describe("reviewAlreadyOpen (wizard's own review gets its durable URL)", () => {
  const liveFile = new Blob(["audio"], { type: "audio/wav" });

  it("recognises the review the wizard just transcribed from the operator's audio", () => {
    const review = { file: liveFile, transcribeJobId: "job123", lyricLook: "cine" };
    expect(reviewAlreadyOpen(review, "job123")).toBe(true);
  });

  it("still resumes from the server for deep links, other songs and restored snapshots", () => {
    expect(reviewAlreadyOpen(null, "job123")).toBe(false);
    // history / campaign queue / reload: no File in memory
    expect(reviewAlreadyOpen({ file: null, transcribeJobId: "job123" }, "job123")).toBe(false);
    // another song
    expect(reviewAlreadyOpen({ file: liveFile, transcribeJobId: "other" }, "job123")).toBe(false);
    // rehydrated sessionStorage snapshot (stub file)
    expect(reviewAlreadyOpen({ file: { name: "a.wav", _restoredStub: true }, transcribeJobId: "job123" }, "job123")).toBe(false);
    // /variant wizard
    expect(reviewAlreadyOpen({ file: liveFile, transcribeJobId: "job123", variantMode: true }, "job123")).toBe(false);
  });
});
