// Variante del look en los 4 caminos de envío: creación (/generate),
// edición (diff typography), variante (absoluto) y campañas.
import { describe, expect, it } from "vitest";
import { buildGenerationJob } from "./buildGenerationJob.js";
import { buildEditReview, buildEditCurrent, resolveEditSubmission } from "./editSubmission.js";
import { buildVariantPayload } from "./variantPayload.js";
import { campaignGenerateForm, reviewCreativeSettings } from "./campaignCreative.js";
import { appendLookVariantFields, makeLookResolvedPin } from "./lyricLookVariants.js";

const RESOLVED = { look: "cinetico", genre: "metal", variant: 3 };

describe("creación: /generate", () => {
  const approved = (extra = {}) => buildGenerationJob({
    file: { name: "a.mp3" }, artist: "Almafuerte", songTitle: "Toro y Pampa", lyricLook: "cinetico", ...extra,
  });
  const form = (job) => {
    const fd = new FormData();
    appendLookVariantFields(fd, job);
    return Object.fromEntries(fd.entries());
  };

  it("sin elegir nada y sin preview resuelto no manda campos de variante", () => {
    expect(form(approved())).toEqual({});
  });

  it("los overrides elegidos sobreviven el adapter y viajan", () => {
    const job = approved({ lyricGenre: "rock", lyricFontKey: "creepster", lyricAccentColor: "#FF0000" });
    expect(form(job)).toEqual({ lyric_genre: "rock", lyric_font_key: "creepster", lyric_accent_color: "#FF0000" });
  });

  it("en automático viaja el número/género que mostró el preview de esa canción", () => {
    const pin = makeLookResolvedPin(RESOLVED, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    expect(form(approved({ lookResolved: pin }))).toEqual({ lyric_genre: "metal", lyric_variant: "3" });
  });

  it("sin look nada (aunque haya overrides guardados)", () => {
    expect(form(approved({ lyricLook: "", lyricGenre: "rock", lyricVariant: 2 }))).toEqual({});
  });
});

describe("edición: diff typography", () => {
  const JOB = {
    artist: "Almafuerte", song_title: "Toro y Pampa", status: "pending_review", segments_json: [],
    render_params: { lyric_look: "cinetico", lyric_genre: "metal", lyric_variant: 3 },
  };
  const submit = (overrides = {}) => {
    const { initialFields, baseline } = buildEditReview(JOB, null);
    const current = buildEditCurrent({ ...initialFields, ...overrides }, { editedSegments: [], bgSelectMode: "auto" });
    return resolveEditSubmission({ baseline, current, jobStatus: "pending_review" });
  };

  it("se siembra de render_params y sin tocar no hay diff (aunque haya un pin)", () => {
    const { initialFields } = buildEditReview(JOB, null);
    expect(initialFields).toMatchObject({ lyricGenre: "metal", lyricVariant: 3, lyricFontMain: "" });
    const pin = makeLookResolvedPin({ ...RESOLVED, variant: 9 }, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    expect(submit({ lookResolved: pin }).payload).toBeNull();
  });

  it("otra variante / fuentes / colores viajan en typography", () => {
    const out = submit({ lyricVariant: 4, lyricFontMain: "anton", lyricAltColor: "#112233", lyricColor: "#445566" });
    expect(out.editType).toBe("typography");
    expect(out.payload).toMatchObject({
      lyric_variant: 4, lyric_font_main: "anton", lyric_alt_color: "#112233", lyric_color: "#445566",
    });
  });

  it("Restaurar manda '' (reset explícito) o el número resuelto si ya lo hay", () => {
    const out = submit({ lyricVariant: "" });
    expect(out.payload.lyric_variant).toBe("");
    const pin = makeLookResolvedPin({ ...RESOLVED, variant: 5 }, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    expect(submit({ lyricVariant: "", lookResolved: pin }).payload.lyric_variant).toBe(5);
    // Restaurar a lo mismo que ya tiene el video: nada que re-renderizar.
    const same = makeLookResolvedPin(RESOLVED, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    expect(submit({ lyricVariant: "", lookResolved: same }).payload).toBeNull();
  });

  it("cambiar de look manda la variante resuelta para el look nuevo", () => {
    const pin = makeLookResolvedPin({ look: "neon", genre: "metal", variant: 1 }, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    const out = submit({ lyricLook: "neon", lyricVariant: "", lyricGenre: "", lookResolved: pin });
    expect(out.payload).toMatchObject({ lyric_look: "neon", lyric_variant: 1 });
    expect(out.payload.lyric_genre).toBeUndefined(); // metal == metal
  });

  it("el blanco del texto con look viaja como '' (= el de la variante)", () => {
    const job = { ...JOB, render_params: { ...JOB.render_params, lyric_color: "#123456" } };
    const { initialFields, baseline } = buildEditReview(job, null);
    const current = buildEditCurrent({ ...initialFields, lyricColor: "#FFFFFF" }, { editedSegments: [], bgSelectMode: "auto" });
    expect(resolveEditSubmission({ baseline, current, jobStatus: "pending_review" }).payload.lyric_color).toBe("");
  });
});

describe("variante: absoluto", () => {
  it("manda los 6 campos con look ('' = automático) y lyric_color", () => {
    const payload = buildVariantPayload({
      review: { lyricLook: "cinetico", lyricFontMain: "anton", lyricColor: "#FFFFFF", artist: "A", songTitle: "T",
        lookResolved: makeLookResolvedPin(RESOLVED, { artist: "A", songTitle: "T" }) },
    });
    expect(payload).toMatchObject({
      lyric_look: "cinetico", lyric_genre: "metal", lyric_variant: 3, lyric_font_main: "anton",
      lyric_font_key: "", lyric_alt_color: "", lyric_accent_color: "", lyric_color: "",
    });
  });

  it("sin look: campos vacíos y sin lyric_color", () => {
    const payload = buildVariantPayload({ review: { lyricLook: "", lyricFontMain: "anton" } });
    expect(payload.lyric_font_main).toBe("");
    expect(payload).not.toHaveProperty("lyric_color");
  });
});

describe("campañas", () => {
  it("la excepción de una canción guarda los campos de variante y llegan al form de generación", () => {
    const settings = reviewCreativeSettings({ lyricLook: "cine", lyricGenre: "rock", lyricVariant: 2, lyricFontKey: "creepster" }, {});
    expect(settings).toMatchObject({ lyric_look: "cine", lyric_genre: "rock", lyric_variant: 2, lyric_font_key: "creepster" });
    const form = campaignGenerateForm({ job_id: "j", assignment: { revision: 1 }, settings }, { segments_json: [] });
    expect(form.get("lyric_genre")).toBe("rock");
    expect(form.get("lyric_variant")).toBe("2");
    expect(form.get("lyric_font_key")).toBe("creepster");
  });

  it("sin elegir, no se agregan claves", () => {
    expect(reviewCreativeSettings({ lyricLook: "cine" }, {})).not.toHaveProperty("lyric_genre");
  });
});
