import { useI18n } from "../../i18n";
import TagChips from "./TagChips";

const TITLE_MAX = 100;
const DESC_MAX = 5000;

function Counter({ value, max }) {
  const ratio = value / max;
  const cls = ratio >= 1 ? "text-red-400" : ratio >= 0.9 ? "text-amber-400" : "text-gray-600";
  return <span className={`text-[10px] ${cls}`}>{value}/{max}</span>;
}

// Editable metadata form: what the user sees here is exactly what gets
// published (the backend uploads it verbatim).
export default function MetadataEditor({ metadata, onChange }) {
  const { t } = useI18n();

  const set = (key, value) => onChange({ ...metadata, [key]: value });

  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs text-gray-500 uppercase tracking-wider">{t("yt.publish.meta_title")}</label>
          <Counter value={(metadata.title || "").length} max={TITLE_MAX} />
        </div>
        <input
          value={metadata.title || ""}
          maxLength={TITLE_MAX}
          onChange={(e) => set("title", e.target.value)}
          className="input-field text-sm w-full"
        />
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs text-gray-500 uppercase tracking-wider">{t("yt.publish.meta_description")}</label>
          <Counter value={(metadata.description || "").length} max={DESC_MAX} />
        </div>
        <textarea
          value={metadata.description || ""}
          maxLength={DESC_MAX}
          rows={6}
          onChange={(e) => set("description", e.target.value)}
          className="input-field text-sm w-full whitespace-pre-line"
        />
      </div>

      <div>
        <label className="text-xs text-gray-500 uppercase tracking-wider block mb-1">Tags</label>
        <TagChips tags={metadata.tags || []} onChange={(tags) => set("tags", tags)} />
      </div>
    </div>
  );
}
