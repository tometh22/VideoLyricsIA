// Contrato del submit del wizard de EDICIÓN.
//
// El test que importa acá es el INVARIANTE DE PARIDAD baseline↔current, no la
// enumeración de ejes. Motivo: `computeFieldDiff` ya tenía 40 tests en verde
// mientras producción emitía el bucket `typography` en el 100% de las
// ediciones, porque los dos fixtures del test se pasaban `baseline` y
// `current` a mano y coincidían entre ellos — no con App.jsx, donde `baseline`
// tenía los 5 `title_*` y `current` ninguno.
//
// Consecuencias que ese bug tenía en producción:
//   1. la guarda "No cambiaste nada" nunca disparaba en edición;
//   2. en un job `done`, un cambio SÓLO de fondo dejaba de caer en la alerta
//      "No se puede regenerar el fondo" (que exige un único bucket) y pasaba a
//      degradarse a una edición de LETRA, descartando el cambio en silencio;
//   3. una portada no-default se revertía a auto/1.0 en cada edición.
//
// Por eso los dos primeros tests derivan TODO de `buildEditReview` +
// `buildEditCurrent` (las funciones que produce la app) en vez de declarar
// objetos. Un eje nuevo agregado a uno y no al otro rompe el test solo.
// Mismo principio que lib/renderParity.test.js, que documenta el mirror del
// FontScalePicker retirado: "passed while the actual preview drifted 1.7×".

import { describe, it, expect } from "vitest";
import {
  buildEditReview,
  buildEditCurrent,
  resolveEditSubmission,
  EDIT_TYPE_PRIORITY,
} from "./editSubmission.js";
import { computeFieldDiff } from "./editWizardDiff.js";

/** Job "completo": todos los ejes en valores NO-default, para que cualquier
 *  campo que se pierda en el viaje job → baseline/current aparezca como diff. */
const JOB_FULL = {
  artist: "Bersuit",
  song_title: "La Argentinidad Al Palo",
  status: "pending_review",
  segments_json: [{ start: 1, end: 2, text: "hola" }],
  render_params: {
    font: "poppins-bold",
    text_case: "lower",
    text_contrast: "high",
    font_scale: 1.15,
    lyrics_animation: "karaoke",
    line_transition: "fade",
    lyric_color: "#FF0000",
    lyric_sung_color: "#00FF00",
    movement_style: "estatico",
    genre: "rock",
    concept: "urbano",
    match_lyrics: false,
    effect: "snow",
    background_hint: "un carnaval argentino",
    bg_verbatim: true,
    background_mode: "imagen",
    frame_format: "cine",
    title_template: "hero",
    title_size: 1.4,
    title_artist_font: "anton",
    title_song_font: "bebas",
    title_song_break: "La Argentinidad|Al Palo",
  },
};

/** Job "pelado": render_params vacío — ejercita todos los defaults. */
const JOB_BARE = {
  artist: "Coti",
  song_title: "Dias",
  status: "pending_review",
  segments_json: [],
  render_params: {},
};

// Reconstruye el `review` del wizard tal como lo arma App.jsx: los
// initialFields se spreadean dentro de currentReview.
const reviewFrom = (job) => {
  const { initialFields, baseline } = buildEditReview(job, null);
  return { ...initialFields, baseline, jobStatus: job.status };
};

const currentFrom = (job, overrides = {}) => {
  const review = { ...reviewFrom(job), ...overrides };
  return buildEditCurrent(review, {
    editedSegments: JSON.parse(JSON.stringify(job.segments_json || [])),
    bgSelectMode: "auto",
    backgroundId: null,
  });
};

describe("invariante: un job sin tocar no produce diff", () => {
  // ESTE es el test que falla antes del fix (bucket `typography` presente por
  // los title_* ausentes en `current`).
  for (const [name, job] of [["job completo", JOB_FULL], ["job pelado", JOB_BARE]]) {
    it(`${name}: computeFieldDiff(baseline, current) === {}`, () => {
      const { baseline } = buildEditReview(job, null);
      const current = currentFrom(job);
      expect(computeFieldDiff(baseline, current)).toEqual({});
    });
  }

  it("y por lo tanto resolveEditSubmission no manda nada", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL),
      jobStatus: "pending_review",
    });
    expect(out.presentBuckets).toEqual([]);
    expect(out.payload).toBeNull();
    expect(out.editType).toBeNull();
  });
});

describe("invariante estructural: baseline y current cubren las mismas claves", () => {
  // Sin esto, agregar un eje a baseline y olvidarlo en current vuelve a emitir
  // un bucket en cada edición. Falla automáticamente con cualquier eje nuevo.
  it("toda clave de baseline existe en current", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const current = currentFrom(JOB_FULL);
    const missing = Object.keys(baseline).filter((k) => !(k in current));
    expect(missing).toEqual([]);
  });

  it("y todo eje comparado por el diff está en los dos lados", () => {
    // Un eje que `computeFieldDiff` mira pero que sólo existe en uno de los dos
    // objetos es el bug exacto. Detectamos por comportamiento: mutar CADA clave
    // de baseline tiene que producir a lo sumo un diff, nunca un crash ni un
    // bucket sorpresa cuando el valor coincide.
    const { baseline } = buildEditReview(JOB_FULL, null);
    const current = currentFrom(JOB_FULL);
    for (const key of Object.keys(baseline)) {
      if (key === "segments") continue; // comparado por contenido, no por igualdad
      const diff = computeFieldDiff({ ...baseline }, { ...current, [key]: current[key] });
      expect(diff, `clave ${key} produce diff sin haber cambiado`).toEqual({});
    }
  });
});

