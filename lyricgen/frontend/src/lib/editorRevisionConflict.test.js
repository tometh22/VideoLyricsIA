import { describe, expect, it } from "vitest";
import {
  editorRevisionConflictDetail,
  isEditorRevisionConflict,
} from "./editorRevisionConflict";

const response = { status: 409 };

describe("editor revision conflict wire contract", () => {
  it.each([
    [{ detail: { detail: "editor_revision_conflict", server_revision: 3 } }],
    [{ code: "stale_revision", detail: "editor_revision_conflict", current_revision: 3 }],
    [{ detail: "editor_revision_conflict", server_revision: 3 }],
  ])("recognizes equivalent 409 payloads: %j", (payload) => {
    expect(isEditorRevisionConflict(response, payload)).toBe(true);
    expect(editorRevisionConflictDetail(payload)).toBeTruthy();
  });

  it("does not classify unrelated 409s as editor conflicts", () => {
    expect(isEditorRevisionConflict(response, {
      detail: { code: "youtube_already_published" },
    })).toBe(false);
    expect(isEditorRevisionConflict({ status: 200 }, {
      detail: "editor_revision_conflict",
    })).toBe(false);
    expect(editorRevisionConflictDetail({
      detail: { code: "youtube_already_published" },
    })).toBeNull();
  });
});
