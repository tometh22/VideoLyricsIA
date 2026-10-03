import { useEffect, useState } from "react";

export function mediaIdentity(url = "") {
  try {
    const parsed = new URL(url, "https://preview.invalid");
    for (const key of [...parsed.searchParams.keys()]) {
      if (/^x-amz-/i.test(key)) parsed.searchParams.delete(key);
    }
    return `${parsed.origin}${parsed.pathname}${parsed.search}`;
  } catch { return url; }
}

// Polling renews the R2 signature every five seconds. Reassigning src for
// those renewals aborts playback. Only a new object/render replaces it;
// an expired/broken URL can be renewed explicitly.
export default function RequestVideo({ url, poster, renderIdentity, videoRef, label }) {
  const identity = `${mediaIdentity(url)}:${renderIdentity}`;
  const [source, setSource] = useState(url);
  const [error, setError] = useState(false);
  useEffect(() => {
    setSource(url);
    setError(false);
    // A signature renewal alone must not reset the player.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity]);

  const retry = () => {
    setError(false);
    if (source !== url) setSource(url);
    else videoRef.current?.load();
  };

  return <>
    <video ref={videoRef} src={source} poster={poster || undefined}
      controls preload="metadata" playsInline
      onError={() => setError(true)}
      className="aspect-video max-h-[25rem] w-full bg-black object-contain"
      aria-label={label} />
    {error && <div role="alert" className="p-3 text-caption text-amber-200">
      No se pudo cargar el video. La letra guardada no se modifica.
      <button type="button" onClick={retry} className="ml-2 underline">Reintentar reproducción</button>
    </div>}
  </>;
}
