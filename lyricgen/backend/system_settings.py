"""Operator-editable switches kept in the LOCAL database (never the portal DB).

An operator flips these from the admin panel instead of editing Railway
variables and redeploying. Reads are cached for a few seconds per process, so a
change reaches every API/worker process almost at once. A missing row or an
unreadable database means "no override": callers fall back to their env default.
"""
import time

TTL_S = 10.0
_CACHE: dict = {}


def clear_cache() -> None:
    _CACHE.clear()


def get_setting(key: str):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL_S:
        return hit[1]
    try:
        from database import SessionLocal, SystemSetting
        db = SessionLocal()
        try:
            row = db.get(SystemSetting, key)
            value = row.value if row is not None else None
        finally:
            db.close()
    except Exception:
        # Keep the last known value through a blip; otherwise "no override".
        return hit[1] if hit else None
    _CACHE[key] = (now, value)
    return value


def set_setting(db, key: str, value, user_id=None) -> None:
    from database import SystemSetting, utcnow
    row = db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key)
        db.add(row)
    row.value = None if value is None else str(value)
    row.updated_at = utcnow()
    row.updated_by_user_id = user_id
    db.commit()
    clear_cache()
