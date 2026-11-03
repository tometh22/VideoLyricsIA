import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import OperatorActivity, { productionDay } from "./OperatorActivity";

const result = { generated: 36, requested: 36, edited_songs: 11, edit_requests: 12, approved_now: 27, pending_review_now: 9,
  campaigns: [{ id: "camp", name: "Universal Argentina", count: 36 }], songs: [{ job_id: "song", campaign_id: "camp", artist: "Artista", title: "Canción" }] };
const json = (data, status = 200) => ({ ok: status === 200, status, json: async () => data });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); });

it("uses Argentina's calendar date at UTC midnight and across month boundaries", () => {
  expect(productionDay(0, new Date("2026-11-03T01:00:00Z"))).toBe("2026-11-02");
  expect(productionDay(-1, new Date("2026-11-01T12:00:00Z"))).toBe("2026-10-31");
});

it("makes yesterday discoverable, attributes to operator and separates edits from songs", async () => {
  localStorage.setItem("genly_token", "test-token");
  const fetch = vi.fn(async () => json(result)); vi.stubGlobal("fetch", fetch);
  render(<MemoryRouter><OperatorActivity /></MemoryRouter>);
  expect(screen.getByRole("status")).toHaveTextContent("Cargando");
  await screen.findByText("Canciones convertidas a video");
  expect(screen.getByText(/27 aprobados · 9 por revisar/)).toBeInTheDocument();
  expect(screen.getByText(/12 solicitudes · no suman canciones nuevas/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Ayer" }));
  await screen.findByText("Canciones convertidas a video");
  expect(fetch).toHaveBeenLastCalledWith(`/batch/activity/me?day=${productionDay(-1)}`, expect.objectContaining({ cache: "no-store", headers: { Authorization: "Bearer test-token" } }));
  fireEvent.click(screen.getByText("Ver las 36 canciones"));
  expect(screen.getByRole("link", { name: "Artista · Canción" })).toHaveAttribute("href", "/campaigns/camp");
});

it("never presents a failed request as zero and can retry", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(json(result)));
  render(<MemoryRouter><OperatorActivity /></MemoryRouter>);
  await screen.findByRole("alert");
  expect(screen.queryByText("Canciones convertidas a video")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Actualizar" }));
  await screen.findByText("Canciones convertidas a video");
});

it("ignores a late response for the previously selected date", async () => {
  let finish;
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce(json(result)));
  render(<MemoryRouter><OperatorActivity /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "Ayer" }));
  await screen.findByText("Ver las 36 canciones");
  await act(async () => finish(json({ ...result, generated: 99 })));
  expect(screen.queryByText("Ver las 99 canciones")).not.toBeInTheDocument();
});

it("hides the optional home card when campaigns are unavailable", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "disabled" }, 404)));
  const { container } = render(<MemoryRouter><OperatorActivity optional /></MemoryRouter>);
  await act(async () => {});
  expect(within(container).queryByRole("region")).not.toBeInTheDocument();
});
