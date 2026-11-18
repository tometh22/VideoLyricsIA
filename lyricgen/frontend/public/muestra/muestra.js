// Genly · muestra privada.
// 1) Un solo video sonando a la vez.
document.addEventListener("play", (event) => {
  const playing = event.target;
  if (!(playing instanceof HTMLVideoElement)) return;
  document.querySelectorAll("video").forEach((video) => {
    if (video !== playing && !video.paused) video.pause();
  });
}, true);

// 2) Estudio: un reproductor, un selector de formato y una lista de versiones.
document.addEventListener("DOMContentLoaded", () => {
  const studio = document.querySelector("[data-studio]");
  if (!studio) return;
  const base = studio.dataset.media || "/muestra/the-home-music-rf7k2q/media/";
  const screen = studio.querySelector(".screen");
  const player = studio.querySelector("[data-player]");
  const still = studio.querySelector("[data-still]");
  const title = studio.querySelector("[data-title]");
  const meta = studio.querySelector("[data-meta]");
  const download = studio.querySelector("[data-download]");
  const formats = [...studio.querySelectorAll("[data-format]")];
  const picks = [...studio.querySelectorAll(".pick")];
  let current = picks.find((p) => p.getAttribute("aria-pressed") === "true") || picks[0];
  let format = "wide";

  const render = (autoplay) => {
    const d = current.dataset;
    // Not every item has every format (the art track has no thumbnail or
    // Canvas): fall back to 16:9 and disable what is missing.
    if (!d[format]) format = "wide";
    formats.forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.format === format));
      b.disabled = !d[b.dataset.format];
    });
    picks.forEach((p) => p.setAttribute("aria-pressed", String(p === current)));
    screen.dataset.screen = format;
    title.textContent = d.title;
    meta.innerHTML = d.meta;
    if (format === "thumb") {
      player.pause();
      player.hidden = true;
      still.hidden = false;
      still.src = base + d.thumb;
      still.alt = "Thumbnail · " + d.title;
      download.href = base + d.thumb;
      download.textContent = "Baixar thumbnail";
      return;
    }
    still.hidden = true;
    player.hidden = false;
    // Spotify Canvas: an 8-second silent loop, shown the way Spotify plays it.
    const canvas = format === "canvas";
    player.loop = canvas;
    player.muted = canvas;
    const src = base + d[format];
    if (!player.src.endsWith(src)) {
      player.pause();
      player.poster = base + d[format + "Poster"];
      player.src = src;
      if (autoplay || canvas) player.play().catch(() => {});
    }
    download.href = src;
    download.textContent = { tall: "Baixar vertical", canvas: "Baixar Canvas" }[format] || "Baixar 16:9";
  };

  formats.forEach((b) => b.addEventListener("click", () => {
    format = b.dataset.format;
    render(!player.paused);
  }));
  picks.forEach((p) => p.addEventListener("click", () => {
    const wasPlaying = !player.paused;
    current = p;
    render(wasPlaying);
    if (window.matchMedia("(max-width: 900px)").matches) {
      studio.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }));
  render(false);
});
