import { useState, useRef, useCallback, useEffect } from "react";
import Sidebar from "./components/Sidebar";
import UploadZone from "./components/UploadZone";
import LyricsEditor from "./components/LyricsEditor";
import BatchProgress from "./components/BatchProgress";
import JobDetail from "./components/JobDetail";

const API = "";

export default function App() {
  const [files, setFiles] = useState([]); // [{file, artist, language}]
  const style = "oscuro";

  // Lyrics review state
  const [reviewQueue, setReviewQueue] = useState([]); // files pending review
  const [currentReview, setCurrentReview] = useState(null); // {file, artist, language, segments}
  const [approvedJobs, setApprovedJobs] = useState([]); // [{file, artist, segments}]
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState(null);
  const [readyToGenerate, setReadyToGenerate] = useState(false); // batch summary screen

  const [jobs, setJobs] = useState([]);
  const [started, setStarted] = useState(false);
  const [history, setHistory] = useState([]);
  const [selectedJob, setSelectedJob] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const pollingRef = useRef(null);

  useEffect(() => { fetchHistory(); }, []);

  const fetchHistory = async () => {
    try { setHistory(await (await fetch(`${API}/jobs`)).json()); } catch {}
  };

  const pollJob = useCallback((jobId) => {
    return new Promise((resolve) => {
      const iv = setInterval(async () => {
        try {
          const data = await (await fetch(`${API}/status/${jobId}`)).json();
          setJobs((prev) => prev.map((j) =>
            j.job_id === jobId ? { ...j, status: data.status, current_step: data.current_step, progress: data.progress, error: data.error } : j
          ));
          if (data.status === "done" || data.status === "error") {
            clearInterval(iv);
            fetchHistory();
            resolve(data.status);
          }
        } catch {}
      }, 1000);
      pollingRef.current = iv;
    });
  }, []);

  // Start transcription + review flow
  const handleStartReview = async () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    setReviewQueue([...files]);
    transcribeNext([...files], 0);
  };

  // Skip review — generate directly (fast mode)
  const handleGenerateDirect = () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    const jobList = files.map((f) => ({
      filename: f.file.name, _file: f.file, artist: f.artist.trim(),
      language: f.language, status: "queued", current_step: null,
      progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    setStarted(true);
    processQueueDirect(jobList);
  };

  const transcribeNext = async (queue, idx) => {
    if (idx >= queue.length) return; // all reviewed
    const entry = queue[idx];
    setTranscribing(true);
    setTranscribeError(null);

    const formData = new FormData();
    formData.append("file", entry.file);
    if (entry.language) formData.append("language", entry.language);

    try {
      const res = await fetch(`${API}/transcribe`, { method: "POST", body: formData });
      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`Server error ${res.status}: ${errText}`);
      }
      const text = await res.text();
      if (!text) throw new Error("Empty response from server");
      const data = JSON.parse(text);
      setTranscribing(false);
      setCurrentReview({
        file: entry.file, artist: entry.artist, language: entry.language,
        segments: data.segments, referenceLyrics: data.reference_lyrics || "",
        queueIdx: idx, queue,
      });
    } catch (err) {
      setTranscribing(false);
      setTranscribeError("Error transcribiendo: " + err.message);
    }
  };

  const handleApproveLyrics = (editedSegments) => {
    const r = currentReview;
    const newApproved = [...approvedJobs, {
      file: r.file, artist: r.artist, language: r.language, segments: editedSegments,
    }];
    setApprovedJobs(newApproved);
    setCurrentReview(null);

    const nextIdx = r.queueIdx + 1;
    if (nextIdx < r.queue.length) {
      // More songs to review
      transcribeNext(r.queue, nextIdx);
    } else if (r.queue.length === 1) {
      // Single song — generate immediately
      startGenerationWithSegments(newApproved);
    } else {
      // Batch complete — show summary (don't auto-generate)
      setReadyToGenerate(true);
    }
  };

  const startGenerationWithSegments = async (approved) => {
    const jobList = approved.map((a) => ({
      filename: a.file.name, _file: a.file, artist: a.artist,
      language: a.language, segments: a.segments,
      status: "queued", current_step: null, progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    setStarted(true);
    setReviewQueue([]);
    setApprovedJobs([]);

    for (let i = 0; i < jobList.length; i++) {
      setJobs((prev) => prev.map((j, idx) =>
        idx === i ? { ...j, status: "processing", current_step: "background", progress: 22 } : j
      ));

      const formData = new FormData();
      formData.append("file", jobList[i]._file);
      formData.append("artist", jobList[i].artist);
      formData.append("style", style);
      if (jobList[i].language) formData.append("language", jobList[i].language);
      formData.append("segments_json", JSON.stringify(jobList[i].segments));

      try {
        const res = await fetch(`${API}/generate`, { method: "POST", body: formData });
        const data = await res.json();
        setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
        await pollJob(data.job_id);
      } catch {
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "error", error: "Upload failed" } : j
        ));
      }
    }
  };

  // Direct generation (no review)
  const processQueueDirect = async (jobList) => {
    for (let i = 0; i < jobList.length; i++) {
      setJobs((prev) => prev.map((j, idx) =>
        idx === i ? { ...j, status: "processing", current_step: "whisper", progress: 0 } : j
      ));
      const formData = new FormData();
      formData.append("file", jobList[i]._file);
      formData.append("artist", jobList[i].artist);
      formData.append("style", style);
      if (jobList[i].language) formData.append("language", jobList[i].language);

      try {
        const res = await fetch(`${API}/upload`, { method: "POST", body: formData });
        const data = await res.json();
        setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
        await pollJob(data.job_id);
      } catch {
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "error", error: "Upload failed" } : j
        ));
      }
    }
  };

  useEffect(() => () => { if (pollingRef.current) clearInterval(pollingRef.current); }, []);

  const handleReset = () => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    setFiles([]); setJobs([]); setStarted(false); setSelectedJob(null);
    setReviewQueue([]); setCurrentReview(null); setApprovedJobs([]);
    setTranscribing(false); setReadyToGenerate(false); setTranscribeError(null);
    fetchHistory();
  };

  const handleGenerateBatch = () => {
    setReadyToGenerate(false);
    startGenerationWithSegments(approvedJobs);
  };

  const handleSelectJob = async (jobId) => {
    try { setSelectedJob(await (await fetch(`${API}/status/${jobId}`)).json()); setStarted(false); setCurrentReview(null); }
    catch {}
  };

  const allHaveArtist = files.length > 0 && files.every((f) => f.artist.trim());
  const showUpload = !started && !selectedJob && !currentReview && !transcribing && !readyToGenerate;
  const showReview = currentReview && !started;

  return (
    <div className="min-h-screen bg-surface flex">
      <Sidebar
        history={history} selectedId={selectedJob?.job_id}
        onSelect={handleSelectJob}
        onNew={() => { setSelectedJob(null); setStarted(false); setCurrentReview(null); setTranscribing(false); }}
        open={sidebarOpen} onToggle={() => setSidebarOpen(!sidebarOpen)}
      />

      <div className={`flex-1 min-h-screen transition-all duration-300 ${sidebarOpen ? "ml-72" : "ml-0"}`}>
        <div className="fixed inset-0 pointer-events-none">
          <div className="absolute top-[-30%] left-[20%] w-[600px] h-[600px] bg-brand/[0.03] rounded-full blur-[120px]" />
          <div className="absolute bottom-[-20%] right-[-5%] w-[500px] h-[500px] bg-brand-light/[0.02] rounded-full blur-[100px]" />
        </div>

        <header className="relative z-10 flex items-center justify-between px-8 py-5 border-b border-white/[0.04]">
          <div className="flex items-center gap-3">
            {!sidebarOpen && (
              <button onClick={() => setSidebarOpen(true)} className="mr-2 text-gray-400 hover:text-white transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
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

                {allHaveArtist && (
                  <div className="flex gap-3">
                    <button onClick={handleStartReview} className="btn-primary flex-1 py-4">
                      Revisar lyrics antes de generar
                    </button>
                    <button onClick={handleGenerateDirect} className="btn-secondary flex-1 py-4 text-sm">
                      Generar directo
                    </button>
                  </div>
                )}

                {files.length > 0 && !allHaveArtist && (
                  <p className="text-center text-xs text-amber-400/70">
                    Completa el nombre del artista en todos los archivos
                  </p>
                )}
              </div>
            </div>
          )}

          {transcribing && (
            <div className="w-full max-w-md mt-16 animate-fade-in text-center">
              <div className="w-12 h-12 mx-auto mb-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              <h2 className="text-xl font-bold mb-2">Transcribiendo lyrics</h2>
              <p className="text-gray-500 text-sm">Analizando audio...</p>
            </div>
          )}

          {transcribeError && !transcribing && !started && (
            <div className="w-full max-w-md mt-8 animate-fade-in">
              <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-5 py-4 text-center">
                <p className="text-sm text-red-400">{transcribeError}</p>
                <button onClick={() => setTranscribeError(null)}
                  className="mt-3 text-xs text-gray-400 hover:text-white transition-colors underline">
                  Cerrar
                </button>
              </div>
            </div>
          )}

          {showReview && (
            <LyricsEditor
              segments={currentReview.segments}
              filename={currentReview.file.name}
              referenceLyrics={currentReview.referenceLyrics || ""}
              onApprove={handleApproveLyrics}
              onBack={handleReset}
              isBatch={currentReview.queue.length > 1}
            />
          )}

          {readyToGenerate && !started && (
            <div className="w-full max-w-xl animate-fade-in">
              <div className="text-center mb-8">
                <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-accent/10 flex items-center justify-center">
                  <svg className="w-7 h-7 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                </div>
                <h2 className="text-2xl font-bold mb-2">
                  {approvedJobs.length} lyrics aprobadas
                </h2>
                <p className="text-gray-500">
                  Todas las canciones fueron revisadas. Podes generar los videos ahora o dejarlos para mas tarde.
                </p>
              </div>

              <div className="space-y-1.5 mb-8 max-h-60 overflow-y-auto">
                {approvedJobs.map((job, i) => (
                  <div key={i} className="flex items-center gap-3 glass rounded-xl px-4 py-2.5">
                    <div className="w-2 h-2 rounded-full bg-accent shrink-0" />
                    <span className="text-sm text-white truncate flex-1">
                      {job.file.name.replace(/\.mp3$/i, "")}
                    </span>
                    <span className="text-xs text-gray-500">{job.segments.length} lineas</span>
                  </div>
                ))}
              </div>

              <div className="flex gap-3 justify-center">
                <button onClick={handleGenerateBatch} className="btn-primary text-lg py-4 px-8">
                  Generar {approvedJobs.length} videos
                  <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </button>
                <button onClick={handleReset} className="btn-secondary">
                  Cancelar
                </button>
              </div>

              <p className="text-center text-xs text-gray-600 mt-6">
                La generacion puede tardar varios minutos. Podes cerrar el navegador y volver despues.
              </p>
            </div>
          )}

          {started && <BatchProgress jobs={jobs} onReset={handleReset} />}
          {selectedJob && !started && !currentReview && (
            <JobDetail job={selectedJob} onBack={() => setSelectedJob(null)} />
          )}
        </main>
      </div>
    </div>
  );
}
