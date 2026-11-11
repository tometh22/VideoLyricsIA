// Genly · muestra privada: un solo video sonando a la vez.
document.addEventListener("play", (event) => {
  const playing = event.target;
  if (!(playing instanceof HTMLVideoElement)) return;
  document.querySelectorAll("video").forEach((video) => {
    if (video !== playing && !video.paused) video.pause();
  });
}, true);
