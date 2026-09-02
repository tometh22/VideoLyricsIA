import { useState, useMemo } from "react";
import { useI18n } from "../../i18n";

// Mini-video-style demos that simulate the real flow inside a small stage.
// Each demo runs CSS keyframes on a loop; the Replay button forces a
// remount by bumping a key. All visuals are CSS+SVG only — no canvas, no
// libraries, no recorded video.

function ReplayWrap({ children, label }) {
  const [k, setK] = useState(0);
  return (
    <div className="hc-anim" key={k}>
      {children}
      <button
        type="button"
        className="hc-anim-replay"
        onClick={() => setK((v) => v + 1)}
      >
        ▶ {label}
      </button>
    </div>
  );
}

// Virtual cursor SVG — used across all demos so movement reads as
// "someone is operating the UI" rather than "stuff just appears".
function Cursor({ className }) {
  return (
    <svg className={"hc-cursor " + (className || "")} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 2 L3 18 L7.5 14 L10 21 L13 20 L10.5 13 L17 13 Z"
            fill="#F5F7FA" stroke="#09090F" strokeWidth="1.2" strokeLinejoin="round"/>
    </svg>
  );
}

// ─── 1. Upload flow: drop file → metadata fills → generate ───────────
function UploadFlow() {
  const { t } = useI18n();
  return (
    <div className="hc-anim-stage hc-mini-window">
      <div className="hc-mini-titlebar">
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-title">{t("nav.new_batch") || "Crear video"}</span>
      </div>
      <div className="hc-mini-body">
        {/* Drop zone */}
        <div className="hc-up2-dropzone">
          <div className="hc-up2-dropicon">⤴</div>
          <div className="hc-up2-droptxt">{t("help.anim.upload.zone") || "Arrastrá tu MP3"}</div>
        </div>
        {/* File card (appears mid-anim) */}
        <div className="hc-up2-filecard">
          <div className="hc-up2-fileicon">♪</div>
          <div className="hc-up2-filebody">
            <div className="hc-up2-filename">cancion.mp3</div>
            <div className="hc-up2-fileinputs">
              <div className="hc-up2-input">
                <span className="hc-up2-inputlabel">Artista</span>
                <span className="hc-up2-inputtype" />
              </div>
              <div className="hc-up2-input hc-up2-input2">
                <span className="hc-up2-inputlabel">Título</span>
                <span className="hc-up2-inputtype hc-up2-inputtype2" />
              </div>
            </div>
          </div>
        </div>
        {/* CTA */}
        <div className="hc-up2-cta">{t("help.anim.upload.cta") || "Generar →"}</div>
        {/* Floating MP3 file traveling into the dropzone */}
        <div className="hc-up2-flyfile">
          <div className="hc-up2-flyicon">♪</div>
          <div className="hc-up2-flytxt">cancion.mp3</div>
        </div>
        {/* Virtual cursor */}
        <Cursor className="hc-up2-cursor" />
      </div>
    </div>
  );
}

// ─── 2. Editor sync ───────────────────────────────────────────────────
function EditorSync() {
  const { t } = useI18n();
  return (
    <div className="hc-anim-stage hc-mini-window">
      <div className="hc-mini-titlebar">
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-title">{t("nav.editor") || "Editor de letras"}</span>
      </div>
      <div className="hc-mini-body hc-ed2-body">
        {/* Playbar with animated wave */}
        <div className="hc-ed2-playbar">
          <div className="hc-ed2-playbtn">▶</div>
          <div className="hc-ed2-wave">
            {Array.from({ length: 24 }).map((_, i) => (
              <span key={i} className="hc-ed2-wavebar" style={{ animationDelay: `${i * 60}ms` }} />
            ))}
          </div>
          <div className="hc-ed2-time">0:24</div>
        </div>
        {/* Sync entry button + virtual press indicator */}
        <div className="hc-ed2-controls">
          <span className="hc-ed2-syncbtn">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1" fill="currentColor"/></svg>
          </span>
          <span className="hc-ed2-syncbadge">Sync mode</span>
          <span className="hc-ed2-spacekey">SPACE</span>
        </div>
        {/* Lines */}
        <div className="hc-ed2-lines">
          <div className="hc-ed2-line hc-ed2-line1">
            <span className="hc-ed2-ts">0:10</span>
            <span className="hc-ed2-text">{t("help.anim.sync.l1") || "Si el cielo se nubla"}</span>
          </div>
          <div className="hc-ed2-line hc-ed2-line2">
            <span className="hc-ed2-ts hc-ed2-ts2">0:24</span>
            <span className="hc-ed2-text">{t("help.anim.sync.l2") || "Vuelvo a empezar"}</span>
          </div>
          <div className="hc-ed2-line hc-ed2-line3">
            <span className="hc-ed2-ts hc-ed2-ts3">0:38</span>
            <span className="hc-ed2-text">{t("help.anim.sync.l3") || "Sin mirar atrás"}</span>
          </div>
        </div>
        <Cursor className="hc-ed2-cursor" />
      </div>
    </div>
  );
}

