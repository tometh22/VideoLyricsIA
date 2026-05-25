import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";

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

export default function WizardLivePreview({
  style = "auto",
  customColors = "",
  movementStyle = "",
  effect = "",
  lyricsAnimation = "none",
  lineTransition = "none",
  mode = "lyrics",
  lyric,
  clipSrc = "/movement_samples/estandar.mp4",
  // Phase C 2026-05-25: ref que recibe playback tick desde LyricsEditor.
  // Cuando el operador clickea play en el editor (paso 6), el ref publica
  // {activeLine, activeStart, activeEnd, currentTime} a 60fps. Lo leemos
  // con nuestro propio rAF para renderizar word-jump real sincronizado al
  // audio, sin disparar re-renders en App.jsx/UploadZone.
  playbackTickRef = null,
}) {
  const { t } = useI18n();
  // Phase C: estado local del tick. Inicializa vacío; el rAF interno lo
  // actualiza periodicamente desde playbackTickRef. setState dispara
  // re-render del preview, NO de los componentes padres (gracias al ref).
  const [livePlaybackTick, setLivePlaybackTick] = useState(null);
  const lastTickRef = useRef({ activeLine: "", currentTime: -1 });
  useEffect(() => {
    if (!playbackTickRef) return undefined;
    let raf = 0;
    const loop = () => {
      const tick = playbackTickRef.current;
      if (tick && tick.activeLine) {
        // Solo dispara setState si cambió la línea activa o el currentTime
        // se movió >40ms (suficiente para word-jump perceptible, ~25Hz).
        // Sin este guard, setState a 60fps haría thrashing.
        const last = lastTickRef.current;
        if (
          tick.activeLine !== last.activeLine ||
          Math.abs(tick.currentTime - last.currentTime) > 0.04
        ) {
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
  // With an effect active, compose it over a CALM, neutral premium scene (not
  // the movement-sample clip) so particles never clash with a busy scene like
  // the nebula. Without an effect, show the chosen movement's own clip.
  const baseClip = effect ? "/preview_base.mp4" : clipSrc;
  // With an effect active the base is a FIXED calm scene, so the chosen movement
  // is conveyed by a CSS camera transform on it (Estático=still, Cinematográfico
  // =zoom/drift, Foto+parallax=lateral pan…) — clicking a register visibly
  // changes the preview's motion. Without an effect the base IS the movement
  // clip (motion already baked in), so no extra transform.
  const baseAnim = effect ? (MOVE_ANIM[movementStyle] || "none") : "none";
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
  const sample = t("upload.preview_sample") || "esta es tu letra";
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
  const dim = isMinimal ? "rgba(0,0,0,.34)" : "rgba(255,255,255,.4)";
  const lit = isMinimal ? "#0f9b83" : "#19E0BC";
  // Phase C 2026-05-25: cuando hay un livePlaybackTick activo (audio
  // reproduciendo en el editor), el preview muestra la línea REAL con
  // word-jump driven por currentTime — mismo style que el list view del
  // editor para mantener coherencia visual. Sin tick, fallback al loop
  // sample del modo legacy.
  const liveActive = livePlaybackTick && livePlaybackTick.activeLine
    ? livePlaybackTick
    : null;
  let lyricContent;
  if (liveActive) {
    const segText = liveActive.activeLine;
    const segStart = liveActive.activeStart;
    const segEnd = liveActive.activeEnd;
    const ct = liveActive.currentTime;
    const tokens = segText.split(/(\s+)/);
    const wordsOnly = tokens.filter((tok) => /\S/.test(tok));
    const N = wordsOnly.length;
    const dur = Math.max(0.001, segEnd - segStart);
    const wDur = dur / Math.max(1, N);
    const elapsed = Math.max(0, ct - segStart);
    const activeWordIdx = Math.min(N - 1, Math.max(0, Math.floor(elapsed / wDur)));
    let nonSpaceIdx = -1;
    lyricContent = tokens.map((tok, i) => {
      if (!/\S/.test(tok)) return <span key={i}>{tok}</span>;
      nonSpaceIdx += 1;
      const wActive = nonSpaceIdx === activeWordIdx;
      const wPast = nonSpaceIdx < activeWordIdx;
      return (
        <span
          key={i}
          style={{
            display: "inline-block",
            transform: wActive ? "scale(1.10)" : "scale(1)",
            transformOrigin: "center bottom",
            color: wActive
              ? (isMinimal ? "#0f9b83" : "#19E0BC")
              : wPast
                ? (isMinimal ? "rgba(0,0,0,0.85)" : "rgba(255,255,255,0.95)")
                : (isMinimal ? "rgba(0,0,0,0.40)" : "rgba(255,255,255,0.55)"),
            textShadow: wActive
              ? (isMinimal ? "0 0 14px rgba(20,200,168,0.6)" : "0 0 14px rgba(25,224,188,0.7)")
              : "none",
            transition: "transform 140ms cubic-bezier(.2,1.4,.35,1), color 200ms ease, text-shadow 200ms ease",
          }}
        >
          {tok}
        </span>
      );
    });
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
          its camera movement). The mood/palette grades it on top. */}
      <video
        key={baseClip}
        src={baseClip}
        className="absolute inset-0 w-full h-full object-cover"
        style={baseAnim !== "none" ? { animation: baseAnim, willChange: "transform" } : undefined}
        autoPlay loop muted playsInline
      />
      {/* effect overlay — particles screen-blended over the footage, BELOW the
          grade so the palette tints them too (mirrors the backend
          bg→effect→grade→subs order). mix-blend-screen makes the black loop
          background transparent, exactly like ffmpeg blend=screen. */}
      {effect ? (
        <video
          key={`fx-${effect}`}
          src={`/fx_raw/${effect}.mp4`}
          className="absolute inset-0 w-full h-full object-cover pointer-events-none"
          style={{ mixBlendMode: "screen" }}
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
            key={`${lyricsAnimation}:${sample}`}
            className={`font-extrabold tracking-[-0.03em] leading-[1.02] ${isMinimal ? "text-gray-900" : "text-white"}`}
            style={{ fontSize: "clamp(18px,7.5cqw,68px)", textShadow: isMinimal ? "0 1px 0 rgba(255,255,255,.5)" : "-1px -1px 0 rgba(0,0,0,.6), 1px -1px 0 rgba(0,0,0,.6), -1px 1px 0 rgba(0,0,0,.6), 1px 1px 0 rgba(0,0,0,.6)", animation: isWordAnim ? undefined : lineAnim }}
          >
            {lyricContent}
          </div>
        </div>
      </div>

      {/* live indicator (clearly a status, not a button) */}
      <div className="absolute top-4 left-4 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] font-medium" style={{ color: isMinimal ? "rgba(0,0,0,.6)" : "rgba(255,255,255,.72)" }}>
        <span className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: "#14C8A8" }} />
        {t("upload.preview_live") || "Preview"}
      </div>
      {/* info caption — plain text, not pills. El span del movimiento usa
          `key={moveLabel + effectLabel}` para que React lo desmonte/remonte
          al cambiar opción → la animación de pulse vuelve a fire. Feedback
          visual de "el preview SE actualizó". UX 2026-05-24. */}
      <div className="absolute bottom-3.5 left-5 right-5 flex items-center justify-between text-label" style={{ color: isMinimal ? "rgba(0,0,0,.55)" : "rgba(255,255,255,.6)", textShadow: isMinimal ? "none" : "0 1px 8px rgba(0,0,0,.5)" }}>
        <span className="truncate">{modeLabel}</span>
        <span
          key={`${moveLabel}-${effectLabel}`}
          className="shrink-0 ml-3 px-2 py-0.5 rounded-full"
          style={{
            animation: "wlp-badge-pulse 0.7s cubic-bezier(.2,.8,.2,1) 1",
            background: isMinimal ? "rgba(0,0,0,.06)" : "rgba(255,255,255,.06)",
          }}
        >
          {(t("upload.preview_motion") || "Movimiento")}: {moveLabel}{effectLabel ? ` · ${t("upload.effect_label") || "Efecto"}: ${effectLabel}` : ""}
        </span>
      </div>
    </div>
  );
}
