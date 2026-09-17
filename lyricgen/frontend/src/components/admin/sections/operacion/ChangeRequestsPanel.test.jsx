import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChangeRequestsPanel, {
  buildLyricsPreview,
  publicationStatus,
} from "./ChangeRequestsPanel";

afterEach(cleanup);

const BASE_PUBLICATION = {
  revision: 1,
  content_updated_at: null,
  approved_at: null,
  approved_by_label: null,
  stale_since: null,
  stale_reason: null,
  needs_publish: false,
  prores_pending: [],
  awaiting_review: false,
  job_status: "done",
};

const REQUEST = {
  id: 7,
  comment: "0:13 sin signos de interrogación",
  submitted_at: "2026-09-15T17:12:12Z",
  resolved_at: null,
  publication: BASE_PUBLICATION,
  delivery: {
    id: 289,
    artist: "Silvia Infantas y Los Baqueanos",
    song: "Yo vengo de San Rosendo",
    label: "Campaña",
    job_id: "f7752c6feed4",
    portal_id: "chile",
  },
};

function renderPanel(overrides = {}, props = {}) {
  const item = { ...REQUEST, ...overrides };
  render(
    <ChangeRequestsPanel
      changeRequests={[item]}
      crStatusFilter="pending"
      setCrStatusFilter={() => {}}
      crPendingCount={1}
      crResolvedCount={0}
      crLoading={false}
      crResolvingId={null}
      resolveChangeRequest={() => {}}
      reopenChangeRequest={() => {}}
      crPublishingId={null}
      crPublishNotice={null}
      dismissPublishNotice={() => {}}
      publishDeliveryUpdate={() => {}}
      {...props}
    />,
  );
  return item;
}

// El orden de prioridad es el orden en que los estados bloquean al operador.
describe("publicationStatus", () => {
  it("blocks publishing while the job is still re-rendering", () => {
    const status = publicationStatus({
      ...BASE_PUBLICATION, job_status: "editing", needs_publish: true,
    });
    expect(status.canPublish).toBe(false);
    expect(status.title).toMatch(/Re-renderizando/);
  });

  it("warns that the broadcast master is still the previous cut", () => {
    // El caso de producción del 2026-09-15: el MP4 ya está corregido y el
    // .mov PRE-EDIT sigue descargable en la misma key.
    const status = publicationStatus({
      ...BASE_PUBLICATION, needs_publish: true, prores_pending: ["umg_master"],
    });
    expect(status.title).toMatch(/ProRes todavía es el corte anterior/);
    // Publicar SÍ se ofrece: encola el master y el backend contesta 202.
    expect(status.canPublish).toBe(true);
    expect(status.publishLabel).toMatch(/Preparar master/);
  });

  it("surfaces a corrected render that was never published", () => {
    const status = publicationStatus({ ...BASE_PUBLICATION, needs_publish: true });
    expect(status.title).toMatch(/render nuevo está listo para revisar/i);
    expect(status.detail).toMatch(/Abrí el video/);
    expect(status.canPublish).toBe(true);
  });

  it("does not offer publishing when the portal already has this cut", () => {
    expect(publicationStatus(BASE_PUBLICATION).canPublish).toBe(false);
  });

  it("reports a published version the client has not reviewed", () => {
    const status = publicationStatus({
      ...BASE_PUBLICATION, revision: 2,
      content_updated_at: "2026-09-15T18:00:00Z", awaiting_review: true,
    });
    expect(status.title).toMatch(/Versión 2 publicada · esperando al cliente/);
  });

  it("reports the client's approval once it arrives", () => {
    const status = publicationStatus({
      ...BASE_PUBLICATION, revision: 2,
      content_updated_at: "2026-09-15T18:00:00Z",
      approved_at: "2026-09-15T19:00:00Z", approved_by_label: "UMG",
    });
    expect(status.title).toMatch(/aprobada por UMG/);
  });
});

describe("buildLyricsPreview", () => {
  it("applies only selected text operations to the full lyric snapshot", () => {
    const segments = [
      { _id: "a", start: 0, end: 2, text: "Primera línea" },
      { _id: "b", start: 2, end: 4, text: "Padre Fahey" },
      { _id: "c", start: 4, end: 6, text: "Última línea" },
    ];
    const operations = [{
      id: "op-1", status: "pending", applicable: true,
      current_segments: [segments[1]],
      proposed_segments: [{ ...segments[1], text: "padre fhay" }],
    }];
    const preview = buildLyricsPreview(segments, operations, ["op-1"]);
    expect(preview.map((row) => row.resultText)).toEqual([
      "Primera línea", "padre fhay", "Última línea",
    ]);
    expect(preview.filter((row) => row.changed)).toHaveLength(1);
    expect(buildLyricsPreview(segments, operations, [])[1].resultText)
      .toBe("Padre Fahey");
  });

  it("collapses an exact multi-segment phrase into one preview row", () => {
    const segments = [
      { _id: "a", start: 1, end: 2, text: "Borracho" },
      { _id: "b", start: 2, end: 4, text: "y agresivo" },
      { _id: "c", start: 4, end: 6, text: "Después" },
    ];
    const operations = [{
      id: "merge-1", kind: "merge_phrase", status: "pending", applicable: true,
      current_segments: segments.slice(0, 2),
      proposed_segments: [{ _id: "a", start: 1, end: 4, text: "Borracho y agresivo" }],
    }];
    const preview = buildLyricsPreview(segments, operations, ["merge-1"]);
    expect(preview).toHaveLength(2);
    expect(preview[0]).toMatchObject({
      currentText: "Borracho / y agresivo",
      resultText: "Borracho y agresivo",
      changed: true,
    });
  });
});

