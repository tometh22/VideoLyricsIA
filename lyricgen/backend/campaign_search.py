"""Accent-insensitive, unordered search terms shared by campaign entry points."""
import unicodedata


def normalize_search(value):
    text = "".join(char for char in unicodedata.normalize("NFKD", str(value or ""))
                   if not unicodedata.category(char).startswith("M")).lower()
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def matches_search(query, *values):
    haystack = " ".join(normalize_search(value) for value in values)
    return all(term in haystack for term in normalize_search(query).split())


def matches_song(query, item, job=None):
    return matches_search(query, item.title, item.artist, item.filename, item.technical_code,
                          item.id, job.job_id if job else "",
                          job.song_title if job else "", job.artist if job else "")
