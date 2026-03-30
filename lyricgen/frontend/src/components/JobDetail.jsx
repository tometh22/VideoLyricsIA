import { useState } from "react";

const API = "";

const TABS = [
  { key: "video", label: "Lyric Video", desc: "1920x1080" },
  { key: "short", label: "Short", desc: "1080x1920" },
  { key: "thumbnail", label: "Thumbnail", desc: "1280x720" },
];

export default function JobDetail({ job, onBack }) {
  const [activeTab, setActiveTab] = useState("video");
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
      a.href = `${API}/download/${job.job_id}/${type}`;
      a.download = "";
      a.click();
    });
  };

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-4">
          <button
            onClick={onBack}
            className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <button onClick={downloadAll} className="btn-primary text-sm py-2.5 px-5">
          <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Descargar todo
        </button>
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
            src={`${API}/preview/${job.job_id}/thumbnail`}
            alt="Thumbnail"
            className="w-full max-h-[500px] object-contain bg-black/30"
          />
        ) : (
          <video
            key={activeTab}
            src={`${API}/preview/${job.job_id}/${activeTab}`}
            controls
            className={`w-full bg-black/30 ${
              activeTab === "short" ? "max-h-[600px] mx-auto" : "max-h-[500px]"
            }`}
            style={activeTab === "short" ? { maxWidth: "340px", margin: "0 auto", display: "block" } : {}}
          />
        )}
      </div>

      {/* File info + individual download */}
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500">
          {TABS.find((t) => t.key === activeTab)?.desc}
          {activeTab !== "thumbnail" ? " MP4" : " JPG"}
        </p>
        <a
          href={`${API}/download/${job.job_id}/${activeTab}`}
          download
          className="text-xs font-medium text-brand hover:text-brand-light transition-colors flex items-center gap-1.5"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Descargar {TABS.find((t) => t.key === activeTab)?.label}
        </a>
      </div>
    </div>
  );
}
