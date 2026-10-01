import { afterEach, describe, expect, it } from "vitest";
import { getPublicationMode, setPublicationMode } from "./publicationMode";

import {
  CORRECTION_STEPS,
  correctionPrimary,
  correctionSecondary,
  correctionSentence,
  correctionStepList,
  correctionSteps,
  describePublishError,
  requestWorkflow,
} from "./changeRequestWorkflow";

const JARGON = /preflight|\bQC\b|fingerprint/i;
const workflow = (key, allowed, extra = {}) => ({
  key, activeStep: 0, label: "Etiqueta del servidor", detail: "Detalle del servidor",
  tone: "attention", allowed_actions: allowed, ...extra,
});
const states = (steps) => steps.map((step) => step.state);

describe("correctionSteps: mapeo de las 5 etapas del servidor a 3 pasos", () => {
  it("expone exactamente los tres pasos pedidos", () => {
    expect(CORRECTION_STEPS.map((step) => step.label)).toEqual(["Corregir", "Generar el video nuevo", "Publicar"]);
  });

  // [clave, acciones permitidas, extras del workflow, ctx, estados, principal, secundarios]
  const TABLE = [
    ["analyze", ["edit", "resolve", "analyze"], {}, { proposalEnabled: true },
      ["active", "todo", "todo"], "Corregir en el editor", ["suggest", "close"]],
    ["apply", ["edit", "resolve", "analyze", "review_proposal"], { activeStep: 1 },
      { proposalEnabled: true, effectiveProposal: { status: "ready" } },
      ["active", "todo", "todo"], "Revisar los cambios sugeridos", ["edit", "close"]],
    ["render", ["edit", "resolve", "analyze", "review_render"], { activeStep: 2 },
      { proposalEnabled: true, effectiveProposal: { status: "applied" } },
      ["done", "active", "todo"], "Generar el video corregido", ["edit", "close"]],
    ["rendering", ["refresh"], { activeStep: 2, tone: "busy" }, {},
      ["done", "active", "todo"], "Generando el video nuevo…", ["edit"]],
    ["publish", ["edit", "resolve", "analyze", "publish"], { activeStep: 3 },
      { status: { canPublish: true }, publication: { needs_publish: true } },
      ["done", "done", "active"], "Publicar en el portal y dar por resuelto", ["edit", "close"]],
    ["publish", ["edit", "resolve", "prepare_master"], { activeStep: 3 },
      { status: { canPublish: true }, publication: { prores_pending: ["umg_master"] } },
      // One click: publishing prepares the master by itself; "solo preparar" stays as a small link.
      ["done", "done", "active"], "Publicar en el portal y dar por resuelto", ["prepare_only", "edit", "close"]],
    ["review", ["edit", "resolve", "analyze"], { activeStep: 3 }, {},
      ["done", "done", "done"], "Dar por resuelto", ["edit"]],
    ["resolved", ["reopen"], { activeStep: 5, tone: "done" }, { isResolved: true },
      ["done", "done", "done"], "Reabrir pedido", []],
    ["resolved", ["reopen"], { activeStep: 0, tone: "idle" }, { isResolved: true },
      ["todo", "todo", "todo"], "Reabrir pedido", []],
    ["blocked", ["edit", "refresh"], { activeStep: 2 }, {},
      ["done", "active", "todo"], "Ver el error", []],
    ["unknown", ["refresh"], {}, {},
      ["active", "todo", "todo"], "Actualizar estado", ["edit"]],
  ];

  it.each(TABLE)("%s (%j)", (key, allowed, extra, ctx, expectedStates, label, secondary) => {
    const result = correctionSteps(workflow(key, allowed, extra), { hasJob: true, ...ctx });
    expect(states(result.steps)).toEqual(expectedStates);
    expect(result.primary).toMatchObject({ label });
    expect(result.secondary.map((link) => link.key)).toEqual(secondary);
    expect(result.sentence.length).toBeGreaterThan(10);
  });

  it("una clave desconocida cae en Corregir y nunca se queda sin acción principal", () => {
    const result = correctionSteps(workflow("algo-nuevo", ["refresh"]));
    expect(states(result.steps)).toEqual(["active", "todo", "todo"]);
    expect(result.primary.label).toBeTruthy();
    expect(correctionStepList(undefined).map((step) => step.state)).toEqual(["active", "todo", "todo"]);
  });

  it("la bandeja de campaña usa las mismas claves (correct, closed, resolved)", () => {
    expect(states(correctionStepList({ key: "correct" }))).toEqual(["active", "todo", "todo"]);
    expect(states(correctionStepList({ key: "rendering" }))).toEqual(["done", "active", "todo"]);
    expect(states(correctionStepList({ key: "publish" }))).toEqual(["done", "done", "active"]);
    expect(states(correctionStepList({ key: "resolved" }))).toEqual(["done", "done", "done"]);
    expect(states(correctionStepList({ key: "closed" }))).toEqual(["todo", "todo", "todo"]);
    expect(states(correctionStepList({ key: "blocked" }))).toEqual(["done", "active", "todo"]);
  });

  it("mapea también la proyección local (sin workflow del servidor)", () => {
    const item = { publication: { job_status: "done", render_matches_editor: true, needs_publish: true } };
    const flow = requestWorkflow(item, null, false);
    expect(states(correctionStepList(flow))).toEqual(["done", "done", "active"]);
    const applied = requestWorkflow({ proposal: { status: "applied" }, publication: {} }, null, true);
    expect(states(correctionStepList(applied))).toEqual(["done", "active", "todo"]);
  });
});

