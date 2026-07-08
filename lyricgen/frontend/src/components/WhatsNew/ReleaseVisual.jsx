import { useI18n } from "../../i18n";

function TranscriptionVisual({ compact = false }) {
  const { t } = useI18n();
  const bars = [32, 54, 42, 70, 38, 64, 86, 58, 40, 76, 52, 34, 66, 48, 72, 44, 60, 36];
  return (
    <div className={`relative overflow-hidden rounded-2xl bg-[#0b0b12] ring-1 ring-white/[0.08] ${compact ? "p-3" : "p-5"}`}>
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/60 to-transparent" />
      <div className="mb-4 flex items-center justify-between">
        <div>
          <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{t("release.visual.audio")}</p>
          <p className="mt-1 text-xs text-white">02:18.42</p>
        </div>
        <span className="rounded-full bg-accent/12 px-2 py-1 text-[10px] font-semibold text-accent ring-1 ring-accent/25">
          {t("release.visual.synced")}
        </span>
      </div>
      <div className="relative flex h-24 items-center gap-1.5 overflow-hidden rounded-xl bg-surface/80 px-3">
        <div className="absolute bottom-0 left-[42%] top-0 w-px bg-accent shadow-[0_0_18px_rgba(20,200,168,.55)]" />
        {bars.map((h, idx) => (
          <div
            key={idx}
            className={`w-full rounded-full ${idx >= 6 && idx <= 9 ? "bg-accent" : "bg-white/18"}`}
            style={{ height: `${h}%` }}
          />
        ))}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-1.5">
        {[
          ["00:41", t("release.visual.timing_fixed"), true],
          ["01:08", t("release.visual.silence"), false],
          ["02:18", t("release.visual.active_line"), true],
        ].map(([time, label, active]) => (
          <div key={time} className={`rounded-lg px-2 py-2 ${active ? "bg-brand/12 ring-1 ring-brand/25" : "bg-white/[0.035]"}`}>
            <p className="text-[10px] tabular-nums text-gray-500">{time}</p>
            <p className={`mt-0.5 text-[10.5px] leading-tight ${active ? "text-white" : "text-gray-400"}`}>{label}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function TextCaseVisual({ compact = false }) {
  const { t } = useI18n();
  const cases = [
    ["MAY", "COMO EL VIENTO"],
    ["Aa", "Como El Viento"],
    ["Abc", "Como el viento"],
    ["ori", "como el viento"],
  ];
  return (
    <div className={`rounded-2xl bg-[#0b0b12] ring-1 ring-white/[0.08] ${compact ? "p-3" : "p-5"}`}>
      <p className="mb-3 text-[10px] uppercase tracking-[0.18em] text-gray-500">{t("release.visual.typography")}</p>
      <div className="grid grid-cols-2 gap-2">
        {cases.map(([code, sample]) => {
          const active = code === "Abc";
          return (
            <div key={code} className={`rounded-xl px-3 py-3 ${active ? "bg-brand/14 ring-1 ring-brand/35" : "bg-white/[0.035] ring-1 ring-white/[0.04]"}`}>
              <div className="mb-2 flex items-center justify-between">
                <span className={`text-xs font-extrabold ${active ? "text-brand-light" : "text-gray-500"}`}>{code}</span>
                {active && <span className="h-1.5 w-1.5 rounded-full bg-accent" />}
              </div>
              <p className="text-sm font-semibold leading-tight text-white">{sample}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CinemaVisual({ compact = false }) {
  const { t } = useI18n();
  return (
    <div className={`rounded-2xl bg-[#0b0b12] ring-1 ring-white/[0.08] ${compact ? "p-3" : "p-5"}`}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{t("release.visual.frame")}</p>
        <div className="flex rounded-full bg-white/[0.05] p-0.5 text-[10px]">
          <span className="px-2 py-1 text-gray-500">{t("release.visual.full")}</span>
          <span className="rounded-full bg-white px-2 py-1 font-bold text-black">{t("release.visual.cine")}</span>
        </div>
      </div>
      <div className="relative aspect-video overflow-hidden rounded-xl bg-gradient-to-br from-cyan-400 via-indigo-500 to-rose-400">
        <div className="absolute inset-x-0 top-0 h-[13.4%] bg-black" />
        <div className="absolute inset-x-0 bottom-0 h-[13.4%] bg-black" />
        <div className="absolute inset-0 flex items-center justify-center">
          <p className="text-sm font-extrabold tracking-wide text-white drop-shadow">LYRIC VIDEO</p>
        </div>
      </div>
      <p className="mt-2 text-[11px] text-gray-500">{t("release.visual.cine_note")}</p>
    </div>
  );
}

function MediaVisual({ entry, compact = false }) {
  const isVideo = (entry.media || "").endsWith(".mp4");
  if (!entry.media) return <TranscriptionVisual compact={compact} />;
  return (
    <div className="overflow-hidden rounded-2xl bg-black ring-1 ring-white/[0.08]">
      {isVideo ? (
        <video src={entry.media} autoPlay muted loop playsInline className="aspect-video w-full object-cover" />
      ) : (
        <img src={entry.media} alt="" className="aspect-video w-full object-cover" />
      )}
    </div>
  );
}

export default function ReleaseVisual({ entry, compact = false }) {
  if (entry?.visual === "textcase") return <TextCaseVisual compact={compact} />;
  if (entry?.visual === "cinema") return <CinemaVisual compact={compact} />;
  if (entry?.visual === "media") return <MediaVisual entry={entry} compact={compact} />;
  return <TranscriptionVisual compact={compact} />;
}
