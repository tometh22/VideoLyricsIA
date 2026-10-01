// Same token matching as backend/campaign_search.py. Terms may span fields
// and appear in any order; punctuation, case and accents do not affect a match.
export function normalizeCampaignSearch(value) {
  return String(value ?? "").normalize("NFKD").replace(/\p{M}/gu, "")
    .toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

export function matchesCampaignSearch(query, ...values) {
  const haystack = values.map(normalizeCampaignSearch).join(" ");
  return normalizeCampaignSearch(query).split(/\s+/).filter(Boolean).every(term => haystack.includes(term));
}

export function matchesCampaignSong(query, song) {
  return matchesCampaignSearch(query, song.title, song.artist, song.filename,
    song.technical_code, song.job_id, song.item_id, song.id);
}
