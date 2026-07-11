import { forwardRef, useEffect, useState } from "react";

const STATUS_LABELS = {
  processing: "Generando preview con IA",
  queued: "Esperando turno de render",
  editing: "Actualizando preview",
  error: "Preview no disponible",
  validation_failed: "Preview no disponible",
  rejected: "Preview no disponible",
};

const ACTIVE_STATUSES = new Set(["processing", "queued", "editing", "transcribing", "transcribing_queued", "awaiting_upload"]);

const MediaPreview = forwardRef(function MediaPreview({
  src,
  alt = "",
  status = "",
  className = "",
  imageClassName = "",
  label,
  children,
  ...props
}, ref) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);

  const showImage = Boolean(src) && !failed;
  const isLoading = !showImage && !failed && ACTIVE_STATUSES.has(status);
  const fallbackLabel = failed || (!src && !isLoading)
    ? "Miniatura no disponible"
    : label || STATUS_LABELS[status] || "Preparando preview";
  const mediaState = showImage ? "ready" : isLoading ? "loading" : "unavailable";

  return (
    <div ref={ref} className={`media-preview ${className}`} data-media-state={mediaState} {...props}>
      <div className="media-preview__fallback" aria-hidden={showImage}>
        <div className="media-preview__signal"><span /><span /><span /><span /><span /></div>
        <small>{fallbackLabel}</small>
        <span className="media-preview__ai-label">GENLY AI</span>
      </div>
      {showImage && (
        <img src={src} alt={alt} loading="lazy" className={`media-preview__image ${imageClassName}`} onError={() => setFailed(true)} />
      )}
      {children && <div className="media-preview__overlay">{children}</div>}
    </div>
  );
});

export default MediaPreview;
