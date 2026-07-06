import { useState } from "react";

// Editable tag chips: Enter/comma commits, Backspace on empty input pops
// the last chip. YouTube's total tag budget is ~500 chars.
const TAG_BUDGET = 500;

export default function TagChips({ tags, onChange }) {
  const [draft, setDraft] = useState("");
  const totalChars = tags.reduce((n, t) => n + t.length, 0);

  const commit = () => {
    const value = draft.trim().replace(/,+$/, "");
    if (value && !tags.includes(value)) onChange([...tags, value]);
    setDraft("");
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && !draft && tags.length) {
      onChange(tags.slice(0, -1));
    }
  };

  return (
    <div>
      <div className="flex flex-wrap gap-1.5 items-center glass rounded-xl px-3 py-2 min-h-[42px]">
        {tags.map((tag, i) => (
          <span key={`${tag}-${i}`}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-lg bg-surface-3/50 text-xs text-gray-300">
            {tag}
            <button type="button" onClick={() => onChange(tags.filter((_, j) => j !== i))}
              className="text-gray-500 hover:text-red-400 transition-colors leading-none" aria-label={`remove ${tag}`}>
              ×
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onBlur={commit}
          className="flex-1 min-w-[80px] bg-transparent text-xs text-white outline-none py-1"
          placeholder={tags.length ? "" : "tag1, tag2, …"}
        />
      </div>
      <p className={`text-[10px] mt-1 ${totalChars > TAG_BUDGET ? "text-red-400" : "text-gray-600"}`}>
        {totalChars}/{TAG_BUDGET}
      </p>
    </div>
  );
}
