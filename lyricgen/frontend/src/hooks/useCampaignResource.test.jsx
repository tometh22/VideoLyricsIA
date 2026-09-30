import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import useCampaignResource, { clearCampaignResourceCache, invalidateCampaignResources, mutateCampaignResource } from "./useCampaignResource";

function Probe({ resourceKey, fetcher, label = "probe" }) {
  const { data, loading, error } = useCampaignResource(resourceKey, fetcher, { pollMs: 0 });
  return <p data-testid={label}>{loading ? "loading" : error ? `error:${error.message}` : data}</p>;
}
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

afterEach(() => { cleanup(); clearCampaignResourceCache(); });

describe("useCampaignResource", () => {
  it("serves the cached snapshot instantly to a remount instead of reloading", async () => {
    const fetcher = vi.fn(async () => "uno");
    const first = render(<Probe resourceKey="k1" fetcher={fetcher} />);
    await screen.findByText("uno");
    first.unmount();
    render(<Probe resourceKey="k1" fetcher={fetcher} />);
    expect(screen.getByTestId("probe")).toHaveTextContent("uno");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("never lets a slower, older response overwrite a newer one", async () => {
    const slow = deferred();
    let calls = 0;
    const fetcher = vi.fn(() => { calls += 1; return calls === 1 ? slow.promise : Promise.resolve("nuevo"); });
    render(<Probe resourceKey="k2" fetcher={fetcher} />);
    await act(async () => { await invalidateCampaignResources("k2"); });
    expect(screen.getByTestId("probe")).toHaveTextContent("nuevo");
    await act(async () => { slow.resolve("viejo"); });
    expect(screen.getByTestId("probe")).toHaveTextContent("nuevo");
  });

  it("keeps the last data on a failed refresh and supports optimistic updates", async () => {
    let fail = false;
    const fetcher = vi.fn(async () => { if (fail) throw new Error("caído"); return "ok"; });
    render(<Probe resourceKey="k3" fetcher={fetcher} />);
    await screen.findByText("ok");
    act(() => mutateCampaignResource("k3", (value) => `${value}!`));
    expect(screen.getByTestId("probe")).toHaveTextContent("ok!");
    fail = true;
    await act(async () => { await invalidateCampaignResources("k3"); });
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("error:caído"));
  });

  it("drops cached data when access is lost", async () => {
    let status = 0;
    const fetcher = vi.fn(async () => { if (status) throw Object.assign(new Error("sin acceso"), { status }); return "privado"; });
    render(<Probe resourceKey="k5" fetcher={fetcher} />);
    await screen.findByText("privado");
    status = 404;
    await act(async () => { await invalidateCampaignResources("k5"); });
    expect(screen.getByTestId("probe")).toHaveTextContent("error:sin acceso");
    expect(screen.queryByText("privado")).not.toBeInTheDocument();
  });

  it("does not fetch while disabled", () => {
    const fetcher = vi.fn(async () => "x");
    function Disabled() { useCampaignResource("k4", fetcher, { enabled: false }); return null; }
    render(<Disabled />);
    expect(fetcher).not.toHaveBeenCalled();
  });
});
