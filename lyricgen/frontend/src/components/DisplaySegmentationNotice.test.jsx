import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DisplaySegmentationNotice from "./DisplaySegmentationNotice";

const report = { policy_version: "display-phrases-v1", mode: "shadow", proposed_count: 2,
  candidates: [{ index: 0, start: 10, end: 20 }], review: [{ index: 1, start: 25, end: 30, reason: "word_coverage_incomplete" }] };

describe("display segmentation guidance", () => {
  it("shows shadow without claiming application, with every bounded review point", () => {
    const seek = vi.fn();
    render(<DisplaySegmentationNotice report={report} onSeek={seek} />);
    expect(screen.getByText(/2 divisiones, todavía sin aplicar/)).toBeTruthy();
    expect(screen.getAllByRole("button")).toHaveLength(2);
    fireEvent.click(screen.getAllByRole("button")[0]);
    expect(seek).toHaveBeenCalledWith(24.5);
    expect(screen.getByText(/canción completa sigue siendo necesaria/)).toBeTruthy();
  });
  it("shows applied counts without claiming lyric or timing correctness", () => {
    render(<DisplaySegmentationNotice report={{ ...report, applied_count: 2, mode: "enforce" }} />);
    expect(screen.getByText(/dividieron 2 bloques/)).toBeTruthy();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });
  it("disables stale navigation and never truncates more than 24 points", () => {
    const candidates = Array.from({ length: 30 }, (_, index) => ({ index, start: index * 10, end: index * 10 + 5 }));
    render(<DisplaySegmentationNotice report={{ ...report, candidates }} stale />);
    expect(screen.getAllByRole("button")).toHaveLength(31);
    expect(screen.getAllByRole("button").every((button) => button.disabled)).toBe(true);
    expect(screen.getByText(/versión anterior/)).toBeTruthy();
  });
  it("does not present a failed analysis as absence of errors", () => {
    render(<DisplaySegmentationNotice report={{ policy_version: "display-phrases-v1", status: "unavailable" }} />);
    expect(screen.getByText(/división no pudo comprobarse/)).toBeTruthy();
  });
  it("ignores malformed clocks and off mode", () => {
    const { rerender } = render(<DisplaySegmentationNotice report={{ ...report, candidates: [{ start: "NaN" }, { start: -1, end: 3 }], review: [] }} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    rerender(<DisplaySegmentationNotice report={{ ...report, mode: "off" }} />);
    expect(screen.queryByTestId("display-segmentation-notice")).toBeNull();
  });
});
