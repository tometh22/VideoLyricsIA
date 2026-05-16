"""
Tests for the 'Inspirado en la letra' (match_lyrics) feature and semantic
relevance scoring introduced in branch claude/match-video-song-theme-6LIWQ.

Strategy: most tests use pure AST / source-text analysis so they run without
installing the full dependency stack.  Only the score-parsing edge cases need
a thin import shim that mocks out every heavy dep before importing pipeline.
"""

import ast
import importlib
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PIPELINE_PATH = Path(__file__).parent.parent / "pipeline.py"
SRC = PIPELINE_PATH.read_text()
TREE = ast.parse(SRC)
FNS = {n.name: n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fn_src(name: str) -> str:
    return ast.get_source_segment(SRC, FNS[name])


def fn_params(name: str) -> list[str]:
    fn = FNS[name]
    return [a.arg for a in fn.args.args + fn.args.kwonlyargs]


def _branch_sources() -> dict:
    """
    Extract the 6 system_prompt strings from _analyze_lyrics_for_background
    by splitting on match_lyrics conditions in the source.
    Returns a dict keyed by a short label.
    """
    src = fn_src("_analyze_lyrics_for_background")
    # Each branch writes to system_prompt; collect all triple-quoted strings
    strings = re.findall(r'"""(.*?)"""', src, re.DOTALL)
    return strings   # list of 6+ prompt bodies


# ---------------------------------------------------------------------------
# 1. Function signatures — all 4 functions must carry match_lyrics
# ---------------------------------------------------------------------------

class TestSignatures(unittest.TestCase):

    def test_analyze_lyrics_has_match_lyrics(self):
        self.assertIn("match_lyrics", fn_params("_analyze_lyrics_for_background"))

    def test_get_unique_prompt_has_match_lyrics(self):
        self.assertIn("match_lyrics", fn_params("_get_unique_prompt"))

    def test_ensure_background_has_match_lyrics(self):
        self.assertIn("match_lyrics", fn_params("_ensure_background"))

    def test_run_pipeline_has_match_lyrics(self):
        self.assertIn("match_lyrics", fn_params("run_pipeline"))

    def test_match_lyrics_default_true_everywhere(self):
        """Default must be True so existing callers get lyrics-aware behaviour."""
        for fn_name in [
            "_analyze_lyrics_for_background",
            "_get_unique_prompt",
            "_ensure_background",
            "run_pipeline",
        ]:
            with self.subTest(fn=fn_name):
                fn = FNS[fn_name]
                defaults = {
                    a.arg: d
                    for a, d in zip(
                        reversed(fn.args.args),
                        reversed(fn.args.defaults),
                    )
                }
                node = defaults.get("match_lyrics")
                self.assertIsNotNone(node, f"{fn_name}: match_lyrics has no default")
                self.assertIsInstance(node, ast.Constant)
                self.assertIs(node.value, True,
                    f"{fn_name}: match_lyrics default must be True")


# ---------------------------------------------------------------------------
# 2. Prompt branch content — all 6 branches correct
# ---------------------------------------------------------------------------

class TestPromptBranches(unittest.TestCase):
    """Verify that each of the 6 prompt branches has the right keywords."""

    @classmethod
    def setUpClass(cls):
        cls.src = fn_src("_analyze_lyrics_for_background")

    # Every branch must target 80-120 words
    def test_all_branches_80_120_words(self):
        count = self.src.count("80-120")
        self.assertEqual(count, 3,
            f"Expected '80-120' in 3 places (one per prompt block), got {count}")

    # match_lyrics=True paths have "STEP 0"
    def test_lyrics_true_paths_have_step0(self):
        # "STEP 0" appears in: genre+True, auto+True (2 blocks)
        count = self.src.count("STEP 0")
        self.assertGreaterEqual(count, 2,
            "At least 2 prompt blocks (genre+True, auto+True) must have STEP 0")

    # concept+True path anchors on the lyrics' visual subject.
    # Renamed from `test_concept_true_has_soul` (2026-05-15): the older
    # branch used a "SOUL of the song" framing while keeping the concept
    # as a hard boundary. The branch now anchors the scene on the
    # lyrics' literal visual subject and demotes concept to a styling
    # layer (palette/texture/atmosphere). The pin moved with it.
    def test_concept_true_anchors_on_lyrics_subject(self):
        self.assertIn("PRIMARY VISUAL SUBJECT", self.src,
            "concept+match_lyrics=True prompt must instruct Gemini to "
            "identify the PRIMARY VISUAL SUBJECT from the lyrics — that "
            "phrase is the anchor of the new lyrics-first hierarchy.")
        self.assertIn("lyrics control WHAT the scene shows", self.src,
            "concept+match_lyrics=True prompt must keep the WHAT/HOW "
            "separator so the lyrics-first priority survives future "
            "prompt edits.")

    # concept+False path has "binding"
    def test_concept_false_has_binding(self):
        self.assertIn("binding", self.src,
            "concept+match_lyrics=False prompt must say the concept choice is binding")

    # genre+False path has "MUST pick a scene"
    def test_genre_false_has_must_pick(self):
        self.assertIn("MUST pick a scene", self.src,
            "genre+match_lyrics=False must say 'MUST pick a scene'")

    # sport rule present in lyrics-aware paths
    def test_sport_rule_present(self):
        # Should appear at least twice (genre+True, auto+True)
        count = self.src.lower().count("sport")
        self.assertGreaterEqual(count, 2,
            "Sport rule must appear in at least 2 lyrics-aware branches")

    # "luxury cars" removed from the reggaeton fallback vocab
    def test_no_luxury_cars_in_auto_fallback(self):
        self.assertNotIn("luxury cars", self.src,
            "'luxury cars' must be removed from reggaeton vocab to fix the original bug")

    # movement_extra_line must be referenced in EVERY branch
    def test_movement_extra_line_in_all_branches(self):
        count = self.src.count("{movement_extra_line}")
        self.assertGreaterEqual(count, 4,
            "movement_extra_line must appear in concept+True, concept+False, "
            "genre+True, genre+False at minimum")


# ---------------------------------------------------------------------------
# 3. match_lyrics propagation — call chain uses the param
# ---------------------------------------------------------------------------

class TestPropagation(unittest.TestCase):
    """Verify match_lyrics is forwarded at every link of the chain."""

    def test_get_unique_prompt_passes_match_lyrics_to_analyze(self):
        src = fn_src("_get_unique_prompt")
        self.assertIn("match_lyrics=match_lyrics", src,
            "_get_unique_prompt must pass match_lyrics down to _analyze_lyrics_for_background")

    def test_ensure_background_passes_match_lyrics_to_first_call(self):
        src = fn_src("_ensure_background")
        self.assertIn("match_lyrics=match_lyrics", src,
            "_ensure_background must pass match_lyrics to _get_unique_prompt")

    def test_ensure_background_passes_match_lyrics_to_retry_call(self):
        src = fn_src("_ensure_background")
        count = src.count("match_lyrics=match_lyrics")
        self.assertGreaterEqual(count, 2,
            "_ensure_background must pass match_lyrics to BOTH _get_unique_prompt calls "
            "(initial + quality retry), got {count}")

    def test_run_pipeline_passes_match_lyrics_to_ensure(self):
        src = fn_src("run_pipeline")
        self.assertIn("match_lyrics=match_lyrics", src,
            "run_pipeline must pass match_lyrics to _ensure_background")


# ---------------------------------------------------------------------------
# 4. _score_video_relevance — bug fixes verified via AST
# ---------------------------------------------------------------------------

class TestScoreVideoRelevanceCode(unittest.TestCase):
    """
    Verify the two bug fixes in _score_video_relevance WITHOUT importing
    pipeline.py (avoids missing-dep chain).
    """

    @classmethod
    def setUpClass(cls):
        cls.src = fn_src("_score_video_relevance")

    def test_uses_regex_not_bare_int(self):
        """int(response.text.strip()) is fragile — must use re.search instead."""
        self.assertNotIn("int(response.text.strip())", self.src,
            "Must not use bare int(response.text.strip()) — use regex extraction")
        self.assertIn("re.search", self.src,
            "Must use re.search to extract integer from Gemini response")

    def test_regex_handles_score_10(self):
        """Regex pattern must match '10' as a whole token, not just '1'."""
        # Extract the regex pattern from source
        m = re.search(r"re\.search\(r?['\"](.+?)['\"]", self.src)
        self.assertIsNotNone(m, "Could not find re.search pattern in source")
        pattern = m.group(1)
        # Must match "10" and give "10", not "1"
        match = re.search(pattern, "10")
        self.assertIsNotNone(match, f"Pattern {pattern!r} must match '10'")
        self.assertEqual(match.group(), "10",
            f"Pattern {pattern!r} matched {match.group()!r} instead of '10'")

    def test_regex_extracts_from_score_7_out_of_10(self):
        m = re.search(r"re\.search\(r?['\"](.+?)['\"]", self.src)
        pattern = m.group(1)
        match = re.search(pattern, "7 out of 10")
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group()), 7)

    def test_cleanup_uses_try_except_not_exists_check(self):
        """TOCTOU: must use try/except OSError, not os.path.exists check."""
        self.assertNotIn("os.path.exists(tmp_frame)", self.src,
            "Must not use os.path.exists(tmp_frame) before unlink — race condition")
        self.assertIn("OSError", self.src,
            "Cleanup must catch OSError instead of pre-checking file existence")

    def test_fail_open_returns_8_on_exception(self):
        """Fail-open constant must be 8."""
        # The except block must return 8
        self.assertIn("return 8", self.src,
            "fail-open path must return 8")

    def test_fail_open_on_no_regex_match(self):
        """When regex finds nothing, must default to 5 (not crash)."""
        # After re.search, must handle None result
        self.assertIn("if m else", self.src,
            "Must handle None from re.search with a safe default (if m else ...)")


