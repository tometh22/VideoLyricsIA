import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import useCampaignResource, { clearCampaignResourceCache, invalidateCampaignResources, mutateCampaignResource } from "./useCampaignResource";

function Probe({ resourceKey, fetcher, label = "probe" }) {
  const { data, loading, error, reload } = useCampaignResource(resourceKey, fetcher, { pollMs: 0 });
  return <><p data-testid={label}>{loading ? "loading" : error ? `error:${error.message}` : data}</p><button onClick={reload}>refresh</button></>;
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

  it("coalesces mutation invalidations and waits for a snapshot requested after the mutation", async () => {
    const slow = deferred();
    let calls = 0;
    const fetcher = vi.fn(() => { calls += 1; return calls === 1 ? slow.promise : Promise.resolve("nuevo"); });
    render(<Probe resourceKey="k2" fetcher={fetcher} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    let refreshes;
    act(() => { refreshes = Promise.all([invalidateCampaignResources("k2"), invalidateCampaignResources("k2")]); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => { slow.resolve("viejo"); await refreshes; });
    expect(screen.getByTestId("probe")).toHaveTextContent("nuevo");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("shares concurrent refreshes without aborting the server request", async () => {
    const pending = deferred();
    let signal;
    const fetcher = vi.fn(options => { signal = options.signal; return pending.promise; });
    render(<Probe resourceKey="shared" fetcher={fetcher} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    act(() => { screen.getByRole("button", { name: "refresh" }).click(); screen.getByRole("button", { name: "refresh" }).click(); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(signal.aborted).toBe(false);
    await act(async () => pending.resolve("done"));
    expect(screen.getByTestId("probe")).toHaveTextContent("done");
  });

  it("does not revalidate a fresh snapshot when the page regains visibility", async () => {
    const fetcher = vi.fn(async () => "fresh");
    render(<Probe resourceKey="visible" fetcher={fetcher} />);
    await screen.findByText("fresh");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(fetcher).toHaveBeenCalledTimes(1);
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
