import { describe, expect, it } from "vitest";
import { reviewJobIdFromLocation, reviewJobPath } from "./reviewJobRoute";

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
