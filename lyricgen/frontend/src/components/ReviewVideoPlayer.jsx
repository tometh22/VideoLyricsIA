import { forwardRef, useImperativeHandle, useRef, useState } from "react";


function formatMediaTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const wholeSeconds = Math.floor(seconds);
  const minutes = Math.floor(wholeSeconds / 60);
  return `${minutes}:${String(wholeSeconds % 60).padStart(2, "0")}`;
}

/**
 * Review player whose controls never cover the encoded image.
 *
 * Browser-native media chrome is intentionally disabled. Some WebKit and
 * Chromium builds render a transient volume popover as an opaque rounded tab
 * attached to the left edge of the video. In a review workflow that looks
 * like a generated artefact even though the downloaded MP4 is clean.
 */
const ReviewVideoPlayer = forwardRef(function ReviewVideoPlayer({
  src,
  isShort = false,
  onError,
}, forwardedRef) {
  const videoRef = useRef(null);
  const playerRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(1);

  useImperativeHandle(forwardedRef, () => videoRef.current);

  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused || video.ended) {
      const playRequest = video.play();
      if (playRequest?.catch) playRequest.catch(() => {});
    } else {
      video.pause();
    }
  };

  const seek = (value) => {
    const video = videoRef.current;
    if (!video) return;
    const nextTime = Number(value);
    video.currentTime = nextTime;
    setCurrentTime(nextTime);
  };

  const changeVolume = (value) => {
    const video = videoRef.current;
    if (!video) return;
    const nextVolume = Number(value);
    video.volume = nextVolume;
    video.muted = nextVolume === 0;
    setVolume(nextVolume);
    setMuted(nextVolume === 0);
  };

  const toggleMuted = () => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !video.muted;
    setMuted(video.muted);
  };

  const toggleFullscreen = async () => {
    if (document.fullscreenElement) {
      await document.exitFullscreen?.();
      return;
    }
    await playerRef.current?.requestFullscreen?.();
  };

  return (
    <div
      ref={playerRef}
      data-tour="jobdetail-preview"
      data-testid="review-video-player"
      className={`job-detail-video-player mx-auto mb-4 ${
        isShort
          ? "job-detail-video-player--short"
          : "job-detail-video-player--landscape"
      }`}
    >
      <div className={`job-detail-media-frame rounded-t-card bg-black overflow-hidden ${
        isShort
          ? "job-detail-media-frame--short"
          : "job-detail-media-frame--landscape"
      }`}>
        <video
          ref={videoRef}
          src={src}
          preload="metadata"
          playsInline
          onError={onError}
          onClick={togglePlayback}
          onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)}
          onDurationChange={(event) => setDuration(event.currentTarget.duration || 0)}
          onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime || 0)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          className="job-detail-media-video w-full h-full block object-contain bg-black/40 cursor-pointer"
        />
      </div>

      <div
        className="flex items-center gap-3 px-3 py-2.5 rounded-b-card bg-surface-1/95 ring-1 ring-white/[0.06] ring-inset"
        data-testid="review-video-controls"
        aria-label="Controles del video"
      >
        <button
          type="button"
          onClick={togglePlayback}
          className="w-8 h-8 shrink-0 rounded-lg flex items-center justify-center text-white hover:bg-white/[0.08] transition-colors"
          aria-label={playing ? "Pausar" : "Reproducir"}
          title={playing ? "Pausar" : "Reproducir"}
        >
          {playing ? (
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M6 5h4v14H6zm8 0h4v14h-4z" />
            </svg>
          ) : (
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M8 5v14l11-7z" />
            </svg>
          )}
        </button>

        <input
          type="range"
          min="0"
          max={duration || 0}
          step="0.1"
          value={Math.min(currentTime, duration || 0)}
          onChange={(event) => seek(event.target.value)}
          className="min-w-0 flex-1 accent-brand cursor-pointer"
          aria-label="Posición del video"
        />

        <span className="text-[11px] tabular-nums text-ink-secondary whitespace-nowrap">
          {formatMediaTime(currentTime)} / {formatMediaTime(duration)}
        </span>

        <button
          type="button"
          onClick={toggleMuted}
          className="w-8 h-8 shrink-0 rounded-lg flex items-center justify-center text-ink-secondary hover:text-white hover:bg-white/[0.08] transition-colors"
          aria-label={muted ? "Activar sonido" : "Silenciar"}
          title={muted ? "Activar sonido" : "Silenciar"}
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M11 5L6 9H2v6h4l5 4V5z" strokeLinecap="round" strokeLinejoin="round" />
            {muted ? (
              <path d="M18 9l4 4m0-4l-4 4" strokeLinecap="round" />
            ) : (
              <path d="M15.5 8.5a5 5 0 010 7M18 6a8 8 0 010 12" strokeLinecap="round" />
            )}
          </svg>
        </button>

        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={muted ? 0 : volume}
          onChange={(event) => changeVolume(event.target.value)}
          className="hidden sm:block w-20 accent-brand cursor-pointer"
          aria-label="Volumen"
        />

        <button
          type="button"
          onClick={toggleFullscreen}
          className="w-8 h-8 shrink-0 rounded-lg flex items-center justify-center text-ink-secondary hover:text-white hover:bg-white/[0.08] transition-colors"
          aria-label="Pantalla completa"
          title="Pantalla completa"
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </div>
  );
});

export default ReviewVideoPlayer;
