/**
 * Server-authoritative Art Track access mirrored for UI eligibility.
 *
 * Admins always keep access so production can expose the internal tool even
 * when the public build kill-switch is off. Non-admin rollouts require both
 * the backend feature flag and the explicit Vite build flag.
 */
export function hasArtTrackAccess(user) {
  return user?.role === "admin" || user?.features?.art_track === true;
}

export function canCreateArtTrack(user, buildEnabled) {
  return user?.role === "admin" || (buildEnabled && hasArtTrackAccess(user));
}
