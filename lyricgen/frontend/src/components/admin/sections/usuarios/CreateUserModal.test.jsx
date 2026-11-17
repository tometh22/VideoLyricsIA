import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CreateUserModal from "./CreateUserModal";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("CreateUserModal", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        workspaces: [{ id: "workspace-alpha", users: 4, campaigns: 1 }],
        campaigns: [{ id: "campaign-beta", name: "UMG Review — Lyrics", tenant_id: "workspace-beta", expected_count: 12 }],
      }),
    });
  });

  it("creates a user with a selected workspace and associated campaign", async () => {
    const onCreate = vi.fn().mockResolvedValue(null);
    render(<CreateUserModal onCreate={onCreate} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText("Ej. danniivanna04"), { target: { value: "reviewer@example" } });
    fireEvent.change(screen.getByPlaceholderText("Contraseña segura"), { target: { value: "secure-password" } });

    fireEvent.focus(screen.getByPlaceholderText("Buscar o escribir un workspace…"));
    fireEvent.click(await screen.findByRole("button", { name: /workspace-alpha/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /UMG Review — Lyrics/ }));
    fireEvent.click(screen.getByRole("button", { name: "Crear usuario" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate.mock.calls[0][0]).toMatchObject({
      username: "reviewer@example",
      tenant_id: "workspace-alpha",
      campaign_ids: ["campaign-beta"],
    });
  });
});
