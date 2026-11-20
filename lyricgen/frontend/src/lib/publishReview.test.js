import { describe, expect, it } from "vitest";
import { clock, headline, publishReviewFrom, tokensOf, totalsOf, totalsText } from "./publishReview";

const summary = (patch = {}) => ({
  job_id: "job1", listening_status: "measured", lines_total: 30, lines_unheard: 4,
  warnings_count: 2, needs_attention: true, token: "t".repeat(64), ...patch,
});

describe("publish review helpers", () => {
  it("reads a 409 detail, wrapped or not, and ignores other codes", () => {
    const detail = { code: "publish_review_required", summaries: [summary()], totals: { songs: 1 } };
    expect(publishReviewFrom(detail)).toEqual({ summaries: detail.summaries, totals: { songs: 1 } });
    expect(publishReviewFrom({ detail })).toEqual({ summaries: detail.summaries, totals: { songs: 1 } });
    expect(publishReviewFrom({ code: "prores_required" })).toBeNull();
    expect(publishReviewFrom("boom")).toBeNull();
    expect(publishReviewFrom({ code: "publish_review_required", summaries: [summary()] }).totals.lines_unheard).toBe(4);
  });

  it("says what nobody heard and nobody looked at", () => {
    expect(headline(summary())).toBe("4 de 30 líneas sin escuchar · 2 avisos sin mirar");
    expect(headline(summary({ lines_unheard: 0, warnings_count: 1 }))).toBe("1 aviso sin mirar");
    expect(totalsText(totalsOf([summary(), summary({ job_id: "job2", lines_unheard: 1, warnings_count: 0 })])))
      .toBe("5 líneas sin escuchar y 2 avisos sin mirar en 2 canciones");
  });

  it("formats times and collects tokens per job", () => {
    expect(clock(61.9)).toBe("1:01");
    expect(clock(-3)).toBe("0:00");
    expect(tokensOf([summary(), summary({ job_id: "job2", token: "u".repeat(64) }), { job_id: "job3" }]))
      .toEqual({ job1: "t".repeat(64), job2: "u".repeat(64) });
  });
});
