import { describe, expect, it } from "vitest";

import {
  changeRequestAdminPath,
  parseChangeRequestEditContext,
} from "./changeRequestEditFlow.js";

describe("change-request editor deep-link", () => {
  it("accepts only a complete, bounded request/proposal pair", () => {
    expect(parseChangeRequestEditContext("?change_request_id=42&proposal_id=abc-123_def"))
      .toEqual({ changeRequestId: 42, proposalId: "abc-123_def" });
    expect(parseChangeRequestEditContext("?change_request_id=42")).toBeNull();
    expect(parseChangeRequestEditContext("?change_request_id=../../admin&proposal_id=x")).toBeNull();
    expect(parseChangeRequestEditContext("?change_request_id=42&proposal_id=https://evil.example"))
      .toBeNull();
  });

  it("builds a fixed internal return path with explicit render state", () => {
    const context = { changeRequestId: 42, proposalId: "abc" };
    expect(changeRequestAdminPath(context, "submitted"))
      .toBe("/admin?section=cambios&change_request_id=42&render_submitted=1");
    expect(changeRequestAdminPath(context, "completed"))
      .toBe("/admin?section=cambios&change_request_id=42&render_completed=1");
  });
});
