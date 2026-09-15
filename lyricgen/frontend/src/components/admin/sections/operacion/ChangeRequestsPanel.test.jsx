import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChangeRequestsPanel, { publicationStatus } from "./ChangeRequestsPanel";

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
    expect(status.title).toMatch(/todavía entrega el corte anterior/);
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
});
