import { describe, expect, it } from "vitest";

import {
  requestKind,
  requestSearchText,
  requestWorkflow,
  workflowCounts,
  workflowMatchesFilter,
} from "./changeRequestWorkflow";

describe("change request workflow", () => {
  it("prioritizes rendering and publication over proposal state", () => {
    const item = {
      proposal: { status: "ready" },
      publication: { job_status: "rendering", needs_publish: true },
    };
    expect(requestWorkflow(item, null, true)).toMatchObject({
      key: "rendering",
      activeStep: 2,
    });

    item.publication = { job_status: "done", needs_publish: true };
    expect(requestWorkflow(item, null, true)).toMatchObject({
      key: "publish",
      activeStep: 4,
    });
  });

  it("groups actionable, rendering and review stages for the queue", () => {
    const items = [
      { id: 1, publication: {} },
      { id: 2, publication: { job_status: "processing" } },
      { id: 3, proposal: { status: "applied" }, publication: { needs_publish: true } },
      { id: 4, resolved_at: "2026-09-17T00:00:00Z", publication: {} },
    ];
    expect(workflowCounts(items, {}, true)).toEqual({
      all: 4,
      action: 1,
      rendering: 1,
      review: 1,
    });
    expect(workflowMatchesFilter(requestWorkflow(items[0]), "action")).toBe(true);
    expect(workflowMatchesFilter(requestWorkflow(items[3]), "action")).toBe(false);
  });

  it("does not mistake a pending master for a handled request without a proposal", () => {
    expect(requestWorkflow({ publication: {
      job_status: "done", prores_pending: ["umg_master"], needs_publish: true,
    } }, null, true)).toMatchObject({ key: "analyze", activeStep: 0 });
  });

  it("classifies requests and searches delivery metadata plus comments", () => {
    const item = {
      comment: "El fondo debería cambiar en el segundo 12",
      delivery: {
        artist: "Artista Prueba",
        song: "Canción Única",
        job_id: "abc123",
        owner_email: "operador@example.com",
      },
    };
    expect(requestKind(item)).toBe("Fondo");
    const searchText = requestSearchText(item);
    expect(searchText).toContain("artista prueba");
    expect(searchText).toContain("canción única");
    expect(searchText).toContain("abc123");
    expect(searchText).toContain("operador@example.com");
  });
});
// A finished legacy cut must not enter the editor/render loop again.
it('offers publication after an exact reviewed render, even for an applied proposal', () => {
  const state = requestWorkflow({ proposal: { status: 'applied' }, publication: {
    job_status: 'pending_review', render_matches_editor: true, needs_publish: true,
  } });
  expect(state.key).toBe('publish');
  expect(state.activeStep).toBe(3);
});
