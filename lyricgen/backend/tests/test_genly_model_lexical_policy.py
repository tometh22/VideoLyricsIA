import unittest
from types import SimpleNamespace

from genly_model_lexical_policy import choose_lexical, fetch_reference


class LexicalPolicyTests(unittest.TestCase):
    def test_single_view_does_not_rewrite(self):
        result = choose_lexical("Mi amor es así.", "Mi favor es así", {"mix": "Mi favor es así"})
        self.assertEqual(result["text"], "Mi amor es así.")
        self.assertEqual(len(result["withheld_edits"]), 1)

    def test_two_views_support_local_edit_without_erasing_style(self):
        result = choose_lexical("¡Mi color es azul!", "Mi dolor es azul",
                                {"mix": "Mi dolor es azul", "vocals": "Mi dolor es azul"})
        self.assertEqual(result["text"], "¡Mi dolor es azul!")
        self.assertFalse(result["automatic_apply_allowed"])
        self.assertEqual(result["accepted_edits"][0]["independent_families"], 1)

    def test_disagreement_elsewhere_does_not_discard_supported_word(self):
        result = choose_lexical("Mi color es azul", "Mi dolor es azul",
                                {"mix": "Mi dolor es azul", "vocals": "Mi dolor es gris"})
        self.assertEqual(result["text"], "Mi dolor es azul")

    def test_reference_can_veto_correlated_wrong_views(self):
        result = choose_lexical("Es tan fácil", "Está fácil",
                                {"mix": "Está fácil", "vocals": "Está fácil"},
                                {"status": "matched_metadata", "text": "Es tan fácil\nOtra línea"})
        self.assertEqual(result["text"], "Es tan fácil")

    def test_adjacent_disagreement_preserves_shared_substitution(self):
        result = choose_lexical("Repara el corazón", "Repare el corazón",
                                {"mix": "Repare el corazón", "vocals": "Repare mi corazón"})
        self.assertEqual(result["text"], "Repare el corazón")

    def test_line_boundary_insertions_may_belong_to_neighbor(self):
        result = choose_lexical("Me quedé", "Me quedé con",
                                {"mix": "Me quedé con", "vocals": "Me quedé con"})
        self.assertEqual(result["text"], "Me quedé")

    def test_reference_never_invents_unheard_words(self):
        result = choose_lexical("mi casa", "mi casa", {"mix": "mi casa"},
                                {"status": "matched_metadata", "text": "mi casa es tu casa"})
        self.assertEqual(result["text"], "mi casa")

    def test_rejects_wrong_artist_or_recording_duration(self):
        record = {"artistName": "Someone Else", "trackName": "Song", "duration": 200, "plainLyrics": "text"}
        get = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: record)
        self.assertEqual(fetch_reference("Artist", "Song", 200, get=get)["reason"], "reference_identity_mismatch")
        record["artistName"] = "Artist"
        self.assertEqual(fetch_reference("Artist", "Song", 250, get=get)["reason"], "reference_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
