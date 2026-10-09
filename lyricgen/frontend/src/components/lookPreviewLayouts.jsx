// Composiciones CSS de los looks de letra para WizardLivePreview (y las
// piezas que comparten las miniaturas del picker). Espejo aproximado de
// backend/lyric_looks.py: no es pixel-perfect, alcanza con que cada look se
// reconozca — composición, color, movimiento y tratamiento del fondo.
//
// Coordenadas: todo se calcula en px de un cuadro de 1920×1080 (como el
// render) y se pasa a `cqw` con ctx.toCqw, así escala con el preview.
//
// Dos modos (igual que los looks originales):
//   - muestra: keyframes one-shot que arrancan con el ciclo (la clave del
//     wrapper cambia en cada vuelta y reinicia todo). Las palabras entran en
//     sampleWordDelay(i).
//   - en vivo: visibilidad por palabra cantada (sungIdx de karaokeTiming) y
//     movimientos de línea calculados del currentTime.

import { useId } from "react";
import {
  kineticRows, blockRows, pickKeyword, handWordTilt,
} from "../lib/lyricLooks";

const FRAME_W = 1920;
const FRAME_H = 1080;
const clamp01 = (x) => Math.max(0, Math.min(1, x));

/** Momento (s) en que entra la palabra i en el loop de muestra. */
export const sampleWordDelay = (i) => 0.15 + i * 0.32;

