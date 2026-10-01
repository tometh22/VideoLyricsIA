import { beforeEach, describe, expect, it } from "vitest";
import { editorSessionHeaders, getEditorSessionId } from "./editorSession";

describe("editor tab session", () => {
  beforeEach(() => sessionStorage.clear());

  it("is stable within one tab and sent on lock calls", () => {
    const first = getEditorSessionId();
    expect(first.length).toBeGreaterThan(7);
    expect(getEditorSessionId()).toBe(first);
    expect(editorSessionHeaders({ Existing: "yes" })).toEqual({
      Existing: "yes", "X-Editor-Session": first,
    });
  });
});
