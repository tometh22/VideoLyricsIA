import json

import pytest

from eval.gemini_heavy_candidates import parse_response


def test_gemini_candidate_schema_is_bounded_to_clip():
    rows = parse_response(json.dumps({"segments": [{"start": 1, "end": 2, "text": "hola"}]}), 4)
    assert rows == [{"start": 1.0, "end": 2.0, "text": "hola"}]
    with pytest.raises(ValueError):
        parse_response(json.dumps({"segments": [{"start": 1, "end": 9, "text": "inventado"}]}), 4)
