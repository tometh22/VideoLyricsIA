/**
 * Ficha de ajustes del video (read-only).
 *
 * Contrato: muestra exactamente los ejes que el operador PUEDE setear
 * (_VARIANT_OVERRIDABLE_FIELDS), con etiquetas legibles, y OMITE los que están
 * en su default — un chip `Concepto: —` es ruido, no información.
 *
 * Por qué importa: en el reclamo que originó esto el operador regeneró el fondo
 * SIETE veces sin poder ver que el video tenía guardado
 * `movement_style: "animado"`. La ficha convierte eso en dos segundos de lectura.
 */
import { describe, expect, it } from "vitest";
import {
  SETTINGS_GROUPS,
  buildSettingsSummary,
  describeSceneSource,
} from "./renderSettingsSummary.js";
// Los resolvers salen del catálogo REAL, no de una copia en el test: una copia
// a mano es exactamente lo que dejó pasar los códigos inventados de contraste
// (`low`/`high`, que no existen — son `subtle`/`strong`), con el test en verde.
import { AXIS_VALUE_LABELS, dynamicAxisLabel } from "./optionLabels.js";

const deps = {
  t: (_k, fb) => fb,
  movementLabel: (c) => ({ animado: "Animado (ilustración)", estatico: "Estático (cámara fija)" }[c]),
  effectLabel: (c) => ({ snow: "Nieve", rain: "Lluvia" }[c]),
  fontLabel: (c) => ({ "poppins-bold": "Poppins Bold", anton: "Anton" }[c]),
  valueLabel: (axisKey, code) =>
    AXIS_VALUE_LABELS((_k, fb) => fb)[axisKey]?.[String(code).trim().toLowerCase()]
    || dynamicAxisLabel((k) => k, axisKey, code),
};

const flat = (groups) => groups.flatMap((g) => g.chips.map((c) => `${c.label}: ${c.value}`));

describe("buildSettingsSummary", () => {
  it("el caso del reclamo: se ve que el video tiene Animado", () => {
    const out = buildSettingsSummary({ movement_style: "animado" }, deps);
    expect(flat(out)).toContain("Movimiento: Animado (ilustración)");
  });

  it("traduce los códigos con el catálogo real, no los muestra crudos", () => {
    const out = buildSettingsSummary(
      { movement_style: "estatico", effect: "snow", font: "anton" }, deps,
    );
    const chips = flat(out);
    expect(chips).toContain("Movimiento: Estático (cámara fija)");
    expect(chips).toContain("Efecto: Nieve");
    expect(chips).toContain("Tipografía: Anton");
    // Nada de snake_case ni códigos internos a la vista.
    expect(chips.join(" ")).not.toMatch(/estatico|snow|anton|movement_style/);
  });

  it("omite los defaults en vez de mostrar chips vacíos", () => {
    const out = buildSettingsSummary({
      movement_style: "",       // Auto
      effect: "",               // Ninguno
      concept: "auto",
      lyrics_animation: "none",
      font_scale: 1.0,          // default
      title_size: "1.0",        // default
      title_template: "auto",
    }, deps);
    expect(out).toEqual([]);
  });

  it("pero sí muestra un tamaño no-default, formateado", () => {
    const out = buildSettingsSummary({ font_scale: 1.15, title_size: 1.4 }, deps);
    const chips = flat(out);
    expect(chips).toContain("Tamaño: 1.15×");
    expect(chips).toContain("Tamaño del título: 1.4×");
  });

  it("agrupa como el operador lo ve en el wizard", () => {
    const out = buildSettingsSummary(
      { movement_style: "animado", font: "anton", title_template: "hero" }, deps,
    );
    expect(out.map((g) => g.id)).toEqual(["fondo", "letra", "portada"]);
  });

  it("un render_params vacío o ausente no rompe", () => {
    expect(buildSettingsSummary({}, deps)).toEqual([]);
    expect(buildSettingsSummary(null, deps)).toEqual([]);
    expect(buildSettingsSummary(undefined)).toEqual([]);
  });

  it("un código desconocido cae al valor crudo en vez de desaparecer", () => {
    // Preferimos mostrar algo raro a esconder que el video tiene un valor.
    const out = buildSettingsSummary({ movement_style: "dinamico" }, deps);
    expect(flat(out)).toContain("Movimiento: dinamico");
  });

  it("el prompt se marca como tal (se renderiza distinto: es texto largo)", () => {
    const out = buildSettingsSummary({ background_hint: "un carnaval" }, deps);
    const chip = out[0].chips.find((c) => c.key === "background_hint");
    expect(chip.isPrompt).toBe(true);
    expect(chip.value).toBe("un carnaval");
  });
});

describe("describeSceneSource: dice la verdad sobre lo que hizo el worker", () => {
  // Espeja background_policy.resolve_creative_mode, donde un prompt no vacío
  // le GANA a match_lyrics. Si la ficha dijera "inspirada en la letra" con un
  // prompt presente estaría mintiendo igual que el wizard mentía antes.
  it("prompt presente + verbatim → 'tal cual', aunque match_lyrics sea true", () => {
    expect(describeSceneSource({ background_hint: "x", bg_verbatim: true, match_lyrics: true }))
      .toBe("Tu prompt, tal cual");
  });

  it("prompt presente sin verbatim → 'mejorado con IA'", () => {
    expect(describeSceneSource({ background_hint: "x", match_lyrics: true }))
      .toBe("Tu prompt, mejorado con IA");
  });

  it("sin prompt y match_lyrics → inspirada en la letra", () => {
    expect(describeSceneSource({ match_lyrics: true })).toBe("Escena inspirada en la letra");
  });

  it("sin prompt y match_lyrics=false → automática", () => {
    expect(describeSceneSource({ match_lyrics: false }))
      .toBe("Escena automática (género y mood)");
  });

  it("un job legacy sin match_lyrics se trata como inspirada (paridad con el backend)", () => {
    expect(describeSceneSource({})).toBe("Escena inspirada en la letra");
  });
});