describe("un cambio real sí viaja", () => {
  it("movimiento: pending_review manda edit_type=background", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado" }),
      jobStatus: "pending_review",
    });
    expect(out.editType).toBe("background");
    expect(out.payload.movement_style).toBe("animado");
    expect(out.willDrop).toEqual([]);
    expect(out.willApply.background.movement_style).toBe("animado");
  });

  it("tipografía sola en pending_review manda edit_type=typography", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { font: "anton" }),
      jobStatus: "pending_review",
    });
    expect(out.editType).toBe("typography");
    expect(out.payload.font).toBe("anton");
  });
});

describe("gates por status: nada se descarta en silencio", () => {
  it("job done + SÓLO fondo → blocked, no se postea", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado" }),
      jobStatus: "done",
    });
    expect(out.blocked).toEqual({ reason: "status", buckets: ["background"] });
    expect(out.payload).toBeNull();
  });

  it("job done + fondo Y tipografía → el fondo se DESCARTA y queda registrado", () => {
    // Este es el caso que antes se degradaba en silencio y encima la telemetría
    // reportaba "background" como si hubiera viajado.
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado", font: "anton" }),
      jobStatus: "done",
    });
    expect(out.willDrop).toEqual(["background"]);
    expect(out.willApply.background).toBeUndefined();
    expect(out.willApply.typography.font).toBe("anton");
    // typography standalone no se acepta en done → piggyback en lyrics.
    expect(out.editType).toBe("lyrics");
    expect(Array.isArray(out.payload.segments)).toBe(true);
  });

  it("job done + sólo tipografía → lyrics con los segments actuales", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { font: "anton" }),
      jobStatus: "done",
    });
    expect(out.editType).toBe("lyrics");
    expect(out.payload.font).toBe("anton");
    expect(out.payload.segments).toEqual([{ start: 1, end: 2, text: "hola" }]);
  });

  it("job done + letra → pasa derecho (lyrics es ungated)", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const current = currentFrom(JOB_FULL);
    current.segments = [{ start: 1, end: 2, text: "otra letra" }];
    const out = resolveEditSubmission({ baseline, current, jobStatus: "done" });
    expect(out.editType).toBe("lyrics");
    expect(out.willDrop).toEqual([]);
  });
});

describe("multi-escena: el backend rechaza el fondo con 400, no lo mandamos", () => {
  const scenePlan = { scenes: [{ recurrence_key: "coro" }] };

  it("sólo fondo → blocked con reason=scenes", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado" }),
      jobStatus: "pending_review",
      scenePlan,
    });
    expect(out.blocked).toEqual({ reason: "scenes", buckets: ["background"] });
  });

  it("fondo + tipografía → el fondo se descarta, la tipografía viaja", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado", font: "anton" }),
      jobStatus: "pending_review",
      scenePlan,
    });
    expect(out.willDrop).toEqual(["background"]);
    expect(out.editType).toBe("typography");
    expect(out.willApply.typography.font).toBe("anton");
  });

  it("sin scenePlan el comportamiento es el de siempre", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado" }),
      jobStatus: "pending_review",
      scenePlan: null,
    });
    expect(out.blocked).toBeNull();
    expect(out.editType).toBe("background");
  });
});

describe("prioridad de edit_type", () => {
  it("background_library supersede al regen IA y borra el bucket background", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { movementStyle: "animado" }),
      jobStatus: "pending_review",
    });
    expect(out.editType).toBe("background");

    const withLibrary = buildEditCurrent(
      { ...reviewFrom(JOB_FULL), movementStyle: "animado" },
      { editedSegments: [], bgSelectMode: "library", backgroundId: 42 },
    );
    const out2 = resolveEditSubmission({
      baseline,
      current: withLibrary,
      jobStatus: "pending_review",
    });
    expect(out2.editType).toBe("background_library");
    expect(out2.payload.background_id).toBe(42);
  });

  it("el orden de prioridad es el que el backend espera", () => {
    expect(EDIT_TYPE_PRIORITY).toEqual([
      "background_library",
      "background",
      "lyrics",
      "metadata",
      "typography",
    ]);
  });
});

describe("forceBackgroundRegen: 'otra versión' sin cambiar campos", () => {
  it("emite el bucket background aunque el diff esté vacío", () => {
    const { baseline } = buildEditReview(JOB_FULL, null);
    const out = resolveEditSubmission({
      baseline,
      current: currentFrom(JOB_FULL, { forceBackgroundRegen: true }),
      jobStatus: "pending_review",
    });
    expect(out.editType).toBe("background");
  });
});

describe("frameFormat: sembrado del job, no fabricado", () => {
  it("un job cine mantiene cine (antes quedaba undefined → 'full' fijo)", () => {
    const { initialFields, baseline } = buildEditReview(JOB_FULL, null);
    expect(initialFields.frameFormat).toBe("cine");
    expect(baseline.frameFormat).toBe("cine");
    expect(currentFrom(JOB_FULL).frameFormat).toBe("cine");
  });

  it("un job sin frame_format cae a full", () => {
    expect(buildEditReview(JOB_BARE, null).baseline.frameFormat).toBe("full");
  });
});
