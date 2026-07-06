"""Re-encrypt every stored YouTube channel token with the current primary key.

Run during key rotation, AFTER deploying with the new TOKEN_ENCRYPTION_KEY
and the previous key in TOKEN_ENCRYPTION_KEYS_OLD:

    python scripts/rotate_token_keys.py           # dry run
    python scripts/rotate_token_keys.py --apply   # re-encrypt

MultiFernet decrypts with any configured key and encrypts with the
primary, so this is safe to run (and re-run) at any point mid-rotation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    apply = "--apply" in sys.argv

    from database import SessionLocal, YouTubeChannel
    from token_crypto import decrypt_token, encrypt_token

    db = SessionLocal()
    rotated = failed = 0
    try:
        rows = (
            db.query(YouTubeChannel)
            .filter(YouTubeChannel.token_encrypted.isnot(None))
            .all()
        )
        for row in rows:
            try:
                data = decrypt_token(row.token_encrypted)
            except Exception as e:
                failed += 1
                print(f"  ✗ channel {row.channel_id} ({row.tenant_id}): cannot decrypt — {e}")
                continue
            if apply:
                row.token_encrypted = encrypt_token(data)
            rotated += 1
            print(f"  ✓ channel {row.channel_id} ({row.tenant_id})")
        if apply:
            db.commit()
        print(f"\n{'Re-encrypted' if apply else 'Would re-encrypt'} {rotated} token(s); {failed} failed.")
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