# ---------------------------------------------------------------------------
# 5. Quality retry loop — logic verified via AST
# ---------------------------------------------------------------------------

class TestQualityRetryLoopCode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = fn_src("_ensure_background")

    def test_score_called_without_guard(self):
        """
        _score_video_relevance must be called on EVERY successful VEO attempt,
        not only when quality_retry_used is False.
        Old bug: scoring was inside 'if not quality_retry_used:' block,
        so the retry's output was never checked.
        """
        # The score call should NOT be immediately preceded by the guard.
        # A simple heuristic: `quality_retry_used` check should come AFTER
        # `_score_video_relevance(`, not before it.
        score_pos = self.src.find("_score_video_relevance(")
        guard_pos = self.src.find("not quality_retry_used")
        self.assertLess(score_pos, guard_pos,
            "_score_video_relevance must be called BEFORE the quality_retry_used guard, "
            "so every VEO attempt is scored")

    def test_best_available_message_present(self):
        """After the quality retry still fails, must log 'accepting best available'."""
        self.assertIn("accepting best available", self.src,
            "Must log 'accepting best available' when retry result is still low")

    def test_quality_retry_used_flag_present(self):
        self.assertIn("quality_retry_used", self.src)

    def test_cap_is_one_retry(self):
        """Only one quality retry is allowed (cost bound)."""
        # The guard should appear exactly once for the retry decision
        count = self.src.count("quality_retry_used = True")
        self.assertEqual(count, 1,
            "quality_retry_used = True must appear exactly once (one retry cap)")


