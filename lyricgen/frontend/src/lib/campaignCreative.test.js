import { describe, it, expect } from "vitest";
import { campaignGenerateForm, reviewCreativeSettings } from "./campaignCreative";

describe("campaign creative wire contract", () => {
  it("carries every saved field, explicit false/empty choices and the assignment revision to native generation", () => {
    const settings = { font: "anton", font_scale: 1.3, effect: "rain", movement_style: "foto-parallax",
      animate_image: false, enable_scenes: false, match_lyrics: true, bg_verbatim: false, background_hint: "",
      title_artist_font: "nunito", title_song_font: "bebas-neue", title_size: 1.2, title_template: "badge",
      title_song_break: "Dos\nlíneas", frame_format: "cine", lyric_color: "#00FF00", lyric_sung_color: "#FFFFFF",
      text_contrast: "strong", lyrics_animation: "karaoke", line_transition: "wipe", custom_colors: "#112233,#445566",
      delivery_profile: "umg", umg_frame_size: "UHD-4K", umg_fps: "29.97", umg_prores_profile: "3", background_id: 7 };
    const form = campaignGenerateForm({ job_id: "job1", artist: "A", title: "Tema", assignment: { revision: 8 }, settings },
      { segments_revision: 6, segments_json: [{ start: 0, end: 1, text: "Humano" }] });
    for (const [k, v] of Object.entries(settings)) expect(form.get(k)).toBe(String(v));
    expect(form.get("campaign_creative_revision")).toBe("8");
    expect(form.get("base_revision")).toBe("6");
    expect(JSON.parse(form.get("segments_json"))[0].text).toBe("Humano");
    expect(form.has("file")).toBe(false);
  });
  it("saves individual visual exceptions without changing lyrics or passing operation controls", () => {
    const result = reviewCreativeSettings({ font: "anton", fontScale: "1.3", effect: "rain", titleSize: "1.2",
      titleSongBreak: "Uno\ndos", backgroundHint: "", bgVerbatim: true, segments: [{ text: "Do not send" }] },
    { match_lyrics: true, enable_scenes: false, background_id: null });
    expect(result).toEqual({ font: "anton", font_scale: 1.3, effect: "rain", title_size: 1.2, title_song_break: "Uno\ndos",
      background_hint: "", scene_source: "lyrics", enable_scenes: false, background_id: null });
    expect(reviewCreativeSettings({ backgroundHint: "Una montaña", bgVerbatim: false }, { match_lyrics: true }).scene_source).toBe("prompt_improved");
  });
});
