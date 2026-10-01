import { describe, expect, it } from "vitest";
import { matchesCampaignSong } from "./campaignSearch";

const song = { title: "Canción del corazón (En Vivo)", artist: "Charly García", filename: "ARF_001-master.wav", technical_code: "ARUM-123", job_id: "abc123def456" };
describe("campaign search", () => {
  it.each(["garcia corazon", " CORAZÓN   charly ", "arf 001", "arum-123", "vivo cancion", "abc123def456", "", "master.wav"])("finds %s across metadata", query => {
    expect(matchesCampaignSong(query, song)).toBe(true);
  });
  it.each(["garcia divididos", "arum999", "Canción inexistente"])("does not match missing terms: %s", query => {
    expect(matchesCampaignSong(query, song)).toBe(false);
  });
});