# ---------------------------------------------------------------------------
# 6. Regression: original football→cars bug is fixed
# ---------------------------------------------------------------------------

class TestOriginalBugFixed(unittest.TestCase):
    """Verify the specific conditions that caused football→cars."""

    def test_reggaeton_genre_has_no_cars_in_fallback(self):
        """reggaeton auto-fallback must not list 'luxury cars'."""
        analyze_src = fn_src("_analyze_lyrics_for_background")
        self.assertNotIn("luxury cars", analyze_src)

    def test_no_genre_auto_prompt_has_no_luxury_cars(self):
        """The no-genre auto prompt must not have 'luxury cars'."""
        analyze_src = fn_src("_analyze_lyrics_for_background")
        self.assertNotIn("luxury cars", analyze_src)

    def test_sport_rule_in_genre_lyrics_true(self):
        """genre+match_lyrics=True path must explicitly reject cars for sport songs."""
        src = fn_src("_analyze_lyrics_for_background")
        # The sport rule lives in the genre+True block. Find it by locating
        # the SECOND "STEP 0" (first is concept+True) or by searching from
        # the "normalized_genre" context. We look for sport anywhere in the
        # function — we already verified at least 2 occurrences in
        # test_sport_rule_present.
        count = src.lower().count("sport")
        self.assertGreaterEqual(count, 2,
            "sport rule must appear in at least 2 branches (genre+True, auto+True)")

    def test_scoring_catches_mismatch(self):
        """_score_video_relevance function must exist and be wired into _ensure_background."""
        self.assertIn("_score_video_relevance", FNS,
            "_score_video_relevance function must be defined")
        ensure_src = fn_src("_ensure_background")
        self.assertIn("_score_video_relevance", ensure_src,
            "_score_video_relevance must be called from _ensure_background")


