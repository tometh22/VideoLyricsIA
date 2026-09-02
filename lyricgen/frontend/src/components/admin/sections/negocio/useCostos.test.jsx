/**
 * Tests de `diasDelMes` y `mesesDisponibles`.
 *
 * Existen por un bug real que sólo se manifiesta el día 1 de cada mes y que
 * por eso pasó la revisión de código y la prueba manual: la versión
 * original derivaba el mes desde AYER, así que el 1 de septiembre "mes
 * actual" devolvía agosto entero. Justo el día en que alguien abre el panel
 * para cerrar el mes.
 *
 * Todo se testea con un reloj inyectado. Un test que use la fecha real
 * pasaría 30 de cada 31 días, que es la peor forma posible de fallar.
 */
import { describe, expect, it } from "vitest";

import { diasDelMes, mesesDisponibles, rangoDelPeriodo } from "./useCostos";

const reloj = (s) => new Date(`${s}T12:00:00Z`);

describe("diasDelMes", () => {
  it("un mes cerrado va del 1 al último día", () => {
    expect(diasDelMes("2026-07", reloj("2026-08-15"))).toMatchObject({
      since: "2026-07-01", until: "2026-07-31", enCurso: false, vacio: false,
    });
  });

  it("el mes en curso se corta en AYER, nunca en hoy", () => {
    // Hoy sigue acumulando: incluirlo dibuja siempre una caída al final
    // que se lee como una mejora.
    expect(diasDelMes("2026-09", reloj("2026-09-15"))).toMatchObject({
      since: "2026-09-01", until: "2026-09-14", enCurso: true,
    });
  });

  it("el día 1 del mes en curso no tiene ningún día cerrado", () => {
    // Acá vivía el bug: devolver agosto entero cuando se pidió septiembre.
    const r = diasDelMes("2026-09", reloj("2026-09-01"));
    expect(r.since).toBe("2026-09-01");
    expect(r.vacio).toBe(true);
  });

  it("el último día del mes todavía cuenta como en curso", () => {
    expect(diasDelMes("2026-08", reloj("2026-08-31"))).toMatchObject({
      since: "2026-08-01", until: "2026-08-30", enCurso: true,
    });
  });

  it("respeta los meses cortos y los bisiestos", () => {
    expect(diasDelMes("2026-02", reloj("2026-05-05")).until).toBe("2026-02-28");
    expect(diasDelMes("2028-02", reloj("2028-05-05")).until).toBe("2028-02-29");
  });

  it("un mes viejo nunca queda en curso ni vacío", () => {
    const r = diasDelMes("2025-12", reloj("2026-08-15"));
    expect(r).toMatchObject({ since: "2025-12-01", until: "2025-12-31",
                              enCurso: false, vacio: false });
  });
});

describe("mesesDisponibles", () => {
  it("arranca en el mes de HOY y va hacia atrás", () => {
    const m = mesesDisponibles(3, reloj("2026-09-01"));
    expect(m.map((x) => x.id)).toEqual(["2026-09", "2026-08", "2026-07"]);
    expect(m[0].enCurso).toBe(true);
    expect(m[1].enCurso).toBe(false);
  });

  it("cruza el año hacia atrás", () => {
    const m = mesesDisponibles(3, reloj("2027-01-10"));
    expect(m.map((x) => x.id)).toEqual(["2027-01", "2026-12", "2026-11"]);
  });
});

describe("rangoDelPeriodo", () => {
  it("un rango móvil cruza el borde de mes", () => {
    // El 3 de septiembre "últimos 7 días" tiene que llegar a agosto. Es
    // justo lo que un selector por mes no puede expresar, y por eso los
    // rangos móviles siguen existiendo.
    expect(rangoDelPeriodo("7d", reloj("2026-09-03"))).toMatchObject({
      since: "2026-08-27", until: "2026-09-02", esMes: false,
    });
  });

  it("marca los meses como mes y los móviles como no-mes", () => {
    // `esMes` es lo que hace que el bloque de costo por video se calle
    // sobre una ventana móvil en vez de mostrar un mes entero rotulado
    // "últimos 7 días".
    expect(rangoDelPeriodo("2026-07", reloj("2026-09-03")).esMes).toBe(true);
    expect(rangoDelPeriodo("30d", reloj("2026-09-03")).esMes).toBe(false);
  });
});
