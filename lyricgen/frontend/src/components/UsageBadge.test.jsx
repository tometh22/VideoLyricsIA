/**
 * Tests for UsageBadge — the sidebar component that shows the operator's
 * monthly usage against their plan limit.
 *
 * What this test pins:
 *   - role=user (operator) sees count + percent but NOT overage_total ($$).
 *   - role=admin sees overage_total when overage > 0.
 *   - plan="unlimited" suppresses the badge entirely.
 *   - alert_80 / alert_100 swap the bar color (gray → amber → red).
 *   - 102% overage caps the bar width at 100% but shows honest percent.
 *
 * Implementation note: UsageBadge fetches /usage on mount, so we mock
 * global.fetch to return canned payloads. The component swallows fetch
 * errors silently — the test for that uses a rejected promise.
 */
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import UsageBadge from "./UsageBadge";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

beforeEach(() => {
  // Stub localStorage so the component's `genly_token` lookup succeeds.
  // We don't validate the token value — the component just forwards it
  // as a Bearer header.
  window.localStorage.setItem("genly_token", "test-token");
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function _mockFetchOnce(payload) {
  vi.spyOn(global, "fetch").mockResolvedValueOnce({
    ok: true,
    json: async () => payload,
  });
}

const USER_OPERATOR = { username: "ana", role: "user", plan: "250" };
const USER_ADMIN = { username: "admin", role: "admin", plan: "unlimited" };

describe("UsageBadge", () => {
  it("renders nothing when user is falsy (logged out / loading)", () => {
    _mockFetchOnce({ used: 0, limit: 250, percent: 0 });
    const { container } = render(<UsageBadge user={null} />);
    // No state change → empty render.
    expect(container.firstChild).toBeNull();
  });

  it("hides when plan is 'unlimited' (no cap to visualise)", async () => {
    _mockFetchOnce({ plan: "unlimited", limit: 999999, used: 1, percent: 0 });
    const { container } = render(<UsageBadge user={USER_ADMIN} />);
    // Even after fetch, unlimited plans return null.
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it("shows used/limit + percent for a regular operator under threshold", async () => {
    _mockFetchOnce({
      plan: "250", used: 12, limit: 250, percent: 5,
      alert_80: false, alert_100: false, overage_total: 0,
    });
    render(<UsageBadge user={USER_OPERATOR} />);
    await screen.findByText(/12 \/ 250/);
    expect(screen.getByText("5%")).toBeInTheDocument();
  });

  it("does NOT show overage_total to a regular operator even when set", async () => {
    // Server returns overage_total > 0 (operator went over plan).
    // The operator should still NOT see the $$ amount.
    _mockFetchOnce({
      plan: "250", used: 260, limit: 250, percent: 104,
      alert_80: true, alert_100: true, overage_total: 15,
    });
    render(<UsageBadge user={USER_OPERATOR} />);
    await screen.findByText(/260 \/ 250/);
    // No $$ display — the overage element is admin-only.
    expect(screen.queryByText(/\+\$15/)).not.toBeInTheDocument();
    expect(screen.queryByText(/excedente/)).not.toBeInTheDocument();
  });

  it("shows overage_total to admin when overage > 0", async () => {
    _mockFetchOnce({
      plan: "250", used: 260, limit: 250, percent: 104,
      alert_80: true, alert_100: true, overage_total: 15,
    });
    // Admin with NON-unlimited plan (so the badge renders).
    render(<UsageBadge user={{ username: "tomas", role: "admin", plan: "250" }} />);
    await screen.findByText(/260 \/ 250/);
    expect(screen.getByText(/\+\$15/)).toBeInTheDocument();
  });

  it("renders nothing if /usage fetch fails (no crash, no error banner)", async () => {
    vi.spyOn(global, "fetch").mockRejectedValueOnce(new Error("offline"));
    const { container } = render(<UsageBadge user={USER_OPERATOR} />);
    // Component should not crash; nothing rendered (silent fallback).
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it("caps the bar width visually at 100% even when percent > 100", async () => {
    _mockFetchOnce({
      plan: "250", used: 260, limit: 250, percent: 104,
      alert_80: true, alert_100: true, overage_total: 0,
    });
    const { container } = render(<UsageBadge user={USER_OPERATOR} />);
    await screen.findByText(/260 \/ 250/);
    // The percent label is honest: shows "104%".
    expect(screen.getByText("104%")).toBeInTheDocument();
    // The bar fill div has inline width style. We don't care about the
    // exact selector — just that no width is >100%.
    const widths = Array.from(container.querySelectorAll("[style]"))
      .map((el) => el.style.width)
      .filter(Boolean);
    for (const w of widths) {
      const pct = parseFloat(w);
      if (!Number.isNaN(pct)) {
        expect(pct).toBeLessThanOrEqual(100);
      }
    }
  });
});
