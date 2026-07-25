// Contrato del submit del wizard de "Crear variante".
//
// El payload lo arma `lib/variantPayload.js` (función pura, testeada acá
// directamente). El DISPATCH vive inline en App.jsx::handleApproveLyrics
// (rama `if (r.variantMode)`), que —como el flujo de edición— es
// impráctico de montar en test: arrastra auth, router, toasts,
// useBackgroundPreview y decenas de efectos. Reproducimos el dispatcher
// en una función chica y validamos su contrato:
//
//   1. Payload ABSOLUTO: todos los ejes overridables viajan, sin diff.
//   2. Nunca manda `segments` (el backend reusa los del padre).
//   3. Biblioteca sólo si el tab activo es "library".
//   4. 402 variant_overage_unconfirmed → confirmación → re-POST idéntico
//      + acknowledge_variant_overage. Si el operador cancela: 1 sola POST.
//   5. Éxito → navega al job NUEVO (/videos/:id, no el legacy /job/:id).
//
// Si el dispatch de App.jsx diverge de este contrato, los dos archivos
// se actualizan juntos.

import { describe, expect, it, vi } from "vitest";
import { buildVariantPayload } from "./lib/variantPayload";

const API = "https://api.test";

// MIRROR de la rama `if (r.variantMode)` de App.jsx::handleApproveLyrics.
async function submitVariantMirror({
  review,
  style,
  customColors,
  bgSelectMode,
  backgroundId,
  backgroundMode,
  authFetch,
  onConfirmOverage,
  onError,
  onNavigate,
}) {
  const parentJobId = review.parentJobId;
  const payload = buildVariantPayload({
    review, style, customColors, bgSelectMode, backgroundId, backgroundMode,
  });

  const doPost = async (body) => {
    const res = await authFetch(`${API}/jobs/${parentJobId}/variant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty */ }
    return { res, data };
  };

  let { res, data } = await doPost(payload);

  if (
    res.status === 402 &&
    data?.detail && typeof data.detail === "object" &&
    data.detail.code === "variant_overage_unconfirmed"
  ) {
    if (!onConfirmOverage?.(data.detail)) return;
    ({ res, data } = await doPost({ ...payload, acknowledge_variant_overage: true }));
  }

  if (!res.ok) {
    onError?.({ status: res.status, data });
    return;
  }
  onNavigate?.(data?.job_id ? `/videos/${data.job_id}` : `/videos/${parentJobId}`);
}

// currentReview sembrado desde el render_params del padre por
// VariantWizardRoute, con algunos ejes ya tocados por el operador.
const review = () => ({
  variantMode: true,
  parentJobId: "parent123abc",
  artist: "Cerati",
  songTitle: "Crimen",
  segments: [{ start: 0, end: 2, text: "una línea" }],
  backgroundHint: "  catedral abandonada al amanecer  ",
  concept: "ruinas y niebla",
  genre: "post-rock",
  matchLyrics: false,
  bgVerbatim: true,
  movementStyle: "estatico",
  effect: "snow",
  lyricsAnimation: "karaoke",
  lineTransition: "slide_up",
  font: "bebas-neue",
  fontScale: "1.25",
  textCase: "title",
  textContrast: "strong",
  frameFormat: "cine",
  titleTemplate: "lower_third",
  titleSize: "1.4",
  titleArtistFont: "montserrat-bold",
  titleSongFont: "playfair",
  titleSongBreak: "Donde Estan\nCorazón",
});

const okResponse = (body = { ok: true, job_id: "newjob456" }) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

const overageResponse = () => ({
  ok: false,
  status: 402,
  json: async () => ({
    detail: {
      code: "variant_overage_unconfirmed",
      message: "…",
      existing_renders: 3,
      included_per_song: 3,
      cost_extra_usd: 0.9,
      artist: "Cerati",
      song_title: "Crimen",
    },
  }),
});

describe("buildVariantPayload", () => {
  it("manda el estado ABSOLUTO de cada eje overridable", () => {
    const payload = buildVariantPayload({
      review: review(),
      style: "neon",
      customColors: " #101820, #F2AA4C ",
      bgSelectMode: "auto",
      backgroundId: null,
      backgroundMode: "as_is",
    });

    expect(payload).toMatchObject({
      // fondo / escena
      background_hint: "catedral abandonada al amanecer",  // trimmed
      concept: "ruinas y niebla",
      genre: "post-rock",
      match_lyrics: false,
      bg_verbatim: true,
      movement_style: "estatico",
      // FX + letra
      effect: "snow",
      lyrics_animation: "karaoke",
      line_transition: "slide_up",
      // tipografía
      font: "bebas-neue",
      font_scale: 1.25,
      text_case: "title",
      text_contrast: "strong",
      frame_format: "cine",
      // portada
      title_template: "lower_third",
      title_size: 1.4,
      title_artist_font: "montserrat-bold",
      title_song_font: "playfair",
      title_song_break: "Donde Estan\nCorazón",
      // paleta — editable en variante (a diferencia de /edit)
      style: "neon",
      custom_colors: "#101820, #F2AA4C",
    });
    // font_scale / title_size viajan como NÚMERO (el wizard los guarda
    // como string); el backend los declara float.
    expect(typeof payload.font_scale).toBe("number");
    expect(typeof payload.title_size).toBe("number");
  });

  it("nunca manda segments — el backend reusa los del padre", () => {
    const payload = buildVariantPayload({ review: review(), style: "auto" });
    expect(payload).not.toHaveProperty("segments");
    expect(payload).not.toHaveProperty("artist");
    expect(payload).not.toHaveProperty("song_title");
  });

  it("un hint vacío viaja como \"\" (clear explícito), no se omite", () => {
    const payload = buildVariantPayload({
      review: { ...review(), backgroundHint: "   " },
      style: "auto",
    });
    expect(payload.background_hint).toBe("");
  });

  it("cae a defaults sanos cuando la review viene vacía", () => {
    const payload = buildVariantPayload({ review: {}, style: "" });
    expect(payload).toMatchObject({
      background_hint: "",
      match_lyrics: false,
      bg_verbatim: false,
      lyrics_animation: "none",
      line_transition: "none",
      text_case: "upper",
      text_contrast: "medium",
      frame_format: "full",
      title_template: "auto",
      font_scale: 1,
      title_size: 1,
      style: "auto",
    });
  });

  it("manda la Biblioteca sólo cuando el tab activo es 'library'", () => {
    const withLib = buildVariantPayload({
      review: review(), style: "auto",
      bgSelectMode: "library", backgroundId: 42, backgroundMode: "variation",
    });
    expect(withLib.background_id).toBe(42);
    expect(withLib.background_mode).toBe("variation");

    // Mismo backgroundId residual pero el tab volvió a "IA": no puede
    // filtrarse (mismo gate que appendBackgroundFields para /generate).
    const withoutLib = buildVariantPayload({
      review: review(), style: "auto",
      bgSelectMode: "auto", backgroundId: 42, backgroundMode: "variation",
    });
    expect(withoutLib).not.toHaveProperty("background_id");
    expect(withoutLib).not.toHaveProperty("background_mode");
  });

  it("normaliza background_mode a as_is cuando no es 'variation'", () => {
    const payload = buildVariantPayload({
      review: review(), style: "auto",
      bgSelectMode: "library", backgroundId: 7, backgroundMode: undefined,
    });
    expect(payload.background_mode).toBe("as_is");
  });

  it("siempre manda exactamente una política de content-validation", () => {
    const forced = buildVariantPayload({ review: review(), style: "auto" });
    expect(forced.force_content_validation).toBe(true);
    expect(forced).not.toHaveProperty("bypass_content_validation");

    const free = buildVariantPayload({
      review: { ...review(), bgRegenValidation: false }, style: "auto",
    });
    expect(free.bypass_content_validation).toBe(true);
    expect(free).not.toHaveProperty("force_content_validation");
  });
});

describe("submit del wizard de variante", () => {
  it("postea una sola vez y navega al job NUEVO", async () => {
    const authFetch = vi.fn().mockResolvedValue(okResponse());
    const onNavigate = vi.fn();

    await submitVariantMirror({
      review: review(), style: "neon", customColors: "",
      bgSelectMode: "auto", authFetch, onNavigate,
    });

    expect(authFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe(`${API}/jobs/parent123abc/variant`);
    expect(opts.method).toBe("POST");
    const body = JSON.parse(opts.body);
    expect(body.acknowledge_variant_overage).toBeUndefined();
    expect(body.style).toBe("neon");
    // La ruta real del detalle es /videos/:id (el modal viejo navegaba a
    // /job/:id, que no existe).
    expect(onNavigate).toHaveBeenCalledWith("/videos/newjob456");
  });

  it("402 overage → confirmación → re-POST idéntico + acknowledge", async () => {
    const authFetch = vi.fn()
      .mockResolvedValueOnce(overageResponse())
      .mockResolvedValueOnce(okResponse({ ok: true, job_id: "extra789" }));
    const onConfirmOverage = vi.fn().mockReturnValue(true);
    const onNavigate = vi.fn();
    const onError = vi.fn();

    await submitVariantMirror({
      review: review(), style: "auto", customColors: "",
      bgSelectMode: "auto", authFetch, onConfirmOverage, onNavigate, onError,
    });

    // El operador vio el costo exacto, no un JSON crudo.
    expect(onConfirmOverage).toHaveBeenCalledTimes(1);
    const detail = onConfirmOverage.mock.calls[0][0];
    expect(detail.existing_renders).toBe(3);
    expect(detail.included_per_song).toBe(3);
    expect(detail.cost_extra_usd).toBe(0.9);

    expect(authFetch).toHaveBeenCalledTimes(2);
    const first = JSON.parse(authFetch.mock.calls[0][1].body);
    const second = JSON.parse(authFetch.mock.calls[1][1].body);
    expect(second.acknowledge_variant_overage).toBe(true);
    // El re-POST no puede perder ni cambiar ningún otro campo.
    expect({ ...second, acknowledge_variant_overage: undefined })
      .toEqual({ ...first, acknowledge_variant_overage: undefined });

    expect(onError).not.toHaveBeenCalled();
    expect(onNavigate).toHaveBeenCalledWith("/videos/extra789");
  });

  it("402 overage rechazado → no se crea nada", async () => {
    const authFetch = vi.fn().mockResolvedValue(overageResponse());
    const onConfirmOverage = vi.fn().mockReturnValue(false);
    const onNavigate = vi.fn();
    const onError = vi.fn();

    await submitVariantMirror({
      review: review(), style: "auto", customColors: "",
      bgSelectMode: "auto", authFetch, onConfirmOverage, onNavigate, onError,
    });

    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(onNavigate).not.toHaveBeenCalled();
    // No es un error: el operador decidió no gastar. Nada de toast rojo.
    expect(onError).not.toHaveBeenCalled();
  });

  it("un 4xx que no es el overage se reporta como error, sin re-POST", async () => {
    const authFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: "No se pueden crear variantes de un Art Track." }),
    });
    const onConfirmOverage = vi.fn();
    const onNavigate = vi.fn();
    const onError = vi.fn();

    await submitVariantMirror({
      review: review(), style: "auto", customColors: "",
      bgSelectMode: "auto", authFetch, onConfirmOverage, onNavigate, onError,
    });

    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(onConfirmOverage).not.toHaveBeenCalled();
    expect(onNavigate).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ status: 400 }),
    );
  });
});
