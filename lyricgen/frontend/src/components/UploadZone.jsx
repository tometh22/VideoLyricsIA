import { useRef, useState } from "react";
import { useI18n } from "../i18n";

export default function UploadZone({ files, onFiles }) {
  const { t } = useI18n();
  const inputRef = useRef();
  const [dragging, setDragging] = useState(false);

  const LANGUAGES = [
    { code: "", label: t("lang.auto") },
    { code: "es", label: t("lang.es") },
    { code: "en", label: t("lang.en") },
    { code: "pt", label: t("lang.pt") },
    { code: "fr", label: t("lang.fr") },
    { code: "it", label: t("lang.it") },
    { code: "de", label: t("lang.de") },
  ];

  const addFiles = (fileList) => {
    const mp3s = Array.from(fileList).filter((f) =>
      f.name.toLowerCase().endsWith(".mp3")
    );
    if (mp3s.length) {
      const newEntries = mp3s.map((f) => ({ file: f, artist: "", language: "" }));
      onFiles((prev) => [...prev, ...newEntries]);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };

  const updateField = (idx, field, value) => {
    onFiles((prev) =>
      prev.map((entry, i) => (i === idx ? { ...entry, [field]: value } : entry))
    );
  };

  const removeFile = (idx, e) => {
    e.stopPropagation();
    onFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current.click()}
        className={`group relative rounded-3xl p-8 text-center cursor-pointer transition-all duration-300
          ${dragging ? "bg-brand/10 border-brand shadow-glow" : files.length > 0 ? "glass" : "glass glass-hover"}
          border-2 ${dragging ? "border-brand" : files.length > 0 ? "border-white/[0.06]" : "border-dashed border-white/[0.08]"}
        `}
      >
        <input
          ref={inputRef} type="file" accept=".mp3" multiple className="hidden"
          onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
        />

        {files.length > 0 ? (
          <div onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-gray-400">
                {files.length} {files.length > 1 ? t("upload.files") : t("upload.file")}
              </span>
              <button
                onClick={(e) => { e.stopPropagation(); inputRef.current.click(); }}
                className="text-xs text-brand hover:text-brand-light transition-colors"
              >{t("upload.add_more")}</button>
            </div>
          </div>
        ) : (
          <div className="py-4">
            <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-surface-3/80 flex items-center justify-center group-hover:bg-brand/10 transition-colors duration-300">
              <svg className="w-7 h-7 text-gray-400 group-hover:text-brand transition-colors duration-300" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" />
              </svg>
            </div>
            <p className="text-gray-300 font-medium mb-1">{t("upload.drag")}</p>
            <p className="text-gray-600 text-sm">{t("upload.drag_sub")}</p>
          </div>
        )}
      </div>

      {files.length > 0 && (
        <div className="mt-3 space-y-2 max-h-96 overflow-y-auto pr-1">
          {files.map((entry, i) => (
            <div key={i} className="glass rounded-2xl px-4 py-3">
              <div className="flex items-center gap-3 mb-2">
                <div className="w-8 h-8 rounded-lg bg-brand/10 flex items-center justify-center shrink-0">
                  <svg className="w-4 h-4 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
                  </svg>
                </div>
                <p className="text-sm text-white truncate flex-1 min-w-0">{entry.file.name}</p>
                <button
                  onClick={(e) => removeFile(i, e)}
                  className="shrink-0 w-7 h-7 rounded-lg hover:bg-red-500/10 flex items-center justify-center text-gray-500 hover:text-red-400 transition-colors"
                >
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={entry.artist}
                  onChange={(e) => updateField(i, "artist", e.target.value)}
                  placeholder={t("upload.artist")}
                  className="flex-1 px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06]
                    focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500 transition-all"
                />
                <select
                  value={entry.language}
                  onChange={(e) => updateField(i, "language", e.target.value)}
                  className="px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06]
                    focus:border-brand/50 focus:outline-none text-sm text-white transition-all appearance-none cursor-pointer"
                >
                  {LANGUAGES.map((l) => (
                    <option key={l.code} value={l.code}>{l.label}</option>
                  ))}
                </select>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