/** Hash chico y determinístico (decide entradas/decoración por línea). */
function seedOf(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/**
 * Estilo de línea de un look en un instante (modo en vivo). Espejo de
 * backend lyric_looks._line_motion / ass_render zoom_through: entrada y salida
 * caen DENTRO de la ventana de la línea. `opts.textLen` lo usa write_on.
 */
export function lookLineMotionStyle(motion, elapsedS, durS, opts = {}) {
  const dur = Math.max(0.05, durS);
  const t = Math.max(0, Math.min(dur, elapsedS));
  if (motion === "zoom_through") {
    const arrive = Math.min(0.4, dur * 0.3);
    const exit = Math.min(0.3, dur * 0.25);
    if (t < arrive) {
      const q = clamp01(t / arrive);
      return { transform: `scale(${(0.3 + 0.7 * q).toFixed(3)})`, opacity: q };
    }
    if (t < dur - exit) {
      const q = clamp01((t - arrive) / Math.max(0.001, dur - exit - arrive));
      return { transform: `scale(${(1 + 0.08 * q).toFixed(3)})`, opacity: 1 };
    }
    const q = clamp01((t - (dur - exit)) / exit);
    return {
      transform: `scale(${(1.08 + 7.92 * q * q).toFixed(3)})`,
      opacity: 1 - q,
      filter: `blur(${(6 * q).toFixed(2)}px)`,
    };
  }
  if (motion === "cine") {
    const enter = Math.min(0.75, Math.max(0.25, dur * 0.3));
    const exit = Math.min(0.42, Math.max(0.16, dur * 0.18));
    if (t < enter) {
      const q = clamp01(t / enter);
      return {
        opacity: q,
        filter: `blur(${(10 * (1 - q)).toFixed(2)}px)`,
        letterSpacing: `${(0.22 - 0.19 * q).toFixed(3)}em`,
      };
    }
    if (t > dur - exit) {
      const q = clamp01((t - (dur - exit)) / exit);
      return { opacity: 1 - q, filter: `blur(${(8 * q).toFixed(2)}px)`, letterSpacing: "0.03em" };
    }
    return { opacity: 1, filter: "blur(0px)", letterSpacing: "0.03em" };
  }
  if (motion === "fade") {
    const f = Math.min(0.16, Math.max(0.06, dur * 0.08));
    return { opacity: Math.min(clamp01(t / f), clamp01((dur - t) / f)) };
  }
  // Cinético: la composición entera se "barre" de costado al final.
  if (motion === "smear") {
    const exit = Math.min(0.26, Math.max(0.12, dur * 0.12));
    const q = clamp01((t - (dur - exit)) / exit);
    if (q <= 0) return { opacity: 1, transform: "none", filter: "blur(0px)" };
    return {
      opacity: 1 - q,
      transform: `scaleX(${(1 + 1.3 * q).toFixed(3)})`,
      filter: `blur(${(8 * q).toFixed(2)}px)`,
    };
  }
  // Duotono: la tinta crece, "hierve" (adentro) y se barre de costado.
  if (motion === "boil") {
    const enter = 0.16;
    const exit = Math.min(0.26, Math.max(0.12, dur * 0.12));
    if (t < enter) {
      const q = clamp01(t / enter);
      return { opacity: q, transform: `scale(${(0.7 + 0.3 * q).toFixed(3)})`, filter: "blur(0px)" };
    }
    const q = clamp01((t - (dur - exit)) / exit);
    if (q <= 0) return { opacity: 1, transform: "none", filter: "blur(0px)" };
    return {
      opacity: 1 - q,
      transform: `scale(${(1 + 2.2 * q).toFixed(3)}, ${(1 + 0.15 * q).toFixed(3)})`,
      filter: `blur(${(10 * q).toFixed(2)}px)`,
    };
  }
  // Romántico: la cursiva se escribe de izquierda a derecha.
  if (motion === "write_on") {
    const write = Math.max(0.05, Math.min(dur * 0.55, 0.07 * (opts.textLen || 16)));
    const q = clamp01(t / write);
    const out = clamp01((t - (dur - 0.32)) / 0.32);
    return {
      clipPath: `inset(-40% ${(105 * (1 - q) - 5).toFixed(2)}% -40% -5%)`,
      opacity: 1 - out,
    };
  }
  return {};
}

/** Brillo del tubo de neón (0-1) en vivo: vidrio apagado, titileo, prendido. */
export function neonLitAlpha(elapsedS, durS) {
  const t = Math.max(0, elapsedS);
  const dur = Math.max(0.3, durS);
  if (t < 0.26) return 0;
  const fadeOut = clamp01((t - (dur - 0.2)) / 0.2);
  let a = 1;
  if ((t >= 0.33 && t < 0.38) || (t >= 0.48 && t < 0.52)) a = 0.25;
  return a * (1 - fadeOut);
}

// ── Keyframes ───────────────────────────────────────────────────────────────

// Entradas del Cinético (backend _ENTRANCES): estado "desde" de cada una.
export const KINETIC_FROM = {
  left: "translateX(-28cqw)",
  right: "translateX(28cqw)",
  drop: "translateY(-14cqw) scaleY(1.2)",
  pop: "scale(.4)",
  spin: "rotate(-80deg) scale(.4)",
  stretch: "scaleX(2.6) scaleY(.6)",
};
const KINETIC_KINDS = ["left", "right", "drop", "pop", "spin", "stretch"];

export const LOOK_LAYOUT_KEYFRAMES = `
  @keyframes wlp-kin-left { 0% { transform: ${KINETIC_FROM.left}; opacity: 0; } 50% { opacity: 1; } 100% { transform: none; opacity: 1; } }
  @keyframes wlp-kin-right { 0% { transform: ${KINETIC_FROM.right}; opacity: 0; } 50% { opacity: 1; } 100% { transform: none; opacity: 1; } }
  @keyframes wlp-kin-drop { 0% { transform: ${KINETIC_FROM.drop}; opacity: 0; } 55% { transform: translateY(0) scaleY(1.2); opacity: 1; } 75% { transform: scaleY(.86); } 100% { transform: none; opacity: 1; } }
  @keyframes wlp-kin-pop { 0% { transform: ${KINETIC_FROM.pop}; opacity: 0; } 60% { transform: scale(1.12); opacity: 1; } 100% { transform: none; opacity: 1; } }
  @keyframes wlp-kin-spin { 0% { transform: ${KINETIC_FROM.spin}; opacity: 0; } 100% { transform: none; opacity: 1; } }
  @keyframes wlp-kin-stretch { 0% { transform: ${KINETIC_FROM.stretch}; opacity: 0; } 100% { transform: none; opacity: 1; } }
  @keyframes wlp-kin-pulse { 0%, 100% { transform: scale(1); } 14% { transform: scale(1.1); } 45% { transform: scale(1); } }
  @keyframes wlp-kin-blink { 0% { opacity: 1; } 50% { opacity: .3; } }
  @keyframes wlp-kin-bubble { from { transform: rotate(var(--rot)) scale(.2); opacity: 0; } to { transform: rotate(var(--rot)) scale(1); opacity: 1; } }
  @keyframes wlp-look-reveal { from { clip-path: inset(-40% 100% -40% -5%); } to { clip-path: inset(-40% -5% -40% -5%); } }
  @keyframes wlp-look-smear { 0%, 92% { transform: none; filter: blur(0); opacity: 1; } 100% { transform: scaleX(2.3); filter: blur(8px); opacity: 0; } }
  @keyframes wlp-look-boilline { 0% { transform: scale(.7); opacity: 0; } 5% { transform: none; opacity: 1; filter: blur(0); } 92% { transform: none; opacity: 1; filter: blur(0); } 100% { transform: scale(3.2, 1.15); opacity: 0; filter: blur(10px); } }
  @keyframes wlp-look-boil { 0% { transform: rotate(-1.2deg) scale(1); } 25% { transform: rotate(.84deg) scale(1.014); } 50% { transform: rotate(-.48deg) scale(.992); } 75% { transform: rotate(1.2deg) scale(1.008); } }
  @keyframes wlp-look-writeon { 0% { clip-path: inset(-40% 100% -40% -5%); opacity: 1; } 35% { clip-path: inset(-40% -5% -40% -5%); } 90% { opacity: 1; } 100% { clip-path: inset(-40% -5% -40% -5%); opacity: 0; } }
  @keyframes wlp-look-write { from { clip-path: inset(-40% 100% -40% -10%); } to { clip-path: inset(-40% -10% -40% -10%); } }
  @keyframes wlp-look-slam { from { transform: scale(1.18); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes wlp-look-keypop { from { transform: scale(1.3); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes wlp-look-doodle { from { transform: scale(.4); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes wlp-look-svgfade { from { fill-opacity: 0; stroke-opacity: 0; } to { fill-opacity: 1; stroke-opacity: 1; } }
  @keyframes wlp-neon-lit { 0%, 8% { opacity: 0; } 8.2%, 10.2% { opacity: 1; } 10.4%, 11.8% { opacity: .25; } 12%, 14.9% { opacity: 1; } 15.1%, 16.1% { opacity: .25; } 16.3%, 93% { opacity: 1; } 100% { opacity: 0; } }
  @keyframes wlp-neon-unlit { 0%, 93% { opacity: 1; } 100% { opacity: 0; } }
  @keyframes wlp-neon-blink { 0% { opacity: 1; } 50% { opacity: .35; } }
  @keyframes wlp-chat-in { from { transform: translateY(2.2cqw); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes wlp-chat-scroll { from { transform: translateY(var(--chat-rise)); } to { transform: none; } }
  @keyframes wlp-arc-spin { from { transform: rotate(0deg); } to { transform: rotate(16deg); } }
  @keyframes wlp-floor { 0% { transform: translateY(0) perspective(60cqw) rotateX(48deg) rotateZ(var(--yaw)) scale(.7); opacity: 0; } 7% { opacity: 1; } 92% { opacity: 1; } 100% { transform: translateY(13.5cqw) perspective(60cqw) rotateX(48deg) rotateZ(var(--yaw)) scale(1.25); opacity: 0; } }
  @keyframes wlp-y2k-in { 0%, 39% { opacity: 0; filter: blur(8px); } 100% { opacity: 1; filter: blur(0); } }
  @keyframes wlp-y2k-ghost-l { 0% { transform: translateX(-3.6cqw); opacity: 0; } 20% { opacity: var(--ghost-a); } 69% { transform: none; opacity: var(--ghost-a); } 100% { transform: none; opacity: 0; } }
  @keyframes wlp-y2k-ghost-r { 0% { transform: translateX(3.6cqw); opacity: 0; } 20% { opacity: var(--ghost-a); } 69% { transform: none; opacity: var(--ghost-a); } 100% { transform: none; opacity: 0; } }
  @keyframes wlp-look-leak { 0% { transform: translate(-6%, -3%) scale(1); opacity: .55; } 100% { transform: translate(5%, 4%) scale(1.12); opacity: .85; } }
`;

// Estilo de entrada de un elemento: keyframe one-shot en muestra, o
// transición entre `hidden` y visible en vivo.
function enterStyle({ live, visible, delay, kf, durS = 0.24, ease = "cubic-bezier(.3,1.3,.5,1)", hidden }) {
  if (live) {
    return visible
      ? {
        opacity: 1,
        transform: "none",
        transition: `opacity ${Math.min(durS, 0.14)}s ease-out, transform ${durS}s ${ease}`,
      }
      : { opacity: 0, ...hidden, transition: "none" };
  }
  return { animation: `${kf} ${durS}s ${delay.toFixed(2)}s ${ease} both` };
}

// ── SVG chicos ─────────────────────────────────────────────────────────────

function burstLines(rays, r0, r1) {
  const out = [];
  for (let i = 0; i < rays; i += 1) {
    const a = (2 * Math.PI * i) / rays;
    out.push([Math.cos(a) * r0, Math.sin(a) * r0, Math.cos(a) * r1, Math.sin(a) * r1]);
  }
  return out;
}

export const HEART_PATH = "M12 21C5 15 1 11 1 7C1 3.5 3.8 1 7 1C9.2 1 11 2.3 12 4C13 2.3 14.8 1 17 1C20.2 1 23 3.5 23 7C23 11 19 15 12 21Z";
export const STAR_POINTS = "12,1 15,9 23,9 16.5,14 19,22 12,17 5,22 7.5,14 1,9 9,9";

function ridgePath(base, amp, phase) {
  const pts = [];
  for (let i = 0; i <= 96; i += 1) {
    const u = i / 96;
    const y = FRAME_H * base - FRAME_H * amp * (
      0.55 * Math.sin(u * 2 * Math.PI * 1.3 + phase)
      + 0.35 * Math.sin(u * 2 * Math.PI * 3.7 + 1.1 + phase)
      + 0.18 * Math.abs(Math.sin(u * 2 * Math.PI * 9.1 + phase)));
    pts.push(`${Math.round(FRAME_W * u)} ${Math.round(y)}`);
  }
  return `M0 ${FRAME_H} L${pts.join(" L")} L${FRAME_W} ${FRAME_H}Z`;
}
const RIDGE_BACK = ridgePath(0.74, 0.10, 2.0);
const RIDGE_FRONT = ridgePath(0.84, 0.09, 0.5);
const STARS = Array.from({ length: 28 }, (_, i) => ({
  x: ((i * 0.6180339) % 1) * FRAME_W,
  y: (((i * 0.4142135) + 0.13) % 1) * FRAME_H * 0.55,
  r: 2 + (i % 3),
  o: 1 - (0x40 + (i % 4) * 0x20) / 255,
}));

/** Degradé de atardecer + dos cordones de montañas + estrellas (Degradé). */
export function DegradeArt({ gradient, mountains, className = "", style }) {
  // id propio por instancia: la miniatura y el preview conviven en la página.
  const skyId = `wlp-degrade-sky-${useId().replace(/:/g, "")}`;
  return (
    <svg
      className={className}
      style={style}
      viewBox={`0 0 ${FRAME_W} ${FRAME_H}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={skyId} x1="0" y1="0" x2="0" y2="1">
          {gradient.map((c, i) => (
            <stop key={c} offset={`${(i / Math.max(1, gradient.length - 1)) * 100}%`} stopColor={c} />
          ))}
        </linearGradient>
      </defs>
      <rect width={FRAME_W} height={FRAME_H} fill={`url(#${skyId})`} />
      {STARS.map((s, i) => <circle key={i} cx={s.x} cy={s.y} r={s.r} fill="#FFFFFF" opacity={s.o} />)}
      <path d={RIDGE_BACK} fill={mountains[0]} opacity={0.81} />
      <path d={RIDGE_FRONT} fill={mountains[1]} />
    </svg>
  );
}

/**
 * Capas de fondo propias de un look (encima del fondo del job): degradé
 * con montañas, póster a dos tintas, filtraciones de luz.
 */
export function LookBackdrop({ lp }) {
  if (!lp) return null;
  return (
    <>
      {lp.duotone ? (
        <>
          <div className="absolute inset-0 pointer-events-none" data-look-layer="duotone" style={{ background: lp.duotone[0], mixBlendMode: "screen" }} />
          <div className="absolute inset-0 pointer-events-none" style={{ background: lp.duotone[1], mixBlendMode: "multiply" }} />
        </>
      ) : null}
      {lp.lightLeak ? (
        <div
          className="absolute -inset-[10%] pointer-events-none"
          data-look-layer="light-leak"
          style={{
            background: "radial-gradient(40% 55% at 12% 18%, rgba(255,255,255,.75), transparent 70%), radial-gradient(35% 50% at 88% 78%, rgba(120,225,255,.6), transparent 70%), radial-gradient(25% 35% at 70% 10%, rgba(255,210,240,.45), transparent 70%)",
            mixBlendMode: "screen",
            animation: "wlp-look-leak 4.5s ease-in-out infinite alternate",
          }}
        />
      ) : null}
      {lp.gradient ? (
        <DegradeArt
          gradient={lp.gradient}
          mountains={lp.mountains || ["#5B2A7A", "#21123A"]}
          className="absolute inset-0 w-full h-full pointer-events-none"
          style={{ display: "block" }}
        />
      ) : null}
    </>
  );
}

// ── Palabra del layout "build" con extras (Cuaderno / Y2K) ─────────────────

/**
 * Contenido de una palabra de la composición build cuando el look trae un
 * movimiento propio (`wordMotion`): escritura a mano (clip izquierda→derecha
 * + inclinación por palabra + círculo rojo en la clave) o ecos Y2K (dos
 * copias translúcidas que convergen desde los costados).
 * Devuelve { outerStyle, content }.
 */
export function buildWordParts({ lp, live, visible, i, lineIdx, token, isKey }) {
  const delay = sampleWordDelay(i);
  const outerStyle = { position: "relative", display: "inline-block" };
  if (lp.wordTilt) outerStyle.transform = `rotate(${(-handWordTilt(i, lineIdx, lp.wordTilt)).toFixed(2)}deg)`;
  if (lp.wordMotion === "write") {
    const inner = live
      ? {
        clipPath: visible ? "inset(-40% -10% -40% -10%)" : "inset(-40% 100% -40% -10%)",
        transition: visible ? "clip-path .3s linear" : "none",
      }
      : { animation: `wlp-look-write .32s ${delay.toFixed(2)}s linear both` };
    const ring = isKey && lp.circleKey && (!live || visible) ? (
      <svg
        data-look-circle="true"
        aria-hidden="true"
        viewBox="0 0 100 60"
        preserveAspectRatio="none"
        style={{
          position: "absolute",
          left: "-22%",
          right: "-22%",
          top: "-18%",
          bottom: "-22%",
          width: "144%",
          height: "140%",
          overflow: "visible",
          pointerEvents: "none",
          animation: `wlp-look-reveal .38s ${(live ? 0.3 : delay + 0.3).toFixed(2)}s linear both`,
        }}
      >
        <ellipse cx="50" cy="30" rx="47" ry="26" fill="none" stroke={lp.circleKey} strokeWidth="0.32cqw" vectorEffect="non-scaling-stroke" transform="rotate(-3 50 30)" />
        <ellipse cx="51" cy="31" rx="45" ry="23" fill="none" stroke={lp.circleKey} strokeWidth="0.26cqw" vectorEffect="non-scaling-stroke" transform="rotate(4 50 30)" />
      </svg>
    ) : null;
    return {
      outerStyle,
      content: (
        <>
          <span style={{ display: "inline-block", ...inner }}>{token}</span>
          {ring}
        </>
      ),
    };
  }
  if (lp.wordMotion === "echo") {
    const inner = live
      ? {
        opacity: visible ? 1 : 0,
        filter: visible ? "blur(0px)" : "blur(8px)",
        transition: visible ? "opacity .22s .14s ease-out, filter .22s .14s ease-out" : "none",
      }
      : { animation: `wlp-y2k-in .36s ${delay.toFixed(2)}s ease-out both` };
    const ghosts = !live || visible
      ? [["l", 0.44], ["r", 0.31]].map(([side, a]) => (
        <span
          key={side}
          aria-hidden="true"
          data-look-echo={side}
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            whiteSpace: "nowrap",
            filter: "blur(2px)",
            pointerEvents: "none",
            "--ghost-a": a,
            animation: `wlp-y2k-ghost-${side} .52s ${(live ? 0 : delay).toFixed(2)}s ease-out both`,
          }}
        >
          {token}
        </span>
      ))
      : null;
    return {
      outerStyle,
      content: (
        <>
          {ghosts}
          <span style={{ display: "inline-block", ...inner }}>{token}</span>
        </>
      ),
    };
  }
  return { outerStyle, content: token };
}

/** Corazón y estrella que "hierven" en las esquinas del bloque (Cuaderno). */
export function BuildDoodles({ lp, live, keyVisible, keyIndex, lineIdx }) {
  if (!lp.doodles || keyIndex == null || (live && !keyVisible)) return null;
  const delay = live ? 0.25 : sampleWordDelay(keyIndex) + 0.25;
  const even = lineIdx % 2 === 0;
  const place = [
    { kind: "heart", pos: even ? { right: "-0.9em", top: "-0.55em" } : { left: "-0.9em", top: "-0.4em" } },
    { kind: "star", pos: even ? { left: "-0.9em", bottom: "-0.3em" } : { right: "-0.9em", bottom: "-0.45em" } },
  ];
  return place.map(({ kind, pos }, n) => (
    <span
      key={kind}
      data-look-doodle={kind}
      aria-hidden="true"
      style={{ position: "absolute", width: "0.55em", height: "0.55em", ...pos, animation: `wlp-look-doodle .14s ${(delay + n * 0.12).toFixed(2)}s cubic-bezier(.3,1.4,.5,1) both` }}
    >
      <svg viewBox="0 0 24 24" style={{ width: "100%", height: "100%", overflow: "visible", animation: "wlp-look-boil .5s steps(1,end) infinite" }}>
        {kind === "heart"
          ? <path d={HEART_PATH} fill={lp.doodles.heart} stroke="#202020" strokeWidth="1.2" />
          : <polygon points={STAR_POINTS} fill={lp.doodles.star} stroke="#202020" strokeWidth="1.2" />}
      </svg>
    </span>
  ));
}

// ── Layouts de un look ─────────────────────────────────────────────────────

/**
 * Cuerpo de la letra para los layouts nuevos. Devuelve
 * { body, wrapStyle, frame } o null si el layout no es de este módulo:
 *   - body: JSX de la letra.
 *   - wrapStyle: estilo de movimiento del wrapper (reemplaza lineMotion).
 *   - frame: true si el wrapper debe ocupar TODO el cuadro (posiciones
 *     absolutas: columnas, burbujas, anillo).
 *
 * ctx: { lp, look, tokens, text, live, lineIdx, basePx, toCqw, textColor,
 *        operatorPickedColor, sungIdx, elapsed, dur, loopS, uid, history,
 *        chatFontPx, sampleAlt }
 */
export function renderLookLayout(ctx) {
  const { lp } = ctx;
  switch (lp.layout) {
    case "kinetic": return kineticLayout(ctx);
    case "neon": return neonLayout(ctx);
    case "chat": return chatLayout(ctx);
    case "block": return blockLayout(ctx);
    case "arc": return arcLayout(ctx);
    case "floor": return floorLayout(ctx);
    default: break;
  }
  if (lp.layout === "line" && (lp.motion === "boil" || lp.motion === "write_on")) {
    return lineInkLayout(ctx);
  }
  return null;
}

function kineticLayout(ctx) {
  const { lp, tokens, live, lineIdx, basePx, toCqw, sungIdx, operatorPickedColor, textColor, loopS } = ctx;
  const palette = lp.cardPalettes ? lp.cardPalettes[lineIdx % lp.cardPalettes.length] : [lp.color, lp.color, lp.color];
  const ink = operatorPickedColor ? textColor : palette[0];
  const [, alt, accent] = palette;
  const { rows } = kineticRows(tokens);
  const two = rows.length > 4;
  const blockW = FRAME_W * (two ? 0.38 : 0.46);
  const glyph = 0.46;
  const specs = rows.map((r) => {
    const text = r.idx.map((i) => tokens[i]).join(" ");
    if (r.kind === "script") return { ...r, text, fs: basePx * 1.25, h: basePx * 1.25 * 0.95 };
    const key = r.kind === "key";
    const nat = Math.max(1, text.length * glyph);
    const fs = Math.max(basePx * 0.8, Math.min(basePx * (key ? 5.5 : 4.2), (blockW * (key ? 1 : 0.9)) / nat));
    return { ...r, text, fs, h: fs * 0.84 };
  });
  const half = Math.ceil(specs.length / 2);
  const slot = [0.32, 0.5, 0.68][lineIdx % 3];
  const columns = two
    ? [{ cx: 0.28, specs: specs.slice(0, half) }, { cx: 0.72, specs: specs.slice(half) }]
    : [{ cx: tokens.length > 2 ? slot : 0.5, specs }];
  columns.forEach((col) => {
    const total = col.specs.reduce((a, s) => a + s.h, 0);
    const limit = FRAME_H * 0.84;
    if (total > limit) {
      const f = limit / total;
      col.specs.forEach((s) => { s.fs *= f; s.h *= f; });
    }
  });
  const seed = seedOf(`${lineIdx}:${tokens.join(" ")}`);
  let rowN = 0;
  const body = columns.map((col, ci) => (
    <div
      key={ci}
      data-look-column={ci}
      style={{
        position: "absolute",
        top: "50%",
        left: `${col.cx * 100}%`,
        transform: "translate(-50%, -50%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        width: toCqw(blockW * 1.15),
      }}
    >
      {col.specs.map((sp) => {
        const r = rowN;
        rowN += 1;
        const first = sp.idx[0];
        const visible = live ? first <= sungIdx : true;
        const delay = sampleWordDelay(first);
        const script = sp.kind === "script";
        const kind = script ? "pop" : KINETIC_KINDS[(Math.floor(seed / 7) + r * 3) % KINETIC_KINDS.length];
        const colour = script ? accent : (sp.kind === "key" ? accent : [ink, alt][r % 2]);
        const tilt = script ? (((seed >> r) & 1) ? -7 : 6) : 0;
        const enter = enterStyle({
          live, visible, delay, kf: `wlp-kin-${kind}`, durS: 0.26, hidden: { transform: KINETIC_FROM[kind] },
        });
        const isKey = sp.kind === "key";
        const deco = isKey ? seed % 3 : null;
        const showDeco = isKey && (!live || visible);
        const decoDelay = live ? 0.12 : delay + 0.12;
        return (
          <div
            key={sp.idx.join("-")}
            data-look-row={r}
            data-look-kind={sp.kind}
            data-look-key={isKey ? "true" : undefined}
            style={{
              position: "relative",
              fontSize: toCqw(sp.fs),
              lineHeight: script ? 0.95 : 0.84,
              whiteSpace: "nowrap",
              color: colour,
              fontFamily: script && lp.scriptFont ? lp.scriptFont.css : undefined,
              fontWeight: script && lp.scriptFont ? lp.scriptFont.weight : undefined,
              transform: tilt ? `rotate(${tilt}deg)` : undefined,
              zIndex: isKey ? 1 : 2,
              margin: isKey ? "0.08em 0" : 0,
            }}
          >
            {showDeco && deco === 0 ? (
              <svg
                data-look-deco="rays"
                aria-hidden="true"
                viewBox="-100 -100 200 200"
                preserveAspectRatio="none"
                style={{ position: "absolute", left: "-22%", right: "-22%", top: "-55%", bottom: "-55%", width: "144%", height: "210%", overflow: "visible", zIndex: -1, animation: `wlp-look-doodle .12s ${decoDelay.toFixed(2)}s both, wlp-kin-blink .6s ${(decoDelay + 0.3).toFixed(2)}s steps(1,end) infinite` }}
              >
                {burstLines(16, 68, 100).map(([x0, y0, x1, y1], n) => (
                  <line key={n} x1={x0} y1={y0} x2={x1} y2={y1} stroke={alt} strokeWidth="0.35cqw" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
                ))}
              </svg>
            ) : null}
            {showDeco && deco === 1 ? (
              <span
                data-look-deco="bubble"
                aria-hidden="true"
                style={{
                  position: "absolute",
                  left: "-14%",
                  right: "-14%",
                  top: "-12%",
                  bottom: "-6%",
                  background: alt,
                  borderRadius: "0.45em",
                  zIndex: -1,
                  "--rot": `${seed & 1 ? -4 : 4}deg`,
                  animation: `wlp-kin-bubble .18s ${decoDelay.toFixed(2)}s cubic-bezier(.3,1.4,.5,1) both`,
                }}
              >
                <span style={{ position: "absolute", bottom: "-0.14em", [seed & 2 ? "left" : "right"]: "14%", width: "0.3em", height: "0.3em", background: alt, transform: "rotate(45deg)" }} />
              </span>
            ) : null}
            {showDeco && deco === 2 ? (
              <svg
                data-look-deco="swoosh"
                aria-hidden="true"
                viewBox="0 0 100 34"
                preserveAspectRatio="none"
                style={{ position: "absolute", left: "-3%", width: "106%", top: "84%", height: "0.3em", overflow: "visible", animation: `wlp-look-reveal .26s ${(decoDelay + 0.08).toFixed(2)}s linear both` }}
              >
                <path d="M0 6C30 20 70 0 100 10L100 22C70 12 30 32 0 18Z" fill={alt} />
              </svg>
            ) : null}
            <span style={{ display: "inline-block", transformOrigin: "50% 100%", ...enter }}>
              <span
                style={{
                  display: "inline-block",
                  animation: isKey ? `wlp-kin-pulse .55s ${(live ? 0.4 : delay + 0.4).toFixed(2)}s ease-out infinite` : undefined,
                }}
              >
                {sp.text}
              </span>
            </span>
          </div>
        );
      })}
    </div>
  ));
  const wrapStyle = live
    ? { ...lookLineMotionStyle("smear", ctx.elapsed, ctx.dur), transition: "transform 60ms linear, opacity 60ms linear, filter 60ms linear" }
    : { animation: `wlp-look-smear ${loopS}s linear both` };
  return { body, wrapStyle, frame: true };
}

function neonLayout(ctx) {
  const { lp, look, text, live, lineIdx, basePx, toCqw, textColor, loopS } = ctx;
  const colors = lp.lineColors || [textColor];
  const col = colors[lineIdx % colors.length];
  const frameCol = colors[(lineIdx + 2) % colors.length];
  const script = lineIdx % 2 === 1 && !!lp.scriptFont;
  const face = script ? lp.scriptFont : look.font;
  // El tubo tiene que entrar en el cuadro: estimamos filas (ancho medio de
  // glifo por cara) y achicamos si el bloque pasa ~70 % del alto.
  let fs = basePx * (script ? 1.35 : 1);
  const glyphW = script ? 0.78 : 0.56;
  const maxW = FRAME_W * 0.8;
  const rowsFor = (size) => Math.max(1, Math.ceil((text.length * size * glyphW) / maxW));
  for (let n = 0; n < 8 && rowsFor(fs) * fs * 1.1 > FRAME_H * 0.7; n += 1) fs *= 0.85;
  const kind = lineIdx % 3;
  const lit = live ? neonLitAlpha(ctx.elapsed, ctx.dur) : null;
  const litStyle = live ? { opacity: lit } : { animation: `wlp-neon-lit ${loopS}s linear both` };
  const unlitStyle = live
    ? { opacity: ctx.elapsed > ctx.dur - 0.2 ? 0 : 1 }
    : { animation: `wlp-neon-unlit ${loopS}s linear both` };
  const tube = (c) => `0 0 0.05em #fff, 0 0 0.14em ${c}, 0 0 0.32em ${c}, 0 0 0.7em ${c}, 0 0 1.2em ${c}`;
  const frameGlow = `drop-shadow(0 0 0.35cqw ${frameCol}) drop-shadow(0 0 0.9cqw ${frameCol})`;
  let frame;
  if (kind === 0) {
    frame = (
      <span
        data-look-frame="box"
        style={{ position: "absolute", inset: "-0.18em -0.4em", borderRadius: "0.35em", border: `0.4cqw solid ${frameCol}`, boxShadow: `0 0 0.6cqw ${frameCol}, inset 0 0 0.6cqw ${frameCol}`, outline: "0.12cqw solid rgba(255,255,255,.85)", outlineOffset: "-0.26cqw" }}
      />
    );
  } else if (kind === 1) {
    frame = (
      <svg data-look-frame="arrow" viewBox="0 -30 120 160" preserveAspectRatio="none" style={{ position: "absolute", left: "-0.4em", right: "-0.9em", top: "-0.5em", bottom: "-0.5em", width: "calc(100% + 1.3em)", height: "calc(100% + 1em)", overflow: "visible", filter: frameGlow }}>
        <polygon points="0,0 100,0 100,-25 120,50 100,125 100,100 0,100" fill="none" stroke={frameCol} strokeWidth="0.4cqw" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        <polygon points="0,0 100,0 100,-25 120,50 100,125 100,100 0,100" fill="none" stroke="#FFFFFF" strokeWidth="0.12cqw" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      </svg>
    );
  } else {
    frame = (
      <svg data-look-frame="rays" viewBox="-100 -100 200 200" preserveAspectRatio="none" style={{ position: "absolute", left: "-14%", right: "-14%", top: "-70%", bottom: "-70%", width: "128%", height: "240%", overflow: "visible", filter: frameGlow }}>
        {burstLines(22, 74, 100).map(([x0, y0, x1, y1], n) => (
          <line key={n} x1={x0} y1={y0} x2={x1} y2={y1} stroke={frameCol} strokeWidth="0.32cqw" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
    );
  }
  const body = (
    <span
      data-look-neon={col}
      style={{ position: "relative", display: "inline-block", fontFamily: face.css, fontWeight: face.weight, fontSize: toCqw(fs), lineHeight: 1.05, padding: "0.12em 0.25em" }}
    >
      <span aria-hidden="true" style={{ position: "absolute", inset: 0, pointerEvents: "none", ...litStyle }}>
        <span style={{ position: "absolute", inset: 0, animation: "wlp-neon-blink 1.1s 0.6s steps(1,end) infinite" }}>{frame}</span>
      </span>
      <span style={{ display: "block", color: "rgba(150,150,150,.14)", WebkitTextStroke: "0.07cqw rgba(170,170,170,.6)", ...unlitStyle }}>{text}</span>
      <span
        data-look-lit="true"
        aria-hidden="true"
        style={{ position: "absolute", inset: 0, padding: "0.12em 0.25em", color: textColor, textShadow: tube(col), ...litStyle }}
      >
        {text}
      </span>
    </span>
  );
  return { body, wrapStyle: {}, frame: false };
}

function chatLayout(ctx) {
  const { lp, text, live, lineIdx, toCqw, textColor, history, sampleAlt, sample } = ctx;
  const fs = ctx.chatFontPx;
  const [incoming, outgoing] = lp.bubbles || ["#E9E9EB", "#1F8BFF"];
  // Conversación: hasta 2 líneas anteriores + la actual (lado alterna).
  const prev = live
    ? (history || []).slice(-2)
    : [lineIdx - 2, lineIdx - 1].filter((n) => n >= 0).map((n) => (n % 2 === 0 ? sample : sampleAlt));
  const msgs = [
    ...prev.map((m, j) => ({ text: m, n: lineIdx - prev.length + j, old: true })),
    { text, n: lineIdx, old: false },
  ];
  const body = (
    <div
      data-look-chat="true"
      style={{
        position: "absolute",
        left: "22%",
        right: "22%",
        bottom: "16%",
        display: "flex",
        flexDirection: "column",
        gap: toCqw(fs * 0.32),
        "--chat-rise": toCqw(fs * 1.7),
        animation: prev.length ? `wlp-chat-scroll .22s ${live ? 0 : 0.1}s ease-out both` : undefined,
      }}
    >
      {msgs.map((m) => {
        const right = m.n % 2 === 1;
        return (
          <div
            key={`${m.n}:${m.text}`}
            data-look-bubble={right ? "out" : "in"}
            data-look-old={m.old ? "true" : undefined}
            style={{
              alignSelf: right ? "flex-end" : "flex-start",
              maxWidth: "75%",
              background: right ? outgoing : incoming,
              color: right ? (lp.accent || "#FFFFFF") : textColor,
              fontSize: toCqw(fs),
              lineHeight: 1.04,
              padding: `${toCqw(fs * 0.32)} ${toCqw(fs * 0.55)}`,
              borderRadius: toCqw(fs * 0.62),
              [right ? "borderBottomRightRadius" : "borderBottomLeftRadius"]: toCqw(fs * 0.12),
              textAlign: "left",
              opacity: m.old ? 0.67 : undefined,
              boxShadow: "0 0.2cqw 0.6cqw rgba(0,0,0,.18)",
              animation: m.old ? undefined : `wlp-chat-in .22s ${live ? 0 : 0.1}s ease-out both`,
            }}
          >
            {m.text}
          </div>
        );
      })}
    </div>
  );
  // La conversación queda en pantalla: sin salida por línea.
  return { body, wrapStyle: {}, frame: true };
}

function blockLayout(ctx) {
  const { lp, look, tokens, live, basePx, toCqw, sungIdx, textColor, loopS } = ctx;
  const { rows } = blockRows(tokens);
  const blockW = FRAME_W * (lp.rowWidth || 0.46);
  const specs = rows.map((r) => {
    const text = r.idx.map((i) => tokens[i]).join(" ");
    const g = r.heavy ? 0.46 : 0.4;
    const fs = Math.max(basePx * 0.55, Math.min(basePx * 4.5, blockW / Math.max(1, text.length * g)));
    return { ...r, text, fs };
  });
  // El backend limita a 80 % del alto con interlineado 0,84; en CSS la caja
  // de línea de Big Shoulders sobresale un poco más, así que dejamos aire.
  const total = specs.reduce((a, s) => a + s.fs * 0.84, 0);
  if (total > FRAME_H * 0.72) {
    const f = (FRAME_H * 0.72) / total;
    specs.forEach((s) => { s.fs *= f; });
  }
  const body = (
    <span style={{ display: "inline-flex", flexDirection: "column", alignItems: "center" }}>
      {specs.map((sp, r) => {
        const first = sp.idx[0];
        const visible = live ? first <= sungIdx : true;
        return (
          <span
            key={r}
            data-look-row={r}
            data-look-key={sp.key ? "true" : undefined}
            data-look-weight={sp.heavy ? "heavy" : "light"}
            style={{
              display: "block",
              fontSize: toCqw(sp.fs),
              lineHeight: 0.84,
              fontWeight: sp.heavy ? look.font.weight : (lp.lightWeight || 300),
              color: sp.key ? (lp.accent || textColor) : textColor,
              whiteSpace: "nowrap",
              letterSpacing: "0.01em",
              ...enterStyle({ live, visible, delay: sampleWordDelay(first), kf: "wlp-look-slam", durS: 0.09, ease: "cubic-bezier(.3,.7,.4,1)", hidden: { transform: "scale(1.18)" } }),
            }}
          >
            {sp.text}
          </span>
        );
      })}
    </span>
  );
  return { body, wrapStyle: live ? {} : { animation: `wlp-look-blockout ${loopS}s linear both` }, frame: false };
}

function arcLayout(ctx) {
  const { lp, tokens, live, basePx, sungIdx, textColor, loopS, uid } = ctx;
  const k = tokens.length > 2 ? pickKeyword(tokens) : null;
  const arcIdx = tokens.map((_t, i) => i).filter((i) => i !== k);
  const g = lp.glyphWidth || 0.72;
  let fs = basePx;
  const chars = arcIdx.map((i) => tokens[i]).join(" ").length;
  let total = chars * fs * (g + 0.1);
  let r = FRAME_H * 0.30;
  if (total > 1.5 * Math.PI * r) r = total / (1.5 * Math.PI);
  if (r > FRAME_H * 0.44) {
    const f = (FRAME_H * 0.44) / r;
    fs = Math.max(18, fs * f);
    r = FRAME_H * 0.44;
    total *= f;
  }
  const cx = FRAME_W / 2;
  const cy = FRAME_H / 2 + FRAME_H * 0.04;
  const ringR = r - fs * 0.22;
  const pathId = `wlp-arc-${uid}`;
  const d = `M ${cx} ${cy + r} A ${r} ${r} 0 1 1 ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx} ${cy + r}`;
  const spin = Math.min(28, 5 * Math.max(0.1, ctx.dur || 3));
  const spinStyle = live
    ? { transform: `rotate(${(spin * clamp01(ctx.elapsed / Math.max(0.05, ctx.dur))).toFixed(2)}deg)` }
    : { animation: `wlp-arc-spin ${loopS}s linear both` };
  const wordVis = (i) => (live
    ? { fillOpacity: i <= sungIdx ? 1 : 0, strokeOpacity: i <= sungIdx ? 1 : 0, transition: "fill-opacity .12s, stroke-opacity .12s" }
    : { animation: `wlp-look-svgfade .12s ${sampleWordDelay(i).toFixed(2)}s linear both` });
  const kfs = k != null
    ? Math.min(basePx * (lp.keyScale || 2.1), (ringR * 1.6) / Math.max(1, tokens[k].length * g))
    : 0;
  const keyVisible = k != null && (!live || k <= sungIdx);
  const body = (
    <svg
      data-look-arc="true"
      viewBox={`0 0 ${FRAME_W} ${FRAME_H}`}
      style={{ position: "absolute", inset: 0, width: "100%", height: "100%", overflow: "visible", WebkitTextStroke: "0" }}
    >
      <defs><path id={pathId} d={d} fill="none" /></defs>
      <g style={{ transformOrigin: `${cx}px ${cy}px`, transformBox: "view-box", ...spinStyle }}>
        <circle cx={cx} cy={cy} r={ringR} fill="none" stroke={textColor} strokeOpacity={0.44} strokeWidth={2} />
        <text fontSize={fs} fill={textColor} stroke="rgba(0,0,0,.7)" strokeWidth={fs * 0.04} paintOrder="stroke" letterSpacing={fs * 0.1}>
          <textPath href={`#${pathId}`} startOffset="50%" textAnchor="middle">
            {arcIdx.map((i, n) => (
              <tspan key={i} data-look-word={i} style={wordVis(i)}>{(n ? " " : "") + tokens[i]}</tspan>
            ))}
          </textPath>
        </text>
      </g>
      {keyVisible ? (
        <text
          data-look-key="true"
          x={cx}
          y={cy}
          textAnchor="middle"
          dominantBaseline="central"
          fontSize={kfs}
          fill={lp.accent || textColor}
          stroke="rgba(0,0,0,.7)"
          strokeWidth={kfs * 0.03}
          paintOrder="stroke"
          style={{ transformBox: "fill-box", transformOrigin: "center", animation: `wlp-look-keypop .12s ${(live ? 0 : sampleWordDelay(k)).toFixed(2)}s cubic-bezier(.3,.7,.4,1) both` }}
        >
          {tokens[k]}
        </text>
      ) : null}
    </svg>
  );
  const wrapStyle = live
    ? { ...lookLineMotionStyle("fade", ctx.elapsed, ctx.dur), transition: "opacity 60ms linear" }
    : { animation: `wlp-look-fade ${loopS}s linear both` };
  return { body, wrapStyle, frame: true };
}

function floorLayout(ctx) {
  const { lp, tokens, live, lineIdx, basePx, toCqw, textColor, loopS } = ctx;
  const k = pickKeyword(tokens);
  const yaw = lineIdx % 2 === 0 ? -6 : 7;
  let motion;
  if (live) {
    const dur = Math.max(0.05, ctx.dur);
    const q = clamp01(ctx.elapsed / dur);
    const s = 0.7 + 0.55 * q ** 1.4;
    const op = Math.min(clamp01(ctx.elapsed / 0.22), clamp01((dur - ctx.elapsed) / 0.26));
    motion = {
      transform: `translateY(${(13.5 * q).toFixed(2)}cqw) perspective(60cqw) rotateX(48deg) rotateZ(${yaw}deg) scale(${s.toFixed(3)})`,
      opacity: op,
      transition: "transform 60ms linear, opacity 60ms linear",
    };
  } else {
    motion = { animation: `wlp-floor ${loopS}s linear both` };
  }
  const body = (
    <span
      data-look-floor="true"
      style={{ display: "inline-block", fontSize: toCqw(basePx), transformOrigin: "50% 50%", "--yaw": `${yaw}deg`, ...motion }}
    >
      {tokens.map((tok, i) => (
        <span key={i} data-look-key={i === k ? "true" : undefined} style={{ color: i === k ? (lp.accent || textColor) : undefined }}>
          {(i ? " " : "") + tok}
        </span>
      ))}
    </span>
  );
  return { body, wrapStyle: {}, frame: false };
}

// Línea entera con movimiento propio: Duotono (tinta que hierve y se barre)
// y Romántico (cursiva que se escribe sola).
function lineInkLayout(ctx) {
  const { lp, text, live, lineIdx, basePx, toCqw, textColor, operatorPickedColor, loopS } = ctx;
  if (lp.motion === "boil") {
    const inks = lp.lineColors || [textColor];
    const ink = operatorPickedColor ? textColor : inks[lineIdx % inks.length];
    const body = (
      <span data-look-ink={ink} style={{ display: "inline-block", fontSize: toCqw(basePx), color: ink }}>
        <span style={{ display: "inline-block", animation: "wlp-look-boil .5s steps(1,end) infinite" }}>{text}</span>
      </span>
    );
    const wrapStyle = live
      ? { ...lookLineMotionStyle("boil", ctx.elapsed, ctx.dur), transition: "transform 60ms linear, opacity 60ms linear, filter 60ms linear" }
      : { animation: `wlp-look-boilline ${loopS}s linear both` };
    return { body, wrapStyle, frame: false };
  }
  const body = <span data-look-write="true" style={{ fontSize: toCqw(basePx) }}>{text}</span>;
  const wrapStyle = live
    ? lookLineMotionStyle("write_on", ctx.elapsed, ctx.dur, { textLen: text.length })
    : { animation: `wlp-look-writeon ${loopS}s linear both` };
  return { body, wrapStyle, frame: false };
}
