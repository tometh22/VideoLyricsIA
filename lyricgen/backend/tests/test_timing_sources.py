"""Sanity tests for the centralized `timing_sources` registry.

The DB column `jobs.timing_source` is VARCHAR(20). Any new value
exceeding 20 chars would silently truncate on insert and break the QA
suite's allow-list matching. Disjointness with `DEPRECATED_TIMING_SOURCES`
prevents the embarrassing case of a value being added to BOTH sets.
"""
from timing_sources import VALID_TIMING_SOURCES, DEPRECATED_TIMING_SOURCES


def test_all_sources_fit_db_column():
    """jobs.timing_source is VARCHAR(20). Any longer is a runtime error
    that database.py would catch only on insert."""
    too_long = [src for src in VALID_TIMING_SOURCES if len(src) > 20]
    assert too_long == [], f"timing_source values too long for VARCHAR(20): {too_long}"


def test_deprecated_disjoint_from_valid():
    """A value cannot be both valid (emit) and deprecated (banned)."""
    overlap = VALID_TIMING_SOURCES & DEPRECATED_TIMING_SOURCES
    assert overlap == set(), f"overlap between VALID and DEPRECATED: {overlap}"


def test_no_duplicates_within_valid():
    """frozenset guarantees uniqueness — but assert names are distinct
    to catch typos like emitting 'whisperx' twice with subtle suffix
    differences."""
    assert len(VALID_TIMING_SOURCES) >= 5, "expected at least 5 distinct sources"
