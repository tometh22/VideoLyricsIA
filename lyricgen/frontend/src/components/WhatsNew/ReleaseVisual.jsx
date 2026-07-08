import { useI18n } from "../../i18n";

function TranscriptionVisual({ compact = false }) {
  const { t } = useI18n();
  const bars = [26, 44, 36, 58, 42, 72, 86, 64, 38, 76, 56, 34, 62, 48, 70, 44, 58, 32];
  if (compact) {
    return (
      <div className="relative flex h-full min-h-[88px] flex-col justify-between overflow-hidden rounded-lg bg-[#080a10] p-2 ring-1 ring-white/[0.08]">
        <div className="flex items-center justify-between">
          <p className="text-[8px] uppercase tracking-[0.12em] text-gray-500">{t("release.visual.audio")}</p>
          <span className="rounded-full bg-accent/12 px-1.5 py-0.5 text-[8px] font-semibold text-accent ring-1 ring-accent/20">
            {t("release.visual.synced")}
          </span>
        </div>
        <div className="relative flex h-12 items-center justify-center gap-0.5 overflow-hidden rounded-md bg-surface/80 px-1.5">
          <div className="absolute bottom-1 top-1 left-[46%] w-px bg-accent/80" />
          {bars.slice(2, 15).map((h, idx) => (
            <div
              key={idx}
              className={`min-w-[3px] max-w-[8px] flex-1 rounded-full ${idx >= 4 && idx <= 7 ? "bg-accent" : "bg-white/16"}`}
              style={{ height: `${Math.max(22, h)}%` }}
            />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="relative overflow-hidden rounded-xl bg-[#080a10] p-3.5 ring-1 ring-white/[0.08]">
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/60 to-transparent" />
      <div className="mb-2.5 flex items-center justify-between">
        <div>
          <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">
            <span>{t("release.visual.audio")}</span>
            <span aria-hidden="true"> · Timeline IA</span>
          </p>
          <p className="mt-1 text-[11.5px] font-semibold text-white">02:18.42 · Motor v2</p>
        </div>
        <span className="rounded-full bg-accent/12 px-2 py-1 text-[10px] font-semibold text-accent ring-1 ring-accent/25">
          {t("release.visual.synced")}
        </span>
      </div>
      <div className="relative overflow-hidden rounded-lg bg-[#0d1018] p-2.5 ring-1 ring-white/[0.045]">
        <div className="mb-2.5 flex items-center justify-between text-[10px] text-gray-500">
          <span className="font-mono tabular-nums">00:41.08</span>
          <span className="rounded-full bg-white/[0.045] px-2 py-1 text-gray-400">voz detectada</span>
          <span className="font-mono tabular-nums">02:18.42</span>
        </div>
        <div className="relative flex h-20 items-center justify-center gap-1.5 overflow-hidden rounded-lg bg-black/35 px-4">
          <div className="absolute bottom-0 left-[48%] top-0 w-px bg-accent shadow-[0_0_18px_rgba(20,200,168,.55)]" />
          <div className="absolute left-[48%] top-2 rounded-full bg-accent px-2 py-1 text-[9px] font-bold text-black">
            activa
          </div>
          {bars.map((h, idx) => (
            <div
              key={idx}
              className={`min-w-[5px] max-w-[15px] flex-1 rounded-full ${idx >= 6 && idx <= 10 ? "bg-accent" : "bg-white/15"}`}
              style={{ height: `${h}%` }}
            />
          ))}
        </div>
      </div>
      <div className="mt-2.5 grid gap-2 sm:grid-cols-[0.84fr_1fr]">
        <div className="rounded-lg bg-white/[0.035] p-2.5 ring-1 ring-white/[0.045]">
          <p className="text-[10px] font-semibold uppercase text-gray-500">Antes</p>
          <p className="mt-2 text-[12px] leading-snug text-gray-400">silencios largos y líneas fuera de tiempo</p>
        </div>
        <div className="rounded-lg bg-accent/[0.08] p-2.5 ring-1 ring-accent/20">
          <p className="text-[10px] font-semibold uppercase text-accent">Ahora</p>
          <p className="mt-2 text-[12px] leading-snug text-white">segmentos alineados, pausas filtradas y revisión más limpia</p>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-3 gap-1.5">
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
  if (compact) {
    return (
      <div className="flex h-full min-h-[88px] flex-col justify-between rounded-lg bg-[#0b0b12] p-2 ring-1 ring-white/[0.08]">
        <p className="text-[8px] uppercase tracking-[0.12em] text-gray-500">Texto</p>
        <div className="grid grid-cols-4 gap-1">
          {cases.map(([code]) => {
            const active = code === "Abc";
            return (
              <div
                key={code}
                className={`grid h-6 place-items-center rounded-md text-[9px] font-extrabold ${
                  active ? "bg-brand/18 text-brand-light ring-1 ring-brand/40" : "bg-white/[0.045] text-gray-500"
                }`}
              >
                {code}
              </div>
            );
          })}
        </div>
        <div className="rounded-md bg-brand/10 px-2 py-1.5 ring-1 ring-brand/25">
          <p className="text-[11px] font-semibold leading-tight text-white">Como el viento</p>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-[#0b0b12] p-5 ring-1 ring-white/[0.08]">
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
  if (compact) {
    return (
      <div className="flex h-full min-h-[88px] flex-col justify-between rounded-lg bg-[#0b0b12] p-2 ring-1 ring-white/[0.08]">
        <div className="flex items-center justify-between gap-2">
          <p className="text-[8px] uppercase tracking-[0.12em] text-gray-500">Video</p>
          <span className="rounded-full bg-white px-1.5 py-0.5 text-[8px] font-bold text-black">
            {t("release.visual.cine")}
          </span>
        </div>
        <div className="relative aspect-video overflow-hidden rounded-md bg-gradient-to-br from-cyan-400 via-indigo-500 to-rose-400">
          <div className="absolute inset-x-0 top-0 h-[13.4%] bg-black" />
          <div className="absolute inset-x-0 bottom-0 h-[13.4%] bg-black" />
          <div className="absolute inset-0 flex items-center justify-center">
            <p className="text-[8px] font-extrabold tracking-wide text-white drop-shadow">LYRIC VIDEO</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-[#0b0b12] p-5 ring-1 ring-white/[0.08]">
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
    <div className={`overflow-hidden bg-black ring-1 ring-white/[0.08] ${compact ? "h-full min-h-[88px] rounded-lg" : "rounded-2xl"}`}>
      {isVideo ? (
        <video src={entry.media} autoPlay muted loop playsInline className={compact ? "h-full w-full object-cover" : "aspect-video w-full object-cover"} />
      ) : (
        <img src={entry.media} alt="" className={compact ? "h-full w-full object-cover" : "aspect-video w-full object-cover"} />
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
