import { describe, expect, it } from "vitest";
import { buildSongs, canDiscard, canGenerate, canSend, filterSongs, nextLyricSong, primaryAction, sortSongs, statusNote } from "./songModel";

const item = (id, stage, extra = {}) => ({ id, item_id: id, ordinal: Number(id.replace(/\D/g, "")) || 1, title: `Tema ${id}`, artist: "García", stage, job_id: `j${id}`, current_job_id: `j${id}`, portals: [], ...extra });

describe("song model", () => {
  it("joins pipeline items with review, creative and current video by id", () => {
    const [song] = buildSongs([item("1", "lyrics", { current_job_id: "v2" })], {
      reviewRows: [{ item_id: "x" }, { item_id: "1", is_draft: true }],
      creativeItems: [{ id: "1", status: "transcribed" }],
      videos: [{ job_id: "v2", title: "video" }],
    });
    expect(song).toMatchObject({ id: "1", review: { is_draft: true, queuePosition: 1 }, creative: { status: "transcribed" }, video: { title: "video" } });
  });

  it("offers exactly one primary action per stage and respects permissions and locks", () => {
    expect(primaryAction(item("1", "lyrics"))).toEqual({ key: "review", label: "Revisar" });
    expect(primaryAction({ ...item("1", "lyrics"), review: { state: "ready", job_id: "j1", is_draft: true } })).toEqual({ key: "review", label: "Continuar revisión" });
    expect(primaryAction({ ...item("1", "lyrics"), review: { state: "reviewing", job_id: "j1", reviewer_lock_active: true, reviewer_is_current_user: false } })).toBeNull();
    expect(primaryAction(item("1", "ready"))).toEqual({ key: "generate", label: "Generar" });
    expect(primaryAction(item("1", "ready"), { kind: "art_track" })).toBeNull();
    expect(primaryAction(item("1", "qc", { has_video: true }))).toEqual({ key: "play", label: "Revisar video" });
    const approved = item("1", "approved", { has_video: true, current_status: "done", approved_at: "2026-09-01" });
    expect(primaryAction(approved)).toEqual({ key: "play", label: "Ver video" });
    expect(primaryAction(approved, { canManage: true })).toEqual({ key: "send", label: "Enviar" });
    expect(primaryAction({ ...approved, stage: "delivered", portal_outdated: true }, { canManage: true })).toEqual({ key: "send", label: "Reenviar" });
    expect(primaryAction({ ...approved, stage: "delivered" }, { canManage: true })).toEqual({ key: "play", label: "Ver video" });
    expect(primaryAction(item("1", "attention"), { canManage: true })).toEqual({ key: "retry", label: "Reintentar" });
    expect(primaryAction(item("1", "discarded"))).toEqual({ key: "restore", label: "Recuperar" });
    expect(primaryAction(item("1", "audio", { metadata_error: "missing_metadata" }), { canManage: true })).toEqual({ key: "metadata", label: "Completar datos" });
    expect(primaryAction(item("1", "rendering"))).toBeNull();
  });

  it("only exposes actions the backend accepts", () => {
    expect(canGenerate({ ...item("1", "ready"), creative: { status: "lyrics_approved" } })).toBe(true);
    expect(canGenerate({ ...item("1", "ready"), creative: { status: "lyrics_approved", discarded: true } })).toBe(false);
    expect(canGenerate({ ...item("1", "ready"), creative: null })).toBe(false);
    expect(canSend(item("1", "approved", { current_status: "done", approved_at: "x", has_video: true }))).toBe(true);
    expect(canSend(item("1", "qc", { current_status: "pending_review", has_video: true }))).toBe(false);
    expect(canDiscard({ ...item("1", "lyrics"), review: { can_discard: false } })).toBe(false);
    expect(canDiscard(item("1", "lyrics"))).toBe(true);
    expect(canDiscard(item("1", "ready"))).toBe(false);
  });

  it("filters by stage, search and stage-specific chips", () => {
    const songs = buildSongs([
      item("1", "lyrics"), item("2", "lyrics", { title: "Corazón" }), item("3", "discarded"),
      item("4", "delivered", { portals: ["chile"], pending_change_requests: 1 }), item("5", "approved"),
    ], { reviewRows: [{ item_id: "2", is_draft: true, review_priority: "manual_full", version: "live" }, { item_id: "1", review_priority: "standard" }] });
    expect(filterSongs(songs, { view: "all" }).map((song) => song.id)).toEqual(["1", "2", "4", "5"]);
    expect(filterSongs(songs, { view: "discarded" }).map((song) => song.id)).toEqual(["3"]);
    expect(filterSongs(songs, { view: "lyrics", q: "garcia corazon" }).map((song) => song.id)).toEqual(["2"]);
    expect(filterSongs(songs, { view: "lyrics", drafts: true }).map((song) => song.id)).toEqual(["2"]);
    expect(filterSongs(songs, { view: "lyrics", classification: "standard" }).map((song) => song.id)).toEqual(["1"]);
    expect(filterSongs(songs, { view: "lyrics", version: "live" }).map((song) => song.id)).toEqual(["2"]);
    expect(filterSongs(songs, { view: "all", portal: "sent" }).map((song) => song.id)).toEqual(["4"]);
    expect(filterSongs(songs, { view: "all", portal: "unsent" }).map((song) => song.id)).toEqual(["1", "2", "5"]);
    expect(filterSongs(songs, { view: "all", portal: "changes" }).map((song) => song.id)).toEqual(["4"]);
  });

  it("keeps the backend effort order for lyrics and puts work first in 'all'", () => {
    const songs = buildSongs([item("1", "lyrics"), item("2", "lyrics"), item("3", "delivered"), item("4", "attention")],
      { reviewRows: [{ item_id: "2" }, { item_id: "1" }] });
    expect(sortSongs(songs.filter((song) => song.stage === "lyrics"), "lyrics").map((song) => song.id)).toEqual(["2", "1"]);
    expect(sortSongs(songs, "all").map((song) => song.id)).toEqual(["4", "1", "2", "3"]);
  });

  it("finds the next reviewable lyric after the current one, wrapping and skipping foreign locks", () => {
    const songs = [
      { ...item("1", "lyrics"), review: { state: "reviewing", job_id: "j1", reviewer_lock_active: true, reviewer_is_current_user: false } },
      { ...item("2", "lyrics") }, { ...item("3", "ready") }, { ...item("4", "lyrics") },
    ];
    expect(nextLyricSong(songs).id).toBe("2");
    expect(nextLyricSong(songs, "2").id).toBe("4");
    expect(nextLyricSong(songs, "4").id).toBe("2");
    expect(nextLyricSong([item("9", "qc")])).toBeNull();
  });

  it("explains the state without repeating the badge", () => {
    expect(statusNote({ ...item("1", "lyrics"), review: { reviewer_lock_active: true, reviewer_is_current_user: false, reviewer_name: "Agus" } })).toBe("En revisión por Agus");
    expect(statusNote({ ...item("1", "lyrics"), review: { is_draft: true, last_reviewed_name: "Agus" } })).toBe("Cambios guardados por Agus");
    expect(statusNote(item("1", "audio", { upload_state: "uploaded", phase: "transcribing" }))).toBe("Transcribiendo");
    expect(statusNote(item("1", "attention", { error: "Veo no disponible" }))).toBe("Veo no disponible");
    expect(statusNote(item("1", "qc"))).toBe("");
  });
});
