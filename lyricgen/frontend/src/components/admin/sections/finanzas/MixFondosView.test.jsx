/**
 * Mix de fondos: el total se calcula sólo sobre las campañas tildadas y la
 * selección sobrevive a recargar la pestaña.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../../adminApi";
import MixFondosView from "./MixFondosView";
import { STORAGE_KEY, summarize } from "./mixFondosModel";

// Números ficticios.
const MIX = {
  groups: [
    { key: "campaign:a", campaign_id: "a", name: "Campaña A", portal: "argentina", videos: 10, veo: 6, foto: 4, sin_dato: 0,
      veo_models: { lite: 5, fast: 1 }, efectos: { bokeh: 4 }, first_delivery_at: "2026-10-01T12:00:00Z", last_delivery_at: "2026-10-05T12:00:00Z" },
    { key: "campaign:b", campaign_id: "b", name: "Campaña B", portal: "chile", videos: 4, veo: 4, foto: 0, sin_dato: 0,
      veo_models: { lite: 4 }, efectos: {}, first_delivery_at: "2026-09-10T12:00:00Z", last_delivery_at: "2026-09-10T12:00:00Z" },
  ],
  items: [
    { job_id: "j1", group: "campaign:a", portal: "argentina", artist: "Artista Uno", song: "Tema", type: "foto", effect: "bokeh", added_at: "2026-10-01T12:00:00Z" },
    { job_id: "j2", group: "campaign:b", portal: "chile", artist: "Artista Dos", song: "Otro", type: "veo", veo_model: "lite", added_at: "2026-09-10T12:00:00Z" },
  ],
  excluded_art_tracks: 0, jobs_not_found: 0, single_environment: false,
};

beforeEach(() => {
  window.localStorage.removeItem(STORAGE_KEY);
  vi.spyOn(adminApi, "fetchJson").mockResolvedValue(MIX);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("summarize", () => {
  it("suma sólo lo tildado y calcula lo que falta para el 50/50", () => {
    const s = summarize(MIX.groups, new Set(["campaign:a", "campaign:b"]), 20);
    expect([s.veo, s.foto, s.faltanFoto, s.veoNoLite]).toEqual([10, 4, 6, 1]);
    expect(s.meta).toMatchObject({ restantes: 6, foto: 6, veo: 0, alcanzable: true });
    const solo = summarize(MIX.groups, new Set(["campaign:a"]), 0);
    expect([solo.veo, solo.foto, solo.meta]).toEqual([6, 4, null]);
    expect(summarize(MIX.groups, new Set(["campaign:b"]), 6).meta.alcanzable).toBe(false);
  });
});

describe("MixFondosView", () => {
  it("arranca con todo tildado y recalcula al destildar", async () => {
    render(<MixFondosView />);
    await waitFor(() => expect(screen.getByText("Campaña A")).toBeTruthy());
    // 10 Veo de 14 = 71,4%.
    expect(screen.getByText("71,4%")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Incluir Campaña B (Chile)"));
    expect(screen.getByText("60%")).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY)).selected).toEqual(["campaign:a"]);
  });

  it("respeta la selección guardada", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ selected: ["campaign:b"], meta: 0 }));
    render(<MixFondosView />);
    await waitFor(() => expect(screen.getByText("Campaña A")).toBeTruthy());
    expect(screen.getByLabelText("Incluir Campaña A (Argentina)").checked).toBe(false);
    expect(screen.getByText("100%")).toBeTruthy();
    fireEvent.click(screen.getByText("Ver videos (4)"));
    expect(screen.getByText("Artista Dos")).toBeTruthy();
    expect(screen.queryByText("Artista Uno")).toBeNull();
  });
});
