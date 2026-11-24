import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { applyCase } from "./fontCatalog";

const fontCache = new Map();
async function loadFont(request, filename) {
  if (!fontCache.has(filename)) {
    const promise = request(`/editor/preview/fonts/${encodeURIComponent(filename)}`).then(async response => {
      if (!response.ok) throw new Error("preview_font_unavailable");
      return new Uint8Array(await response.arrayBuffer());
    }).catch(error => { fontCache.delete(filename); throw error; });
    fontCache.set(filename, promise);
  }
  return fontCache.get(filename);
}

// Both the browser's WASM libass and the video's native libass receive the
// track built by compose_lyric_overlay and the very same bundled font files.
// Always draw at the video resolution; CSS only scales the finished image.
export default function NativeLyricPreview({ segments, settings, jobId, request, currentTime,
  backgroundUrl = null, isPlaying = false, onFontResolved, onStateChange }) {
  const { t } = useI18n();
  const frame = useRef(null);
  const host = useRef(null);
  const loadedFonts = useRef(new Set());
  const [displayWidth, setDisplayWidth] = useState(1920);
  const [dimensions, setDimensions] = useState({ width: 1920, height: 1080 });
  const dimensionsRef = useRef(dimensions);
  const video = useRef(null);
  const renderer = useRef(null);
  const chain = useRef(Promise.resolve());
  const clock = useRef(currentTime);
  const resolved = useRef(onFontResolved);
  const notifyState = useRef(onStateChange);
  clock.current = currentTime;
  resolved.current = onFontResolved;
  notifyState.current = onStateChange;
  const [state, setState] = useState("loading");
  const [retry, setRetry] = useState(0);
  const body = JSON.stringify({ segments, settings });
  const spokenText = segments.filter(line => currentTime >= Number(line.start) && currentTime < Number(line.end))
    .map(line => applyCase(line.text || "", settings.text_case || "upper")).join(" / ");
  const imageLabel = `${t("review_workspace.render_preview") || "Vista previa del texto del video"}${spokenText ? `: ${spokenText}` : ""}`;

  useEffect(() => { notifyState.current?.(state); }, [state]);

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    setState("loading");
    const timer = setTimeout(async () => {
      try {
        const response = await request(`/editor/${encodeURIComponent(jobId)}/text-preview`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body, signal: controller.signal,
        });
        if (!response.ok) throw new Error("preview_unavailable");
        const track = await response.json();
        const fonts = await Promise.all(track.fonts.map(filename => loadFont(request, filename)));
        if (!alive) return;
        // Serialize worker IPC. A slower response can never replace newer text.
        chain.current = chain.current.catch(() => {}).then(async () => {
          if (!alive) return;
          dimensionsRef.current = { width: track.width, height: track.height };
          setDimensions(dimensionsRef.current);
          if (!renderer.current) {
            const { createSubtitleRenderer } = await import("../lib/nativeSubtitleRenderer");
            if (!alive) return;
            const canvas = document.createElement("canvas");
            canvas.width = track.width; canvas.height = track.height;
            canvas.style.cssText = `width:${track.width}px;height:${track.height}px;display:block`;
            host.current.replaceChildren(canvas);
            renderer.current = createSubtitleRenderer(canvas, track.ass, fonts, track.height);
            loadedFonts.current = new Set(track.fonts);
            await renderer.current.ready;
          } else {
            const canvas = host.current.querySelector("canvas");
            if (canvas) { canvas.style.width = `${track.width}px`; canvas.style.height = `${track.height}px`; }
            renderer.current.prescaleHeightLimit = track.height;
            renderer.current.maxRenderHeight = track.height;
            const missing = fonts.filter((_, index) => !loadedFonts.current.has(track.fonts[index]));
            if (missing.length) await renderer.current.renderer.addFonts(missing);
            track.fonts.forEach(filename => loadedFonts.current.add(filename));
            await renderer.current.renderer.setTrack(track.ass);
          }
          if (!alive || !renderer.current) return;
          await renderer.current.manualRender({ expectedDisplayTime: performance.now(),
            width: track.width, height: track.height, mediaTime: clock.current }, true);
          if (!alive) return;
          setState("ready");
          if (track.resolved_font) resolved.current?.(track.resolved_font);
        });
        await chain.current;
      } catch (error) {
        if (alive && error.name !== "AbortError") setState("error");
      }
    }, 180);
    return () => { alive = false; clearTimeout(timer); controller.abort(); };
  }, [body, jobId, request, retry]);

  useEffect(() => {
    const instance = renderer.current;
    if (!instance || state !== "ready") return;
    instance.manualRender({ expectedDisplayTime: performance.now(), ...dimensionsRef.current,
      mediaTime: currentTime }, true).catch(() => setState("error"));
  }, [currentTime, state]);

  useEffect(() => {
    const observer = new ResizeObserver(entries => setDisplayWidth(entries[0].contentRect.width));
    if (frame.current) observer.observe(frame.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => () => {
    // Destroy only after queued IPC finishes; a remount gets a fresh canvas.
    const instance = renderer.current;
    renderer.current = null;
    if (instance) chain.current.finally(() => instance.destroy()).catch(() => {});
  }, []);

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (Number.isFinite(element.duration) && element.duration > 0) {
      const desired = currentTime % element.duration;
      if (!isPlaying || Math.abs(element.currentTime - desired) > 0.25) element.currentTime = desired;
    }
    if (isPlaying) element.play()?.catch(() => {});
    else element.pause();
  }, [currentTime, isPlaying, backgroundUrl]);

  return <div ref={frame} className="relative overflow-hidden rounded-xl bg-[#0a0910]" style={{ aspectRatio: dimensions.width / dimensions.height }} data-testid="native-lyric-preview" aria-label={t("review_workspace.render_preview") || "Vista previa del texto del video"}>
    {backgroundUrl && <video ref={video} src={backgroundUrl} muted loop playsInline className="absolute inset-0 h-full w-full object-cover" />}
    <div ref={host} role="img" aria-label={imageLabel} aria-busy={state !== "ready"} className={`absolute left-0 top-0 origin-top-left ${state === "ready" ? "" : "invisible"}`}
      style={{ width: dimensions.width, height: dimensions.height, transform: `scale(${displayWidth / dimensions.width})` }} />
    {state !== "ready" && <div role="status" className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-3 text-center text-xs text-ink-secondary">
      <p>{state === "error" ? (t("review_workspace.preview_error") || "No se pudo mostrar la vista previa del texto.") : (t("review_workspace.preview_loading") || "Actualizando vista previa…")}</p>
      {state === "error" && <button type="button" className="rounded-lg px-3 py-2 ring-1 ring-white/20" onClick={() => { const old = renderer.current; renderer.current = null; old?.destroy().catch(() => {}); setRetry(value => value + 1); }}>{t("review_workspace.preview_retry") || "Reintentar"}</button>}
    </div>}
  </div>;
}
