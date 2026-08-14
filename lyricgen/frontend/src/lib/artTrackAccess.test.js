import { describe, expect, it } from "vitest";
import { canCreateArtTrack, hasArtTrackAccess } from "./artTrackAccess";

describe("Art Track access", () => {
  it("keeps Art Track available to admins when the public build flag is off", () => {
    const admin = { role: "admin", features: { art_track: false } };
    expect(hasArtTrackAccess(admin)).toBe(true);
    expect(canCreateArtTrack(admin, false)).toBe(true);
  });

  it("requires both server access and the build flag for non-admin users", () => {
    const enabledTenant = { role: "user", features: { art_track: true } };
    expect(hasArtTrackAccess(enabledTenant)).toBe(true);
    expect(canCreateArtTrack(enabledTenant, false)).toBe(false);
    expect(canCreateArtTrack(enabledTenant, true)).toBe(true);
  });

  it("keeps ordinary users out even when the public build flag is on", () => {
    const user = { role: "user", features: { art_track: false } };
    expect(hasArtTrackAccess(user)).toBe(false);
    expect(canCreateArtTrack(user, true)).toBe(false);
  });
});
