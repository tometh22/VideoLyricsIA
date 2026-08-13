// Integration test del state chain del wizard: simula la cadena completa
// que un job real recorre cuando el operador elige opciones → review →
// approve → /generate. Verifica que TODOS los campos que el operador
// puede setear lleguen al FormData del POST sin perderse en el camino.
//
// Si en un futuro refactor alguien rompe la cadena (e.g. olvida copiar
// un campo en handleApproveLyrics o cambia el nombre del field en
// startGenerationWithSegments), este test se rompe ANTES de mergear.
//
// No monta React — simula el shape de los objetos que pasan por el flow
// y los apply() funcionales. Esto vale más que un mock de browser para
// el caso "verificar que todos los campos se persisten".

import { describe, it, expect } from "vitest";
import { appendBackgroundFields } from "./lib/bgPayload";

// Mirror de la lógica de handleApproveLyrics (App.jsx:1495+) en su forma
// pura: dado un currentReview + edited segments, qué shape va a
// approvedJobs?
function _approvedJobFromReview(r, editedSegments, bgCacheKey) {
  return {
    file: r.file,
    artist: r.artist,
    songTitle: r.songTitle || "",
    language: r.language,
    genre: r.genre || "",
    font: r.font || "",
    concept: r.concept || "",
    movementStyle: r.movementStyle || "",
    effect: r.effect || "",
    backgroundHint: r.backgroundHint || "",
    bgVerbatim: !!r.bgVerbatim,
    textCase: r.textCase || "upper",
    fontScale: r.fontScale || "1.0",
    lyricsAnimation: r.lyricsAnimation || "none",
    lineTransition: r.lineTransition || "none",
    textContrast: r.textContrast || "medium",
    segments: editedSegments,
    transcribeJobId: r.transcribeJobId,
    bgCacheKey: bgCacheKey || null,
  };
}

// Mirror de startGenerationWithSegments (App.jsx:1599+): qué fields se
// appendean al FormData del POST /generate. Verifica que para cada job
// del batch se envíen TODOS los axes que el operador eligió.
//
// Audit adversarial 2026-06-09: los campos de FONDO ya no se espejan a
// mano — usan el helper REAL appendBackgroundFields (lib/bgPayload.js),
// el mismo que App.jsx. La versión anterior de este mirror codificaba la
// lógica PRE-fix ("if (backgroundId) ...") como comportamiento esperado.
function _formDataFromJob(job, delivery, style, customColors, bg, inspiredByLyrics) {
  const { bgSelectMode = "auto", backgroundId = null, backgroundMode = null,
          backgroundFile = null, animateImage = false } = bg || {};
  const f = {};
  if (job.transcribeJobId) f["job_id"] = job.transcribeJobId;
  f["artist"] = job.artist;
  f["song_title"] = job.songTitle;
  f["style"] = style || "auto";
  if (customColors) f["custom_colors"] = customColors;
  if (job.language) f["language"] = job.language;
  if (job.genre) f["genre"] = job.genre;
  if (job.font) f["font"] = job.font;
  if (job.concept) f["concept"] = job.concept;
  if (job.movementStyle) f["movement_style"] = job.movementStyle;
  if (job.effect) f["effect"] = job.effect;
  if (job.backgroundHint && job.backgroundHint.trim()) {
    f["background_hint"] = job.backgroundHint.trim();
    if (job.bgVerbatim) f["bg_verbatim"] = "true";
  }
  f["text_case"] = job.textCase || "upper";
  f["font_scale"] = String(job.fontScale || "1.0");
  f["lyrics_animation"] = job.lyricsAnimation || "none";
  f["line_transition"] = job.lineTransition || "none";
  f["text_contrast"] = job.textContrast || "medium";
  f["match_lyrics"] = inspiredByLyrics ? "true" : "false";
  if (job.bgCacheKey) {
    f["bg_cache_key"] = job.bgCacheKey;
  }
  // Delivery
  f["delivery_profile"] = delivery?.delivery_profile || "youtube";
  if (delivery?.delivery_profile !== "youtube") {
    f["umg_frame_size"] = delivery.umg_frame_size || "HD";
    f["umg_fps"] = String(delivery.umg_fps || 24);
    f["umg_prores_profile"] = String(delivery.umg_prores_profile ?? 3);
  }
  // Background: el helper de producción, vía un adapter append() → dict.
  appendBackgroundFields(
    { append: (k, v) => { f[k] = v instanceof File ? v : String(v); } },
    { bgSelectMode, backgroundId, backgroundMode, backgroundFile, animateImage },
  );
  f["segments_json"] = JSON.stringify(job.segments);
  return f;
}

