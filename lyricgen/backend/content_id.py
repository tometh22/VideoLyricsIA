"""Content ID pre-check — provider interface, dormant until UMG grants access.

The check answers "will this upload get claimed by Content ID?" BEFORE the
video goes public. It requires YouTube Partner API access that only a
Content ID CMS owner (UMG) can grant:

  1. UMG adds our OAuth client / Google account as a linked user on their
     content owner in Studio CMS with at least "Claims: view" permission —
     that authorizes youtubePartner v1 calls with
     onBehalfOfContentOwner=<their CMS id>.
  2. The channel OAuth consent must include the scope
     https://www.googleapis.com/auth/youtubepartner.
  3. With that, the practical check is post-upload claim polling via
     claimSearch.list(videoId=...). A true PRE-upload fingerprint match
     additionally needs reference-library read access ("Assets/References:
     view") — a bigger grant.

Until configured (env below), get_provider() returns the Null provider:
status "unknown", and the API layer hides the field entirely so the UI
shows nothing.

Env: YOUTUBE_PARTNER_ENABLED, YOUTUBE_PARTNER_CONTENT_OWNER_ID,
YOUTUBE_PARTNER_TOKEN_PATH.
"""

import os
from datetime import datetime, timezone


def is_configured() -> bool:
    return (
        os.environ.get("YOUTUBE_PARTNER_ENABLED", "").lower() in ("1", "true", "yes")
        and bool(os.environ.get("YOUTUBE_PARTNER_CONTENT_OWNER_ID", "").strip())
    )


class NullContentIdProvider:
    """Default: partner access not granted — always unknown."""

    name = "not_configured"

    def check_claim_status(self, video_path=None, audio_path=None, metadata=None) -> dict:
        return {
            "status": "unknown",
            "provider": self.name,
            "detail": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


class YouTubePartnerProvider:
    """Stub for the real integration. Lights up when UMG grants CMS
    access; v1 intentionally returns unknown with a reason so a partially
    configured deployment is observable, never wrong."""

    name = "youtube_partner"

    def __init__(self):
        self.content_owner_id = os.environ["YOUTUBE_PARTNER_CONTENT_OWNER_ID"]

    def check_claim_status(self, video_path=None, audio_path=None, metadata=None) -> dict:
        # TODO(partner-access): youtubePartner v1 claimSearch.list with
        # onBehalfOfContentOwner=self.content_owner_id once the grant and
        # the youtubepartner scope exist.
        return {
            "status": "unknown",
            "provider": self.name,
            "detail": {"reason": "partner_api_stub"},
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def get_provider():
    if is_configured():
        return YouTubePartnerProvider()
    return NullContentIdProvider()


def check_claim_status(video_path=None, audio_path=None, metadata=None) -> dict:
    return get_provider().check_claim_status(video_path, audio_path, metadata)
