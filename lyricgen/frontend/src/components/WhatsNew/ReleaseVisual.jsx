import { useI18n } from "../../i18n";

function TranscriptionVisual({ compact = false }) {
  const { t } = useI18n();
  const bars = [26, 44, 36, 58, 42, 72, 86, 64, 38, 76, 56, 34, 62, 48, 70, 44, 58, 32];
  if (compact) {
    return (
      <div className="relative flex h-[104px] items-center gap-3 overflow-hidden rounded-lg bg-[#080a10] p-3 ring-1 ring-white/[0.08]">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <p className="truncate text-[9px] uppercase tracking-[0.14em] text-gray-500">{t("release.visual.audio")}</p>
            <span className="shrink-0 rounded-full bg-accent/10 px-2 py-0.5 text-[9px] font-semibold text-accent ring-1 ring-accent/20">
              v2
            </span>
          </div>
          <div className="relative flex h-10 items-center justify-center gap-0.5 overflow-hidden rounded-md bg-surface/80 px-2">
            <div className="absolute bottom-1 top-1 left-[52%] w-px bg-accent/80" />
            {bars.slice(3, 16).map((h, idx) => (
              <div
                key={idx}
                className={`min-w-[3px] flex-1 rounded-full ${idx >= 4 && idx <= 7 ? "bg-accent" : "bg-white/16"}`}
                style={{ height: `${Math.max(22, h)}%` }}
              />
            ))}
          </div>
          <div className="flex items-center gap-1.5">
            <span className="rounded-full bg-white/[0.045] px-2 py-0.5 text-[9px] text-gray-400">{t("release.visual.timing_fixed")}</span>
            <span className="rounded-full bg-white/[0.045] px-2 py-0.5 text-[9px] text-gray-400">{t("release.visual.silence")}</span>
          </div>
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
      <div className="grid h-[104px] grid-cols-[74px_1fr] items-center gap-3 rounded-lg bg-[#0b0b12] p-3 ring-1 ring-white/[0.08]">
        <div className="grid h-[68px] w-[68px] place-items-center rounded-lg bg-brand/14 text-[24px] font-extrabold text-brand-light ring-1 ring-brand/30">
          Abc
        </div>
        <div className="min-w-0">
          <p className="text-[9px] uppercase tracking-[0.14em] text-gray-500">{t("release.visual.typography")}</p>
          <p className="mt-1.5 truncate text-[13px] font-semibold leading-tight text-white">Como el viento</p>
          <p className="mt-1 truncate text-[10px] text-gray-500">{t("announce.typocase_tagline")}</p>
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
      <div className="grid h-[104px] grid-cols-[minmax(0,1fr)_86px] items-center gap-3 rounded-lg bg-[#0b0b12] p-3 ring-1 ring-white/[0.08]">
        <div className="min-w-0">
          <p className="text-[9px] uppercase tracking-[0.14em] text-gray-500">{t("release.visual.frame")}</p>
          <p className="mt-1.5 text-[13px] font-semibold text-white">{t("release.visual.cine")}</p>
          <span className="mt-2 inline-flex rounded-full bg-white px-2 py-0.5 text-[9px] font-bold text-black">
            {t("release.visual.cine")}
          </span>
        </div>
        <div className="relative aspect-video overflow-hidden rounded-md bg-black ring-1 ring-white/[0.08]">
          <div className="absolute inset-x-0 top-[22%] bottom-[22%] bg-gradient-to-br from-cyan-500 via-indigo-500 to-rose-400" />
          <div className="absolute left-2 right-2 top-[22%] border-t border-white/20" />
          <div className="absolute bottom-[22%] left-2 right-2 border-t border-white/15" />
          <div className="absolute inset-0 grid place-items-center">
            <span className="h-1 w-7 rounded-full bg-white/70" />
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
      <div className="relative aspect-video overflow-hidden rounded-xl bg-black ring-1 ring-white/[0.06]">
        <div className="absolute inset-x-0 top-[18%] bottom-[18%] bg-gradient-to-br from-cyan-400 via-indigo-500 to-rose-400" />
        <div className="absolute left-4 right-4 top-[18%] border-t border-white/20" />
        <div className="absolute bottom-[18%] left-4 right-4 border-t border-white/15" />
        <div className="absolute inset-0 flex items-center justify-center">
          <p className="text-sm font-extrabold tracking-wide text-white drop-shadow">LYRIC VIDEO</p>
        </div>
      </div>
      <p className="mt-2 text-[11px] text-gray-500">{t("release.visual.cine_note")}</p>
    </div>
  );
}

function ControlVisual({ compact = false }) {
  const { t } = useI18n();
  const features = [
    {
      key: "lyrics",
      label: t("release.visual.official_lyrics"),
      icon: (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4" aria-hidden="true">
          <path d="M7 3.5h7l3 3V20H7z" />
          <path d="M14 3.5V7h3M9.5 11h5M9.5 14h5M9.5 17h3" strokeLinecap="round" />
        </svg>
      ),
    },
    {
      key: "editor",
      label: t("release.visual.new_editor"),
      icon: (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4" aria-hidden="true">
          <rect x="3.5" y="5" width="17" height="14" rx="2" />
          <path d="M8 9h8M8 12h5M8 15h7" strokeLinecap="round" />
        </svg>
      ),
    },
    {
      key: "library",
      label: t("release.visual.background_library"),
      icon: (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4" aria-hidden="true">
          <rect x="3.5" y="4" width="17" height="16" rx="2" />
          <path d="m6.5 16 3.5-4 2.5 2.5 2.5-3 2.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
          <circle cx="15.5" cy="8.5" r="1.25" />
        </svg>
      ),
    },
  ];

  if (compact) {
    return (
      <div className="relative h-[104px] overflow-hidden rounded-lg bg-[#090a12] p-3 ring-1 ring-white/[0.08]">
        <div className="absolute -right-8 -top-12 h-32 w-32 rounded-full bg-brand/30 blur-3xl" />
        <div className="absolute -bottom-14 left-4 h-24 w-28 rounded-full bg-accent/15 blur-3xl" />
        <div className="relative flex h-full items-end justify-between gap-3">
          <div className="min-w-0 pb-0.5">
            <p className="text-[8px] font-bold uppercase tracking-[0.18em] text-accent">{t("release.visual.more_control")}</p>
            <p className="mt-1 max-w-[150px] text-[14px] font-black leading-[1.02] tracking-[-0.03em] text-white">
              {t("release.visual.control_statement")}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {features.map((feature) => (
              <span key={feature.key} title={feature.label} className="grid h-8 w-8 place-items-center rounded-lg bg-white/[0.055] text-white ring-1 ring-white/[0.08]">
                {feature.icon}
              </span>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative min-h-[224px] overflow-hidden rounded-2xl bg-[#080910] p-3 ring-1 ring-white/[0.09] sm:p-4">
      <div className="absolute -right-12 -top-20 h-64 w-64 rounded-full bg-brand/25 blur-[70px]" />
      <div className="absolute -bottom-24 left-6 h-56 w-64 rounded-full bg-accent/15 blur-[80px]" />
      <div className="absolute inset-0 opacity-[0.10] [background-image:linear-gradient(rgba(255,255,255,.14)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.14)_1px,transparent_1px)] [background-size:28px_28px]" />

      <div className="relative mx-auto mt-8 max-w-[560px] overflow-hidden rounded-xl bg-[#10121a]/95 shadow-[0_18px_55px_rgba(0,0,0,.55)] ring-1 ring-white/[0.12] sm:mt-7">
        <div className="flex h-7 items-center justify-between border-b border-white/[0.07] px-3">
          <div className="flex items-center gap-1.5" aria-hidden="true">
            <span className="h-1.5 w-1.5 rounded-full bg-white/20" />
            <span className="h-1.5 w-1.5 rounded-full bg-white/10" />
            <span className="h-1.5 w-1.5 rounded-full bg-white/10" />
          </div>
          <p className="text-[8px] font-bold uppercase tracking-[0.18em] text-gray-500">{t("release.visual.studio")}</p>
          <span className="flex items-center gap-1 text-[8px] font-semibold text-accent">
            <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_8px_rgba(20,200,168,.85)]" />
            {t("release.visual.available_now")}
          </span>
        </div>

        <div className="relative h-[108px] overflow-hidden bg-gradient-to-br from-[#251846] via-[#50306c] to-[#0b7779] sm:h-[124px]">
          <div className="absolute -left-8 top-4 h-24 w-24 rounded-full bg-fuchsia-400/25 blur-2xl" />
          <div className="absolute right-5 top-2 h-24 w-24 rounded-full bg-cyan-300/25 blur-2xl" />
          <div className="absolute inset-0 bg-[linear-gradient(110deg,rgba(0,0,0,.34),transparent_52%,rgba(0,0,0,.26))]" />
          <div className="absolute inset-0 grid place-items-center px-10 text-center">
            <p className="whitespace-pre-line text-[18px] font-black uppercase leading-[0.92] tracking-[-0.045em] text-white drop-shadow-[0_4px_18px_rgba(0,0,0,.5)] sm:text-[24px]">
              {t("release.visual.control_statement")}
            </p>
          </div>
          <div className="absolute inset-x-3 bottom-2 flex items-center gap-2 rounded-full bg-black/35 px-2.5 py-1.5 backdrop-blur-md ring-1 ring-white/[0.08]">
            <span className="grid h-4 w-4 place-items-center rounded-full bg-white text-[7px] text-black">▶</span>
            <div className="relative h-1 flex-1 overflow-hidden rounded-full bg-white/15">
              <div className="h-full w-[62%] rounded-full bg-accent" />
            </div>
            <span className="font-mono text-[7px] tabular-nums text-white/65">01:42</span>
          </div>
        </div>

        <div className="grid grid-cols-3 divide-x divide-white/[0.07] border-t border-white/[0.05]">
          {features.map((feature, index) => (
            <div key={feature.key} className={`flex min-w-0 items-center gap-2 px-2.5 py-2 ${index === 0 ? "bg-brand/[0.09]" : "bg-white/[0.015]"}`}>
              <span className={`grid h-6 w-6 shrink-0 place-items-center rounded-md ${index === 0 ? "bg-brand/20 text-brand-light" : "bg-white/[0.05] text-gray-400"}`}>
                {feature.icon}
              </span>
              <p className="truncate text-[9px] font-semibold text-gray-200">{feature.label}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function MediaVisual({ entry, compact = false }) {
  const isVideo = (entry.media || "").endsWith(".mp4");
  if (!entry.media) return <TranscriptionVisual compact={compact} />;
  return (
    <div className={`overflow-hidden bg-black ring-1 ring-white/[0.08] ${compact ? "h-[104px] rounded-lg" : "rounded-2xl"}`}>
      {isVideo ? (
        <video src={entry.media} autoPlay muted loop playsInline className={compact ? "h-full w-full object-cover" : "aspect-video w-full object-cover"} />
      ) : (
        <img src={entry.media} alt="" className={compact ? "h-full w-full object-cover" : "aspect-video w-full object-cover"} />
      )}
    </div>
  );
}

export default function ReleaseVisual({ entry, compact = false }) {
  if (entry?.visual === "control") return <ControlVisual compact={compact} />;
  if (entry?.visual === "textcase") return <TextCaseVisual compact={compact} />;
  if (entry?.visual === "cinema") return <CinemaVisual compact={compact} />;
  if (entry?.visual === "media") return <MediaVisual entry={entry} compact={compact} />;
  return <TranscriptionVisual compact={compact} />;
}
