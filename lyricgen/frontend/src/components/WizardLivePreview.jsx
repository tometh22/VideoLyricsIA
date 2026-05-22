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
const MOVE_ANIM = {
  "":             "wlp-sutil 7s ease-in-out infinite alternate",
  estatico:       "none",
  sutil:          "wlp-sutil 7s ease-in-out infinite alternate",
  estandar:       "wlp-estandar 6s ease-in-out infinite alternate",
  "foto-parallax":"wlp-parallax 6s ease-in-out infinite alternate",
  animado:        "wlp-anim 1.8s linear infinite",
};

export default function WizardLivePreview({ style = "auto", customColors = "", movementStyle = "", mode = "lyrics", lyric }) {
  const { t } = useI18n();
  const isAnimado = movementStyle === "animado";
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
  const sample = (lyric || "").trim() || (t("upload.preview_sample") || "tu letra, en pantalla");
  const moveLabel = {
    "": t("upload.movement_auto") || "Auto",
    estatico: t("upload.movement_estatico") || "Estático",
    sutil: t("upload.movement_sutil") || "Sutil",
    estandar: t("upload.movement_estandar") || "Estándar",
    "foto-parallax": t("upload.movement_foto_parallax") || "Parallax",
    animado: t("upload.movement_animado") || "Animado",
  }[movementStyle] || (t("upload.movement_auto") || "Auto");
  const modeLabel = {
    auto: t("upload.mode_auto") || "Auto",
    lyrics: t("upload.inspired_by_lyrics_label") || "Inspirado en la letra",
    prompt: t("upload.bg_prompt_label") || "Mi prompt",
  }[mode] || "";

  return (
    <div className="relative w-full aspect-video rounded-2xl overflow-hidden ring-1 ring-white/[0.08] shadow-[0_24px_70px_-24px_#000] bg-black select-none" style={{ containerType: "inline-size" }}>
      <style>{`
        @keyframes wlp-sutil { to { transform: translate(2%,1.4%) scale(1.05); } }
        @keyframes wlp-estandar { to { transform: translate(-8%,4%) scale(1.2); } }
        @keyframes wlp-parallax { to { transform: translate(-5%,0) scale(1.12); } }
        @keyframes wlp-anim { to { background-position: 52px 0; } }
        @keyframes wlp-glow { to { transform: translate(-34px,26px) scale(1.1); } }
        @keyframes wlp-lyric-in { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
      `}</style>

      {/* REAL cinematic footage as the base — the preview reads like a real
          result, not a flat gradient. The camera MOVEMENT is a transform on
          the footage; the mood/palette grades it on top. */}
      <div
        className="absolute"
        style={{
          inset: "-12%",
          animation: MOVE_ANIM[movementStyle] ?? MOVE_ANIM[""],
          willChange: "transform",
          filter: isAnimado ? "saturate(2.6) contrast(1.3)" : "none",
        }}
      >
        <video src="/movement_samples/preview-bg.mp4" className="w-full h-full object-cover" autoPlay loop muted playsInline />
      </div>
      {/* palette / mood color-grade over the footage */}
      <div className="absolute inset-0 pointer-events-none" style={{ background: bgGradient, mixBlendMode: isMinimal ? "screen" : "color", opacity: isMinimal ? 0.55 : 0.55 }} />
      <div className="absolute inset-0 pointer-events-none" style={{ background: bgGradient, mixBlendMode: "soft-light", opacity: 0.4 }} />
      {/* bottom darken for lyric legibility */}
      <div className="absolute inset-x-0 bottom-0 h-2/3 pointer-events-none" style={{ background: "linear-gradient(to top, rgba(0,0,0,.62), transparent)" }} />
      {/* film grain + vignette */}
      <div className="absolute inset-0 pointer-events-none" style={{ background: "repeating-linear-gradient(0deg,transparent 0 2px,rgba(255,255,255,.022) 2px 3px)" }} />
      <div className="absolute inset-0 pointer-events-none" style={{ background: "radial-gradient(120% 80% at 50% 50%, transparent 55%, rgba(0,0,0,.55))" }} />

      {/* lyric */}
      <div className="absolute inset-x-0 bottom-[20%] px-[8%] text-center">
        <div
          key={sample}
          className={`font-extrabold tracking-[-0.03em] leading-[1.02] ${isMinimal ? "text-gray-900" : "text-white"}`}
          style={{ fontSize: "clamp(18px,7.5cqw,68px)", textShadow: isMinimal ? "0 1px 12px rgba(255,255,255,.4)" : "0 6px 34px rgba(0,0,0,.7)", animation: "wlp-lyric-in .7s cubic-bezier(.2,.8,.2,1) both" }}
        >
          {sample}
        </div>
      </div>

      {/* live indicator (clearly a status, not a button) */}
      <div className="absolute top-4 left-4 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] font-medium" style={{ color: isMinimal ? "rgba(0,0,0,.6)" : "rgba(255,255,255,.72)" }}>
        <span className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: "#14C8A8" }} />
        {t("upload.preview_live") || "Preview"}
      </div>
      {/* info caption — plain text, not pills */}
      <div className="absolute bottom-3.5 left-5 right-5 flex items-center justify-between text-[11px] font-medium" style={{ color: isMinimal ? "rgba(0,0,0,.55)" : "rgba(255,255,255,.6)", textShadow: isMinimal ? "none" : "0 1px 8px rgba(0,0,0,.5)" }}>
        <span className="truncate">{modeLabel}</span>
        <span className="shrink-0 ml-3">{(t("upload.preview_motion") || "Movimiento")}: {moveLabel}</span>
      </div>
    </div>
  );
}
