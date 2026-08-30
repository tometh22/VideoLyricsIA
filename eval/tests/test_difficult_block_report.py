from eval.difficult_block_report import _corpus_wer


def test_corpus_wer_is_micro_averaged():
    rows = [
        {"word_errors": 1, "reference_words": 10},
        {"word_errors": 9, "reference_words": 90},
    ]
    assert _corpus_wer(rows) == 0.1
