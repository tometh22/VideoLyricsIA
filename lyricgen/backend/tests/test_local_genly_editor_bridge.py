"""Exercise the real editor persistence/apply path in a disposable local DB."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EditorBridgeTests(unittest.TestCase):
    def test_publish_manual_apply_version_and_layout(self):
        backend = Path(__file__).resolve().parents[1]
        code = '''
from datetime import datetime, timezone
from database import Base, engine, SessionLocal, User, Job, EditorVersion
from editor import ensure_document, apply_quality_proposal
from local_genly_review import normalize_snapshot, publish_review
from reviewer_shadow import source_binding
Base.metadata.create_all(engine)
with SessionLocal() as db:
    user=User(username='local-test',hashed_password='unused',tenant_id='test')
    db.add(user); db.flush()
    segments=[dict(start=1.,end=3.,text='mi casa',pos=[.2,.8],scale=1.2,rot=7,
                   content_asr_score=.99)]
    job=Job(job_id='localtest001',user_id=user.id,tenant_id='test',artist='Test',
            song_title='Test',filename='test.wav',style='oscuro',status='transcribed_pending',
            delivery_profile='youtube',segments_json=segments,segments_revision=0,
            audio_revision=1,input_audio_sha256='a'*64)
    db.add(job); db.flush()
    document=ensure_document(db,job.job_id,'test',segments)
    db.commit()
    song=normalize_snapshot(dict(job_id=job.job_id,revision=0,audio_revision=1,
                                 audio_sha256='a'*64,segments=document.current_segments))
    report=dict(source=source_binding(song),observations=[dict(source=source_binding(song),
                line_index=0,window=dict(start=1.,end=3.),clip_sha256='b'*64,
                reference_conditioned=False,model_id='local-test',status='recognized',text='mi taza')])
    assert publish_review(db,song,report)['published']
    assert document.quality_proposal['local_genly']['source']==source_binding(song)
    assert document.current_segments[0]['text']=='mi casa'
    assert not publish_review(db,song,report)['published']
    proposal=document.quality_proposal
    document,version,applied=apply_quality_proposal(db,job,document,user.id,
        proposal_id=proposal['id'],base_revision=0,
        window_ids=[proposal['windows'][0]['id']],idempotency_key='local-click-1')
    db.commit()
    assert applied and version is not None and document.revision==1
    row=document.current_segments[0]
    assert row['text']=='mi taza' and row['pos']==[.2,.8] and row['scale']==1.2 and row['rot']==7
    assert 'content_asr_score' not in row
    assert document.quality_proposal['decision_history'][-1]['decision']=='accepted'
    assert db.query(EditorVersion).filter_by(job_id=job.job_id,revision=1).one().segments[0]['text']=='mi taza'
    from unittest.mock import patch
    from quality_jobs import _persist_if_current
    from transcription_quality import segments_hash
    db.commit()
    with patch('local_genly_review.quality_ranking',return_value={'available':True,'model_id':'test-risk'}):
        assert _persist_if_current(job.job_id,1,segments_hash(document.current_segments),
                                   {'decision':'review','unsafe_windows':[]},
                                   expected_audio_revision=1,expected_audio_sha256='a'*64)
    db.refresh(job)
    assert job.transcription_quality['local_review_risk']['model_id']=='test-risk'
    assert job.segments_json[0]['text']=='mi taza'
    job.approved_at=datetime.now(timezone.utc); db.commit()
    assert publish_review(db,song,report)['reason']=='job_missing_or_approved'
print('local_editor_bridge_passed')
'''
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "PYTHONPATH": str(backend), "ENVIRONMENT": "test",
                   "DATABASE_URL": "sqlite:///" + str(Path(directory) / "editor.db"),
                   "QUALITY_OPERATOR_SUGGESTIONS_ENABLED": "1",
                   "QUALITY_LEARNING_CAPTURE_ENABLED": "0"}
            result = subprocess.run([sys.executable, "-c", code], cwd=directory,
                                    env=env, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local_editor_bridge_passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
