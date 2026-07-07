import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { REF_W, tierForLength, fontSizeFactor } from "../lib/lyricTiers";
import { activeWordIndex } from "../lib/karaokeTiming";
import { FONT_BY_CODE, applyCase } from "./fontCatalog";

// Studio Console live preview. Shows a sample lyric line over the selected
// palette/mood with the selected camera movement applied as a real CSS
// animation, so the operator SEES the result while deciding — instead of
// generating blind. This is GenLy's edge over template pickers like Rotor:
// the lyric is front and center, on the chosen mood + motion, before a
// single Veo credit is spent. Pure CSS, no network, no Veo cost.
//
// `style`   — palette code (oscuro/neon/minimal/calido), mirrors STYLES.
// `movementStyle` — "" | estatico | sutil | estandar | foto-parallax | animado.
// `mode`    — "auto" | "lyrics" | "prompt" (just tweaks the helper caption).
// `lyric`   — sample line to render (falls back to a placeholder).

const PALETTE_BG = {
  oscuro:  "radial-gradient(120% 90% at 70% 18%, #3a1d6e 0%, #16093a 45%, #06040f 100%)",
  neon:    "radial-gradient(120% 90% at 30% 20%, #6b0a8c 0%, #0a2740 55%, #07040f 100%)",
  minimal: "radial-gradient(120% 90% at 60% 20%, #c8bed2 0%, #8b94ad 55%, #4a4f63 100%)",
  calido:  "radial-gradient(120% 90% at 65% 22%, #b45a14 0%, #7a2a0c 50%, #2a0f06 100%)",
};

// Each movement maps to a CSS animation on the background layer. Static = none.
// UX 2026-05-24: amplificadas para que el cambio entre movimientos sea
// visiblemente distinto al click. Antes los deltas eran tan chicos
// (~2%/1.4%) que el operador no notaba el cambio.
const MOVE_ANIM = {
  "":             "wlp-sutil 5s ease-in-out infinite alternate",
  estatico:       "none",
  sutil:          "wlp-sutil 5s ease-in-out infinite alternate",
  estandar:       "wlp-estandar 4s ease-in-out infinite alternate",
  "foto-parallax":"wlp-parallax 4.5s ease-in-out infinite alternate",
  animado:        "wlp-anim 1.8s linear infinite",
};

// Typography + case transform now come from the shared ./fontCatalog so the
// preview can't drift from the picker. The inline copy here used to omit
// fredoka/quicksand/nunito, so those three fell through FONT_BY_CODE[font]
// to the "" (Auto) default and rendered in the generic Tailwind sans — the
// operator picked "Fredoka (redondeada)" and saw a plain sans (2026-06-17).

// Outline + shadow strength per contrast code. Same shape as
// LyricVideoPreview.CONTRAST_STYLES (paso 6) so the wizard preview
// reads consistent with what the operator will see in the editor.
const CONTRAST_STYLES = {
  subtle: { WebkitTextStroke: "0px",                 textShadow: "0 1px 5px rgba(0,0,0,.55)" },
  // 2026-06-04: dropped the wide radial glow (medium `0 0 18px`, strong `0 0 6px`
  // full-black). On 2-line all-caps text the per-glyph glows merged into a dark
  // cloud that read as a "recuadro" behind the lyrics — a preview-only artifact
  // (libass uses a crisp outline + drop shadow, no wide glow, so the rendered
  // video never showed it). Keep the crisp outline + stroke for legibility.
  medium: { WebkitTextStroke: "1px rgba(0,0,0,.55)", textShadow: "0 2px 0 #000, 0 0 3px rgba(0,0,0,.5)" },
  strong: { WebkitTextStroke: "1.5px #000",          textShadow: "-1px -1px 0 #000, 1px 1px 0 #000, 0 2px 0 #000" },
};

