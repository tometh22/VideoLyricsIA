import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import TrialSupportChat from "./TrialSupportChat";
import { isTrialSupportEnabled, trialSupport } from "../lib/trialSupport";

vi.mock("../lib/trialSupport", () => ({
  isTrialSupportEnabled: vi.fn(() => true),
  supportUserKey: (user) => user?.id ? `${user.tenant_id || ""}:${user.id}` : null,
  trialSupport: { setOwner: vi.fn(), open: vi.fn() },
}));

describe("trial support affordance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    isTrialSupportEnabled.mockReturnValue(true);
    trialSupport.open.mockResolvedValue();
  });
  afterEach(cleanup);
  it("is absent outside trial and for logged-out users", () => {
    const { rerender } = render(<TrialSupportChat userKey={null} />);
    expect(screen.queryByRole("button")).toBeNull();
    isTrialSupportEnabled.mockReturnValue(false);
    rerender(<TrialSupportChat userKey="tenant:a" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(trialSupport.open).not.toHaveBeenCalled();
  });
  it("requires an explicit open and shows an email fallback", async () => {
    render(<TrialSupportChat userKey="tenant:a" />);
    fireEvent.click(screen.getByRole("button", { name: "Soporte Genly" }));
    expect(trialSupport.open).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Contactar por email" })).toHaveAttribute("href", "mailto:tomas@epical.digital");
    expect(screen.getByText(/El chat funciona con Crisp/)).toBeVisible();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Abrir chat" })));
    expect(trialSupport.open).toHaveBeenCalledOnce();
  });
  it("keeps help and the email fallback usable on SDK failure", async () => {
    trialSupport.open.mockRejectedValue(new Error("blocked"));
    render(<TrialSupportChat userKey="tenant:a" />);
    fireEvent.click(screen.getByRole("button", { name: "Soporte Genly" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Abrir chat" })));
    expect(screen.getByRole("alert")).toHaveTextContent("No pudimos abrir el chat");
    expect(screen.getByRole("link", { name: "Contactar por email" })).toBeVisible();
  });
  it("clears ownership and closes help on logout", () => {
    const { rerender } = render(<TrialSupportChat userKey="tenant:a" />);
    fireEvent.click(screen.getByRole("button", { name: "Soporte Genly" }));
    rerender(<TrialSupportChat userKey={null} />);
    expect(trialSupport.setOwner).toHaveBeenLastCalledWith(null);
    expect(screen.queryByRole("button")).toBeNull();
  });
  it("ignores an earlier account's delayed load failure", async () => {
    let reject;
    trialSupport.open.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    const { rerender } = render(<TrialSupportChat userKey="tenant:a" />);
    fireEvent.click(screen.getByRole("button", { name: "Soporte Genly" }));
    fireEvent.click(screen.getByRole("button", { name: "Abrir chat" }));
    rerender(<TrialSupportChat userKey="tenant:b" />);
    await act(async () => reject(new Error("old account cancelled")));
    fireEvent.click(screen.getByRole("button", { name: "Soporte Genly" }));
    expect(screen.getByRole("button", { name: "Abrir chat" })).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();
  });
  it("fails closed on cross-tab account changes but not token refresh", () => {
    render(<TrialSupportChat userKey="tenant:a" />);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "genly_token", newValue: "refreshed" })));
    expect(screen.getByRole("button", { name: "Soporte Genly" })).toBeVisible();
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "genly_user", newValue: JSON.stringify({ tenant_id: "tenant", id: "a", display_name: "New name" }) })));
    expect(screen.getByRole("button", { name: "Soporte Genly" })).toBeVisible();
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "genly_user", newValue: "different" })));
    expect(trialSupport.setOwner).toHaveBeenLastCalledWith(null);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
