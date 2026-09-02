/**
 * Tests de la navegación del Admin.
 *
 * Cubren lo que rompe de verdad al persistir un destino: que lo guardado
 * apunte a algo que ya no existe. No es hipotético — al consolidar el panel
 * de costos se eliminó la sub-vista `gestion/infra`, y cualquiera que la
 * tuviera guardada habría abierto el panel en una pantalla vacía.
 */
import { describe, expect, it } from "vitest";

import { leerDestino } from "./AdminPanel";
import { SECCIONES, defaultSubTab, subTabValida } from "./layout/AdminSidebar";

const guardado = (o) => JSON.stringify(o);

describe("el hash de la URL manda", () => {
  it("un link compartido lleva a su pantalla, no a la última visitada", () => {
    const d = leerDestino("#/gestion/costos", guardado({ section: "ahora", subTab: null }));
    expect(d).toEqual({ section: "gestion", subTab: "costos" });
  });

  it("una sección sin sub-vistas no inventa una", () => {
    expect(leerDestino("#/rendimiento")).toEqual({
      section: "rendimiento", subTab: defaultSubTab("rendimiento"),
    });
  });

  it("tolera la barra final y la ausencia de barra inicial", () => {
    expect(leerDestino("#gestion/creditos").section).toBe("gestion");
    expect(leerDestino("#/gestion/creditos/").subTab).toBe("creditos");
  });
});

describe("destinos que ya no existen", () => {
  it("una sub-vista eliminada cae al default de SU sección, no al inicio", () => {
    // `gestion/infra` existió hasta la consolidación de Costos. Mandar a
    // alguien al inicio por eso sería castigarlo por un cambio nuestro.
    const d = leerDestino("#/gestion/infra");
    expect(d.section).toBe("gestion");
    expect(d.subTab).toBe(defaultSubTab("gestion"));
    expect(subTabValida("gestion", "infra")).toBe(false);
  });

  it("una sección inventada cae a Ahora", () => {
    expect(leerDestino("#/no-existe/nada")).toEqual({
      section: "ahora", subTab: defaultSubTab("ahora"),
    });
  });

  it("un guardado corrupto no impide abrir el panel", () => {
    expect(leerDestino("", "{ esto no es json").section).toBe("ahora");
    expect(leerDestino("", guardado({ section: 42 })).section).toBe("ahora");
  });
});

describe("lo guardado se usa cuando no hay hash", () => {
  it("devuelve la última pantalla visitada", () => {
    const d = leerDestino("", guardado({ section: "gestion", subTab: "facturacion" }));
    expect(d).toEqual({ section: "gestion", subTab: "facturacion" });
  });

  it("sin hash ni guardado, arranca en Ahora", () => {
    expect(leerDestino("", null).section).toBe("ahora");
  });
});

describe("el NAV es la única fuente de verdad", () => {
  it("SECCIONES sale del NAV, no de una lista paralela", () => {
    // Una segunda lista de secciones es lo que deja el nav diciendo una
    // cosa y el router otra. Ya pasó con Insights: su encabezado decía
    // "sin sub-tabs" mientras el nav definía cinco.
    expect([...SECCIONES].sort()).toEqual(
      ["ahora", "gestion", "insights", "rendimiento"].sort());
  });

  it("cada sección con sub-vistas tiene un default válido", () => {
    for (const s of SECCIONES) {
      const d = defaultSubTab(s);
      if (d !== null) expect(subTabValida(s, d)).toBe(true);
    }
  });
});
