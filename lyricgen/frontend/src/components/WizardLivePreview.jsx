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

export default function WizardLivePreview({ style = "oscuro", movementStyle = "", mode = "lyrics", lyric }) {
  const { t } = useI18n();
  const isAnimado = movementStyle === "animado";
  const isMinimal = style === "minimal";
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
    <div className="relative w-full aspect-video rounded-2xl overflow-hidden ring-1 ring-white/[0.08] shadow-[0_24px_70px_-24px_#000] bg-black select-none">
      <style>{`
        @keyframes wlp-sutil { to { transform: translate(2%,1.4%) scale(1.05); } }
        @keyframes wlp-estandar { to { transform: translate(-8%,4%) scale(1.2); } }
        @keyframes wlp-parallax { to { transform: translate(-5%,0) scale(1.12); } }
        @keyframes wlp-anim { to { background-position: 52px 0; } }
        @keyframes wlp-glow { to { transform: translate(-34px,26px) scale(1.1); } }
      `}</style>

      {/* moving background layer (the "camera"/scene motion) */}
      <div
        className="absolute"
        style={{
          inset: "-18%",
          background: isAnimado
            ? "repeating-linear-gradient(45deg,#6D4AFF 0 18px,#14C8A8 18px 36px)"
            : (PALETTE_BG[style] || PALETTE_BG.oscuro),
          animation: MOVE_ANIM[movementStyle] ?? MOVE_ANIM[""],
          willChange: "transform",
        }}
      />
      {!isAnimado && (
        <div
          className="absolute rounded-full"
          style={{
            width: "46%", height: "46%", left: "46%", top: "14%",
            background: "radial-gradient(circle, rgba(109,74,255,.45), transparent 62%)",
            filter: "blur(22px)", mixBlendMode: "screen",
            animation: movementStyle === "estatico" ? "none" : "wlp-glow 9s ease-in-out infinite alternate",
          }}
        />
      )}
      {/* film grain + vignette */}
      <div className="absolute inset-0 pointer-events-none" style={{ background: "repeating-linear-gradient(0deg,transparent 0 2px,rgba(255,255,255,.022) 2px 3px)" }} />
      <div className="absolute inset-0 pointer-events-none" style={{ background: "radial-gradient(120% 80% at 50% 50%, transparent 55%, rgba(0,0,0,.55))" }} />

      {/* lyric */}
      <div className="absolute inset-x-0 bottom-[20%] px-[8%] text-center">
        <div
          className={`font-extrabold tracking-[-0.03em] leading-[1.02] ${isMinimal ? "text-gray-900" : "text-white"}`}
          style={{ fontSize: "clamp(20px,4.4vw,46px)", textShadow: isMinimal ? "0 1px 12px rgba(255,255,255,.4)" : "0 4px 30px rgba(0,0,0,.6)" }}
        >
          {sample}
        </div>
      </div>

      {/* status chips */}
      <div className="absolute top-3 left-3 flex items-center gap-2">
        <span className="text-[10px] md:text-[11px] px-2.5 py-1 rounded-lg bg-black/55 backdrop-blur-md ring-1 ring-white/[0.08] text-gray-200">
          {t("upload.preview_live") || "Vista previa en vivo"}
        </span>
        {modeLabel && (
          <span className="text-[10px] md:text-[11px] px-2.5 py-1 rounded-lg bg-black/55 backdrop-blur-md ring-1 ring-white/[0.08] text-brand-light">
            {modeLabel}
          </span>
        )}
      </div>
      <div className="absolute bottom-3 right-3">
        <span className="text-[10px] px-2 py-1 rounded-md bg-black/55 backdrop-blur-md ring-1 ring-white/[0.08] text-gray-300">
          {(t("upload.preview_motion") || "Movimiento")}: {moveLabel}
        </span>
      </div>
    </div>
  );
}
