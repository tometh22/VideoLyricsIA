export const REVIEW_FIELDS = {
  font: "font", fontScale: "font_scale", textCase: "text_case", lyricColor: "lyric_color",
  lyricSungColor: "lyric_sung_color", textContrast: "text_contrast", lyricsAnimation: "lyrics_animation",
  lineTransition: "line_transition", movementStyle: "movement_style", effect: "effect", genre: "genre",
  concept: "concept", backgroundHint: "background_hint", titleTemplate: "title_template",
  titleSize: "title_size", titleArtistFont: "title_artist_font", titleSongFont: "title_song_font",
  titleSongBreak: "title_song_break", frameFormat: "frame_format",
};

export function reviewCreativeSettings(review, top) {
  const out = {};
  for (const [camel, snake] of Object.entries(REVIEW_FIELDS)) {
    if (review[camel] != null) out[snake] = ["fontScale", "titleSize"].includes(camel) ? Number(review[camel]) : review[camel];
  }
  Object.assign(out, top);
  out.scene_source = out.background_hint?.trim()
    ? review.bgVerbatim ? "prompt_literal" : "prompt_improved"
    : top.match_lyrics ? "lyrics" : "auto";
  delete out.match_lyrics;
  return out;
}

export function campaignGenerateForm(item, job) {
  const form = new FormData();
  form.set("job_id", item.job_id);
  form.set("artist", item.artist || job.artist || "");
  form.set("song_title", item.title || job.song_title || "");
  form.set("segments_json", JSON.stringify(job.segments_json || job.segments || []));
  form.set("base_revision", String(job.segments_revision ?? 0));
  form.set("campaign_creative_revision", String(item.assignment?.revision ?? ""));
  for (const [key, value] of Object.entries(item.settings || {})) {
    if (value != null) form.set(key, String(value));
  }
  return form;
}