describe("correctionSteps: una única acción principal", () => {
  const KEYS = ["analyze", "apply", "render", "rendering", "publish", "review", "resolved",
    "blocked", "unknown", "refresh", "manual", "empty", "algo-nuevo"];
  const ACTIONS = [
    undefined, [], ["refresh"], ["edit", "resolve"], ["edit", "resolve", "analyze"],
    ["edit", "resolve", "analyze", "review_proposal", "review_render", "publish", "prepare_master"],
    ["reopen"],
  ];
  const CONTEXTS = [
    {}, { hasJob: false }, { proposalEnabled: true }, { isResolved: true },
    { status: { canPublish: true }, publication: { needs_publish: true } },
    { status: { canPublish: true }, publication: { prores_pending: ["m"] } },
    { status: { canPublish: true, needsProResSetup: true }, publication: { prores_pending: ["m"] } },
    { proposalEnabled: true, effectiveProposal: { status: "applied" } },
    { proposalEnabled: true, summaryProposal: { status: "ready" }, effectiveProposal: { status: "ready" } },
    { busy: { proposal: true, publishing: true, resolving: true } },
    { qcReview: { label: "Completar la revisión del video" } },
  ];

  it("devuelve siempre un solo principal con nombre, y los enlaces nunca lo repiten", () => {
    let combinations = 0;
    KEYS.forEach((key) => ACTIONS.forEach((allowed) => CONTEXTS.forEach((ctx) => {
      const flow = workflow(key, allowed);
      if (allowed === undefined) delete flow.allowed_actions;
      const result = correctionSteps(flow, ctx);
      combinations += 1;
      expect(Array.isArray(result.primary), "primary no es una lista").toBe(false);
      expect(result.primary.key).toBeTruthy();
      expect(result.primary.label.trim().length).toBeGreaterThan(3);
      expect(result.steps).toHaveLength(3);
      expect(result.steps.filter((step) => step.state === "active").length).toBeLessThanOrEqual(1);
      const secondaryKeys = result.secondary.map((link) => link.key);
      expect(new Set(secondaryKeys).size).toBe(secondaryKeys.length);
      if (["edit", "see_error"].includes(result.primary.key)) expect(secondaryKeys).not.toContain("edit");
      if (result.primary.key === "close") expect(secondaryKeys).not.toContain("close");
    })));
    expect(combinations).toBe(KEYS.length * ACTIONS.length * CONTEXTS.length);
  });

  it("nombra lo que hace cada botón, sin los nombres confusos de antes", () => {
    const labels = new Set();
    KEYS.forEach((key) => ACTIONS.forEach((allowed) => CONTEXTS.forEach((ctx) => {
      if (ctx.qcReview) return;
      const flow = workflow(key, allowed);
      const result = correctionSteps(flow, ctx);
      labels.add(result.primary.label);
      result.secondary.forEach((link) => labels.add(link.label));
    })));
    ["Analizar pedido", "Analizar este pedido", "Revisar propuesta", "Revisar y confirmar render",
      "Publicar actualización", "Marcar como resuelto", "Editar letra", "Revisá el motivo del bloqueo"]
      .forEach((old) => expect(labels.has(old), old).toBe(false));
    ["Corregir en el editor", "Revisar los cambios sugeridos", "Generar el video corregido",
      "Publicar en el portal y dar por resuelto", "Solo preparar el archivo profesional (sin publicar)",
      "Reabrir pedido", "Ver el error", "Pedir sugerencia a la IA", "Editar letra a mano",
      "Cerrar sin publicar"].forEach((name) => expect(labels.has(name), name).toBe(true));
  });

  it("sin job no ofrece abrir el editor", () => {
    const primary = correctionPrimary(workflow("analyze", ["edit", "analyze"]), { hasJob: false });
    expect(primary).toMatchObject({ key: "none", disabled: true });
    expect(correctionSecondary(workflow("analyze", ["edit"]), primary, { hasJob: false })
      .map((link) => link.key)).not.toContain("edit");
  });

  it("sólo ofrece pedir ayuda a la IA si está habilitada y el pedido sigue en Corregir", () => {
    const flow = workflow("analyze", ["edit", "analyze"]);
    const primary = correctionPrimary(flow, {});
    expect(correctionSecondary(flow, primary, { proposalEnabled: false }).map((l) => l.key)).not.toContain("suggest");
    expect(correctionSecondary(flow, primary, { proposalEnabled: true }).map((l) => l.key)).toContain("suggest");
    const publish = workflow("publish", ["edit", "analyze", "publish"]);
    expect(correctionSecondary(publish, { key: "publish" }, { proposalEnabled: true }).map((l) => l.key))
      .not.toContain("suggest");
    expect(correctionSecondary(flow, primary, { proposalEnabled: true, busy: { proposal: true } })
      .find((l) => l.key === "suggest")).toMatchObject({ label: "Analizando…", disabled: true });
  });

  it("conserva el camino directo al render y sólo ofrece cerrar si el servidor lo permite", () => {
    const flow = workflow("apply", ["edit", "resolve", "review_proposal", "review_render"]);
    const ctx = { publication: { can_render: true } };
    const primary = correctionPrimary(flow, ctx);
    expect(primary.key).toBe("review_proposal");
    expect(correctionSecondary(flow, primary, ctx).map((link) => link.key)).toEqual(["render", "edit", "close"]);
    expect(correctionSecondary(flow, { key: "render" }, ctx).map((link) => link.key)).not.toContain("render");
    expect(correctionSecondary(workflow("apply", ["edit", "review_proposal"]), primary, ctx).map((l) => l.key))
      .not.toContain("close");
  });

  it("deshabilita el principal mientras trabaja y conserva el nombre", () => {
    expect(correctionPrimary(workflow("publish", ["publish"]), {
      status: { canPublish: true }, busy: { publishing: true },
    })).toMatchObject({ key: "publish", label: "Publicando…", disabled: true });
    expect(correctionPrimary(workflow("resolved", ["reopen"]), { isResolved: true, busy: { resolving: true } }))
      .toMatchObject({ key: "reopen", disabled: true });
    expect(correctionPrimary(workflow("review", ["resolve"]), { busy: { noNote: true } }))
      .toMatchObject({ key: "close", disabled: true });
  });

  it("pide elegir el formato del archivo profesional cuando falta configurarlo", () => {
    expect(correctionPrimary(workflow("publish", ["prepare_master"]), {
      publication: { prores_pending: ["m"] }, status: { needsProResSetup: true },
    }).label).toBe("Elegir formato y publicar");
  });

  it("una revisión bloqueante ocupa el lugar del principal y sólo si el llamador la indica", () => {
    const flow = workflow("publish", ["edit", "publish"]);
    const ctx = { status: { canPublish: true } };
    expect(correctionPrimary(flow, ctx).key).toBe("publish");
    expect(correctionPrimary(flow, { ...ctx, qcReview: { label: "Completar la revisión del video" } }))
      .toMatchObject({ key: "qc_review", label: "Completar la revisión del video" });
  });
});

