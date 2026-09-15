import { describe, expect, it } from "vitest";

import {
  STATUS_RANK,
  bannerDismissKey,
  formatUptime,
  statusStyle,
  worstStatus,
} from "./serviceStatus";

describe("worstStatus", () => {
  it("devuelve el peor estado real", () => {
    expect(worstStatus(["operational", "degraded", "major_outage"])).toBe("major_outage");
    expect(worstStatus(["operational", "degraded"])).toBe("degraded");
  });

  it("unknown y no_data pierden contra cualquier dato real", () => {
    // Un hueco de observación NO es una caída. Si `unknown` compitiera en
    // el ranking, un componente sin sonda pintaría toda la página.
    expect(worstStatus(["unknown", "operational"])).toBe("operational");
    expect(worstStatus(["no_data", "operational"])).toBe("operational");
  });

  it("sin ningún dato real devuelve unknown, no operational", () => {
    // Al revés sería el peor default posible: afirmar que todo está bien
    // porque no se midió nada.
    expect(worstStatus(["unknown", "no_data"])).toBe("unknown");
    expect(worstStatus([])).toBe("unknown");
    expect(worstStatus(null)).toBe("unknown");
  });
});

describe("statusStyle", () => {
  it("cae a gris para un estado que el backend agregó y el frontend no conoce", () => {
    expect(statusStyle("estado_del_futuro")).toBe(statusStyle("unknown"));
    expect(statusStyle(undefined)).toBe(statusStyle("unknown"));
  });

  it("cubre todos los estados del ranking", () => {
    // Si el backend agrega un estado a COMPONENT_STATUS_RANK y acá no,
    // se dibuja gris en silencio. Este test obliga a mantenerlos en línea.
    for (const status of Object.keys(STATUS_RANK)) {
      expect(statusStyle(status)).not.toBe(statusStyle("unknown"));
    }
  });
});

describe("bannerDismissKey", () => {
  it("cambia con cada actualización del mismo incidente", () => {
    const a = bannerDismissKey({
      incident: { id: 5, updated_at: "2026-09-03T12:00:00Z" },
    });
    const b = bannerDismissKey({
      incident: { id: 5, updated_at: "2026-09-03T13:00:00Z" },
    });
    expect(a).not.toBe(b);
  });

  it("es estable para el caso automático sin importar el orden", () => {
    const a = bannerDismissKey({ auto_affected: ["render", "transcription"] });
    const b = bannerDismissKey({ auto_affected: ["transcription", "render"] });
    expect(a).toBe(b);
  });

  it("no muta el array que recibe", () => {
    const affected = ["render", "api"];
    bannerDismissKey({ auto_affected: affected });
    expect(affected).toEqual(["render", "api"]);
  });

  it("devuelve null cuando no hay nada que descartar", () => {
    expect(bannerDismissKey(null)).toBeNull();
    expect(bannerDismissKey({ incident: null, auto_affected: [] })).toBeNull();
  });
});

describe("formatUptime", () => {
  it("null/undefined no se convierten en 0% ni en 100%", () => {
    expect(formatUptime(null, 0)).toBeNull();
    expect(formatUptime(undefined, 0)).toBeNull();
  });

  it("marca cobertura baja para que la UI no afirme un porcentaje pelado", () => {
    expect(formatUptime(100, 4)).toMatchObject({ lowCoverage: true, coverage: "4%" });
    expect(formatUptime(99.9, 98)).toMatchObject({ lowCoverage: false });
  });

  it("formatea con dos decimales", () => {
    expect(formatUptime(99.4, 90).value).toBe("99.40%");
  });

  it("un 0% real se formatea y no se confunde con falta de dato", () => {
    expect(formatUptime(0, 100)).toMatchObject({ value: "0.00%" });
  });
});