describe("el catálogo de ejes no se desincroniza del backend", () => {
  it("cubre los ejes de _VARIANT_OVERRIDABLE_FIELDS que son visuales", () => {
    // custom_colors, bg_verbatim y match_lyrics se muestran de otra forma
    // (paleta / describeSceneSource), no como chip propio.
    const shown = SETTINGS_GROUPS.flatMap((g) => g.axes.map((a) => a.key));
    for (const key of [
      "background_hint", "concept", "genre", "movement_style", "effect",
      "lyrics_animation", "line_transition", "font", "font_scale", "text_case",
      "text_contrast", "frame_format", "title_template", "title_size",
      "title_artist_font", "title_song_font",
    ]) {
      expect(shown, `falta el eje ${key}`).toContain(key);
    }
  });

  it("no hay ejes duplicados entre grupos", () => {
    const keys = SETTINGS_GROUPS.flatMap((g) => g.axes.map((a) => a.key));
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("no se le muestran códigos internos ni defaults al operador", () => {
  // Forma REAL de un render_params de staging (job 5faa4b3f810b, el del
  // reclamo). La primera versión de esta ficha mostraba con estos datos
  // `Formato: full` (un default, en inglés) y `Mayúsculas: lower` (código
  // interno) — encontrado corriendo la ficha contra la DB, no con un fixture.
  const REAL = {
    font: "poppins-bold", genre: "rock", style: "auto", effect: "",
    concept: "", font_scale: 1.15, text_case: "lower", frame_format: "full",
    match_lyrics: true, movement_style: "animado", title_size: 1.0,
    line_transition: "none", lyrics_animation: "none", title_template: "auto",
    background_hint: "", bg_verbatim: true,
  };

  it("los defaults por eje no generan chips", () => {
    const keys = buildSettingsSummary(REAL, deps).flatMap((g) => g.chips.map((c) => c.key));
    expect(keys).not.toContain("frame_format");   // "full" es el default
    expect(keys).not.toContain("text_contrast");  // "medium" es el default
    expect(keys).not.toContain("title_size");     // 1.0 es el default
  });

  it("pero un valor NO default sí se muestra, con etiqueta legible", () => {
    const chips = flat(buildSettingsSummary(REAL, deps));
    expect(chips).toContain("Mayúsculas: todo en minúsculas");
    expect(chips.join(" ")).not.toMatch(/\blower\b|\bfull\b|\bmedium\b/);
  });

  it("cine SÍ se muestra (no es el default)", () => {
    const chips = flat(buildSettingsSummary({ ...REAL, frame_format: "cine" }, deps));
    expect(chips).toContain("Formato: Cine — franjas (2.39:1)");
  });

  it("todos los ejes enum tienen etiqueta para todos sus códigos", () => {
    const cases = {
      text_case: ["upper", "title", "lower", "sentence", "original"],
      frame_format: ["full", "cine"],
      text_contrast: ["subtle", "medium", "strong"],
      lyrics_animation: ["none", "karaoke", "word_reveal", "pop", "glow"],
      line_transition: ["none", "slide_up", "slide_side", "wipe", "dissolve_blur"],
      title_template: ["auto", "centered", "lower_third", "badge"],
    };
    for (const [key, codes] of Object.entries(cases)) {
      for (const code of codes) {
        const chips = buildSettingsSummary({ [key]: code }, deps).flatMap((g) => g.chips);
        const chip = chips.find((c) => c.key === key);
        if (!chip) continue;  // default u "empty": omitido a propósito
        expect(chip.value, `${key}=${code} muestra el código crudo`).not.toBe(code);
      }
    }
  });
});

describe("un valor de texto libre no revienta el panel", () => {
  // En staging hay `concept` de 970 caracteres: descripciones de escena
  // enteras. El campo parece un enum pero el backend lo acepta libre. Sin
  // truncar, el chip volcaba el párrafo completo. Detectado con la app real.
  const LARGO = "Static locked-off cinematic composition inside a cozy sun-drenched Argentine apartment at golden hour, ".repeat(9);

  it("se trunca a algo que entra en un chip", () => {
    const chip = buildSettingsSummary({ concept: LARGO }, deps)[0].chips[0];
    expect(chip.value.length).toBeLessThanOrEqual(45);
    expect(chip.value.endsWith("…")).toBe(true);
  });

  it("pero el texto completo queda accesible para el tooltip", () => {
    const chip = buildSettingsSummary({ concept: LARGO }, deps)[0].chips[0];
    expect(chip.full).toBe(LARGO);
  });

  it("un valor corto no se toca", () => {
    const chip = buildSettingsSummary({ genre: "rock" }, deps)[0].chips[0];
    expect(chip.value).toBe("rock");
    expect(chip.value.endsWith("…")).toBe(false);
  });
});