describe("ChangeRequestsPanel", () => {
  it("offers the two steps that actually answer the request", () => {
    renderPanel({ publication: { ...BASE_PUBLICATION, needs_publish: true } });
    expect(screen.getByText("Editar letra")).toHaveAttribute(
      "href", "/videos/f7752c6feed4/edit-lyrics",
    );
    expect(screen.getByRole("button", { name: "Publicar actualización" }))
      .toBeEnabled();
  });

  it("publishes to the portal the delivery belongs to", () => {
    const publish = vi.fn();
    renderPanel(
      { publication: { ...BASE_PUBLICATION, needs_publish: true } },
      { publishDeliveryUpdate: publish },
    );
    screen.getByRole("button", { name: "Publicar actualización" }).click();
    expect(publish).toHaveBeenCalledWith("f7752c6feed4", "chile", 7);
  });

  it("keeps manual resolution available but names it for what it is", () => {
    renderPanel();
    // Marcar resuelto sin publicar no cambia el archivo del cliente: la
    // etiqueta lo dice, para que no se use como si lo hiciera.
    expect(
      screen.getByRole("button", { name: "Marcar resuelto sin publicar" }),
    ).toBeInTheDocument();
  });

  it("explains an auto-closed request instead of crediting an operator", () => {
    renderPanel({
      resolved_at: "2026-09-15T18:30:00Z",
      resolved_by_revision: 2,
      resolution_source: "publication",
      resolved_by: "admin",
      publication: { ...BASE_PUBLICATION, revision: 2 },
    });
    expect(screen.getByText(/Resuelto al publicar la versión 2/))
      .toBeInTheDocument();
  });

  it("shows how long the client has been looking at a version in flux", () => {
    renderPanel({
      publication: {
        ...BASE_PUBLICATION,
        stale_since: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
        stale_reason: "editing",
        job_status: "editing",
      },
    });
    expect(screen.getByText(/Cambios en curso desde hace 20 min/))
      .toBeInTheDocument();
  });

  it("offers deterministic analysis when the assist flag is enabled", () => {
    const generate = vi.fn();
    renderPanel({}, { proposalEnabled: true, generateProposal: generate });
    screen.getByRole("button", { name: "Analizar pedido" }).click();
    expect(generate).toHaveBeenCalledWith(7);
  });

  it("loads a persisted proposal summary instead of regenerating it", () => {
    const load = vi.fn();
    renderPanel(
      { proposal: { id: "proposal-1", status: "ready", applicable_count: 2 } },
      { proposalEnabled: true, loadProposal: load },
    );
    screen.getByRole("button", { name: "Ver propuesta" }).click();
    expect(load).toHaveBeenCalledWith(7);
  });

  it("applies only the selected revision-bound operations", async () => {
    const apply = vi.fn();
    const proposal = {
      id: "proposal-1",
      status: "ready",
      base_revision: 4,
      updated_at: "2026-09-16T00:00:00Z",
      lyrics_context: {
        revision: 4,
        matches_base: true,
        segments: [
          { _id: "before", start: 10, end: 12, text: "Línea anterior" },
          { _id: "target", start: 13, end: 15, text: "Texto viejo" },
          { _id: "after", start: 16, end: 18, text: "Línea siguiente" },
        ],
      },
      operations: [{
        id: "op-1",
        kind: "replace_text",
        status: "pending",
        applicable: true,
        scope: "single",
        current_segments: [{ _id: "target", start: 13, end: 15, text: "Texto viejo" }],
        proposed_segments: [{ _id: "target", start: 13, end: 15, text: "Texto correcto" }],
      }],
    };
    renderPanel({}, {
      proposalEnabled: true,
      proposalApplyEnabled: true,
      proposalDetails: { 7: proposal },
      applyProposal: apply,
    });
    const button = await screen.findByRole("button", { name: "Aplicar seleccionadas (1)" });
    await waitFor(() => expect(button).toBeEnabled());
    const preview = screen.getByLabelText("Vista previa de la letra resultante");
    expect(preview).toHaveTextContent("Pedido original");
    expect(preview).toHaveTextContent(REQUEST.comment);
    expect(preview).toHaveTextContent("Línea anterior");
    expect(preview).toHaveTextContent("Texto viejo");
    expect(preview).toHaveTextContent("Texto correcto");
    expect(preview).toHaveTextContent("Línea siguiente");

    fireEvent.click(screen.getByLabelText("Seleccionar cambio: Texto viejo"));
    expect(preview).toHaveTextContent("0 cambio(s) seleccionado(s)");
    expect(screen.getByRole("button", { name: "Aplicar seleccionadas (0)" }))
      .toBeDisabled();
    fireEvent.click(screen.getByLabelText("Seleccionar cambio: Texto viejo"));
    button.click();
    expect(apply).toHaveBeenCalledWith(7, "proposal-1", ["op-1"], 4);
  });

  it("keeps timing instructions review-only", () => {
    renderPanel({}, {
      proposalEnabled: true,
      proposalApplyEnabled: true,
      proposalDetails: { 7: {
        id: "proposal-2", status: "needs_input", base_revision: 4,
        operations: [{
          id: "manual-1", kind: "timing_review", status: "pending",
          applicable: false, timecode_seconds: 83,
        }],
      } },
    });
    expect(screen.getByText("Revisar timing en el editor")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Aplicar seleccionadas/ }))
      .not.toBeInTheDocument();
  });

  it("offers a direct background-editor action for visual requests", () => {
    renderPanel({}, {
      proposalEnabled: true,
      proposalDetails: { 7: {
        id: "proposal-bg", status: "needs_input", base_revision: 4,
        operations: [{
          id: "manual-bg", kind: "background_review", status: "pending",
          applicable: false,
        }],
      } },
    });
    expect(screen.getByRole("link", { name: "Abrir editor de fondo" }))
      .toHaveAttribute("href", "/videos/f7752c6feed4/edit-lyrics");
  });

  it("shows an editable visual prompt and regenerates without publishing", () => {
    const regenerate = vi.fn();
    renderPanel({}, {
      proposalEnabled: true,
      proposalDetails: { 7: {
        id: "proposal-bg", status: "ready", base_revision: 4,
        operations: [{
          id: "visual-bg", kind: "background_review", status: "pending",
          applicable: false,
          visual_action: "regenerate_background",
          regeneration_supported: true,
          current_prompt: "Calle nocturna con autos",
          suggested_prompt: "Nueva calle nocturna, sin armas, sin texto ni logos.",
          background_mode: "veo",
        }],
      } },
      regenerateBackground: regenerate,
    });

    expect(screen.getByText("Prompt usado hasta ahora")).toBeInTheDocument();
    const prompt = screen.getByLabelText("Prompt sugerido para el fondo");
    expect(prompt).toHaveValue("Nueva calle nocturna, sin armas, sin texto ni logos.");
    fireEvent.change(prompt, {
      target: { value: "Barrio al amanecer, sin armas, sin texto ni logos." },
    });
    fireEvent.click(screen.getByRole("button", {
      name: "Regenerar fondo con este prompt",
    }));

    expect(regenerate).toHaveBeenCalledWith(
      7,
      "proposal-bg",
      "visual-bg",
      "f7752c6feed4",
      "Barrio al amanecer, sin armas, sin texto ni logos.",
      "veo",
    );
    expect(screen.queryByRole("button", { name: /Aplicar seleccionadas/ }))
      .not.toBeInTheDocument();
  });

  it("previews operator drafts in context and blocks apply until they are saved", () => {
    const adjust = vi.fn();
    const segment = { _id: "target", start: 13, end: 15, text: "Texto viejo" };
    renderPanel({}, {
      proposalEnabled: true,
      proposalApplyEnabled: true,
      proposalDetails: { 7: {
        id: "proposal-3", status: "ready", base_revision: 4,
        updated_at: "2026-09-16T00:00:00Z",
        lyrics_context: {
          revision: 4, matches_base: true,
          segments: [segment],
        },
        operations: [{
          id: "op-1", kind: "replace_text", status: "pending",
          applicable: true, scope: "single",
          current_segments: [segment],
          proposed_segments: [{ ...segment, text: "Texto correcto" }],
        }],
      } },
      adjustProposal: adjust,
    });

    fireEvent.change(screen.getByDisplayValue("Texto correcto"), {
      target: { value: "Texto corregido por operador" },
    });
    const preview = screen.getByLabelText("Vista previa de la letra resultante");
    expect(preview).toHaveTextContent("Texto corregido por operador");
    expect(screen.getByText(/Guardá los ajustes de texto/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aplicar seleccionadas (1)" }))
      .toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(adjust).toHaveBeenCalledWith(
      7, "proposal-3", "op-1", "Texto corregido por operador", 4,
    );
  });
});
