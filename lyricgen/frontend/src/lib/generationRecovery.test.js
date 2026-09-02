import { describe, expect, it } from "vitest";
import {
  canRebuildMissingGenerationJob,
  isMissingGenerationJob,
  rebuildGenerationRequestFromLocalAudio,
} from "./generationRecovery";

describe("generation recovery", () => {
  it("recognizes only the explicit missing-job response", () => {
    expect(isMissingGenerationJob({ status: 404 }, { code: "job_not_found" })).toBe(true);
    expect(isMissingGenerationJob({ status: 404 }, { detail: "Job not found." })).toBe(true);
    expect(isMissingGenerationJob({ status: 404 }, { detail: "Background not found." })).toBe(false);
    expect(isMissingGenerationJob({ status: 409 }, { code: "job_not_generatable" })).toBe(false);
  });

  it("rebuilds from a real local File without retaining stale editor selectors", () => {
    const audio = new Blob(["audio"], { type: "audio/mpeg" });
    const form = new FormData();
    form.append("job_id", "old-job");
    form.append("base_revision", "7");
    form.append("editor_revision", "7");
    form.append("editor_version_id", "version-7");
    form.append("segments_json", "[]");

    expect(canRebuildMissingGenerationJob({ _file: audio })).toBe(true);
    rebuildGenerationRequestFromLocalAudio(form, { _file: audio, filename: "mi-nina.mp3" });

    expect(form.get("job_id")).toBeNull();
    expect(form.get("base_revision")).toBeNull();
    expect(form.get("editor_revision")).toBeNull();
    expect(form.get("editor_version_id")).toBeNull();
    expect(form.get("segments_json")).toBe("[]");
    expect(form.get("file")).toBeInstanceOf(File);
    expect(canRebuildMissingGenerationJob({ _file: { name: "stub.mp3" } })).toBe(false);
  });
});