// ─── 3. Approve flow ──────────────────────────────────────────────────
function ApproveFlow() {
  const { t } = useI18n();
  return (
    <div className="hc-anim-stage hc-mini-window">
      <div className="hc-mini-titlebar">
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-title">Job · pending_review</span>
      </div>
      <div className="hc-mini-body hc-ap2-body">
        {/* Status badges */}
        <div className="hc-ap2-badges">
          <span className="hc-ap2-pendingbadge">PENDIENTE</span>
          <span className="hc-ap2-approvedbadge">{t("help.anim.approve.done") || "Aprobado · ahora"}</span>
        </div>
        {/* Preview tile */}
        <div className="hc-ap2-preview">
          <div className="hc-ap2-preview-bg" />
          <div className="hc-ap2-preview-play">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>
          </div>
          <div className="hc-ap2-preview-lyric">{t("help.anim.sync.l2") || "Vuelvo a empezar"}</div>
        </div>
        {/* Action buttons */}
        <div className="hc-ap2-actions">
          <span className="hc-ap2-btn-approve">✓ {t("help.anim.approve.btn") || "Aprobar"}</span>
          <span className="hc-ap2-btn-reject">✗ Rechazar</span>
        </div>
        {/* Confirm modal */}
        <div className="hc-ap2-confirm">
          <div className="hc-ap2-confirm-title">{t("help.anim.approve.confirm") || "¿Aprobar este video?"}</div>
          <div className="hc-ap2-confirm-actions">
            <span className="hc-ap2-confirm-cancel">Cancelar</span>
            <span className="hc-ap2-confirm-ok">Sí, aprobar</span>
          </div>
        </div>
        {/* Quota counter */}
        <div className="hc-ap2-quota">
          <span className="hc-ap2-quota-label">Uso mensual</span>
          <span className="hc-ap2-quota-counter">27 → 28 / 30</span>
        </div>
        <Cursor className="hc-ap2-cursor" />
      </div>
    </div>
  );
}

// ─── 4. Download flow ─────────────────────────────────────────────────
function DownloadFlow() {
  const { t } = useI18n();
  return (
    <div className="hc-anim-stage hc-mini-window">
      <div className="hc-mini-titlebar">
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-dot" />
        <span className="hc-mini-title">{t("detail.download_all") || "Descargar"}</span>
      </div>
      <div className="hc-mini-body hc-dl2-body">
        {/* Three download buttons */}
        <div className="hc-dl2-buttons">
          <div className="hc-dl2-btn hc-dl2-btn1">
            <svg className="hc-dl2-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
            <span>MP4 1080p</span>
            <span className="hc-dl2-size">142 MB</span>
          </div>
          <div className="hc-dl2-btn hc-dl2-btn2">
            <svg className="hc-dl2-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
            <span>Short 9:16</span>
            <span className="hc-dl2-size">64 MB</span>
          </div>
          <div className="hc-dl2-btn hc-dl2-btn3">
            <svg className="hc-dl2-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
            <span>Thumbnail</span>
            <span className="hc-dl2-size">230 KB</span>
          </div>
        </div>
        {/* Progress card */}
        <div className="hc-dl2-progress">
          <div className="hc-dl2-progress-head">
            <span>cancion.mp4</span>
            <span className="hc-dl2-percent">0%</span>
          </div>
          <div className="hc-dl2-progress-bar"><div className="hc-dl2-progress-fill" /></div>
        </div>
        {/* Done toast */}
        <div className="hc-dl2-toast">
          <span className="hc-dl2-toast-check">✓</span>
          <span>{t("help.anim.dl.done") || "Guardado"} · cancion.mp4</span>
        </div>
        <Cursor className="hc-dl2-cursor" />
      </div>
    </div>
  );
}

const REGISTRY = {
  "upload-flow": UploadFlow,
  "editor-sync": EditorSync,
  "approve-flow": ApproveFlow,
  "download-flow": DownloadFlow,
};

export default function HelpAnimation({ name }) {
  const { t } = useI18n();
  const Comp = useMemo(() => REGISTRY[name] || null, [name]);
  if (!Comp) return null;
  return (
    <ReplayWrap label={t("help.anim.replay") || "Reproducir de nuevo"}>
      <Comp />
    </ReplayWrap>
  );
}
