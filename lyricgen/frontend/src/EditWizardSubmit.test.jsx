// Mirror test for App.jsx's edit-wizard submit branch.
//
// The submit logic lives inline inside App.jsx::handleApproveLyrics (the
// `if (r.editMode || r.editingJobId) { ... }` block). Mounting App.jsx
// in test is impractical — it pulls auth, react-router, toast provider,
// useBackgroundPreview, and dozens of side effects. We reproduce the
// submit dispatcher in a small async function and validate its contract:
//
//   1. No-changes diff → toast + no fetch.
//   2. Single bucket → 1 POST with the right body + ed_type.
//   3. Multiple buckets → N POSTs in stable order, each shaped right.
//   4. YouTube 409 → confirm prompt → second POST with allow_youtube_drift.
//   5. Non-409 error → break the sequence, no further POSTs.
//
// If App.jsx's dispatch diverges from this contract, both files update.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { computeFieldDiff, buildEditPayloads } from "./lib/editWizardDiff";

const API = "https://api.test";

// MIRROR of the submit logic inside App.jsx::handleApproveLyrics.
// Keep field-for-field parallel with the production block.
async function submitEditMirror({
  jobId,
  baseline,
  current,
  authFetch,
  onConfirmYoutube,
  onToast,
  onError,
  onSuccess,
}) {
  const diff = computeFieldDiff(baseline, current);
  const payloads = buildEditPayloads(diff);
  if (payloads.length === 0) {
    onToast?.("no_changes");
    return;
  }
  for (const payload of payloads) {
    const res = await authFetch(`${API}/edit/${jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 409 && data?.detail?.code === "youtube_already_published") {
      if (!onConfirmYoutube?.()) return;
      const retry = await authFetch(`${API}/edit/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, allow_youtube_drift: true }),
      });
      const retryData = await retry.json().catch(() => ({}));
      if (!retry.ok) {
        onError?.({ status: retry.status, data: retryData });
        return;
      }
      continue;
    }
    if (!res.ok) {
      onError?.({ status: res.status, data });
      return;
    }
  }
  onSuccess?.();
}

const baseline = () => ({
  artist: "Cerati",
  songTitle: "Crimen",
  font: "jost",
  fontScale: "1.0",
  textCase: "upper",
  textContrast: "medium",
  lyricsAnimation: "none",
  lineTransition: "none",
  effect: "",
  movementStyle: "",
  backgroundHint: "",
  bgVerbatim: false,
  backgroundMode: "",
  segments: [{ start: 0, end: 2, text: "Línea" }],
});

function makeFetchMock(responses) {
  // responses: array of { status, body } applied in order to consecutive calls.
  let idx = 0;
  return vi.fn().mockImplementation(() => {
    const r = responses[idx] || responses[responses.length - 1];
    idx += 1;
    return Promise.resolve({
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      json: async () => r.body ?? {},
    });
  });
}

describe("edit-wizard submitEdit dispatch", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("no-changes diff → toast + no fetch", async () => {
    const fetch = vi.fn();
    const toast = vi.fn();
    const success = vi.fn();
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current: { ...baseline(), segments: JSON.parse(JSON.stringify(baseline().segments)) },
      authFetch: fetch,
      onToast: toast,
      onSuccess: success,
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith("no_changes");
    expect(success).not.toHaveBeenCalled();
  });

  it("metadata-only change → 1 POST with edit_type=metadata + artist field", async () => {
    const fetch = makeFetchMock([{ status: 202, body: { ok: true } }]);
    const success = vi.fn();
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current: { ...baseline(), artist: "Soda Stereo" },
      authFetch: fetch,
      onSuccess: success,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe(`${API}/edit/abc123`);
    const body = JSON.parse(opts.body);
    expect(body).toEqual({ edit_type: "metadata", artist: "Soda Stereo" });
    expect(success).toHaveBeenCalled();
  });

  it("metadata + lyrics → 2 POSTs in stable order: metadata first, then lyrics", async () => {
    const fetch = makeFetchMock([
      { status: 202, body: { ok: true } },
      { status: 202, body: { ok: true } },
    ]);
    const success = vi.fn();
    const current = {
      ...baseline(),
      artist: "Soda Stereo",
      segments: [
        { start: 0, end: 2.5, text: "Línea corregida" }, // text + timing changed
      ],
    };
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current,
      authFetch: fetch,
      onSuccess: success,
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    const body1 = JSON.parse(fetch.mock.calls[0][1].body);
    const body2 = JSON.parse(fetch.mock.calls[1][1].body);
    expect(body1.edit_type).toBe("metadata");
    expect(body2.edit_type).toBe("lyrics");
    expect(success).toHaveBeenCalled();
  });

  it("metadata + typography → typography bundles INTO metadata (1 POST, not 2)", async () => {
    const fetch = makeFetchMock([{ status: 202, body: { ok: true } }]);
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current: { ...baseline(), artist: "Soda Stereo", font: "lobster" },
      authFetch: fetch,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    const body = JSON.parse(fetch.mock.calls[0][1].body);
    expect(body).toEqual({
      edit_type: "metadata",
      artist: "Soda Stereo",
      font: "lobster",
    });
  });

  it("YouTube 409 → confirm = false aborts WITHOUT retry", async () => {
    const fetch = makeFetchMock([
      {
        status: 409,
        body: {
          detail: {
            code: "youtube_already_published",
            youtube_url: "https://yt/x",
          },
        },
      },
    ]);
    const confirm = vi.fn(() => false);
    const success = vi.fn();
    const err = vi.fn();
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current: { ...baseline(), artist: "Soda" },
      authFetch: fetch,
      onConfirmYoutube: confirm,
      onSuccess: success,
      onError: err,
    });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1); // no retry
    expect(success).not.toHaveBeenCalled();
    expect(err).not.toHaveBeenCalled();
  });

  it("YouTube 409 → confirm = true triggers retry with allow_youtube_drift=true", async () => {
    const fetch = makeFetchMock([
      {
        status: 409,
        body: {
          detail: {
            code: "youtube_already_published",
            youtube_url: "https://yt/x",
          },
        },
      },
      { status: 202, body: { ok: true } },
    ]);
    const confirm = vi.fn(() => true);
    const success = vi.fn();
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current: { ...baseline(), artist: "Soda" },
      authFetch: fetch,
      onConfirmYoutube: confirm,
      onSuccess: success,
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    const retryBody = JSON.parse(fetch.mock.calls[1][1].body);
    expect(retryBody.allow_youtube_drift).toBe(true);
    expect(retryBody.edit_type).toBe("metadata");
    expect(retryBody.artist).toBe("Soda");
    expect(success).toHaveBeenCalled();
  });

  it("non-409 error → break the sequence, no further POSTs, error surfaced", async () => {
    // Job in editing state — backend rejects with 400. We should not POST
    // the second bucket even if the first succeeded; the user has to retry
    // the whole edit. Order: metadata fails → lyrics never sent.
    const fetch = makeFetchMock([
      { status: 400, body: { detail: "Job must be in pending_review..." } },
      { status: 202, body: { ok: true } }, // would be lyrics — should NOT fire
    ]);
    const success = vi.fn();
    const err = vi.fn();
    await submitEditMirror({
      jobId: "abc123",
      baseline: baseline(),
      current: {
        ...baseline(),
        artist: "Soda Stereo",
        segments: [{ start: 0, end: 2.5, text: "x" }],
      },
      authFetch: fetch,
      onSuccess: success,
      onError: err,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(err).toHaveBeenCalledWith({ status: 400, data: expect.any(Object) });
    expect(success).not.toHaveBeenCalled();
  });
});
