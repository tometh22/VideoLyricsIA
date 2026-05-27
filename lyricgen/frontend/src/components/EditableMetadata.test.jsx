/**
 * Tests for the click-to-edit EditableMetadataField added in PR C
 * (feat/edit-metadata, 2026-05-26).
 *
 * The component is defined inline inside JobDetail.jsx. To test it in
 * isolation without mounting the whole JobDetail tree (heavy: many
 * fetches, modals, i18n provider), we render JobDetail with a minimum
 * job shape and exercise just the metadata-editing affordance.
 *
 * Coverage:
 *   - Disabled (plain text) when the job is in a non-editable status.
 *   - Pencil button visible on hover when enabled.
 *   - Click pencil → input replaces text + prefilled with current value.
 *   - Empty trimmed input → inline error, no POST fired.
 *   - Successful POST → fetch called with the right URL/body, edit
 *     handler triggered.
 *   - YouTube 409 → confirm prompt, second POST with allow_youtube_drift.
 *
 * We mock global fetch + the I18n provider + react-router (JobDetail
 * pulls useNavigate). We do NOT mount the heavy children — most paths
 * in JobDetail short-circuit on the job status guards.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, within, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { I18nProvider } from "../i18n.jsx";
import { AlertProvider } from "./AlertProvider";
import JobDetail from "./JobDetail.jsx";

function renderJobDetail(jobOverrides = {}, opts = {}) {
  const baseJob = {
    job_id: "test_meta_001",
    artist: "Sin Gamulan",
    song_title: "Los Abuelos De La Nada",
    filename: "test.mp3",
    status: "done",
    delivery_profile: "youtube",
    progress: 100,
    edit_count: 0,
    edits_remaining: 3,
    segments_json: [],
    s3_keys: {},
    ...jobOverrides,
  };
  const onJobUpdate = vi.fn();
  const onBack = vi.fn();
  const utils = render(
    <MemoryRouter>
      <I18nProvider>
        <AlertProvider>
          <JobDetail job={baseJob} onBack={onBack} onJobUpdate={onJobUpdate} />
        </AlertProvider>
      </I18nProvider>
    </MemoryRouter>
  );
  return { ...utils, onJobUpdate, onBack };
}

describe("EditableMetadataField — integrated in JobDetail header", () => {
  beforeEach(() => {
    // Stub global fetch — every render of JobDetail kicks off a handful
    // of fetches (status polling, drive status, media URLs). Default
    // them to 404 so the component shows its terminal state without
    // throwing; individual tests override per-URL when the metadata
    // submit actually fires.
    global.fetch = vi.fn(() => Promise.resolve({
      ok: false, status: 404,
      json: async () => ({}),
      text: async () => "",
    }));
    localStorage.setItem("genly_token", "test-token");
    // window.confirm default to true for the 409 path test.
    window.confirm = vi.fn(() => true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("renders pencil buttons next to song title and artist when done", async () => {
    renderJobDetail({ status: "done", song_title: "El Título" });
    // Pencil buttons surface via aria-label. Both should be present.
    const titleEdit = await screen.findByLabelText(/editar título/i);
    const artistEdit = await screen.findByLabelText(/editar artista/i);
    expect(titleEdit).toBeTruthy();
    expect(artistEdit).toBeTruthy();
  });

  it("does NOT render pencils for non-editable statuses (queued)", () => {
    const { container } = renderJobDetail({ status: "queued" });
    expect(container.querySelector('[aria-label*="Editar título"]')).toBeNull();
    expect(container.querySelector('[aria-label*="Editar artista"]')).toBeNull();
  });

  it("clicking the pencil opens an input prefilled with the current value", async () => {
    renderJobDetail({ status: "pending_review", song_title: "Título Original" });
    const pencil = await screen.findByLabelText(/editar título/i);
    fireEvent.click(pencil);
    const input = await screen.findByDisplayValue("Título Original");
    expect(input.tagName).toBe("INPUT");
  });

  it("empty trimmed input shows inline error and skips POST", async () => {
    renderJobDetail({ status: "done", song_title: "Old" });
    const pencil = await screen.findByLabelText(/editar título/i);
    fireEvent.click(pencil);
    const input = await screen.findByDisplayValue("Old");
    fireEvent.change(input, { target: { value: "   " } });
    const save = screen.getByText(/guardar/i);
    fireEvent.click(save);
    // The error renders inside an alert role
    const alert = await screen.findByRole("alert");
    expect(alert.textContent.toLowerCase()).toMatch(/vacío|empty/);
    // No POST should have fired (we stubbed fetch to track this).
    const postCalls = global.fetch.mock.calls.filter(([url, opts]) => {
      return opts && opts.method === "POST" && String(url).includes("/edit/");
    });
    expect(postCalls.length).toBe(0);
  });

  it("successful POST fires onJobUpdate with editing status", async () => {
    // Override fetch: the /edit POST returns 200 with edit_count etc.
    global.fetch = vi.fn((url, opts) => {
      if (opts && opts.method === "POST" && String(url).includes("/edit/")) {
        return Promise.resolve({
          ok: true, status: 200,
          json: async () => ({
            ok: true, job_id: "test_meta_001",
            edit_type: "metadata", edit_count: 0,
            edits_remaining: 3, edit_limit_exempt: false,
          }),
        });
      }
      return Promise.resolve({ ok: false, status: 404, json: async () => ({}), text: async () => "" });
    });
    const { onJobUpdate } = renderJobDetail({ status: "done", song_title: "Old Title" });
    const pencil = await screen.findByLabelText(/editar título/i);
    fireEvent.click(pencil);
    const input = await screen.findByDisplayValue("Old Title");
    fireEvent.change(input, { target: { value: "New Title" } });
    const save = screen.getByText(/guardar/i);
    fireEvent.click(save);
    await waitFor(() => {
      expect(onJobUpdate).toHaveBeenCalled();
    });
    const calledWith = onJobUpdate.mock.calls[0][0];
    expect(calledWith.status).toBe("editing");
    // POST was fired against /edit/<id> with edit_type=metadata + song_title.
    const postCall = global.fetch.mock.calls.find(
      ([url, opts]) => opts && opts.method === "POST" && String(url).includes("/edit/")
    );
    expect(postCall).toBeTruthy();
    const body = JSON.parse(postCall[1].body);
    expect(body.edit_type).toBe("metadata");
    expect(body.song_title).toBe("New Title");
  });

  it("YouTube 409 triggers confirm prompt + second POST with allow_youtube_drift", async () => {
    let postCount = 0;
    global.fetch = vi.fn((url, opts) => {
      if (opts && opts.method === "POST" && String(url).includes("/edit/")) {
        postCount += 1;
        const body = JSON.parse(opts.body);
        if (!body.allow_youtube_drift) {
          // First call: 409 youtube_already_published.
          return Promise.resolve({
            ok: false, status: 409,
            json: async () => ({
              detail: { code: "youtube_already_published", message: "...", youtube_url: "https://youtu.be/x" },
            }),
          });
        }
        // Second call after confirm: 200.
        return Promise.resolve({
          ok: true, status: 200,
          json: async () => ({
            ok: true, edit_type: "metadata", edit_count: 0,
            edits_remaining: 3, edit_limit_exempt: false,
          }),
        });
      }
      return Promise.resolve({ ok: false, status: 404, json: async () => ({}), text: async () => "" });
    });
    const { onJobUpdate } = renderJobDetail({
      status: "done",
      song_title: "Old",
      youtube_data: { url: "https://youtu.be/x" },
    });
    const pencil = await screen.findByLabelText(/editar título/i);
    fireEvent.click(pencil);
    const input = await screen.findByDisplayValue("Old");
    fireEvent.change(input, { target: { value: "New" } });
    fireEvent.click(screen.getByText(/guardar/i));
    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(postCount).toBe(2);  // first 409, then retry with drift=true
    });
    await waitFor(() => {
      expect(onJobUpdate).toHaveBeenCalled();
    });
  });
});
