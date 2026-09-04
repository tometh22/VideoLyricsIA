/**
 * Tests de la navegación del Admin.
 *
 * Cubren lo que rompe de verdad al persistir un destino: que lo guardado
 * apunte a algo que ya no existe. No es hipotético — al consolidar el panel
 * de costos se eliminó la sub-vista `gestion/infra`, y cualquiera que la
 * tuviera guardada habría abierto el panel en una pantalla vacía.
 */
import { afterEach, describe, expect, it } from "vitest";

import { leerDestino } from "./AdminPanel";
import {
  SECCIONES,
  defaultSubTab,
  seccionesVisibles,
  subTabValida,
} from "./layout/AdminSidebar";

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

// ---------------------------------------------------------------------------
// Permisos: el filtro tiene que valer también para las sub-vistas
// ---------------------------------------------------------------------------
//
// Regresión real de este trabajo: al mover las sub-vistas fuera del `map` del
// sidebar perdieron el filtro de `showInsights`. Un admin no-super que abría
// `#/insights/margen` veía la barra de tabs de una sección que ni figura en su
// columna, con el contenido en blanco — y como clickear persiste el destino,
// el panel quedaba roto en cada apertura siguiente.

describe("secciones que el usuario no puede ver", () => {
  it("un no-super-admin no aterriza en Insights por un link compartido", () => {
    const d = leerDestino("#/insights/margen", null, false);
    expect(d.section).toBe("ahora");
  });

  it("un super-admin sí llega", () => {
    const d = leerDestino("#/insights/margen", null, true);
    expect(d).toEqual({ section: "insights", subTab: "margen" });
  });

  it("tampoco lo restaura desde lo guardado", () => {
    const g = JSON.stringify({ section: "insights", subTab: "features" });
    expect(leerDestino("", g, false).section).toBe("ahora");
    expect(leerDestino("", g, true).section).toBe("insights");
  });

  it("seccionesVisibles es la única lista, y respeta el permiso", () => {
    expect(seccionesVisibles(false).map((s) => s.id)).not.toContain("insights");
    expect(seccionesVisibles(true).map((s) => s.id)).toContain("insights");
  });
});

describe("localStorage de verdad, sin inyectar", () => {
  // El parámetro `guardado` de los tests de arriba es un bypass: con él, la
  // rama que toca `localStorage` nunca se ejecuta. Estos sí la ejercitan.
  afterEach(() => window.localStorage.clear());

  it("lee el destino guardado del storage real", () => {
    window.localStorage.setItem("genly_admin_destino",
      JSON.stringify({ section: "gestion", subTab: "facturacion" }));
    expect(leerDestino("", null, false)).toEqual({
      section: "gestion", subTab: "facturacion",
    });
  });

  it("un storage que tira excepción no impide abrir el panel", () => {
    const orig = window.localStorage.getItem;
    window.localStorage.getItem = () => { throw new Error("bloqueado"); };
    try {
      expect(leerDestino("", null, false).section).toBe("ahora");
    } finally {
      window.localStorage.getItem = orig;
    }
  });
});
