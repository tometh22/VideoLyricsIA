import { useState } from "react";

const API = "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenParam() {
  const token = localStorage.getItem("genly_token");
  return token ? `token=${encodeURIComponent(token)}` : "";
}

const TABS = [
  { key: "video", label: "Lyric Video", desc: "1920x1080" },
  { key: "short", label: "Short", desc: "1080x1920" },
  { key: "thumbnail", label: "Thumbnail", desc: "1280x720" },
];

export default function JobDetail({ job, onBack }) {
  const [activeTab, setActiveTab] = useState("video");
  const [uploading, setUploading] = useState(false);
  const [youtubeResult, setYoutubeResult] = useState(job.youtube || null);
  const [metadataPreview, setMetadataPreview] = useState(null);
  const [showYoutubePanel, setShowYoutubePanel] = useState(false);
  const name = (job.filename || "").replace(/\.mp3$/i, "");

  if (job.status !== "done") {
    return (
      <div className="w-full max-w-2xl animate-fade-in text-center py-20">
        <p className="text-gray-400">Este job no esta disponible para preview.</p>
        <button onClick={onBack} className="btn-secondary mt-4">Volver</button>
      </div>
    );
  }

  const downloadAll = () => {
    ["video", "short", "thumbnail"].forEach((type) => {
      const a = document.createElement("a");
      a.href = `${API}/download/${job.job_id}/${type}?${tokenParam()}`;
      a.download = "";
      a.click();
    });
  };

  const previewMetadata = async () => {
    setShowYoutubePanel(true);
    try {
      const res = await fetch(`${API}/youtube/metadata/${job.job_id}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      setMetadataPreview(data);
    } catch (err) {
      setMetadataPreview({ error: err.message });
    }
  };

  const uploadToYoutube = async (privacy = "unlisted") => {
    setUploading(true);
    try {
      const res = await fetch(`${API}/youtube/upload/${job.job_id}?privacy=${privacy}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      setYoutubeResult(data);
    } catch (err) {
      setYoutubeResult({ error: err.message });
    }
    setUploading(false);
  };

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-4">
          <button onClick={onBack}
            className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={downloadAll} className="btn-secondary text-sm py-2.5 px-4">
            <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Descargar
          </button>
          {!youtubeResult && (
            <button onClick={previewMetadata} className="btn-primary text-sm py-2.5 px-4">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              Publicar en YouTube
            </button>
          )}
          {youtubeResult && !youtubeResult.error && (
            <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
              className="btn-primary text-sm py-2.5 px-4 bg-red-600 hover:bg-red-700">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              Ver en YouTube
            </a>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 p-1 glass rounded-2xl w-fit">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-5 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 ${
              activeTab === tab.key
                ? "bg-brand text-white shadow-glow"
                : "text-gray-400 hover:text-white"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Preview area */}
      <div className="glass rounded-3xl overflow-hidden mb-6">
        {activeTab === "thumbnail" ? (
          <img
            src={`${API}/preview/${job.job_id}/thumbnail?${tokenParam()}`}
            alt="Thumbnail"
            className="w-full max-h-[500px] object-contain bg-black/30"
          />
        ) : (
          <video
            key={activeTab}
            src={`${API}/preview/${job.job_id}/${activeTab}?${tokenParam()}`}
            controls
            className={`w-full bg-black/30 ${
              activeTab === "short" ? "max-h-[600px] mx-auto" : "max-h-[500px]"
            }`}
            style={activeTab === "short" ? { maxWidth: "340px", margin: "0 auto", display: "block" } : {}}
          />
        )}
      </div>

      {/* File info */}
      <div className="flex items-center justify-between mb-6">
        <p className="text-xs text-gray-500">
          {TABS.find((t) => t.key === activeTab)?.desc}
          {activeTab !== "thumbnail" ? " MP4" : " JPG"}
        </p>
        <a href={`${API}/download/${job.job_id}/${activeTab}?${tokenParam()}`} download
          className="text-xs font-medium text-brand hover:text-brand-light transition-colors flex items-center gap-1.5">
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Descargar {TABS.find((t) => t.key === activeTab)?.label}
        </a>
      </div>

      {/* YouTube Panel */}
      {showYoutubePanel && (
        <div className="glass rounded-3xl p-6 animate-fade-in">
          <h3 className="font-semibold mb-4 flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24">
              <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
            </svg>
            Publicar en YouTube
          </h3>

          {!metadataPreview && !youtubeResult && (
            <div className="flex items-center justify-center py-8">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              <span className="ml-3 text-sm text-gray-400">Generando metadata con IA...</span>
            </div>
          )}

          {metadataPreview && !metadataPreview.error && !youtubeResult && (
            <div className="space-y-4">
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Titulo</label>
                <p className="text-sm text-white mt-1 glass rounded-xl px-4 py-2.5">{metadataPreview.title}</p>
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Descripcion</label>
                <p className="text-sm text-gray-300 mt-1 glass rounded-xl px-4 py-2.5 whitespace-pre-line">{metadataPreview.description}</p>
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Tags</label>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {(metadataPreview.tags || []).map((tag, i) => (
                    <span key={i} className="px-2 py-1 rounded-lg bg-surface-3/50 text-xs text-gray-400">{tag}</span>
                  ))}
                </div>
              </div>

              <div className="flex gap-3 pt-2">
                <button onClick={() => uploadToYoutube("unlisted")} disabled={uploading}
                  className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
                  {uploading ? (
                    <><div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />Subiendo...</>
                  ) : (
                    "Subir como No Listado"
                  )}
                </button>
                <button onClick={() => uploadToYoutube("public")} disabled={uploading}
                  className="btn-secondary text-sm py-2.5 px-5 disabled:opacity-50">
                  Subir como Publico
                </button>
                <button onClick={() => setShowYoutubePanel(false)}
                  className="text-xs text-gray-500 hover:text-white transition-colors ml-auto">
                  Cancelar
                </button>
              </div>
            </div>
          )}

          {youtubeResult && !youtubeResult.error && (
            <div className="text-center py-6">
              <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-accent/10 flex items-center justify-center">
                <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <p className="text-sm font-medium text-white mb-1">Video publicado en YouTube</p>
              <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
                className="text-sm text-brand hover:text-brand-light transition-colors underline">
                {youtubeResult.url}
              </a>
              <p className="text-xs text-gray-500 mt-2">Estado: {youtubeResult.privacy}</p>
            </div>
          )}

          {(metadataPreview?.error || youtubeResult?.error) && (
            <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-center">
              <p className="text-sm text-red-400">{metadataPreview?.error || youtubeResult?.error}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
