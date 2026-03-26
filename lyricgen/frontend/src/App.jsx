import { useState, useRef, useCallback, useEffect } from "react";
import UploadZone from "./components/UploadZone";
import ConfigPanel from "./components/ConfigPanel";
import ProgressPanel from "./components/ProgressPanel";
import ResultsPanel from "./components/ResultsPanel";

const API = "";

export default function App() {
  const [file, setFile] = useState(null);
  const [artist, setArtist] = useState("");
  const style = "oscuro"; // style selection removed — backgrounds are random
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const pollingRef = useRef(null);

  const startPolling = useCallback((id) => {
    pollingRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API}/status/${id}`);
        const data = await res.json();
        setJobStatus(data);
        if (data.status === "done" || data.status === "error") {
          clearInterval(pollingRef.current);
        }
      } catch {
        /* network hiccup, retry next tick */
      }
    }, 2000);
  }, []);

  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, []);

  const handleGenerate = async () => {
    if (!file || !artist.trim()) return;

    const formData = new FormData();
    formData.append("file", file);
    formData.append("artist", artist.trim());
    formData.append("style", style);

    const res = await fetch(`${API}/upload`, { method: "POST", body: formData });
    const data = await res.json();
    setJobId(data.job_id);
    setJobStatus({
      status: "processing",
      current_step: "whisper",
      progress: 0,
    });
    startPolling(data.job_id);
  };

  const handleReset = () => {
    setFile(null);
    setArtist("");
    setJobId(null);
    setJobStatus(null);
  };

  const isDone = jobStatus?.status === "done";
  const isProcessing = jobStatus?.status === "processing";
  const isError = jobStatus?.status === "error";

  return (
    <div className="min-h-screen bg-surface flex flex-col items-center px-4 py-10">
      <h1 className="text-4xl font-bold text-brand mb-2">LyricGen</h1>
      <p className="text-gray-400 mb-10 text-center">
        Sube un MP3 y genera un lyric video, un YouTube Short y un thumbnail en segundos.
      </p>

      {!jobId && (
        <div className="w-full max-w-2xl space-y-6">
          <UploadZone file={file} onFile={setFile} />
          <ConfigPanel artist={artist} onArtist={setArtist} />
          <button
            onClick={handleGenerate}
            disabled={!file || !artist.trim()}
            className="w-full py-3 rounded-xl font-semibold text-lg transition
              bg-brand hover:bg-brand/80 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Generar videos
          </button>
        </div>
      )}

      {isProcessing && <ProgressPanel status={jobStatus} />}

      {isError && (
        <div className="mt-8 text-center space-y-4">
          <p className="text-red-400 text-lg">Error: {jobStatus.error || "Algo salió mal."}</p>
          <button
            onClick={handleReset}
            className="px-6 py-2 rounded-lg bg-surface-light text-white hover:bg-brand/30"
          >
            Intentar de nuevo
          </button>
        </div>
      )}

      {isDone && (
        <ResultsPanel jobId={jobId} files={jobStatus.files} onReset={handleReset} />
      )}
    </div>
  );
}