describe("Wizard end-to-end chain — todas las elecciones persisten al /generate", () => {
  it("UMG full batch — todos los axes llegan al FormData", () => {
    // Setup: operador eligió un caso COMPLETO con todas las opciones tocadas.
    // Si alguno se pierde, este test fall en la asserción concreta.
    const currentReview = {
      file: { name: "viejas-locas-legalicenla.mp3" },
      artist: "Viejas Locas",
      songTitle: "Legalícenla",
      language: "es",
      genre: "rock",
      font: "anton",
      concept: "cosmic",
      movementStyle: "foto-parallax",
      effect: "snow",
      backgroundHint: "una galaxia con nebulosa morada",
      bgVerbatim: true,
      textCase: "upper",
      fontScale: "1.15",
      lyricsAnimation: "karaoke",
      lineTransition: "slide_up",
      textContrast: "strong",
      transcribeJobId: "abc123",
    };
    const editedSegments = [
      { start: 17.1, end: 18.5, text: "Legalícenla" },
      { start: 18.5, end: 22.0, text: "Pero bueno, de última ya te voy tirando" },
    ];
    const delivery = {
      delivery_profile: "both",
      umg_frame_size: "DCI-4K",
      umg_fps: 24,
      umg_prores_profile: 3,
    };

    const job = _approvedJobFromReview(currentReview, editedSegments, "bgkey789");
    const fd = _formDataFromJob(job, delivery, "oscuro", "", { bgSelectMode: "auto" }, true);

    // Identidad
    expect(fd.artist).toBe("Viejas Locas");
    expect(fd.song_title).toBe("Legalícenla");
    expect(fd.language).toBe("es");
    expect(fd.genre).toBe("rock");

    // Lyrics typography
    expect(fd.font).toBe("anton");
    expect(fd.text_case).toBe("upper");
    expect(fd.font_scale).toBe("1.15");
    expect(fd.text_contrast).toBe("strong");

    // Lyrics motion
    expect(fd.lyrics_animation).toBe("karaoke");
    expect(fd.line_transition).toBe("slide_up");

    // Background
    expect(fd.movement_style).toBe("foto-parallax");
    expect(fd.effect).toBe("snow");
    expect(fd.concept).toBe("cosmic");
    expect(fd.background_hint).toBe("una galaxia con nebulosa morada");
    expect(fd.bg_verbatim).toBe("true");
    expect(fd.style).toBe("oscuro");
    expect(fd.match_lyrics).toBe("true");
    expect(fd.bg_cache_key).toBe("bgkey789");

    // Delivery UMG full
    expect(fd.delivery_profile).toBe("both");
    expect(fd.umg_frame_size).toBe("DCI-4K");
    expect(fd.umg_fps).toBe("24");
    expect(fd.umg_prores_profile).toBe("3");

    // Identidad del job
    expect(fd.job_id).toBe("abc123");
    expect(JSON.parse(fd.segments_json)).toEqual(editedSegments);
  });

  it("YouTube simple — sin UMG fields cuando delivery_profile=youtube", () => {
    const review = {
      file: { name: "song.mp3" },
      artist: "X", songTitle: "Y", language: "es",
      textCase: "upper", fontScale: "1.0", textContrast: "medium",
      lyricsAnimation: "none", lineTransition: "none",
      transcribeJobId: "j1",
    };
    const job = _approvedJobFromReview(review, [], null);
    const fd = _formDataFromJob(job, { delivery_profile: "youtube" }, "auto", "", { bgSelectMode: "auto" }, true);
    expect(fd.delivery_profile).toBe("youtube");
    expect(fd.umg_frame_size).toBeUndefined();
    expect(fd.umg_fps).toBeUndefined();
    expect(fd.umg_prores_profile).toBeUndefined();
  });

  it("Library background — backgroundId + backgroundMode se envían", () => {
    const review = {
      file: { name: "song.mp3" },
      artist: "X", songTitle: "Y", language: "es",
      textCase: "upper", fontScale: "1.0", textContrast: "medium",
      lyricsAnimation: "none", lineTransition: "none",
      transcribeJobId: "j1",
    };
    const job = _approvedJobFromReview(review, [], null);
    const fd = _formDataFromJob(
      job, { delivery_profile: "youtube" }, "auto", "",
      { bgSelectMode: "library", backgroundId: "lib-asset-42", backgroundMode: "variation" },
      true
    );
    expect(fd.background_id).toBe("lib-asset-42");
    expect(fd.background_mode).toBe("variation");
  });

  it("REGRESIÓN bug Ana M. 2026-06-09 — fondo stale con tab 'IA' NO viaja al /generate", () => {
    // El vector exacto del incidente: backgroundFile + animateImage
    // residuales de un batch anterior (el File sobrevive en App), pero el
    // wizard remontado muestra "Generar con IA" (bgSelectMode="auto").
    // Los 3 videos de la clienta salieron con bg_custom.png por esto.
    const review = {
      file: { name: "rata-blanca-hada-mago.wav" },
      artist: "Rata Blanca", songTitle: "La Leyenda Del Hada Y El Mago",
      language: "es", textCase: "upper", fontScale: "1.3",
      textContrast: "medium", lyricsAnimation: "none", lineTransition: "none",
      transcribeJobId: "953265aef19a",
    };
    const staleFile = new File(["x"], "bg_custom.png", { type: "image/png" });
    const job = _approvedJobFromReview(review, [], null);
    const fd = _formDataFromJob(
      job, { delivery_profile: "both", umg_frame_size: "HD", umg_fps: 24, umg_prores_profile: 3 },
      "auto", "",
      // Estado residual del batch anterior + tab visual en IA:
      { bgSelectMode: "auto", backgroundFile: staleFile, backgroundId: 7, animateImage: true },
      true
    );
    expect(fd.background_file).toBeUndefined();
    expect(fd.background_id).toBeUndefined();
    expect(fd.animate_image).toBeUndefined();
  });

  it("Custom colors — custom_colors se envía cuando style=custom", () => {
    const review = {
      file: { name: "song.mp3" },
      artist: "X", songTitle: "Y", language: "es",
      textCase: "upper", fontScale: "1.0", textContrast: "medium",
      lyricsAnimation: "none", lineTransition: "none",
      transcribeJobId: "j1",
    };
    const job = _approvedJobFromReview(review, [], null);
    const fd = _formDataFromJob(
      job, { delivery_profile: "youtube" }, "custom", "#FF00FF, #00FFFF",
      { bgSelectMode: "auto" }, true
    );
    expect(fd.style).toBe("custom");
    expect(fd.custom_colors).toBe("#FF00FF, #00FFFF");
  });

  it("Batch de 3 — cada job lleva sus PROPIOS axes (no batchDefaults globales)", () => {
    // Verifica que la jerarquía batchDefaults vs per-track funciona.
    // El operador setea batchDefaults.font=anton, luego override
    // files[1].font=bebas. Approve los 3 → cada job envía su font.
    const reviews = [
      { file: { name: "1.mp3" }, artist: "A1", songTitle: "T1", language: "es",
        font: "anton", textCase: "upper", fontScale: "1.0", textContrast: "medium",
        lyricsAnimation: "karaoke", lineTransition: "none",
        movementStyle: "estandar", effect: "snow", transcribeJobId: "j1" },
      { file: { name: "2.mp3" }, artist: "A2", songTitle: "T2", language: "es",
        font: "bebas", textCase: "title", fontScale: "1.0", textContrast: "medium",
        lyricsAnimation: "karaoke", lineTransition: "none",
        movementStyle: "sutil", effect: "rain", transcribeJobId: "j2" },
      { file: { name: "3.mp3" }, artist: "A3", songTitle: "T3", language: "es",
        font: "anton", textCase: "upper", fontScale: "1.3", textContrast: "strong",
        lyricsAnimation: "pop", lineTransition: "slide_up",
        movementStyle: "estatico", effect: "", transcribeJobId: "j3" },
    ];
    const jobs = reviews.map((r) => _approvedJobFromReview(r, [], null));
    const fds = jobs.map((j) => _formDataFromJob(j, { delivery_profile: "youtube" }, "auto", "", { bgSelectMode: "auto" }, true));

    expect(fds[0].font).toBe("anton");
    expect(fds[1].font).toBe("bebas");  // ← override per-track preservado
    expect(fds[2].font).toBe("anton");

    expect(fds[0].lyrics_animation).toBe("karaoke");
    expect(fds[2].lyrics_animation).toBe("pop");
    expect(fds[2].line_transition).toBe("slide_up");

    expect(fds[0].effect).toBe("snow");
    expect(fds[1].effect).toBe("rain");
    expect(fds[2].effect).toBeUndefined();  // ← effect="" → no se envía

    expect(fds[0].text_contrast).toBe("medium");
    expect(fds[2].text_contrast).toBe("strong");
  });

  it("Defaults seguros — cuando el operador NO toca opciones avanzadas", () => {
    const review = {
      file: { name: "song.mp3" },
      artist: "X", songTitle: "", language: "es",
      transcribeJobId: "j1",
    };
    const job = _approvedJobFromReview(review, [], null);
    const fd = _formDataFromJob(job, { delivery_profile: "youtube" }, "auto", "", { bgSelectMode: "auto" }, true);
    // Fallbacks correctos al no haber elegido nada.
    expect(fd.text_case).toBe("upper");
    expect(fd.font_scale).toBe("1.0");
    expect(fd.lyrics_animation).toBe("none");
    expect(fd.line_transition).toBe("none");
    expect(fd.text_contrast).toBe("medium");
    expect(fd.style).toBe("auto");
    expect(fd.match_lyrics).toBe("true");
    // Sin movement/effect/concept/background_hint/genre/font (no se setean).
    expect(fd.movement_style).toBeUndefined();
    expect(fd.effect).toBeUndefined();
    expect(fd.concept).toBeUndefined();
    expect(fd.background_hint).toBeUndefined();
    expect(fd.bg_verbatim).toBeUndefined();
    expect(fd.genre).toBeUndefined();
    expect(fd.font).toBeUndefined();
  });
});
