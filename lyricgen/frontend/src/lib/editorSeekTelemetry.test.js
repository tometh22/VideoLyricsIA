import { expect, it } from "vitest";
import { editorSeekProperties } from "./editorSeekTelemetry";

it("distinguishes a selected line from the destination and captures visible review markers", () => {
  const result = editorSeekProperties({ from: 25, to: 3, revision: 7,
    segment: { _id: 0, segment_id: "stable-0", start: 12, review: true, text: "never emit this" },
    lineIndex: 0, qualityMarked: true, unsavedChanges: true });
  expect(result).toMatchObject({ line_context: "selected", review_marker: "both",
    line_id: "0", segment_id: "stable-0", line_index: 0, unsaved_changes: true,
    from_position_ms: 25000, position_ms: 3000, line_start_ms: 12000 });
  expect(JSON.stringify(result)).not.toContain("never emit this");
});

it("does not invent a line or revision for a free seek with no selection", () => {
  const result = editorSeekProperties({ from: 8, to: 1 });
  expect(result).toMatchObject({ line_context: "none", review_marker: "unknown" });
  expect(result).not.toHaveProperty("line_id");
  expect(result).not.toHaveProperty("revision");
});

it("records quality-window-only review independently of the segment review flag", () => {
  expect(editorSeekProperties({ from: 2, to: 4, segment: { _id: 1, start: 6 },
    targeted: true, qualityMarked: true })).toMatchObject({
    line_context: "target", review_marker: "quality_window",
  });
});
