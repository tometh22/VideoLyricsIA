import { describe, it, expect } from "vitest";
import {
  TERMINAL_STATUSES,
  ACTIVE_STATUSES,
  isTerminalStatus,
  isActiveStatus,
  isSuccessStatus,
  isErrorStatus,
} from "./jobStatus";

describe("jobStatus canonical sets", () => {
  it("TERMINAL_STATUSES mirrors the backend SSE terminal set (incl. bg_preview_done)", () => {
    // Kept in lockstep with lyricgen/backend/main.py TERMINAL. Missing any of
    // these reintroduces the infinite-poll / frozen-hero bug (audit 2026-07-27).
    for (const s of [
      "done", "pending_review", "error", "rejected",
      "validation_failed", "transcription_failed",
      "bg_preview_done", "bg_preview_failed",
    ]) {
      expect(isTerminalStatus(s)).toBe(true);
    }
    expect(TERMINAL_STATUSES).toContain("bg_preview_done");
  });

  it("classifies success vs error terminal states", () => {
    expect(isSuccessStatus("done")).toBe(true);
    expect(isSuccessStatus("pending_review")).toBe(true);
    expect(isSuccessStatus("error")).toBe(false);

    expect(isErrorStatus("error")).toBe(true);
    expect(isErrorStatus("validation_failed")).toBe(true);
    expect(isErrorStatus("rejected")).toBe(true);
    expect(isErrorStatus("done")).toBe(false);
    // bg_preview_done is terminal but neither success nor error.
    expect(isErrorStatus("bg_preview_done")).toBe(false);
    expect(isSuccessStatus("bg_preview_done")).toBe(false);
    expect(isTerminalStatus("bg_preview_done")).toBe(true);
  });

  it("recognises in-flight statuses and rejects unknown ones", () => {
    for (const s of ACTIVE_STATUSES) expect(isActiveStatus(s)).toBe(true);
    expect(isActiveStatus("processing")).toBe(true);
    expect(isTerminalStatus("quantum_flux")).toBe(false);
    expect(isActiveStatus("quantum_flux")).toBe(false);
  });
});
