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
import AdminPanel from "./components/AdminPanel";

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
  const [delivery, setDelivery] = useState({
    delivery_profile: "youtube",
    umg_frame_size: "HD",
    umg_fps: 24,
    umg_prores_profile: 3,
  });
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
  const [backgroundFile, setBackgroundFile] = useState(null);
  const [backgroundId, setBackgroundId] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const pollingIntervals = useRef(new Set());
  const PARALLEL_WORKERS = 5;

  // --- Handle URL params (billing callbacks, email verification) ---
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("billing") === "success") {
      // Refresh user data after successful checkout
      if (getToken()) {
        authFetch(`${API}/auth/me`).then(r => r.json()).then(userData => {
          localStorage.setItem("genly_user", JSON.stringify(userData));
          setUser(userData);
        }).catch(() => {});
      }
      window.history.replaceState({}, "", window.location.pathname);
    }
    if (params.get("verify_email")) {
      fetch("/auth/verify-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: params.get("verify_email") }),
      }).catch(() => {});
      window.history.replaceState({}, "", window.location.pathname);
    }
    if (params.get("reset_password")) {
      setShowLanding(false);
      setView("login");
    }
  }, []);

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
    try {
      const res = await authFetch(`${API}/jobs`);
      if (res.status === 401) { handleLogout(); return; }
      const data = await res.json();
      if (Array.isArray(data)) setHistory(data);
    } catch {}
  }, []);

  useEffect(() => { if (token) fetchHistory(); }, [token, fetchHistory]);

  const pollJob = useCallback((jobId) => {
    // Poll every 3 s (instead of 1 s) and skip the tick entirely when the tab
    // is hidden. For a user with a few tabs open and 20 active jobs this cuts
    // the request rate by ~90%.
    return new Promise((resolve) => {
      const iv = setInterval(async () => {
        if (typeof document !== "undefined" && document.hidden) return;
        try {
          const data = await (await authFetch(`${API}/status/${jobId}`)).json();
          setJobs((prev) => prev.map((j) =>
            j.job_id === jobId ? { ...j, status: data.status, current_step: data.current_step, progress: data.progress, error: data.error } : j
          ));
          if (data.status === "done" || data.status === "error" || data.status === "pending_review" || data.status === "validation_failed") {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            fetchHistory();
            resolve(data.status);
          }
        } catch {}
      }, 3000);
      pollingIntervals.current.add(iv);
    });
  }, [fetchHistory]);

  useEffect(() => () => {
    pollingIntervals.current.forEach((iv) => clearInterval(iv));
  }, []);

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
      setTranscribeError(t("batch.error_server"));
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

    let nextIdx = 0;
    const worker = async () => {
      while (nextIdx < jobList.length) {
        const i = nextIdx++;
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "processing", current_step: "background", progress: 22 } : j
        ));
        const formData = new FormData();
        formData.append("file", jobList[i]._file);
        formData.append("artist", jobList[i].artist);
        formData.append("style", style);
        if (jobList[i].language) formData.append("language", jobList[i].language);
        formData.append("segments_json", JSON.stringify(jobList[i].segments));
        formData.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          formData.append("umg_frame_size", delivery.umg_frame_size);
          formData.append("umg_fps", String(delivery.umg_fps));
          formData.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }
        if (backgroundId) formData.append("background_id", backgroundId);
        else if (backgroundFile) formData.append("background_file", backgroundFile);

        try {
          const res = await authFetch(`${API}/generate`, { method: "POST", body: formData });
          const data = await res.json();
          if (data.detail) {
            // Plan limit error
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: data.detail } : j
            ));
            continue;
          }
          setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
          await pollJob(data.job_id);
        } catch {
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: t("batch.error_server") } : j
          ));
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL_WORKERS, jobList.length) }, () => worker()));
  };

  const processQueueDirect = async (jobList) => {
    let nextIdx = 0;
    const worker = async () => {
      while (nextIdx < jobList.length) {
        const i = nextIdx++;
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "processing", current_step: "whisper", progress: 0 } : j
        ));
        const formData = new FormData();
        formData.append("file", jobList[i]._file);
        formData.append("artist", jobList[i].artist);
        formData.append("style", style);
        formData.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          formData.append("umg_frame_size", delivery.umg_frame_size);
          formData.append("umg_fps", String(delivery.umg_fps));
          formData.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }
        if (jobList[i].language) formData.append("language", jobList[i].language);
        if (backgroundId) formData.append("background_id", backgroundId);
        else if (backgroundFile) formData.append("background_file", backgroundFile);

        // Retry-on-429 with exponential backoff. Batch uploads of 30+ files can
        // briefly exceed the per-user rate limit; 429s are transient, not failures.
        let res = null;
        let data = null;
        let attempt = 0;
        const MAX_429_RETRIES = 5;
        let networkError = false;
        while (attempt <= MAX_429_RETRIES) {
          try {
            res = await authFetch(`${API}/upload`, { method: "POST", body: formData });
          } catch {
            networkError = true;
            break;
          }
          if (res.status !== 429) {
            data = await res.json();
            break;
          }
          // 429: wait 2^attempt seconds (2, 4, 8, 16, 32) then retry.
          const waitMs = Math.min(32000, 2000 * Math.pow(2, attempt));
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "queued", error: null } : j
          ));
          await new Promise((r) => setTimeout(r, waitMs));
          attempt++;
        }
        if (networkError || !res || !data) {
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: t("batch.error_server") } : j
          ));
          continue;
        }
        if (data.detail) {
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: data.detail } : j
          ));
          continue;
        }
        setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
        await pollJob(data.job_id);
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL_WORKERS, jobList.length) }, () => worker()));
  };

  const handleReset = (skipConfirm = false) => {
    const hasActive = jobs.some((j) => j.status === "processing" || j.status === "queued");
    if (hasActive && !skipConfirm && !window.confirm(t("batch.confirm_cancel"))) return;
    pollingIntervals.current.forEach((iv) => clearInterval(iv));
    pollingIntervals.current.clear();
    setFiles([]); setJobs([]); setSelectedJob(null); setBackgroundFile(null); setBackgroundId(null);
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
    else if (id === "admin") { setView("admin"); }
  };

  const allHaveArtist = files.length > 0 && files.every((f) => f.artist.trim());

  // --- Landing (always first, public) ---
  if (showLanding) {
    return <Landing
      onStart={() => { if (token) setShowLanding(false); else setView("login"); setShowLanding(false); }}
      onLogin={() => { setShowLanding(false); setView("login"); }}
      isLoggedIn={!!token}
    />;
  }

  // --- Login/Register ---
  if (!token && view === "login") {
    return <LoginPage onLogin={(t, u) => { handleLogin(t, u); setView("dashboard"); }} onBack={() => setShowLanding(true)} />;
  }

  // --- Not authenticated, redirect to landing ---
  if (!token) {
    return <Landing
      onStart={() => setView("login")}
      onLogin={() => setView("login")}
      isLoggedIn={false}
    />;
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
        <header className="sticky top-0 z-20 flex items-center justify-between px-8 py-4 border-b border-white/[0.04] bg-surface/80 backdrop-blur-xl" style={{boxShadow: '0 1px 12px rgba(0,0,0,0.2)'}}>
          <div className="flex items-center gap-3">
            {!sidebarOpen && (
              <button onClick={() => setSidebarOpen(true)} className="mr-2 text-gray-400 hover:text-white transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
              </button>
            )}
          </div>
          <div className="flex items-center gap-4">
            {user && (
              <div className="flex items-center gap-2">
                <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-brand/20 to-brand-light/20 flex items-center justify-center border border-white/[0.06]">
                  <span className="text-[10px] font-bold text-brand uppercase">{user.username?.charAt(0)}</span>
                </div>
                <span className="text-xs text-gray-500">{user.username}</span>
              </div>
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
                <UploadZone
                  files={files}
                  onFiles={setFiles}
                  onDeliveryChange={setDelivery}
                  backgroundFile={backgroundFile}
                  onBackgroundFile={setBackgroundFile}
                  backgroundId={backgroundId}
                  onBackgroundId={setBackgroundId}
                />

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

          {/* Admin panel */}
          {view === "admin" && user?.role === "admin" && (
            <AdminPanel onBack={() => setView("dashboard")} />
          )}

          {/* Job detail */}
          {view === "detail" && selectedJob && (
            <div className="flex justify-center">
              <JobDetail
                job={selectedJob}
                onBack={() => setView("dashboard")}
                onJobUpdate={(updatedJob) => { setSelectedJob(updatedJob); fetchHistory(); }}
              />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
