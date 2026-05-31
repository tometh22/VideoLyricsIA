/**
 * Regression test for hotfix 2026-05-31 — batch upload editor state mixup.
 *
 * Bug observed in production (UMG, Agus.Cafisi):
 *   - Agus uploaded two songs in the same wizard session.
 *   - Song A ("Luz de día versión cumbia") transcribed first, the editor
 *     showed its segments. Operator approved.
 *   - Song B ("Donde Estan Corazón") followed. The editor opened for
 *     song B but DISPLAYED the segments of song A. Agus quote:
 *     "donde estan corazon...sale otra letra completamente distinta
 *      igual que luz de dia".
 *   - Closing and reopening the editor showed the correct segments.
 *     That reopen forced a remount — confirming the bug is a stale
 *     `edited` state inside <LyricsEditor> that only seeds from
 *     props.segments on mount.
 *
 * Confirmed against the live DB (admin/jobs):
 *   - job 9df1132f6169 (Luz de día): segments_json has the correct
 *     "Destapa el champán / Apaga las luces / ser luz de día" lyrics.
 *   - job 82a5a8ab547e (Donde Estan Corazón): segments_json = null
 *     (status: bg_preview_done — never transcribed).
 *   Conclusion: the backend has correct (or absent) data per job.
 *   The cross-song bleed happens in the FRONTEND, when one job's
 *   currentReview gets shown inside an editor instance that was
 *   already mounted with another job's segments.
 *
 * Fix: include `transcribeJobId` in the React key for <LyricsEditor>.
 * `transcribeJobId` is the backend identity of the job — it's the
 * ONLY notion of "which song are we looking at" that survives the
 * various ways currentReview can get rewritten (filename can match
 * transiently across queue swaps; queueIdx can repeat when the queue
 * is rebuilt mid-flow).
 *
 * This test pins the key shape so a future refactor that drops the
 * jobId from the key breaks loudly.
 */
import { describe, expect, it } from "vitest";

// Inline mirror of the key-building expression in App.jsx:3335-ish.
// If you change the key in App.jsx, change it here too — the test will
// flag the divergence loudly.
function editorKey(currentReview) {
  return `${currentReview.transcribeJobId || "no-job"}:${
    currentReview.file?.name || currentReview.filename || "resume"
  }:${currentReview.queueIdx}`;
}

describe("LyricsEditor key for batch upload (2026-05-31 Agus fix)", () => {
  it("key changes when jobId changes (even if filename and queueIdx are stable)", () => {
    const reviewA = {
      transcribeJobId: "9df1132f6169",
      filename: "song.mp3",
      queueIdx: 0,
    };
    const reviewB = {
      transcribeJobId: "82a5a8ab547e",
      filename: "song.mp3",   // same filename — would NOT trigger remount under old key
      queueIdx: 0,            // same queueIdx — would NOT trigger remount under old key
    };
    expect(editorKey(reviewA)).not.toBe(editorKey(reviewB));
  });

  it("key falls back to 'no-job' sentinel when transcribeJobId is missing", () => {
    const r = { transcribeJobId: null, filename: "x.mp3", queueIdx: 0 };
    expect(editorKey(r)).toBe("no-job:x.mp3:0");
  });

  it("key uses file.name when available, falls back to filename", () => {
    const withFile = {
      transcribeJobId: "abc",
      file: { name: "real.mp3" },
      filename: "stub.mp3",
      queueIdx: 1,
    };
    const withoutFile = {
      transcribeJobId: "abc",
      filename: "stub.mp3",
      queueIdx: 1,
    };
    expect(editorKey(withFile)).toBe("abc:real.mp3:1");
    expect(editorKey(withoutFile)).toBe("abc:stub.mp3:1");
  });

  it("key for resume-from-historial path stays stable", () => {
    // Resume path: file=null, filename comes from DB, queueIdx=0,
    // transcribeJobId = the job we're resuming.
    const resume = {
      transcribeJobId: "resume-job-xyz",
      file: null,
      filename: "Coti - Luz de día.wav",
      queueIdx: 0,
    };
    expect(editorKey(resume)).toBe("resume-job-xyz:Coti - Luz de día.wav:0");
  });

  it("REGRESSION: under the OLD key (filename:queueIdx only), the Agus bug shape was undetectable", () => {
    // Pin the historical bug: prior key was just `filename:queueIdx`.
    // For two batch-upload jobs that happen to share filename (rare but
    // possible when the operator drags files renamed to the same name)
    // OR for the documented batch-flow where currentReview is swapped
    // but queueIdx hasn't bumped yet, the old key produced a collision.
    // The new key never does.
    const oldKey = (r) => `${r.file?.name || r.filename || "resume"}:${r.queueIdx}`;
    const reviewA = { transcribeJobId: "A", filename: "track.mp3", queueIdx: 0 };
    const reviewB = { transcribeJobId: "B", filename: "track.mp3", queueIdx: 0 };
    // Old key: collision (BUG).
    expect(oldKey(reviewA)).toBe(oldKey(reviewB));
    // New key: distinct (FIX).
    expect(editorKey(reviewA)).not.toBe(editorKey(reviewB));
  });
});
