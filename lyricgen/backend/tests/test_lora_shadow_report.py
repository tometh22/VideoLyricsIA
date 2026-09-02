from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "report_lora_shadow.py"
    spec = importlib.util.spec_from_file_location("report_lora_shadow", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_shadow_report_aggregates_only_observed_songs(monkeypatch):
    module = _module()

    class Row:
        def __init__(self, job_id, observed):
            self.job_id = job_id
            self.created_at = None
            self.transcription_quality = (
                {"retry": {"lora_shadow": observed}} if observed else {}
            )

    observed = {
        "comparisons": 2,
        "with_consensus": 2,
        "without_consensus": 1,
        "lora_contributed_lines": 1,
        "new_consensus_lines": 1,
        "lost_consensus_lines": 0,
    }

    class Query:
        def order_by(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def yield_per(self, _size):
            return iter([Row("observed", observed), Row("old", None)])

    class DB:
        def query(self, _model):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr(module, "SessionLocal", lambda: DB())
    report = module.collect(50)

    assert report["songs_observed"] == 1
    assert report["totals"]["new_consensus_lines"] == 1
    assert report["totals"]["new_consensus_rate"] == 0.5
    assert report["replacement_allowed"] is False