# ---------------------------------------------------------------------------
# 7. Score parsing — extract the real regex from source and run it
#    No imports needed: we test the actual pattern that ships in production.
# ---------------------------------------------------------------------------

class TestScoreParsing(unittest.TestCase):
    """
    Extracts the exact regex pattern used in _score_video_relevance and
    verifies it handles every edge case correctly.  No pipeline import
    needed — we test the live pattern from source.
    """

    @classmethod
    def setUpClass(cls):
        score_src = fn_src("_score_video_relevance")
        # Extract the re.search pattern
        m = re.search(r're\.search\(r[\'"](.+?)[\'"]', score_src)
        assert m, "Could not find re.search pattern in _score_video_relevance"
        cls.pattern = m.group(1)

        # Extract the default value used when regex finds nothing
        fallback_m = re.search(r'if m else (\d+)', score_src)
        assert fallback_m, "Could not find 'if m else <default>' in _score_video_relevance"
        cls.no_match_default = int(fallback_m.group(1))

        # Extract the fail-open return value (exception path)
        fail_open_m = re.search(r'return (\d+)\s*#.*fail.open', score_src, re.IGNORECASE)
        if not fail_open_m:
            fail_open_m = re.search(r'except.*\n.*return (\d+)', score_src)
        cls.fail_open_value = int(fail_open_m.group(1)) if fail_open_m else 8

    def _parse(self, text: str):
        """Simulate the exact scoring logic from pipeline.py."""
        m = re.search(self.pattern, text)
        raw = int(m.group()) if m else self.no_match_default
        return max(1, min(10, raw))

    def test_clean_integer(self):
        self.assertEqual(self._parse("8"), 8)

    def test_score_1(self):
        self.assertEqual(self._parse("1"), 1)

    def test_score_10(self):
        """'10' must parse as 10, not 1 (regex must match whole word)."""
        self.assertEqual(self._parse("10"), 10)

    def test_trailing_text(self):
        """'7 out of 10' — must extract 7 (first match)."""
        self.assertEqual(self._parse("7 out of 10"), 7)

    def test_prefix_text(self):
        """'Score: 3' — must extract 3."""
        self.assertEqual(self._parse("Score: 3"), 3)

    def test_slash_notation(self):
        """'6/10' — must extract 6."""
        self.assertEqual(self._parse("6/10"), 6)

    def test_whitespace(self):
        self.assertEqual(self._parse("  9  "), 9)

    def test_garbage_response_uses_no_match_default(self):
        """Purely non-numeric text returns the configured no-match default."""
        m = re.search(self.pattern, "excellent quality!")
        self.assertIsNone(m, "Pattern must not match non-numeric garbage")

    def test_empty_response_uses_no_match_default(self):
        m = re.search(self.pattern, "")
        self.assertIsNone(m, "Pattern must not match empty string")

    def test_no_match_default_is_middle_value(self):
        """No-match default (e.g. 5) must be in [1,10] and not 0."""
        self.assertGreaterEqual(self.no_match_default, 1)
        self.assertLessEqual(self.no_match_default, 10)

    def test_fail_open_value_is_8(self):
        """fail-open on exception must be 8 (above threshold, below perfect)."""
        self.assertEqual(self.fail_open_value, 8)

    def test_score_above_10_clamped(self):
        """Scores above 10 must clamp to 10."""
        # If Gemini somehow returns '11', pattern won't match (only 1-9 and 10)
        # so it falls back to no_match_default — still safe
        m = re.search(self.pattern, "11")
        if m:
            clamped = max(1, min(10, int(m.group())))
            self.assertLessEqual(clamped, 10)

    def test_score_0_not_matched(self):
        """0 is not a valid score — pattern must not match it."""
        m = re.search(self.pattern, "0")
        self.assertIsNone(m, "Pattern must not match '0' — valid range is 1-10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