// Convert #RRGGBB → rgba(r,g,b,a). Returns null if hex is malformed —
// caller should fall back to a theme default. Used for the un-sung
// karaoke color which needs a softer opacity than the picked hex.
function hexToRgba(hex, alpha) {
  if (typeof hex !== "string" || !/^#[0-9a-fA-F]{6}$/.test(hex)) return null;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export default function WizardLivePreview({
  style = "auto",
  customColors = "",
  movementStyle = "",
  effect = "",
  lyricsAnimation = "none",
  lineTransition = "none",
  // Lyric text colors 2026-05-25:
  // - lyricColor: color del texto (no-cantada para karaoke; texto único
  //   para el resto de animaciones).
  // - lyricSungColor: solo aplica a karaoke = color de la palabra cantada.
  // Default #FFFFFF: preservar el look histórico cuando el operador no
  // toca el picker.
  lyricColor = "#FFFFFF",
  lyricSungColor = "#FFFFFF",
  // Typography 2026-05-26: estos 4 props llegan de batchDefaults en
  // UploadZone (step 4 del wizard). Sin ellos el preview se quedaba
  // mostrando el look default (Montserrat extrabold, UPPER hardcoded en
  // libass pero NO acá, sin contrast/scale) mientras el operador
  // configuraba font/case/size/contrast — disconnect total entre lo
  // elegido y lo que se veía. "" / defaults mantienen el look histórico
  // cuando el operador no toca nada.
  font = "",
  textCase = "upper",
  fontScale = "1.0",
  textContrast = "medium",
  mode = "lyrics",
  lyric,
  clipSrc = "/movement_samples/estandar.mp4",
  // QA fix 2026-05-28: cuando clipSrc apunta a un asset image (library
  // bg .jpg/.png), el <video> element falla a cargar → negro. Esta
  // prop manda renderizar <img> en cambio. Default true para no romper
  // call sites existentes que pasaban solo movement samples (siempre mp4).
  clipIsVideo = true,
  // Phase C 2026-05-25: ref que recibe playback tick desde LyricsEditor.
  // Cuando el operador clickea play en el editor (paso 6), el ref publica
  // {activeLine, activeStart, activeEnd, currentTime} a 60fps. Lo leemos
  // con nuestro propio rAF para renderizar word-jump real sincronizado al
  // audio, sin disparar re-renders en App.jsx/UploadZone.
  playbackTickRef = null,
  // Post-render edit mode: MP4 ya renderizado del job que el operador está
  // editando. Cuando viene set, el preview muta a "video del resultado
  // actual" — sin simulación de karaoke, sin overlays de palette/effect/
  // grade (todo eso ya está horneado en el MP4). Sirve de referencia
  // visual del estado actual mientras el operador edita lyrics/typography
  // en la columna derecha del wizard.
  renderedVideoUrl = null,
  // UI F5 (2026-05-26): cuando el componente sabe que está mostrando el
  // fondo placeholder (no el final que va a entregar la IA), el badge
  // cambia su lenguaje visual: "VISTA PREVIA (muestra)" con dot ámbar
  // estático, en lugar de "VISTA PREVIA EN VIVO" con dot verde
  // pulsante. Sin esto el badge mentía: decía "live" mientras el chip
  // de status abajo decía "fondo es muestra".
  placeholderBg = false,
  // UI F1+F3 (2026-05-26): modo compact para el paso 6 — el preview
  // pasa a thumbnail (~280-360 px). Solo afecta el sizing externo si
  // el caller setea max-width; el componente no se auto-encoge.
  // Mantenemos esta flag para que LyricsEditor / UploadZone puedan
  // alterar la composición interna (ocultar el caption inferior con
  // movement + effect, que en paso 6 ya no se está editando).
  compact = false,
}) {
  const { t } = useI18n();
  // Phase C: estado local del tick. Inicializa vacío; el rAF interno lo
  // actualiza periodicamente desde playbackTickRef. setState dispara
  // re-render del preview, NO de los componentes padres (gracias al ref).
  const [livePlaybackTick, setLivePlaybackTick] = useState(null);
  const lastTickRef = useRef({ activeLine: "", currentTime: -1 });
  // Render-storm detector (P0 UMG Chile 2026-06-16: "duplicar una línea hace
  // titilar toda la pantalla y se queda pegado"). A flicker is the active line
  // changing many times per second — but the 3 s debounced autosave can't
  // record a 60 fps loop, so prod had no trace. We count activeLine changes
  // per 1 s window; if it exceeds a clearly-abnormal rate (normal playback
  // changes lines every few seconds, i.e. <1/s), we emit one tagged warning
  // that observability.js forwards to Sentry (throttled 1/min/tag) with the
  // two oscillating texts. The URL already carries the job_id (/videos/.../edit-lyrics).
  const _stormRef = useRef({ windowStart: 0, changes: 0 });
  const STORM_CHANGES_PER_S = 12; // >12 active-line flips/s = oscillation, not playback
  useEffect(() => {
    if (!playbackTickRef) return undefined;
    let raf = 0;
    const loop = (ts) => {
      const tick = playbackTickRef.current;
      if (tick && tick.activeLine) {
        // Solo dispara setState si cambió la línea activa o el currentTime
        // se movió >40ms (suficiente para word-jump perceptible, ~25Hz).
        // Sin este guard, setState a 60fps haría thrashing.
        const last = lastTickRef.current;
        const lineChanged = tick.activeLine !== last.activeLine;
        if (lineChanged) {
          const st = _stormRef.current;
          if (ts - st.windowStart > 1000) {
            if (st.changes >= STORM_CHANGES_PER_S) {
              // eslint-disable-next-line no-console
              console.warn("[render-storm] preview active line oscillating", {
                changesPerSec: st.changes,
                between: [String(last.activeLine).slice(0, 40), String(tick.activeLine).slice(0, 40)],
                currentTime: Math.round((tick.currentTime || 0) * 100) / 100,
              });
            }
            st.windowStart = ts;
            st.changes = 0;
          }
          st.changes += 1;
        }
        if (lineChanged || Math.abs(tick.currentTime - last.currentTime) > 0.04) {
          lastTickRef.current = { activeLine: tick.activeLine, currentTime: tick.currentTime };
          setLivePlaybackTick({ ...tick });
        }
      } else if (livePlaybackTick !== null) {
        // El operador pausó/paró el audio — limpiar para volver al loop sample.
        lastTickRef.current = { activeLine: "", currentTime: -1 };
        setLivePlaybackTick(null);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playbackTickRef]);
  const isAnimado = movementStyle === "animado";
  // Bug fix 2026-05-25 (reportado por operador): cuando un efecto estaba
  // activo, baseClip se forzaba a /preview_base.mp4 (escena de montañas
  // fija), IGNORANDO la elección de movement del operador. El operador
  // clickeaba el thumbnail de Sutil (interior room) pero el preview seguía
  // mostrando montañas — disconnect visual entre la galería de movements
  // y el preview central.
  //
  // Cambio: usar SIEMPRE el clipSrc del movement elegido. Los samples
  // (estatico/sutil/estandar/foto-parallax) son ya escenas calmas — un
  // overlay de nieve/lluvia no clash. El único caso problemático es
  // 'animado' (nebula 2D) + effect partículas, donde el clash es real;
  // ahí caemos a preview_base.mp4 para preservar la legibilidad de las
  // partículas. Animado SIN effect mantiene su clip original.
  const baseClip = (effect && isAnimado) ? "/preview_base.mp4" : clipSrc;
  // With an effect active the base is a FIXED calm scene, so the chosen movement
  // is conveyed by a CSS camera transform on it (Estático=still, Cinematográfico
  // =zoom/drift, Foto+parallax=lateral pan…) — clicking a register visibly
  // changes the preview's motion. Without an effect the base IS the movement
  // clip (motion already baked in), so no extra transform.
  // Si baseClip es preview_base.mp4 (escena fija calma), aplicamos CSS
  // animation para que se note el movement style (no hay motion baked).
  // Si baseClip es el movement clip directo, el motion ya está horneado
  // en el video — aplicar CSS animation encima compondría. Por eso solo
  // CSS animation cuando estamos en el fallback preview_base.
  const baseAnim = (baseClip === "/preview_base.mp4") ? (MOVE_ANIM[movementStyle] || "none") : "none";
  const isMinimal = style === "minimal";
  // Resolve the background gradient: a preset, the custom colors, or a
  // pleasant default for "auto" (the AI will pick the real colors).
  let bgGradient = PALETTE_BG[style] || PALETTE_BG.oscuro;
  if (style === "custom") {
    const parts = (customColors || "").split(",").map((x) => x.trim()).filter(Boolean);
    const c1 = parts[0] || "#6D4AFF";
    const c2 = parts[1] || "#14C8A8";
    bgGradient = `radial-gradient(120% 90% at 65% 20%, ${c1} 0%, ${c2} 60%, #06040f 100%)`;
  } else if (style === "auto" || !PALETTE_BG[style]) {
    bgGradient = "radial-gradient(120% 90% at 68% 18%, #3a1d6e 0%, #1a1140 45%, #06040f 100%)";
  }
  // A fixed generic phrase (3-4 words) so the preview reads the same for every
  // song and demos the word-level animations well — not the track's own lyric.
  // textCase 2026-05-26: aplicamos el case del operador al sample para que
  // "MAY/Aa/min/ori" se note ANTES de tener letras reales — antes el
  // sample salía siempre lowercase ignorando el pick.
  const sample = applyCase(t("upload.preview_sample") || "esta es tu letra", textCase);
  // Typography resolved values:
  // - fontInfo: { css, weight } o defaults Auto (no override).
  // - scaleN: clamp 0.6–1.5 (mismo rango que LyricVideoPreview).
  // - baseFontSize: el clamp(18px,7.5cqw,68px) histórico, escalado por scaleN.
  // - contrastStyle: outline + shadow del CONTRAST_STYLES.
  const fontInfo = FONT_BY_CODE[font] || FONT_BY_CODE[""];
  const scaleN = Math.max(0.6, Math.min(1.5, parseFloat(fontScale) || 1));
  // WYSIWYG font size (2026-06-04): mirror the render's lyric_fontsize EXACTLY
  // instead of a fixed 7.5cqw (which previewed ~1.5× too big and ignored line
  // length). Render = tier-by-character-count (ass_render.lyric_fontsize) ×
  // font_scale × per-font factor, in PlayResY=1080 → cqw = fontPx / REF_W × 100
  // (fraction of the 1920-wide frame, matching LyricVideoPreview). Length comes
  // from the actual displayed text — the live line if playing, else the sample.
  const _dispText = (livePlaybackTick && livePlaybackTick.activeLine)
    ? applyCase(livePlaybackTick.activeLine, textCase)
    : sample;
  const baseFontSize = `${((tierForLength(_dispText.length).fontPx / REF_W) * 100 * scaleN * fontSizeFactor(font)).toFixed(3)}cqw`;
  const contrastStyle = CONTRAST_STYLES[textContrast] || CONTRAST_STYLES.medium;
  const moveLabel = {
    "": t("upload.movement_auto") || "Auto",
    estatico: t("upload.movement_estatico") || "Estático",
    sutil: t("upload.movement_sutil") || "Sutil",
    estandar: t("upload.movement_estandar") || "Estándar",
    "foto-parallax": t("upload.movement_foto_parallax") || "Parallax",
    animado: t("upload.movement_animado") || "Animado",
  }[movementStyle] || (t("upload.movement_auto") || "Auto");
  const effectLabel = {
    snow: t("upload.effect_snow") || "Nieve",
    rain: t("upload.effect_rain") || "Lluvia",
    stars: t("upload.effect_stars") || "Estrellas",
    bokeh: t("upload.effect_bokeh") || "Bokeh",
    light: t("upload.effect_light") || "Luz",
  }[effect] || "";
  const modeLabel = {
    auto: t("upload.mode_auto") || "Auto",
    lyrics: t("upload.inspired_by_lyrics_label") || "Inspirado en la letra",
    prompt: t("upload.bg_prompt_label") || "Mi prompt",
  }[mode] || "";

  // Lyric-animation preview: mirrors how the chosen libass template will play.
  // Word-level templates (karaoke/word_reveal) split the line into staggered
  // spans; line-level (pop/glow) animate the whole line; none keeps the
  // original entrance. Replays whenever the template or the sample changes.
  const isWordAnim = lyricsAnimation === "karaoke" || lyricsAnimation === "word_reveal";
  const lineAnim = {
    pop: "wlp-pop .55s cubic-bezier(.2,1.4,.35,1) both",
    glow: "wlp-lyric-in .7s cubic-bezier(.2,.8,.2,1) both, wlp-glow-text 2.4s 0.7s ease-in-out infinite",
    none: "wlp-lyric-in .7s cubic-bezier(.2,.8,.2,1) both",
  }[lyricsAnimation] || "wlp-lyric-in .7s cubic-bezier(.2,.8,.2,1) both";
  const sampleWords = sample.split(/\s+/).filter(Boolean);
  // Word-level templates LOOP continuously (a constant stagger keeps the words
  // in lockstep) so the sweep/reveal is always visible — not a one-shot that
  // finishes before the operator looks. Colours come from CSS vars so it reads
  // on both dark and light (minimal) palettes.
  // Colors: si el operador picó algo distinto de #FFFFFF, usalo. Sino
  // mantenemos los defaults históricos (un-sung white/grey, sung
  // white/green). hexToRgba con alpha=0.4 dimm-ea el color un-sung del
  // operador a un tono "fantasma" tipo el grey del libass &H00808080&.
  const operatorPickedColor = lyricColor && lyricColor.toUpperCase() !== "#FFFFFF";
  const operatorPickedSung = lyricSungColor && lyricSungColor.toUpperCase() !== "#FFFFFF";
  const dim = operatorPickedColor
    ? (hexToRgba(lyricColor, 0.4) || (isMinimal ? "rgba(0,0,0,.34)" : "rgba(255,255,255,.4)"))
    : (isMinimal ? "rgba(0,0,0,.34)" : "rgba(255,255,255,.4)");
  const lit = operatorPickedSung
    ? lyricSungColor
    : (isMinimal ? "#0f9b83" : "#19E0BC");
  // Color del texto plain (none/pop/glow): el lyricColor directo si fue
  // pickeado; sino el default por tema (negro en minimal, blanco en oscuro).
  const plainTextColor = operatorPickedColor
    ? lyricColor
    : (isMinimal ? "#111827" /* text-gray-900 */ : "#FFFFFF");
  // Phase C 2026-05-25: cuando hay un livePlaybackTick activo (audio
  // reproduciendo en el editor), el preview muestra la línea REAL con
  // word-jump driven por currentTime — mismo style que el list view del
  // editor para mantener coherencia visual. Sin tick, fallback al loop
  // sample del modo legacy.
  const liveActive = livePlaybackTick && livePlaybackTick.activeLine
    ? livePlaybackTick
    : null;
  let lyricContent;
  let liveLineKey = null;
  if (liveActive) {
    // textCase aplicado a la línea real reproducida (mismo treatment que
    // el sample fallback): el operador eligió "MAY" → el preview lo
    // muestra en MAY también, no en el case original del lyric.
    const segText = applyCase(liveActive.activeLine, textCase);
    liveLineKey = segText;
    const segStart = liveActive.activeStart;
    const segEnd = liveActive.activeEnd;
    const ct = liveActive.currentTime;

    if (lyricsAnimation === "karaoke" || lyricsAnimation === "word_reveal") {
      // Per-word animations: tokenize the active line and compute which
      // word is currently being sung. Karaoke colours/scales the active
      // word; word_reveal makes words appear one-by-one as they're sung.
      // Other lyricsAnimation values (none/pop/glow) fall through to the
      // plain-text branch below — the line-level animation runs on the
      // wrapper instead.
      const tokens = segText.split(/(\s+)/);
      // Word-stamps reales cuando el tick los trae (activeWordIndex valida
      // 1:1 contra los tokens); fallback al reparto uniforme. Con el
      // lead-in (#801) la línea aparece 0.4s antes del canto — el reparto
      // uniforme solo corría adelantado ("karaoke a destiempo", 05/07).
      const activeWordIdx = activeWordIndex(
        segText, liveActive.words, segStart, segEnd, ct);
      let nonSpaceIdx = -1;
      lyricContent = tokens.map((tok, i) => {
        if (!/\S/.test(tok)) return <span key={i}>{tok}</span>;
        nonSpaceIdx += 1;
        const wActive = nonSpaceIdx === activeWordIdx;
        const wPast = nonSpaceIdx < activeWordIdx;
        if (lyricsAnimation === "word_reveal") {
          const visible = wActive || wPast;
          return (
            <span
              key={i}
              style={{
                display: "inline-block",
                opacity: visible ? 1 : 0,
                transform: visible ? "translateY(0)" : "translateY(0.4em)",
                transition: "opacity 220ms ease, transform 320ms cubic-bezier(.2,.8,.2,1)",
              }}
            >
              {tok}
            </span>
          );
        }
        return (
          <span
            key={i}
            style={{
              display: "inline-block",
              transform: wActive ? "scale(1.10)" : "scale(1)",
              transformOrigin: "center bottom",
              // wActive = palabra cantada → lit (lyricSungColor cuando el
              // operador la pickeó, sino el verde default). wPast = palabras
              // ya cantadas (alpha alto). wFuture = aún por cantar (alpha bajo).
              color: wActive
                ? lit
                : wPast
                  ? (operatorPickedColor ? (hexToRgba(lyricColor, 0.95) || (isMinimal ? "rgba(0,0,0,0.85)" : "rgba(255,255,255,0.95)")) : (isMinimal ? "rgba(0,0,0,0.85)" : "rgba(255,255,255,0.95)"))
                  : (operatorPickedColor ? (hexToRgba(lyricColor, 0.55) || (isMinimal ? "rgba(0,0,0,0.40)" : "rgba(255,255,255,0.55)")) : (isMinimal ? "rgba(0,0,0,0.40)" : "rgba(255,255,255,0.55)")),
              textShadow: wActive
                ? `0 0 14px ${hexToRgba(lit, 0.65) || "rgba(25,224,188,0.7)"}`
                : "none",
              transition: "transform 140ms cubic-bezier(.2,1.4,.35,1), color 200ms ease, text-shadow 200ms ease",
            }}
          >
            {tok}
          </span>
        );
      });
    } else {
      // none / pop / glow: render the active line as plain text. The
      // wrapper's lineAnim handles entry — replays on line change because
      // the wrapper key includes liveLineKey.
      lyricContent = segText;
    }
  } else if (isWordAnim) {
    lyricContent = sampleWords.map((w, i) => (
      <span
        key={i}
        style={{
          display: "inline-block",
          marginRight: i < sampleWords.length - 1 ? "0.26em" : 0,
          "--dim": dim,
          "--lit": lit,
          animation:
            lyricsAnimation === "word_reveal"
              ? `wlp-reveal-loop 3s ${i * 0.22}s infinite both`
              : `wlp-karaoke-sweep 2.8s ${i * 0.24}s infinite both`,
        }}
      >
        {w}
      </span>
    ));
  } else {
    lyricContent = sample;
  }

  // Line transition (movement) plays on a wrapper so it composes with the
  // inner animation. Loops continuously so it stays visible in the preview.
  const transWrapAnim = {
    slide_up: "wlp-trans-slideup 3.4s ease-in-out infinite",
    slide_side: "wlp-trans-slideside 3.4s ease-in-out infinite",
    wipe: "wlp-trans-wipe 3.4s ease-in-out infinite",
    dissolve_blur: "wlp-trans-blur 3.4s ease-in-out infinite",
  }[lineTransition];

  // Post-render edit: short-circuit a un MP4 puro sin overlays — el MP4
  // ya trae palette/effect/grade/lyrics horneados, cualquier overlay sería
  // doble exposición. El badge cambia a "Resultado actual" para que el
  // operador entienda qué está mirando.
  if (renderedVideoUrl) {
    return (
      <div className="relative w-full aspect-video rounded-2xl overflow-hidden ring-1 ring-white/[0.08] shadow-[0_24px_70px_-24px_#000] bg-black select-none">
        <video
          key={renderedVideoUrl}
          src={renderedVideoUrl}
          className="absolute inset-0 w-full h-full object-contain bg-black"
          controls
          playsInline
          preload="metadata"
        />
        <div className="absolute top-4 left-4 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] font-medium pointer-events-none" style={{ color: "rgba(255,255,255,.72)" }}>
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: "#14C8A8" }} />
          {t("upload.preview_rendered") || "Resultado actual"}
        </div>
      </div>
    );
  }

  return (
    <div className="relative w-full aspect-video rounded-2xl overflow-hidden ring-1 ring-white/[0.08] shadow-[0_24px_70px_-24px_#000] bg-black select-none" style={{ containerType: "inline-size" }}>
      <style>{`
        /* UX 2026-05-24: deltas más generosos para que cada movimiento se
           note distinto sin marearse (sigue siendo overlay sobre el preview
           base, no el render final). */
        @keyframes wlp-sutil    { to { transform: translate(3.5%,2.4%) scale(1.08); } }
        @keyframes wlp-estandar { to { transform: translate(-14%,7%)  scale(1.30); } }
        @keyframes wlp-parallax { to { transform: translate(-12%,2%)  scale(1.18); } }
        @keyframes wlp-anim     { to { background-position: 52px 0; } }
        /* Pulse del badge cuando el operador cambia de movimiento/efecto —
           feedback visual de "el preview SE actualizó". 0.7s, suave. */
        @keyframes wlp-badge-pulse {
          0%   { transform: scale(1);    box-shadow: 0 0 0 0 rgba(109, 74, 255, 0.6); }
          50%  { transform: scale(1.06); box-shadow: 0 0 0 8px rgba(109, 74, 255, 0); }
          100% { transform: scale(1);    box-shadow: 0 0 0 0 rgba(109, 74, 255, 0); }
        }
        @keyframes wlp-glow { to { transform: translate(-34px,26px) scale(1.1); } }
        @keyframes wlp-lyric-in { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
        /* lyrics-animation templates (mirror the libass render) */
        @keyframes wlp-pop { 0% { opacity: 0; transform: scale(1.16); } 55% { transform: scale(.96); } 80% { transform: scale(1.03); } 100% { opacity: 1; transform: scale(1); } }
        /* outline = thin dark contour (mirrors the libass stroke), no dark halo */
        @keyframes wlp-glow-text { 0%,100% { text-shadow: -1px -1px 0 rgba(0,0,0,.6),1px -1px 0 rgba(0,0,0,.6),-1px 1px 0 rgba(0,0,0,.6),1px 1px 0 rgba(0,0,0,.6); } 50% { text-shadow: 0 0 20px rgba(20,200,168,.9),-1px -1px 0 rgba(0,0,0,.6),1px -1px 0 rgba(0,0,0,.6),-1px 1px 0 rgba(0,0,0,.6),1px 1px 0 rgba(0,0,0,.6); } }
        /* karaoke: a fill sweep passes word by word, then loops */
        @keyframes wlp-karaoke-sweep {
          0% { color: var(--dim); text-shadow: -1px -1px 0 rgba(0,0,0,.55),1px -1px 0 rgba(0,0,0,.55),-1px 1px 0 rgba(0,0,0,.55),1px 1px 0 rgba(0,0,0,.55); }
          18%, 78% { color: var(--lit); text-shadow: 0 0 16px rgba(25,224,188,.55),-1px -1px 0 rgba(0,0,0,.55),1px -1px 0 rgba(0,0,0,.55),-1px 1px 0 rgba(0,0,0,.55),1px 1px 0 rgba(0,0,0,.55); }
          100% { color: var(--dim); text-shadow: -1px -1px 0 rgba(0,0,0,.55),1px -1px 0 rgba(0,0,0,.55),-1px 1px 0 rgba(0,0,0,.55),1px 1px 0 rgba(0,0,0,.55); }
        }
        /* word reveal: each word rises + fades in on its turn, then loops */
        @keyframes wlp-reveal-loop {
          0% { opacity: 0; transform: translateY(9px); }
          12%, 86% { opacity: 1; transform: translateY(0); }
          100% { opacity: 0; transform: translateY(9px); }
        }
        /* line transitions (movement) — loop enter/hold/exit */
        @keyframes wlp-trans-slideup { 0% { transform: translateY(60%); opacity: 0; } 18%, 82% { transform: translateY(0); opacity: 1; } 100% { transform: translateY(-60%); opacity: 0; } }
        @keyframes wlp-trans-slideside { 0% { transform: translateX(-70%); opacity: 0; } 18%, 82% { transform: translateX(0); opacity: 1; } 100% { transform: translateX(70%); opacity: 0; } }
        @keyframes wlp-trans-wipe { 0% { clip-path: inset(0 100% 0 0); } 30%, 100% { clip-path: inset(0 0 0 0); } }
        @keyframes wlp-trans-blur { 0% { filter: blur(10px); opacity: 0; } 26%, 80% { filter: blur(0); opacity: 1; } 100% { filter: blur(10px); opacity: 0; } }
      `}</style>

      {/* REAL Veo clip of the SELECTED movement style as the base — the
          preview shows the actual style's example (the clip already carries
          its camera movement). The mood/palette grades it on top.
          QA fix 2026-05-28: cuando el operador picks un library bg que es
          .jpg/.png, clipIsVideo=false y renderizamos <img>. El movement
          CSS animation aplica igual (transform en img también funciona). */}
      {clipIsVideo ? (
        <video
          key={baseClip}
          src={baseClip}
          className="absolute inset-0 w-full h-full object-cover"
          style={baseAnim !== "none" ? { animation: baseAnim, willChange: "transform" } : undefined}
          autoPlay loop muted playsInline
        />
      ) : (
        <img
          key={baseClip}
          src={baseClip}
          alt=""
          className="absolute inset-0 w-full h-full object-cover"
          style={baseAnim !== "none" ? { animation: baseAnim, willChange: "transform" } : undefined}
          draggable={false}
        />
      )}
      {/* effect overlay — particles screen-blended over the footage, BELOW the
          grade so the palette tints them too (mirrors the backend
          bg→effect→grade→subs order). mix-blend-screen makes the black loop
          background transparent, exactly like ffmpeg blend=screen. */}
      {effect ? (
        <video
          key={`fx-${effect}`}
          src={`/fx_raw/${effect}.mp4`}
          className="absolute inset-0 w-full h-full object-cover pointer-events-none"
          style={{
            mixBlendMode: "screen",
            // Mirror the backend _FX_GAIN perceptually. The raw loops are
            // screen-blended; mid-tone particles (esp. bokeh ~0.28 luma) wash
            // out on bright/candle-lit scenes with no gain, so the preview
            // showed "nothing" while the badge claimed the effect was on.
            // Boost brightness/contrast in CSS so the preview reads like the
            // render (bokeh hardest; bright-point effects need less).
            filter: {
              bokeh: "brightness(1.6) contrast(1.15)",
              stars: "brightness(1.25) contrast(1.1)",
              snow: "brightness(1.15)",
              rain: "brightness(1.1)",
              light: "brightness(1.1)",
            }[effect] || "none",
          }}
          autoPlay loop muted playsInline
        />
      ) : null}
      {/* palette / mood color-grade over the footage — softened: `color` blend
          at 0.55 over busy/colourful scenes recoloured into blotches. */}
      <div className="absolute inset-0 pointer-events-none" style={{ background: bgGradient, mixBlendMode: isMinimal ? "screen" : "color", opacity: isMinimal ? 0.5 : 0.38 }} />
      <div className="absolute inset-0 pointer-events-none" style={{ background: bgGradient, mixBlendMode: "soft-light", opacity: 0.32 }} />
      {/* NO center scrim — the real render (libass) gives the text an outline +
          shadow, not a background dim. A centered dark gradient read as a "black
          box behind the lyrics" on light scenes; legibility now comes from the
          text outline below (see the lyric textShadow), matching the output. */}
      {/* film grain + vignette */}
      <div className="absolute inset-0 pointer-events-none" style={{ background: "repeating-linear-gradient(0deg,transparent 0 2px,rgba(255,255,255,.022) 2px 3px)" }} />
      <div className="absolute inset-0 pointer-events-none" style={{ background: "radial-gradient(120% 80% at 50% 50%, transparent 55%, rgba(0,0,0,.55))" }} />

      {/* lyric — vertically centered, mirroring the real render (\an5 center) */}
      <div className="absolute inset-0 flex items-center justify-center px-[8%] text-center">
        {/* transition wrapper (movement) composes with the inner animation */}
        <div key={`${lineTransition}:${sample}`} style={{ animation: transWrapAnim }}>
          <div
            key={`${lyricsAnimation}:${liveLineKey ?? sample}`}
            className="font-extrabold tracking-[-0.03em] leading-[1.02]"
            style={{
              color: plainTextColor,
              fontSize: baseFontSize,
              // Typography props 2026-05-26: cuando font !== "" overrideamos
              // family + weight (inline gana al `font-extrabold` Tailwind);
              // si es "" (Auto), undefined deja la cascada intacta para
              // preservar el look histórico Montserrat-ish.
              fontFamily: fontInfo.css,
              fontWeight: fontInfo.weight,
              WebkitTextStroke: contrastStyle.WebkitTextStroke,
              // paint-order 2026-07-06: pintar el stroke DEBAJO del fill. Por
              // defecto el browser dibuja el -webkit-text-stroke ENCIMA del
              // relleno, así que el contorno negro cruza el glifo blanco y en
              // fuentes como Roboto/Montserrat/Jost/Outfit se ve una "A"
              // fantasma/esqueleto. Con stroke→fill el relleno tapa la mitad
              // interna del trazo y queda un borde limpio (libass ya lo hace
              // así, por eso el video final nunca mostró el artefacto).
              paintOrder: "stroke fill",
              // Paleta minimal (fondo claro): el contrastStyle.textShadow
              // (sombras oscuras) sirve igual o mejor que el highlight
              // blanco histórico para legibilidad — pero cuando el operador
              // pidió "subtle" en minimal preservamos el highlight blanco
              // original para no introducir un halo oscuro inesperado.
              textShadow: (isMinimal && textContrast === "subtle")
                ? "0 1px 0 rgba(255,255,255,.5)"
                : contrastStyle.textShadow,
              animation: isWordAnim ? undefined : lineAnim,
            }}
          >
            {lyricContent}
          </div>
        </div>
      </div>

      {/* live indicator (clearly a status, not a button).

          UI F5 (2026-05-26): cuando `placeholderBg` está set (paso 6
          mientras el pre-gen del fondo no terminó), el badge cambia de
          "VISTA PREVIA EN VIVO" + dot verde pulsante a "VISTA PREVIA
          (muestra)" + dot ámbar estático. Mismo lenguaje visual que el
          chip de status del fondo abajo — antes los dos se contradecían
          (badge live, chip diciendo "es muestra"). */}
      <div className="absolute top-4 left-4 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] font-medium" style={{ color: isMinimal ? "rgba(0,0,0,.6)" : "rgba(255,255,255,.72)" }}>
        <span
          className={placeholderBg ? "w-1.5 h-1.5 rounded-full" : "w-1.5 h-1.5 rounded-full animate-pulse"}
          style={{ background: placeholderBg ? "#F59E0B" : "#14C8A8" }}
        />
        {placeholderBg
          ? (t("upload.preview_sample_badge") || "Vista previa (muestra)")
          : (t("upload.preview_live") || "Preview")}
      </div>
      {/* info caption — plain text, not pills. El span del movimiento usa
          `key={moveLabel + effectLabel}` para que React lo desmonte/remonte
          al cambiar opción → la animación de pulse vuelve a fire. Feedback
          visual de "el preview SE actualizó". UX 2026-05-24.

          UI F3 (2026-05-26): en compact mode (paso 6 del wizard,
          preview a thumbnail) el operador ya no está modificando
          movimiento/efecto — son decisiones tomadas en pasos previos.
          Ocultar el caption ahorra altura y desclutter al thumbnail.

          UI F12 (2026-05-26): doble text-shadow + backdrop-blur en el
          background del caption suben el contraste sobre frames
          brillantes del fondo (paredes claras del living, etc.). El
          shadow simple original caía bajo WCAG AA en escenas claras. */}
      {!compact && (
        <div
          className="absolute bottom-3.5 left-5 right-5 flex items-center justify-between text-label"
          style={{
            color: isMinimal ? "rgba(0,0,0,.7)" : "rgba(255,255,255,.85)",
            textShadow: isMinimal
              ? "none"
              : "0 1px 4px rgba(0,0,0,.85), 0 0 16px rgba(0,0,0,.55)",
          }}
        >
          <span
            className="truncate px-2 py-0.5 rounded backdrop-blur-sm"
            style={{ background: isMinimal ? "rgba(255,255,255,.4)" : "rgba(0,0,0,.4)" }}
          >
            {modeLabel}
          </span>
          <span
            key={`${moveLabel}-${effectLabel}`}
            className="shrink-0 ml-3 px-2 py-0.5 rounded-full backdrop-blur-sm"
            style={{
              animation: "wlp-badge-pulse 0.7s cubic-bezier(.2,.8,.2,1) 1",
              background: isMinimal ? "rgba(0,0,0,.06)" : "rgba(255,255,255,.10)",
            }}
          >
            {(t("upload.preview_motion") || "Movimiento")}: {moveLabel}{effectLabel ? ` · ${t("upload.effect_label") || "Efecto:"} ${effectLabel}` : ""}
          </span>
        </div>
      )}
    </div>
  );
}