describe("correctionSteps: frase del paso en lenguaje llano", () => {
  it("nunca muestra preflight, QC ni fingerprint en los estados normales", () => {
    const keys = ["analyze", "apply", "render", "rendering", "publish", "review", "resolved", "closed",
      "blocked", "unknown", "refresh", "manual", "empty", "algo-nuevo"];
    keys.forEach((key) => {
      [true, false].forEach((proposalEnabled) => {
        const flow = workflow(key, ["edit", "publish"], { pending_manual: 2, activeStep: 5 });
        const text = [
          correctionSentence(flow, { proposalEnabled, publication: { prores_pending: ["m"] } }),
          ...correctionSteps(flow, { proposalEnabled, status: { canPublish: true } }).steps.map((s) => s.label),
        ].join(" ");
        expect(text, key).not.toMatch(JARGON);
      });
    });
  });

  it("reescribe el detalle técnico del servidor en vez de repetirlo", () => {
    const flow = workflow("publish", ["publish"], {
      detail: "La huella del render (fingerprint) no coincide con el preflight QC",
    });
    expect(correctionSentence(flow)).not.toContain("fingerprint");
    expect(correctionSentence(flow)).toContain("El video nuevo está listo");
  });

  it("explica cada paso y avisa de las indicaciones que hay que mirar a mano", () => {
    expect(correctionSentence(workflow("render", []))).toContain("Generá el video corregido");
    expect(correctionSentence(workflow("rendering", []))).toContain("generando el video nuevo");
    expect(correctionSentence(workflow("blocked", []))).toContain("Abrí el error");
    expect(correctionSentence(workflow("resolved", [], { activeStep: 5 }))).toContain("ya está en el portal");
    expect(correctionSentence(workflow("resolved", [], { activeStep: 0 }))).toContain("sin publicar");
    expect(correctionSentence(workflow("render", [], { pending_manual: 1 }))).toContain("1 indicación");
    expect(correctionSentence(workflow("publish", ["publish"]), { publication: { prores_pending: ["m"] } }))
      .toContain("archivo profesional");
  });

  it("cuando una revisión bloquea la publicación lo dice sin tecnicismos", () => {
    const text = correctionSentence(workflow("publish", ["publish"]), { qcReview: { label: "x" } });
    expect(text).toContain("en pausa");
    expect(text).not.toMatch(JARGON);
  });
});

