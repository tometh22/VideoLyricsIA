import { useState, useRef, useCallback, useEffect } from "react";
import { useI18n } from "./i18n";
import LoginPage from "./components/LoginPage";
import Landing from "./components/Landing";
import Sidebar from "./components/Sidebar";
import Dashboard from "./components/Dashboard";
import HistoryView from "./components/HistoryView";
import UploadZone from "./components/UploadZone";
import LyricsEditor from "./components/LyricsEditor";
import BatchProgress from "./components/BatchProgress";
import JobDetail from "./components/JobDetail";
import Settings from "./components/Settings";

const API = "";

// --- Auth helpers ---
function getToken() {
  return localStorage.getItem("genly_token");
}
function getUser() {
  try {
    return JSON.parse(localStorage.getItem("genly_user") || "null");
  } catch {
    return null;
  }
}
function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
function authFetch(url, opts = {}) {
  const headers = { ...opts.headers, ...authHeaders() };
  return fetch(url, { ...opts, headers });
}

export default function App() {
  const { t } = useI18n();
  const [token, setToken] = useState(getToken());
  const [user, setUser] = useState(getUser());
  const [showLanding, setShowLanding] = useState(true);
  const [view, setView] = useState("dashboard");
  const [files, setFiles] = useState([]);
  const style = "oscuro";

  const [reviewQueue, setReviewQueue] = useState([]);
  const [currentReview, setCurrentReview] = useState(null);
  const [approvedJobs, setApprovedJobs] = useState([]);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState(null);
  const [readyToGenerate, setReadyToGenerate] = useState(false);

  const [jobs, setJobs] = useState([]);
  const [history, setHistory] = useState([]);
  const [selectedJob, setSelectedJob] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const pollingRef = useRef(null);

  // --- Auth ---
  const handleLogin = (newToken, newUser) => {
    localStorage.setItem("genly_token", newToken);
    localStorage.setItem("genly_user", JSON.stringify(newUser));
    setToken(newToken);
    setUser(newUser);
  };

  const handleLogout = () => {
    localStorage.removeItem("genly_token");
    localStorage.removeItem("genly_user");
    setToken(null);
    setUser(null);
    setShowLanding(true);
    setView("dashboard");
  };

  const fetchHistory = useCallback(async () => {
    if (!getToken()) return;
    try { setHistory(await (await authFetch(`${API}/jobs`)).json()); } catch {}
  }, []);

  useEffect(() => { if (token) fetchHistory(); }, [token, fetchHistory]);

  const pollJob = useCallback((jobId) => {
    return new Promise((resolve) => {
      const iv = setInterval(async () => {
        try {
          const data = await (await authFetch(`${API}/status/${jobId}`)).json();
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
  }, [fetchHistory]);

  useEffect(() => () => { if (pollingRef.current) clearInterval(pollingRef.current); }, []);

  // --- Review flow ---
  const handleStartReview = async () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    setReviewQueue([...files]);
    setView("review");
    transcribeNext([...files], 0);
  };

  const handleGenerateDirect = () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    const jobList = files.map((f) => ({
      filename: f.file.name, _file: f.file, artist: f.artist.trim(),
      language: f.language, status: "queued", current_step: null,
      progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    setView("generating");
    processQueueDirect(jobList);
  };

  const transcribeNext = async (queue, idx) => {
    if (idx >= queue.length) return;
    const entry = queue[idx];
    setTranscribing(true);
    setTranscribeError(null);

    const formData = new FormData();
    formData.append("file", entry.file);
    if (entry.language) formData.append("language", entry.language);

    try {
      const res = await authFetch(`${API}/transcribe`, { method: "POST", body: formData });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const text = await res.text();
      if (!text) throw new Error("Empty response");
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
      transcribeNext(r.queue, nextIdx);
    } else if (r.queue.length === 1) {
      startGenerationWithSegments(newApproved);
    } else {
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
    setView("generating");
    setReadyToGenerate(false);
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
        const res = await authFetch(`${API}/generate`, { method: "POST", body: formData });
        const data = await res.json();
        setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
        await pollJob(data.job_id);
      } catch {
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "error", error: "Error de conexion" } : j
        ));
      }
    }
  };

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
        const res = await authFetch(`${API}/upload`, { method: "POST", body: formData });
        const data = await res.json();
        setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
        await pollJob(data.job_id);
      } catch {
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "error", error: "Error de conexion" } : j
        ));
      }
    }
  };

  const handleReset = () => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    setFiles([]); setJobs([]); setSelectedJob(null);
    setReviewQueue([]); setCurrentReview(null); setApprovedJobs([]);
    setTranscribing(false); setReadyToGenerate(false); setTranscribeError(null);
    setView("dashboard");
    fetchHistory();
  };

  const handleGenerateBatch = () => {
    setReadyToGenerate(false);
    startGenerationWithSegments(approvedJobs);
  };

  const handleSelectJob = async (jobId) => {
    try {
      setSelectedJob(await (await authFetch(`${API}/status/${jobId}`)).json());
      setView("detail");
    } catch {}
  };

  const handleNav = (id) => {
    if (id === "dashboard") { setView("dashboard"); setSelectedJob(null); }
    else if (id === "new") { setView("new"); setFiles([]); }
    else if (id === "history") { setView("history"); }
    else if (id === "settings") { setView("settings"); }
  };

  const allHaveArtist = files.length > 0 && files.every((f) => f.artist.trim());

  // --- Not authenticated: show login ---
  if (!token) {
    return <LoginPage onLogin={handleLogin} />;
  }

  // --- Landing ---
  if (showLanding) {
    return <Landing onStart={() => setShowLanding(false)} />;
  }

  return (
    <div className="min-h-screen bg-surface flex">
      <Sidebar
        activeView={view === "new" || view === "review" || view === "generating" ? "new" : view === "detail" ? "history" : view}
        onNav={handleNav}
        open={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
        user={user}
        onLogout={handleLogout}
      />

      <div className={`flex-1 min-h-screen transition-all duration-300 ${sidebarOpen ? "ml-64" : "ml-0"}`}>
        {/* Ambient */}
        <div className="fixed inset-0 pointer-events-none">
          <div className="absolute top-[-30%] left-[20%] w-[600px] h-[600px] bg-brand/[0.03] rounded-full blur-[120px]" />
          <div className="absolute bottom-[-20%] right-[-5%] w-[500px] h-[500px] bg-brand-light/[0.02] rounded-full blur-[100px]" />
        </div>

        {/* Top bar */}
        <header className="relative z-10 flex items-center justify-between px-8 py-4 border-b border-white/[0.04]">
          <div className="flex items-center gap-3">
            {!sidebarOpen && (
              <button onClick={() => setSidebarOpen(true)} className="mr-2 text-gray-400 hover:text-white transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
              </button>
            )}
          </div>
          <div className="flex items-center gap-4">
            {user && (
              <span className="text-xs text-gray-500">{user.username}</span>
            )}
          </div>
        </header>

        {/* Content */}
        <main className="relative z-10 px-8 pt-8 pb-20">

          {/* Dashboard */}
          {view === "dashboard" && (
            <Dashboard
              history={history}
              onSelectJob={handleSelectJob}
              onNewBatch={() => { setView("new"); setFiles([]); }}
              onViewHistory={() => setView("history")}
            />
          )}

          {/* History */}
          {view === "history" && (
            <HistoryView
              history={history}
              onSelect={handleSelectJob}
              onBack={() => setView("dashboard")}
            />
          )}

          {/* New batch */}
          {view === "new" && !currentReview && !transcribing && !readyToGenerate && (
            <div className="w-full max-w-xl mx-auto animate-fade-in">
              <div className="flex items-center gap-3 mb-8">
                <button onClick={() => setView("dashboard")}
                  className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M19 12H5M12 19l-7-7 7-7" />
                  </svg>
                </button>
                <div>
                  <h1 className="text-2xl font-bold">{t("upload.new_batch")}</h1>
                  <p className="text-sm text-gray-500">{t("upload.new_batch_sub")}</p>
                </div>
              </div>

              <div className="space-y-5">
                <UploadZone files={files} onFiles={setFiles} />

                {allHaveArtist && (
                  <div className="flex gap-3">
                    <button onClick={handleStartReview} className="btn-primary flex-1 py-4">
                      {t("upload.review_lyrics")}
                    </button>
                    <button onClick={handleGenerateDirect} className="btn-secondary flex-1 py-4 text-sm">
                      {t("upload.generate_direct")}
                    </button>
                  </div>
                )}

                {files.length > 0 && !allHaveArtist && (
                  <p className="text-center text-xs text-amber-400/70">
                    {t("upload.complete_artist")}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Transcribing */}
          {transcribing && (
            <div className="w-full max-w-md mx-auto mt-16 animate-fade-in text-center">
              <div className="w-12 h-12 mx-auto mb-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              <h2 className="text-xl font-bold mb-2">{t("transcribe.title")}</h2>
              <p className="text-gray-500 text-sm">{t("transcribe.subtitle")}</p>
              {reviewQueue.length > 1 && (
                <p className="text-xs text-gray-600 mt-2">
                  {t("transcribe.song")} {approvedJobs.length + 1} {t("editor.song_of")} {reviewQueue.length}
                </p>
              )}
            </div>
          )}

          {/* Transcribe error */}
          {transcribeError && !transcribing && (
            <div className="w-full max-w-md mx-auto mt-8 animate-fade-in">
              <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-5 py-4 text-center">
                <p className="text-sm text-red-400">{transcribeError}</p>
                <button onClick={() => { setTranscribeError(null); setView("new"); }}
                  className="mt-3 text-xs text-gray-400 hover:text-white transition-colors underline">
                  {t("detail.back")}
                </button>
              </div>
            </div>
          )}

          {/* Lyrics review */}
          {currentReview && !transcribing && (
            <div className="flex justify-center">
              <LyricsEditor
                segments={currentReview.segments}
                filename={currentReview.file.name}
                referenceLyrics={currentReview.referenceLyrics || ""}
                onApprove={handleApproveLyrics}
                onBack={handleReset}
                isBatch={currentReview.queue.length > 1}
                batchProgress={currentReview.queue.length > 1
                  ? `${currentReview.queueIdx + 1} ${t("editor.song_of")} ${currentReview.queue.length}`
                  : ""}
              />
            </div>
          )}

          {/* Ready to generate (batch summary) */}
          {readyToGenerate && (
            <div className="w-full max-w-xl mx-auto animate-fade-in">
              <div className="text-center mb-8">
                <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-accent/10 flex items-center justify-center">
                  <svg className="w-7 h-7 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                </div>
                <h2 className="text-2xl font-bold mb-2">{approvedJobs.length} {t("ready.title")}</h2>
                <p className="text-gray-500">{t("ready.subtitle")}</p>
              </div>

              <div className="space-y-1.5 mb-8 max-h-60 overflow-y-auto">
                {approvedJobs.map((job, i) => (
                  <div key={i} className="flex items-center gap-3 glass rounded-xl px-4 py-2.5">
                    <div className="w-2 h-2 rounded-full bg-accent shrink-0" />
                    <span className="text-sm text-white truncate flex-1">{job.file.name.replace(/\.mp3$/i, "")}</span>
                    <span className="text-xs text-gray-500">{job.segments.length} {t("editor.lines")}</span>
                  </div>
                ))}
              </div>

              <div className="flex gap-3 justify-center">
                <button onClick={handleGenerateBatch} className="btn-primary text-lg py-4 px-8">
                  {t("ready.generate")} {approvedJobs.length} {t("ready.videos")}
                </button>
                <button onClick={handleReset} className="btn-secondary">{t("ready.cancel")}</button>
              </div>
            </div>
          )}

          {/* Generating */}
          {view === "generating" && (
            <div className="flex justify-center">
              <BatchProgress jobs={jobs} onReset={handleReset} />
            </div>
          )}

          {/* Settings */}
          {view === "settings" && (
            <Settings onBack={() => setView("dashboard")} />
          )}

          {/* Job detail */}
          {view === "detail" && selectedJob && (
            <div className="flex justify-center">
              <JobDetail job={selectedJob} onBack={() => setView("dashboard")} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
