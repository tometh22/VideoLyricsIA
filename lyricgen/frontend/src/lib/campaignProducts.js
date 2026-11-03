export const PRODUCT_TABS = [
  { id: "artists_art", label: "Art Tracks Artistas", channel: "artists", kind: "art_track" },
  { id: "hits_lyrics", label: "HitsDeSiempre Lyrics", channel: "hitsdesiempre", kind: "lyric_video" },
  { id: "hits_art", label: "HitsDeSiempre Art Tracks", channel: "hitsdesiempre", kind: "art_track" },
  { id: "rock_lyrics", label: "CancioneroRock Lyrics", channel: "cancionerorock", kind: "lyric_video" },
  { id: "rock_art", label: "CancioneroRock Art Tracks", channel: "cancionerorock", kind: "art_track" },
];

export function productForCampaign(campaign) {
  const kind = campaign.kind || "lyric_video";
  // Art tracks created before product channels existed were artist releases.
  const channel = campaign.default_render_params?.product_channel || (kind === "art_track" ? "artists" : "");
  return PRODUCT_TABS.find((product) => product.kind === kind && product.channel === channel)?.id || "unclassified";
}

export function productForSelection(kind, channel) {
  return PRODUCT_TABS.find((product) => product.kind === kind && product.channel === channel);
}
