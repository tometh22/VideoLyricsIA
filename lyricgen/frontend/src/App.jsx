import { useState, useRef, useCallback, useEffect } from "react";
import Sidebar from "./components/Sidebar";
import UploadZone from "./components/UploadZone";
import ConfigPanel from "./components/ConfigPanel";
import BatchProgress from "./components/BatchProgress";
import JobDetail from "./components/JobDetail";

const API = "";

export default function App() {
  const [files, setFiles] = useState([]);
  const [artist, setArtist] = useState("");
  const style = "oscuro";

  const [jobs, setJobs] = useState([]);
  const [started, setStarted] = useState(false);
  const [history, setHistory] = useState([]);
  const [selectedJob, setSelectedJob] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const pollingRef = useRef(null);

  // Load history on mount
  useEffect(() => {
    fetchHistory();
  }, []);

  const fetchHistory = async () => {
    try {
      const res = await fetch(`${API}/jobs`);
      const data = await res.json();
      setHistory(data);
    } catch { /* ignore */ }
  };

  const pollJob = useCallback((jobId) => {
    return new Promise((resolve) => {
      const iv = setInterval(async () => {
        try {
          const res = await fetch(`${API}/status/${jobId}`);
          const data = await res.json();
          setJobs((prev) =>
            prev.map((j) =>
              j.job_id === jobId
                ? { ...j, status: data.status, current_step: data.current_step, progress: data.progress, error: data.error }
                : j
            )
          );
          if (data.status === "done" || data.status === "error") {
            clearInterval(iv);
            fetchHistory();
            resolve(data.status);
          }
        } catch { /* retry */ }
      }, 2000);
      pollingRef.current = iv;
    });
  }, []);

  const processQueue = useCallback(async (jobList) => {
    for (let i = 0; i < jobList.length; i++) {
      setJobs((prev) =>
        prev.map((j, idx) =>
          idx === i ? { ...j, status: "processing", current_step: "whisper", progress: 0 } : j
        )
      );

      const formData = new FormData();
      formData.append("file", jobList[i]._file);
      formData.append("artist", artist.trim());
      formData.append("style", style);

      try {
        const res = await fetch(`${API}/upload`, { method: "POST", body: formData });
        const data = await res.json();
        setJobs((prev) =>
          prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j))
        );
        await pollJob(data.job_id);
      } catch {
        setJobs((prev) =>
          prev.map((j, idx) => (idx === i ? { ...j, status: "error", error: "Upload failed" } : j))
        );
      }
    }
  }, [artist, style, pollJob]);

  useEffect(() => {
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, []);

  const handleGenerate = () => {
    if (!files.length || !artist.trim()) return;
    const jobList = files.map((f) => ({
      filename: f.name, _file: f, status: "queued",
      current_step: null, progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    setStarted(true);
    setSelectedJob(null);
    processQueue(jobList);
  };

  const handleReset = () => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    setFiles([]);
    setArtist("");
    setJobs([]);
    setStarted(false);
    setSelectedJob(null);
    fetchHistory();
  };

  const handleSelectJob = async (jobId) => {
    try {
      const res = await fetch(`${API}/status/${jobId}`);
      const data = await res.json();
      setSelectedJob(data);
      setStarted(false);
    } catch { /* ignore */ }
  };

  const showUpload = !started && !selectedJob;

  return (
    <div className="min-h-screen bg-surface flex">
      {/* Sidebar */}
      <Sidebar
        history={history}
        selectedId={selectedJob?.job_id}
        onSelect={handleSelectJob}
        onNew={() => { setSelectedJob(null); setStarted(false); }}
        open={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
      />

      {/* Main area */}
      <div className={`flex-1 min-h-screen transition-all duration-300 ${sidebarOpen ? "ml-72" : "ml-0"}`}>
        {/* Ambient glow */}
        <div className="fixed inset-0 pointer-events-none">
          <div className="absolute top-[-30%] left-[20%] w-[600px] h-[600px] bg-brand/[0.03] rounded-full blur-[120px]" />
          <div className="absolute bottom-[-20%] right-[-5%] w-[500px] h-[500px] bg-brand-light/[0.02] rounded-full blur-[100px]" />
        </div>

        {/* Top bar */}
        <header className="relative z-10 flex items-center justify-between px-8 py-5 border-b border-white/[0.04]">
          <div className="flex items-center gap-3">
            {!sidebarOpen && (
              <button onClick={() => setSidebarOpen(true)} className="mr-2 text-gray-400 hover:text-white transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
            )}
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-brand to-brand-light flex items-center justify-center">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
              </svg>
            </div>
            <span className="text-lg font-bold tracking-tight">LyricGen</span>
            <span className="text-[10px] font-medium text-brand bg-brand/10 px-2 py-0.5 rounded-full ml-1 uppercase tracking-widest">Pro</span>
          </div>
          <span className="text-xs text-gray-500 hidden sm:block">Universal Music Group</span>
        </header>

        {/* Content */}
        <main className="relative z-10 flex flex-col items-center px-6 pt-12 pb-20">
          {showUpload && (
            <div className="w-full max-w-xl animate-fade-in">
              <div className="text-center mb-10">
                <h1 className="text-3xl sm:text-4xl font-extrabold tracking-tight mb-3 bg-gradient-to-r from-white via-white to-gray-400 bg-clip-text text-transparent">
                  Crea lyric videos en segundos
                </h1>
                <p className="text-gray-400 max-w-md mx-auto leading-relaxed">
                  Sube uno o varios MP3 y genera lyric videos, YouTube Shorts y thumbnails.
                </p>
              </div>

              <div className="space-y-5">
                <UploadZone files={files} onFiles={setFiles} />
                <ConfigPanel artist={artist} onArtist={setArtist} />
                <button
                  onClick={handleGenerate}
                  disabled={!files.length || !artist.trim()}
                  className="btn-primary w-full text-lg py-5"
                >
                  {files.length > 1 ? `Generar ${files.length} videos` : "Generar contenido"}
                  <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </button>
              </div>
            </div>
          )}

          {started && <BatchProgress jobs={jobs} onReset={handleReset} />}

          {selectedJob && !started && (
            <JobDetail job={selectedJob} onBack={() => setSelectedJob(null)} />
          )}
        </main>
      </div>
    </div>
  );
}