describe("describePublishError", () => {
  const err = (status, detail, message = "x") => Object.assign(new Error(message), {
    status, detail, code: detail && typeof detail === "object" ? detail.code : detail,
  });

  it.each([
    [err(409, { code: "language_review_unresolved" }), "La letra no coincide con el idioma de la referencia. Abrí el editor y confirmá el idioma."],
    [err(409, "lyric_review_pending"), /sin revisar.*editor/],
    [err(402, { code: "quota_exceeded" }), "Falta crédito en la cuenta del video. Cargá crédito y volvé a publicar."],
    [err(402, undefined), /Falta crédito/],
    [err(409, "change_request_job_mismatch"), /pertenece a otro video/],
    [err(409, { code: "fresh_preflight_required" }), /volvé a revisarlo/i],
    [err(409, { code: "stale_preflight" }), /volvé a revisarlo/i],
    [err(409, "publication_context_has_pending_writes"), /se está guardando/],
    [err(503, { code: "publication_storage_unavailable" }), /conserva la versión anterior/],
    [err(409, { code: "prores_required" }), /archivo profesional/],
    [err(409, "El corte cambió durante la publicación."), "El video cambió mientras se publicaba. Actualizá y reintentá."],
    [err(403, "Admin only"), /permiso/],
  ])("traduce %# a un mensaje con qué hacer", (error, expected) => {
    const text = describePublishError(error);
    if (expected instanceof RegExp) expect(text).toMatch(expected);
    else expect(text).toBe(expected);
    expect(text).not.toMatch(/preflight|fingerprint|HTTP 4/i);
  });

  it("el mensaje por defecto nombra el estado HTTP y el código: nunca un callejón sin salida", () => {
    const text = describePublishError(err(422, { code: "algo_raro", message: "No procesable" }, "No procesable"));
    expect(text).toContain("HTTP 422");
    expect(text).toContain("algo_raro");
    expect(text).toMatch(/reintentá/);
    expect(describePublishError(err(400, "Job must be approved before it can be published")))
      .toContain("HTTP 400");
    expect(describePublishError(err(418, undefined, "Soy una tetera"))).toContain("HTTP 418");
    expect(describePublishError(new Error("boom"))).toContain("boom");
  });
});

