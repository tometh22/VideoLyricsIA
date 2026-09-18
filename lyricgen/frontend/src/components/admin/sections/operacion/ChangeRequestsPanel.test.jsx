import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChangeRequestsPanel, {
  appliedTextChecks,
  buildLyricsPreview,
  editorUrlWithRequest,
  publicationStatus,
} from "./ChangeRequestsPanel";

vi.mock("../../../../i18n", () => ({
  useI18n: () => ({ t: (key) => key }),
}));

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/admin");
});

const BASE_PUBLICATION = {
  revision: 1,
  content_updated_at: null,
  approved_at: null,
  approved_by_label: null,
  stale_since: null,
  stale_reason: null,
  needs_publish: false,
  prores_pending: [],
  prores_configured: true,
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
  renderPanelItems([item], props);
  return item;
}

function renderPanelItems(items, props = {}) {
  render(
    <ChangeRequestsPanel
      changeRequests={items}
      crStatusFilter="pending"
      setCrStatusFilter={() => {}}
      crPendingCount={items.filter((item) => !item.resolved_at).length}
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
}

// El orden de prioridad es el orden en que los estados bloquean al operador.
describe("publicationStatus", () => {
  it.each([null, {}, { job_status: "done" }])("does not invent a published revision from %j", publication => {
    expect(publicationStatus(publication)).toMatchObject({ canPublish: false, title: "Publicación por verificar" });
    expect(publicationStatus(publication).detail).not.toContain("mismo corte");
  });
  it("verifies saved corrections when editor snapshots regenerate local row ids", () => {
    const expected = { _id: "old-local-id", start: 70.12, end: 74.2, text: "Soy quien ayer cantó sé vos" };
    const proposal = { operations: [{ status: "applied", proposed_segments: [expected] }],
      lyrics_context: { segments: [{ ...expected, _id: "new-local-id" }] } };
    expect(appliedTextChecks(proposal)[0]).toMatchObject({ located: true, matches: true });
    proposal.lyrics_context.segments[0].text = "Texto anterior";
    expect(appliedTextChecks(proposal)[0]).toMatchObject({ located: true, matches: false });
  });

  it("does not verify a different occurrence or ambiguous timing just because text matches", () => {
    const expected = { _id: "old", start: 70, end: 74, text: "Frase repetida" };
    const proposal = { operations: [{ status: "applied", proposed_segments: [expected] }],
      lyrics_context: { segments: [{ ...expected, _id: "other", start: 170, end: 174 }] } };
    expect(appliedTextChecks(proposal)[0].located).toBe(false);
    proposal.lyrics_context.segments = [{ ...expected, _id: "new1" }, { ...expected, _id: "new2" }];
    expect(appliedTextChecks(proposal)[0].located).toBe(false);
  });
  it("keeps analysis accessible beside the original request when a master is pending", () => {
    const generate = vi.fn();
    const publish = vi.fn();
    renderPanel({ publication: { ...BASE_PUBLICATION, prores_pending: ["umg_master"] } }, {
      proposalEnabled: true, generateProposal: generate, publishDeliveryUpdate: publish,
    });
    fireEvent.click(screen.getByRole("button", { name: "Analizar este pedido" }));
    expect(generate).toHaveBeenCalledWith(7);
    fireEvent.click(screen.getByRole("button", { name: "Analizar pedido" }));
    expect(generate).toHaveBeenCalledTimes(2);
    expect(publish).not.toHaveBeenCalled();
    expect(screen.queryByText("El video de arriba ya tiene la corrección", { exact: false })).toBeNull();
  });
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
    expect(status.title).toMatch(/archivo profesional/);
    // Publicar SÍ se ofrece: encola el master y el backend contesta 202.
    expect(status.canPublish).toBe(true);
    expect(status.publishLabel).toMatch(/Actualizar archivo profesional/);
  });

  it("asks for the missing ProRes format on a legacy delivery", () => {
    const status = publicationStatus({
      ...BASE_PUBLICATION,
      needs_publish: true,
      prores_pending: ["umg_master"],
      prores_configured: false,
    });
    expect(status.needsProResSetup).toBe(true);
    expect(status.publishLabel).toMatch(/Elegir formato/);
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

describe("editorUrlWithRequest", () => {
  it("restores the proposal id after the queue is reloaded", () => {
    expect(editorUrlWithRequest(
      "f7752c6feed4", 7, "proposal-1",
      "/videos/f7752c6feed4/edit-lyrics?change_request_id=7",
    )).toBe(
      "/videos/f7752c6feed4/edit-lyrics?change_request_id=7&proposal_id=proposal-1",
    );
  });

  it("preserves unrelated query parameters and anchors", () => {
    expect(editorUrlWithRequest(
      "f7752c6feed4", 7, "proposal-1",
      "/videos/f7752c6feed4/edit-lyrics?source=admin#lyrics",
    )).toBe(
      "/videos/f7752c6feed4/edit-lyrics?source=admin&change_request_id=7&proposal_id=proposal-1#lyrics",
    );
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
  it("uses a compact queue and preserves the selected request in the URL", () => {
    const second = {
      ...REQUEST,
      id: 8,
      comment: "Cambiar el fondo a una calle al amanecer",
      delivery: { ...REQUEST.delivery, song: "Otra canción", artist: "Otra banda" },
    };
    renderPanelItems([REQUEST, second]);

    expect(screen.getByRole("heading", { name: REQUEST.delivery.song })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /Otra canción/ }));
    expect(screen.getByRole("heading", { name: "Otra canción" })).toBeInTheDocument();
    expect(window.location.search).toContain("change_request_id=8");
    expect(screen.getByRole("option", { name: /Otra canción/ })).toHaveAttribute(
      "aria-selected", "true",
    );
  });

  it("deep-links a request and confirms a render submitted from the editor", () => {
    const second = {
      ...REQUEST,
      id: 8,
      delivery: { ...REQUEST.delivery, song: "Pedido retornado" },
    };
    window.history.replaceState({}, "", "/admin?section=cambios&change_request_id=8&render_submitted=1");
    renderPanelItems([REQUEST, second]);

    expect(screen.getByRole("heading", { name: "Pedido retornado" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/render corregido fue enviado/i);
    expect(window.location.search).not.toContain("render_submitted");
  });

  it("searches the queue and supports J/K navigation", () => {
    const second = {
      ...REQUEST,
      id: 8,
      comment: "Cambiar fondo",
      delivery: { ...REQUEST.delivery, song: "Tema nocturno", artist: "Los Test" },
    };
    renderPanelItems([REQUEST, second]);
    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByRole("heading", { name: "Tema nocturno" })).toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Buscar pedidos" });
    fireEvent.change(search, { target: { value: "San Rosendo" } });
    expect(screen.queryByRole("option", { name: /Tema nocturno/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: REQUEST.delivery.song })).toBeInTheDocument();
  });

  it("keeps the editor available while publishing is the primary action", () => {
    renderPanel({ publication: { ...BASE_PUBLICATION, needs_publish: true } });
    expect(screen.getByText("Editar letra")).toHaveAttribute(
      "href", "/videos/f7752c6feed4/edit-lyrics?change_request_id=7",
    );
    expect(screen.getByRole("button", { name: "Publicar actualización" }))
      .toBeEnabled();
  });

  it("keeps the request context when the editor is the primary action", () => {
    renderPanel();
    expect(screen.getByRole("link", { name: "Editar letra" })).toHaveAttribute(
      "href", "/videos/f7752c6feed4/edit-lyrics?change_request_id=7",
    );
  });

  it("keeps the applied proposal context after reloading the request queue", () => {
    const load = vi.fn();
    renderPanel(
      { proposal: { id: "proposal-1", status: "applied", applied_revision: 5 } },
      { proposalEnabled: true, loadProposal: load },
    );
    expect(screen.getByRole("button", { name: "Revisar y confirmar render" })).toBeEnabled();
    expect(screen.getByRole("link", { name: "Editar letra" }))
      .toHaveAttribute(
        "href",
        "/videos/f7752c6feed4/edit-lyrics?change_request_id=7&proposal_id=proposal-1",
      );
    fireEvent.click(screen.getByRole("button", { name: "Ver propuesta y letra guardada" }));
    expect(load).toHaveBeenCalledWith(7);
  });

  it("updates a configured master without invoking publication", () => {
    const prepare = vi.fn();
    const publish = vi.fn();
    renderPanel({ publication: { ...BASE_PUBLICATION, job_status: "pending_review", prores_pending: ["umg_master"] } },
      { prepareProRes: prepare, publishDeliveryUpdate: publish,
        crPublishNotice: { requestId: 7, tone: "error", text: "La cola no está disponible" } });
    fireEvent.click(screen.getByRole("button", { name: "Actualizar archivo profesional" }));
    expect(prepare).toHaveBeenCalledWith("f7752c6feed4", 7);
    expect(publish).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").closest("footer")).not.toBeNull();
  });

  it.each([true, false])("checks the actual stored text for an applied proposal (matches=%s)", matches => {
    const proposed = { _id: "line-1", start: 72, end: 77, text: "Soy quien ayer cantó sé vos" };
    const proposal = { id: "proposal-1", status: "applied", base_revision: 2, applied_revision: 3,
      operations: [{ id: "op-1", kind: "replace_text", applicable: true, status: "applied",
        current_segments: [{ ...proposed, text: "Texto anterior" }], proposed_segments: [proposed] }],
      lyrics_context: { revision: 4, segments: [{ ...proposed, text: matches ? proposed.text : "Texto anterior" }] } };
    const generate = vi.fn();
    renderPanel({ proposal }, { proposalEnabled: true, proposalDetails: { 7: proposal }, generateProposal: generate });
    expect(screen.getByRole("region", { name: "Verificación de la letra guardada" }))
      .toHaveTextContent(matches ? "Coincide con el pedido" : "No coincide con el pedido");
    const recalculate = screen.getByRole("button", { name: "Volver a analizar con la letra actual" });
    if (matches) expect(recalculate).toBeDisabled();
    else { fireEvent.click(recalculate); expect(generate).toHaveBeenCalledWith(7); }
  });

  it("publishes to the portal the delivery belongs to", () => {
    const publish = vi.fn();
    renderPanel(
      { publication: { ...BASE_PUBLICATION, needs_publish: true } },
      { publishDeliveryUpdate: publish },
    );
    fireEvent.click(screen.getByRole("button", { name: "Publicar actualización" }));
    expect(publish).toHaveBeenCalledWith("f7752c6feed4", "chile", 7, expect.objectContaining({ needs_publish: true }));
  });

  it("opens the format selector instead of attempting an impossible legacy publish", async () => {
    const publish = vi.fn();
    renderPanel(
      {
        publication: {
          ...BASE_PUBLICATION,
          needs_publish: true,
          prores_pending: ["umg_master"],
          prores_configured: false,
        },
      },
      { publishDeliveryUpdate: publish },
    );
    fireEvent.click(screen.getByRole("button", { name: "Elegir formato y actualizar .mov" }));
    expect(await screen.findByRole("dialog", {
      name: "Configurar y actualizar el archivo profesional",
    })).toBeInTheDocument();
    expect(publish).not.toHaveBeenCalled();
  });

  it("keeps manual resolution available but names it for what it is", () => {
    renderPanel();
    // Marcar resuelto sin publicar no cambia el archivo del cliente: la
    // etiqueta lo dice, para que no se use como si lo hiciera.
    expect(
      screen.getByRole("button", { name: "Marcar como resuelto" }),
    ).toBeInTheDocument();
  });

  it("requires a manual-close motive and ignores the global shortcut inside that field", () => {
    const resolve = vi.fn();
    const generate = vi.fn();
    renderPanel({}, { resolveChangeRequest: resolve, proposalEnabled: true, generateProposal: generate });
    const close = screen.getByRole("button", { name: "Marcar como resuelto" });
    expect(close).toBeDisabled();
    const reason = screen.getByLabelText("Motivo del cierre sin publicar");
    fireEvent.change(reason, { target: { value: "Cliente confirmó que no requiere otro video" } });
    fireEvent.keyDown(reason, { key: "Enter", ctrlKey: true });
    expect(generate).not.toHaveBeenCalled();
    expect(resolve).not.toHaveBeenCalled();
    expect(close).toBeEnabled();
    fireEvent.click(close);
    expect(resolve).toHaveBeenCalledWith(7, "Cliente confirmó que no requiere otro video");
  });

  it("does not override server action restrictions with a legacy publication flag", () => {
    const refresh = vi.fn();
    renderPanel({ publication: { ...BASE_PUBLICATION, needs_publish: true },
      workflow: { key: "unknown", activeStep: 0, label: "Falta verificar", detail: "Actualizá", tone: "attention", allowed_actions: ["refresh"] },
    }, { refreshChangeRequests: refresh });
    expect(screen.queryByRole("button", { name: "Publicar actualización" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Actualizar estado" }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("links the published snapshot separately from the candidate video", () => {
    renderPanel({ delivery: { ...REQUEST.delivery, video_url: "/candidate.mp4", published_video_url: "/published/v2.mp4", published_revision: 2 } });
    expect(screen.getByRole("link", { name: "Ver versión publicada" })).toHaveAttribute("href", "/published/v2.mp4");
    expect(screen.getByLabelText(`Video de ${REQUEST.delivery.artist} — ${REQUEST.delivery.song}`)).toHaveAttribute("src", "/candidate.mp4");
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
    fireEvent.click(screen.getByRole("button", { name: "Analizar pedido" }));
    expect(generate).toHaveBeenCalledWith(7);
  });

  it("loads a persisted proposal summary instead of regenerating it", () => {
    const load = vi.fn();
    renderPanel(
      { proposal: { id: "proposal-1", status: "ready", applicable_count: 2 } },
      { proposalEnabled: true, loadProposal: load },
    );
    fireEvent.click(screen.getByRole("button", { name: "Ver propuesta" }));
    expect(load).toHaveBeenCalledWith(7);
  });

  it("applies only the selected revision-bound operations", async () => {
    const apply = vi.fn();
    const proposal = {
      id: "proposal-1",
      content_hash: "hash-preview-1",
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
    fireEvent.click(button);
    expect(apply).toHaveBeenCalledWith(7, "proposal-1", ["op-1"], 4, "hash-preview-1");
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
      .toHaveAttribute(
        "href", "/videos/f7752c6feed4/edit-lyrics?change_request_id=7",
      );
  });

  it.each([
    ["unresolved_visual_constraint", /restricción visual que necesita aclaración/],
    ["background_constraints_exceed_prompt_limit", /condiciones obligatorias superan el límite/],
    ["unresolved_request_requires_review", /partes del pedido que no se pudieron interpretar/],
    ["future_unknown_reason", /requiere revisión manual antes de regenerar/],
  ])("explains a disabled background action for %s without inventing multiple scenes", (reason, message) => {
    renderPanel({}, { proposalEnabled: true, proposalDetails: { 7: {
      id: "p-background", status: "needs_input", base_revision: 4,
      operations: [{ id: "bg", kind: "background_review", applicable: false,
        visual_action: "regenerate_background", regeneration_supported: false,
        suggested_prompt: "Pedido completo", warnings: [reason] }],
    } } });
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByText(/Este video usa varias escenas/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir editor de fondo" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Regenerar fondo con este prompt" })).not.toBeInTheDocument();
  });

  it("shows every blocking visual reason and reserves the scene editor for real multiscene work", () => {
    renderPanel({}, { proposalEnabled: true, proposalDetails: { 7: {
      id: "p-background", status: "needs_input", base_revision: 4,
      operations: [{ id: "bg", kind: "background_review", applicable: false,
        visual_action: "regenerate_background", regeneration_supported: false,
        warnings: ["multi_scene_background_requires_scene_editor", "unresolved_visual_constraint"] }],
    } } });
    expect(screen.getByText(/Este video usa varias escenas/)).toBeInTheDocument();
    expect(screen.getByText(/restricción visual que necesita aclaración/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir editor de escenas" })).toBeInTheDocument();
  });

  it("presents already satisfied text as a textual check, not a failed manual action or published video", () => {
    renderPanel({}, { proposalEnabled: true, proposalDetails: { 7: {
      id: "p-satisfied", status: "ready", base_revision: 4,
      operations: [{ id: "text-ok", kind: "manual_review", status: "already_satisfied", applicable: false,
        verified_segments: [{ _id: "s1", text: "Texto exacto solicitado", start: 3, end: 5 }] }],
    } } });
    expect(screen.getByText("Ya coincide en la letra guardada")).toBeInTheDocument();
    expect(screen.getByText("Texto exacto solicitado")).toBeInTheDocument();
    expect(screen.getByText(/No confirma el render ni la publicación/)).toBeInTheDocument();
    expect(screen.queryByText("Interpretación manual requerida")).not.toBeInTheDocument();
  });

  it.each([
    ['stale', true], ['ready', false], ['ready', undefined], ['applied', true],
  ])('blocks background generation for status %s and preview match %s', (status, matches) => {
    const regenerate = vi.fn();
    renderPanel({}, { proposalEnabled: true, regenerateBackground: regenerate,
      proposalDetails: { 7: { id: 'obsolete-bg', status, base_revision: 4,
        content_hash: 'a'.repeat(64),
        lyrics_context: { revision: 4, matches_base: matches, segments: [] },
        operations: [{ id: 'bg', applicable: false, status: 'pending',
          kind: 'background_review', visual_action: 'regenerate_background',
          regeneration_supported: true, suggested_prompt: 'Paisaje sin texto' }],
      } } });
    const button = screen.getByRole('button', { name: 'Regenerar fondo con este prompt' });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(regenerate).not.toHaveBeenCalled();
  });

  it("shows an editable visual prompt and regenerates without publishing", () => {
    const regenerate = vi.fn();
    renderPanel({}, {
      proposalEnabled: true,
      proposalDetails: { 7: {
        id: "proposal-bg", status: "ready", base_revision: 4,
        content_hash: "a".repeat(64),
        lyrics_context: { revision: 4, matches_base: true, segments: [] },
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
      "a".repeat(64),
    );
    expect(screen.queryByRole("button", { name: /Aplicar seleccionadas/ }))
      .not.toBeInTheDocument();
  });

  it("previews operator drafts, autosaves them and blocks apply until confirmed", async () => {
    const adjust = vi.fn();
    const segment = { _id: "target", start: 13, end: 15, text: "Texto viejo" };
    renderPanel({}, {
      proposalEnabled: true,
      proposalApplyEnabled: true,
      proposalDetails: { 7: {
        id: "proposal-3", status: "ready", base_revision: 4,
        content_hash: "hash-preview-3",
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

    const input = screen.getByDisplayValue("Texto correcto");
    fireEvent.change(input, {
      target: { value: "Texto corregido por operador" },
    });
    const preview = screen.getByLabelText("Vista previa de la letra resultante");
    expect(preview).toHaveTextContent("Texto corregido por operador");
    expect(screen.getByText(/Tenés ajustes sin guardar/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aplicar seleccionadas (1)" }))
      .toBeDisabled();

    fireEvent.blur(input);
    await waitFor(() => {
      expect(adjust).toHaveBeenCalledWith(
        7, "proposal-3", "op-1", "Texto corregido por operador", 4, "hash-preview-3",
      );
    });
  });
});
