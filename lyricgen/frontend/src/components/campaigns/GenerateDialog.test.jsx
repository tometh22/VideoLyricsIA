import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GenerateDialog from "./GenerateDialog";
import { campaignRequest } from "../../lib/campaignApi";
vi.mock("../../lib/campaignApi", () => ({ campaignRequest: vi.fn() }));
const items = [{ id: "i1", job_id: "j1", title: "Tema uno", settings: { effect: "bokeh" } }, { id: "i2", job_id: "j2", title: "Tema dos", settings: { effect: "embers" } }];
beforeEach(() => {
  vi.clearAllMocks();
  campaignRequest.mockImplementation(async path => path.startsWith("/status/") ? { segments_revision: 7, segments_json: [{ start: 0, end: 2, text: "Aprobada" }] } : { status: "queued" });
});
afterEach(cleanup);
describe("generation confirmation", () => {
  it("lets the operator change the background without starting generation", () => {
    const change = vi.fn();
    render(<GenerateDialog items={items} onClose={() => {}} onChangeStyle={change} />);
    fireEvent.click(screen.getByRole("button", { name: "Cambiar fondo" }));
    expect(change).toHaveBeenCalledOnce();
    expect(campaignRequest).not.toHaveBeenCalled();
  });
  it("starts an already confirmed batch once under Strict Mode with no second confirmation", async () => {
    const done = vi.fn();
    render(<StrictMode><GenerateDialog items={items} startImmediately onClose={() => {}} onDone={done} /></StrictMode>);
    expect(screen.queryByRole("button", { name: "Confirmar generación" })).not.toBeInTheDocument();
    await waitFor(() => expect(done).toHaveBeenCalledOnce());
    const generations = campaignRequest.mock.calls.filter(([path]) => path === "/generate");
    expect(generations).toHaveLength(2);
    expect(generations.map(([, request]) => request.body.get("job_id"))).toEqual(["j1", "j2"]);
    expect(done.mock.calls[0][0].sent).toBe(2);
  });
  it("stops at capacity and retains unsent songs in the result", async () => {
    const done = vi.fn();
    campaignRequest.mockImplementation(async path => {
      if (path === "/generate") throw Object.assign(new Error("Cola completa"), { status: 429, code: "batch_render_window_full" });
      return { segments_json: [], segments_revision: 1 };
    });
    render(<GenerateDialog items={items} startImmediately onClose={() => {}} onDone={done} />);
    await waitFor(() => expect(done).toHaveBeenCalledOnce());
    expect(done.mock.calls[0][0]).toMatchObject({ sent: 0, deferred: items, failures: [] });
    expect(campaignRequest.mock.calls.filter(([path]) => path === "/generate")).toHaveLength(1);
  });
});