describe("wording follows the publication mode", () => {
  afterEach(() => setPublicationMode("snapshot"));
  const rendering = { key: "rendering", activeStep: 2 };

  it("never promises that the portal keeps the old cut when it serves the newest render", () => {
    setPublicationMode("snapshot");
    expect(correctionSentence(rendering)).toMatch(/sigue mostrando el anterior/);
    expect(describePublishError({ status: 503, detail: { code: "publication_storage_unavailable" } })).toMatch(/conserva la versión anterior/);

    setPublicationMode("pointer");
    const sentence = correctionSentence(rendering);
    expect(sentence).toMatch(/cliente no lo ve hasta que lo publiques/);
    expect(sentence).not.toMatch(/sigue mostrando/);
    expect(describePublishError({ status: 503, detail: { code: "publication_storage_unavailable" } })).not.toMatch(/copiar|conserva/);
    expect(requestWorkflow({ publication: { job_status: "rendering" } }, null, true).detail).toMatch(/cliente no ve este video hasta que lo publiques/);
  });

  it("only the exact value 'pointer' switches the wording", () => {
    setPublicationMode("pointer");
    setPublicationMode(undefined);
    expect(getPublicationMode()).toBe("snapshot");
    setPublicationMode("anything-else");
    expect(getPublicationMode()).toBe("snapshot");
  });
});

describe("the publish sentence keeps what the server knows about a pending proposal", () => {
  const detail = "El corte corresponde a la letra guardada. La propuesta sigue pendiente: revisá el pedido completo en el video antes de publicar; no hace falta volver a aplicarla si lo corregiste a mano.";
  it("appends the proposal note verbatim and only when the server sent it", () => {
    const withNote = correctionSentence({ key: "publish", activeStep: 3, detail });
    expect(withNote).toMatch(/La propuesta sigue pendiente: revisá el pedido completo en el video antes de publicar; no hace falta volver a aplicarla si lo corregiste a mano\.$/);
    expect(withNote).toMatch(/^El video nuevo está listo\. Miralo y publicalo/);
    const without = correctionSentence({ key: "publish", activeStep: 3, detail: "Reproducí el video y publicalo en el portal para dar por resuelto el pedido." });
    expect(without).not.toMatch(/propuesta/);
  });
  it("also keeps it when the professional master is still pending", () => {
    const sentence = correctionSentence({ key: "publish", detail }, { publication: { prores_pending: ["umg_master"] } });
    expect(sentence).toMatch(/el archivo profesional se prepara solo/);
    expect(sentence).toMatch(/La propuesta sigue pendiente:/);
  });
});

