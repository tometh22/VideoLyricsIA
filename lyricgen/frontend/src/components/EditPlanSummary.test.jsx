/**
 * Resumen "qué va a pasar" en el punto de commit.
 *
 * Cierra el último tramo del reclamo original: el operador ya ve los ajustes
 * correctos en los controles (#996) y con qué se hizo el video (#999), pero
 * seguía sin ver qué va a pasar ANTES de gastar el render.
 *
 * Lo que este test protege sobre todo: que el resumen salga de
 * `resolveEditSubmission` y no del diff. El diff no decide el output — la
 * degradación por status sí — así que un resumen basado en el diff diría
 * "Movimiento: Animado → Estático" en un video que está por descartar ese
 * cambio. Sería el bug original una capa más arriba, con mejor tipografía.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import EditPlanSummary from "./EditPlanSummary";
import {
  buildEditReview, buildEditCurrent, resolveEditSubmission,
} from "../lib/editSubmission";

const t = (_k, fb) => fb;

afterEach(cleanup);

const JOB = {
  artist: "Bersuit",
  song_title: "La Argentinidad Al Palo",
  segments_json: [{ start: 1, end: 2, text: "hola" }],
  render_params: { movement_style: "animado", font: "poppins-bold" },
};

/** Arma el plan por el MISMO camino que la app, no a mano. */
function planFor({ overrides = {}, jobStatus = "pending_review", scenePlan = null }) {
  const { initialFields, baseline } = buildEditReview(JOB, null);
  const review = { ...initialFields, ...overrides };
  return resolveEditSubmission({
    baseline,
    current: buildEditCurrent(review, { editedSegments: JOB.segments_json }),
    jobStatus,
    scenePlan,
  });
}

describe("EditPlanSummary", () => {
  it("sin cambios lo dice, en vez de no decir nada", () => {
    render(<EditPlanSummary plan={planFor({})} t={t} />);
    expect(screen.getByTestId("edit-plan-summary").textContent)
      .toContain("Todavía no cambiaste nada");
  });

  it("un cambio de fondo en pending_review se anuncia como aplicable", () => {
    render(<EditPlanSummary plan={planFor({ overrides: { movementStyle: "estatico" } })} t={t} />);
    expect(screen.getByTestId("plan-applied").textContent).toContain("Fondo");
    expect(screen.queryByTestId("plan-dropped")).toBeNull();
  });

  it("EL CASO QUE IMPORTA: en un job done el fondo se anuncia como DESCARTADO", () => {
    // Antes esto se degradaba en silencio: el operador aprobaba, veía "listo",
    // y el fondo salía igual. Y la telemetría reportaba que había viajado.
    const plan = planFor({
      overrides: { movementStyle: "estatico", font: "anton" },
      jobStatus: "done",
    });
    render(<EditPlanSummary plan={plan} t={t} />);
    expect(screen.getByTestId("plan-dropped").textContent).toContain("Fondo");
    expect(screen.getByTestId("plan-applied").textContent).toContain("Tipografía");
    // Y dice qué hacer al respecto, no sólo que no se puede.
    expect(screen.getByTestId("plan-dropped").textContent).toContain("variante");
  });

  it("no muestra códigos internos de bucket", () => {
    const plan = planFor({ overrides: { movementStyle: "estatico", font: "anton" }, jobStatus: "done" });
    render(<EditPlanSummary plan={plan} t={t} />);
    const txt = screen.getByTestId("edit-plan-summary").textContent;
    expect(txt).not.toMatch(/background|typography|lyrics|metadata/);
  });

  it("cuando está bloqueado no repite: el aviso completo vive en el paso 3", () => {
    const plan = planFor({
      overrides: { movementStyle: "estatico" },
      scenePlan: { scenes: [{ recurrence_key: "coro" }] },
    });
    expect(plan.blocked).not.toBeNull();
    const { container } = render(<EditPlanSummary plan={plan} t={t} />);
    expect(container.firstChild).toBeNull();
  });

  it("plan nulo no rompe", () => {
    const { container } = render(<EditPlanSummary plan={null} t={t} />);
    expect(container.firstChild).toBeNull();
  });
});
