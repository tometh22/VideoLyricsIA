import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import RequestVerification, { verificationGate } from "./RequestVerification";

afterEach(cleanup);

const RESULT = {
  enabled: true,
  recurrences: [{ point_id: "p-2", request_id: 41, excerpt: "2:39 Vení sumate" }],
  verification: {
    revision: 12,
    counts: { ok: 1, fail: 1, manual: 1 },
    points: [
      { id: "p-1", kind: "text", status: "ok", reason: "found_near", excerpt: "2:30 Vení sumate a la ronda",
        line: { start: 150, end: 153, text: "Vení sumate a la ronda" } },
      { id: "p-2", kind: "text", status: "fail", reason: "found_only_elsewhere", excerpt: "2:39 Vení sumate a la ronda",
        line: { start: 159, end: 162, text: "Ven y súmate a la ronda" } },
      { id: "p-3", kind: "visual", status: "manual", reason: "visual", excerpt: "Sacar el borde negro" },
    ],
  },
};

describe("RequestVerification", () => {
  it("shows each point against the lyrics and blocks until the failing one has a reason", async () => {
    const onChange = vi.fn();
    const onSeek = vi.fn();
    render(<RequestVerification load={() => Promise.resolve(RESULT)} onChange={onChange} onSeek={onSeek} />);
    expect(await screen.findByLabelText(/Verificado: 2:30/)).toBeInTheDocument();
    expect(screen.getByLabelText(/No verificado: 2:39/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Revisar: Sacar el borde/)).toBeInTheDocument();
    expect(screen.getByText(/La frase está en otra parte de la canción/)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Reincidencia");
    expect(screen.getByRole("note")).toHaveTextContent("#41");
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ ready: false })));

    fireEvent.click(screen.getByRole("button", { name: "2:39" }));
    expect(onSeek).toHaveBeenCalledWith(159);

    fireEvent.change(screen.getByLabelText(/Justificación interna: 2:39/), { target: { value: "El cliente aceptó dejarla" } });
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ ready: true, overrides: { "p-2": "El cliente aceptó dejarla" } })));
  });

  it("offers quick reasons", async () => {
    const onChange = vi.fn();
    render(<RequestVerification load={() => Promise.resolve(RESULT)} onChange={onChange} />);
    fireEvent.click(await screen.findByRole("button", { name: "Errata en el pedido del cliente" }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ ready: true, overrides: { "p-2": "Errata en el pedido del cliente" } })));
  });

  it("never blocks the UI when the check is off or fails (the server still checks)", async () => {
    const onChange = vi.fn();
    const { unmount } = render(<RequestVerification load={() => Promise.resolve({ enabled: false })} onChange={onChange} />);
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ ready: true, loading: false })));
    unmount();
    render(<RequestVerification load={() => Promise.reject(new Error("HTTP 500"))} onChange={onChange} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No pude verificar");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ ready: true }));
  });

  it("gate counts only non-empty reasons for failing points", () => {
    expect(verificationGate(RESULT.verification, {}).ready).toBe(false);
    expect(verificationGate(RESULT.verification, { "p-2": "  " }).missing).toEqual(["p-2"]);
    expect(verificationGate(RESULT.verification, { "p-2": " ok ", "p-1": "x" }).overrides).toEqual({ "p-2": "ok" });
    expect(verificationGate(null).ready).toBe(true);
  });
});
