import { forwardRef, useEffect, useState } from "react";
import { useI18n } from "../i18n";

const ACTIVE_STATUSES = new Set(["processing", "queued", "editing", "transcribing", "transcribing_queued", "awaiting_upload"]);
const FALLBACK_COPY = {
  "media.generating": "Generando preview con IA",
  "media.queued": "Esperando turno de render",
  "media.updating": "Actualizando preview",
  "media.unavailable": "Miniatura no disponible",
  "media.preparing": "Preparando preview",
};

const MediaPreview = forwardRef(function MediaPreview({
  src,
  alt = "",
  status = "",
  className = "",
  imageClassName = "",
  imageFit = "cover",
  label,
  children,
  ...props
}, ref) {
  const i18n = useI18n?.() || {};
  const t = (key) => i18n.t?.(key) || FALLBACK_COPY[key] || key;
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);

  const showImage = Boolean(src) && !failed;
  const isLoading = !showImage && !failed && ACTIVE_STATUSES.has(status);
  const statusLabel = {
    processing: t("media.generating"),
    queued: t("media.queued"),
    editing: t("media.updating"),
    error: t("media.unavailable"),
    validation_failed: t("media.unavailable"),
    rejected: t("media.unavailable"),
  }[status];
  const fallbackLabel = failed || (!src && !isLoading)
    ? t("media.unavailable")
    : label || statusLabel || t("media.preparing");
  const mediaState = showImage ? "ready" : isLoading ? "loading" : "unavailable";

  return (
    <div ref={ref} className={`media-preview ${className}`} data-media-state={mediaState} {...props}>
      <div className="media-preview__fallback" aria-hidden={showImage} role={showImage ? undefined : "status"} aria-live={isLoading ? "polite" : undefined}>
        <div className="media-preview__signal"><span /><span /><span /><span /><span /></div>
        <small>{fallbackLabel}</small>
        <span className="media-preview__ai-label">GENLY AI</span>
      </div>
      {showImage && (
        <img src={src} alt={alt} loading="lazy" className={`media-preview__image ${imageClassName}`} style={{ objectFit: imageFit }} onError={() => setFailed(true)} />
      )}
      {children && <div className="media-preview__overlay">{children}</div>}
    </div>
  );
});

export default MediaPreview;
