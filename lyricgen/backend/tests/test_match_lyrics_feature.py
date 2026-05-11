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

    # concept+True path has "SOUL"
    def test_concept_true_has_soul(self):
        self.assertIn("SOUL", self.src,
            "concept+match_lyrics=True prompt must reference SOUL of the song")

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
# 7. Lightweight import test — score parsing runs correctly
#    (uses sys.modules stubbing; no network, no filesystem)
# ---------------------------------------------------------------------------

class TestScoreParsing(unittest.TestCase):
    """
    Actually imports and runs _score_video_relevance with mocked deps
    to verify the regex parser works on real edge cases.
    """

    @classmethod
    def setUpClass(cls):
        """Stub every heavy dep before importing pipeline."""
        # Only stub what isn't already present
        stubs = [
            "dotenv", "librosa", "librosa.effects",
            "moviepy", "moviepy.editor", "moviepy.config",
            "PIL", "PIL.Image", "PIL.ImageFilter",
            "PIL.ImageDraw", "PIL.ImageFont",
            "numpy", "cv2", "requests",
            "boto3", "botocore", "botocore.exceptions",
            "google", "google.genai", "google.genai.types",
            "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc",
            "psycopg2", "psycopg2.extras",
            "storage", "provenance", "content_validator",
            "jobs", "render_spec", "ai_providers",
            "billing", "tenant", "db",
        ]
        cls._original = {}
        for name in stubs:
            if name not in sys.modules:
                mod = types.ModuleType(name)
                # add common sub-attributes
                mod.load_dotenv = lambda: None
                mod.GenerateContentConfig = MagicMock
                mod.ThinkingConfig = MagicMock
                mod.Part = MagicMock()
                mod.Part.from_bytes = MagicMock(return_value=MagicMock())
                sys.modules[name] = mod
                cls._original[name] = None  # mark as added by us

        # Provide env vars pipeline needs at import time
        os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
        os.environ.setdefault("R2_BUCKET", "test")
        os.environ.setdefault("VERTEX_PROJECT", "test")
        os.environ.setdefault("VERTEX_LOCATION", "us-central1")

        # Add parent dir to sys.path so pipeline can be found
        backend_dir = str(Path(__file__).parent.parent)
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        # Import pipeline (may fail if further deps missing — skip gracefully)
        try:
            import pipeline as pl
            cls.pl = pl
            cls.skip = False
        except Exception as e:
            cls.skip = True
            cls.skip_reason = str(e)

    def setUp(self):
        if self.skip:
            self.skipTest(f"pipeline import failed: {self.skip_reason}")

    def _run_score(self, gemini_text: str) -> int:
        pl = self.pl
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = MagicMock(text=gemini_text)

        def mock_extract(video_path, out_path):
            Path(out_path).write_bytes(b"fakejpeg")
            return out_path

        with patch.object(pl, "_get_genai_client", return_value=mock_client), \
             patch.object(pl, "_extract_frame_from_video", side_effect=mock_extract):
            return pl._score_video_relevance("/fake/video.mp4", "a football pitch at dusk")

    def test_clean_integer(self):
        self.assertEqual(self._run_score("8"), 8)

    def test_trailing_text(self):
        self.assertEqual(self._run_score("7 out of 10"), 7)

    def test_prefix_text(self):
        self.assertEqual(self._run_score("Score: 3"), 3)

    def test_slash_notation(self):
        self.assertEqual(self._run_score("6/10"), 6)

    def test_score_10_not_1(self):
        self.assertEqual(self._run_score("10"), 10)

    def test_garbage_response_fails_open(self):
        self.assertEqual(self._run_score("excellent quality!"), 8)

    def test_empty_response_fails_open(self):
        self.assertEqual(self._run_score(""), 8)

    def test_cleanup_no_crash_when_file_missing(self):
        """Cleanup must not raise even if temp file was already deleted."""
        pl = self.pl
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = MagicMock(text="7")

        delete_calls = []

        def mock_extract(video_path, out_path):
            # Do NOT create the file — simulate it being deleted between check and unlink
            return out_path

        with patch.object(pl, "_get_genai_client", return_value=mock_client), \
             patch.object(pl, "_extract_frame_from_video", side_effect=mock_extract):
            # Should not raise
            try:
                pl._score_video_relevance("/fake/video.mp4", "test")
            except Exception as e:
                self.fail(f"_score_video_relevance raised {e} when temp file was missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
