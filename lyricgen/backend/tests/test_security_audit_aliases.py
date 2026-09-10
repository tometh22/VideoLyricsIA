"""Advisory aliases must retain the exact existing review and expiry."""
import json
from types import SimpleNamespace

from scripts import security_audit


def run_audit(monkeypatch, tmp_path, *, package='transformers', aliases=None, expires='2099-01-01', vulns=True):
    baseline = tmp_path / 'exceptions.json'
    baseline.write_text(json.dumps({'exceptions': [{'package': 'transformers', 'id': 'CVE-2026-9856', 'expires': expires}]}))
    monkeypatch.setattr(security_audit, 'BASELINE', baseline)
    report = {'dependencies': [{'name': package, 'version': '4.57.6', 'vulns': [
        {'id': 'PYSEC-2026-3929', 'aliases': aliases or []}
    ] if vulns else []}]}
    def fake_run(command, **kwargs):
        with open(command[command.index('--output') + 1], 'w') as output:
            json.dump(report, output)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(security_audit.subprocess, 'run', fake_run)
    return security_audit.main()


def test_new_identifier_matches_same_reviewed_cve(monkeypatch, tmp_path):
    assert run_audit(monkeypatch, tmp_path, aliases=['CVE-2026-9856']) == 0


def test_unreviewed_advisory_remains_blocked(monkeypatch, tmp_path):
    assert run_audit(monkeypatch, tmp_path, aliases=['CVE-2099-9999']) == 1


def test_alias_cannot_borrow_another_packages_review(monkeypatch, tmp_path):
    assert run_audit(monkeypatch, tmp_path, package='different-package', aliases=['CVE-2026-9856']) == 1


def test_alias_does_not_extend_expired_review(monkeypatch, tmp_path):
    assert run_audit(monkeypatch, tmp_path, aliases=['CVE-2026-9856'], expires='2000-01-01') == 2


def test_fixed_advisory_still_requires_removing_stale_review(monkeypatch, tmp_path):
    assert run_audit(monkeypatch, tmp_path, vulns=False) == 1
