from types import SimpleNamespace
import pytest
from full_audio_review import missing_intervals, usable_response, compare_window
from quality_rollout import enrolled, autoapply, review_required
from delivery_qc_runtime import approval_gate
from delivery_acoustic_qc import require_delivery_review, source_binding


def test_full_coverage_union_does_not_count_planned_or_failed_windows():
    assert missing_intervals(60, [[0,24], [36,60]]) == [[24,36]]
    assert missing_intervals(60, [[0,24],[18,42],[36,60]]) == []


def test_invalid_or_conditioned_responses_never_count_as_coverage():
    response = {'tool_status':'ok','received_audio':True,'conditioning_texts':[], 'response':{'text':'hello'}}
    assert not usable_response(response,'openai',24)
    response['response'] = {'words':[], 'text':''}
    assert usable_response(response,'openai',24)
    response['conditioning_texts'] = ['expected lyric']
    assert not usable_response(response,'openai',24)


def test_missing_lyrics_are_detected_outside_existing_segments():
    issues = compare_window({'start':0,'end':24}, {'response':{'words':[{'word':'Hola','start':3,'end':4}]}}, [], 'openai')
    assert issues[0]['code'] == 'ACOUSTIC_POSSIBLE_OMISSION'
    assert issues[0]['start'] == 3
    assert issues[0]['automatic_apply_allowed'] is False


def test_start_and_end_findings_are_independent():
    issues = compare_window({'start':0,'end':24}, {'response':{'events':[{'text':'Hola','start':2,'end':8}]}}, [{'text':'Hola','start':3,'end':6}], 'google')
    assert {i['code'] for i in issues} == {'ACOUSTIC_START','ACOUSTIC_END'}


def test_explicit_enrollment_and_owner_required(monkeypatch):
    job = SimpleNamespace(job_id='one')
    monkeypatch.setenv('QUALITY_PILOT_JOB_IDS','one')
    monkeypatch.setenv('CHANGE_REQUEST_AUTOAPPLY_ENABLED','1')
    assert enrolled(job)
    assert not autoapply(job)
    monkeypatch.setenv('ENVIRONMENT','staging')
    monkeypatch.setenv('CHANGE_REQUEST_OWNER_ENVIRONMENT','staging')
    assert autoapply(job)
    monkeypatch.setenv('QUALITY_PILOT_JOB_IDS',','.join(str(n) for n in range(31)))
    assert not enrolled(job)


def test_review_required_only_for_concrete_findings():
    row = {'issue_id':'a', 'code':'ACOUSTIC_TEXT_DISCREPANCY', 'status':'OPEN', 'result_status':'REVIEW', 'blocking':True, 'requires_recorded_review':True}
    report = {'status':'COMPLETE', 'acoustic':{'required':True}, 'issues':[row]}
    assert approval_gate(report,'off')['blocked']
    row['status'] = 'ACKNOWLEDGED'
    assert not approval_gate(report,'off')['blocked']
    row.update(status='OPEN', requires_recorded_review=False)
    assert not approval_gate(report,'enforce')['blocked']


def test_publication_revalidates_metadata_and_audio(monkeypatch):
    monkeypatch.setenv('QUALITY_PILOT_JOB_IDS','one')
    monkeypatch.setenv('QUALITY_DELIVERY_REVIEW_REQUIRED','1')
    job = SimpleNamespace(job_id='one', segments_json=[], segments_revision=2, audio_revision=1,
                          input_audio_sha256='a'*64, artist='Artist', song_title='Title',edit_count=0, s3_keys={'video':'video.mp4'})
    monkeypatch.setattr('storage.object_etag', lambda key: 'etag-v1')
    job.delivery_qc = {'status':'COMPLETE', 'issues':[],
        'render_asset': {'key':'video.mp4', 'etag':'etag-v1', 'sha256':'b'*64},
        'render_identity': {'sha256':'b'*64}, 'acoustic':{'required':True, 'render_edit_count':0,
        'binding':source_binding(job,[]),'metadata':{'artist':'Artist','song_title':'Title'}}}
    require_delivery_review(job)
    job.artist = 'Changed'
    with pytest.raises(ValueError, match='fresh_delivery'):
        require_delivery_review(job)
    job.artist = 'Artist'
    monkeypatch.setattr('storage.object_etag', lambda key: 'etag-v2')
    with pytest.raises(ValueError, match='render_asset'):
        require_delivery_review(job)


def test_full_listening_records_failed_coverage_and_falls_back_to_mix(tmp_path, monkeypatch):
    import full_audio_review as review
    from reviewer_shadow_audio import file_sha
    master = tmp_path / 'master.wav'
    master.write_bytes(b'synthetic master')
    monkeypatch.setattr(review, 'probe', lambda _: 60.)
    monkeypatch.setattr(review, 'check_sync', lambda *_: {'mix_stem_sync_verified':False})
    monkeypatch.setattr(review, 'extract_clip', lambda source, window, output: output.write_bytes(str(window).encode()))
    seen = []
    class Listener:
        def __init__(self, *_, policy):
            self.calls = 0
            self.policy = policy
        def listen(self, clip, *, provider, view, source, window):
            self.calls += 1
            seen.append((provider, view, window, source))
            if provider == 'google':
                return {'tool_status':'tool_error'}
            return {'tool_status':'ok', 'received_audio':True, 'conditioning_texts':[], 'response':{'text':'', 'words':[]}}
    snapshot = {'revision':0, 'audio_revision':0, 'audio_sha256':file_sha(master), 'segments':[], 'tenant_id':'tenant'}
    result = review.audit(snapshot, master, master, tmp_path / 'cache', listener_factory=Listener)
    assert len(seen) == result['calls'] == 6  # No retries, including failures.
    assert all(view == 'mix' for _, view, _, _ in seen)
    assert seen[0][2]['start'] == 0 and seen[-1][2]['end'] == 60
    assert result['coverage']['openai']['fraction'] == 1
    assert result['coverage']['google']['missing'] == [[0,60]]
    assert result['issues'][0]['requires_recorded_review']
    assert all('segments' not in source for _, _, _, source in seen)


def test_incompatible_cached_provider_identity_never_counts_as_a_witness(tmp_path):
    from dataclasses import replace
    import json
    from reviewer_shadow_audio import BlindAudioTools
    from reviewer_shadow import ShadowPolicy
    clip = tmp_path / 'clip.wav'
    clip.write_bytes(b'audio')
    class Listener(BlindAudioTools):
        @staticmethod
        def _whisper(_):
            return {'response': {'words':[], 'text':''}}
    listener = Listener(tmp_path / 'requests', policy=replace(ShadowPolicy(), version='full-audio-review-v1'))
    args = dict(provider='openai', view='mix', source={'audio_sha256':'a'}, window={'start':0,'end':24})
    assert listener.listen(clip, **args)['tool_status'] == 'ok'
    cached = next(p for p in (tmp_path / 'requests').glob('*.json') if '.attempt.' not in p.name)
    data = json.loads(cached.read_text())
    data['model'] = 'different-model'
    cached.write_text(json.dumps(data))
    assert listener.listen(clip, **args)['tool_status'] == 'incompatible_cache'
    assert listener.calls == 1


def test_encoded_audio_matches_master_and_wrong_audio_does_not(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import wave
    import numpy as np
    from delivery_acoustic_qc import check_rendered_audio
    from reviewer_shadow_audio import file_sha
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg required for synthetic encoded delivery')
    def make_audio(path, hz):
        samples = (8000 * np.sin(2*np.pi*hz*np.arange(16000)/16000)).astype('<i2')
        with wave.open(str(path), 'wb') as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
            handle.writeframes(samples.tobytes())
    master, wrong, video = tmp_path/'master.wav', tmp_path/'wrong.wav', tmp_path/'video.mp4'
    make_audio(master, 440)
    make_audio(wrong, 997)
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=black:s=160x90:r=10:d=1',
        '-i',str(master),'-c:v','libx264','-c:a','aac','-shortest',str(video)],check=True)
    def download(key, path):
        shutil.copyfile(master if key == 'correct' else wrong, path)
        return True
    monkeypatch.setattr('storage.download_object', download)
    job = SimpleNamespace(input_r2_key='correct', input_audio_sha256=file_sha(master))
    assert check_rendered_audio(str(video), job)['status'] == 'PASS'
    job.input_r2_key, job.input_audio_sha256 = 'wrong', file_sha(wrong)
    assert check_rendered_audio(str(video), job)['status'] == 'REVIEW'


def test_overlap_deduplication_keeps_different_repetitions():
    from full_audio_review import finding, deduplicate_findings
    rows = [finding('ACOUSTIC_POSSIBLE_OMISSION',20.,22.,'Voz',heard_text='Hola',provider='google'),
            finding('ACOUSTIC_POSSIBLE_OMISSION',20.1,22.1,'Voz',heard_text='Hola',provider='google'),
            finding('ACOUSTIC_POSSIBLE_OMISSION',26.,28.,'Voz',heard_text='Hola',provider='google')]
    unique = deduplicate_findings(rows)
    assert len(unique) == 2
    assert len(unique[0]['witnesses']) == 2
    assert unique[1]['start'] == 26.


def test_each_recognition_family_can_flag_text_without_authorizing_a_rewrite():
    issues = compare_window({'start':0,'end':24}, {'response':{'words':[
        {'word':'Otra','start':2,'end':3}, {'word':'frase','start':3,'end':4}]}},
        [{'text':'Una frase','start':2,'end':4}], 'openai')
    assert issues[0]['code'] == 'ACOUSTIC_TEXT_DISCREPANCY'
    assert issues[0]['heard_text'] == 'Otra frase'
    assert issues[0]['requires_recorded_review']
    assert not issues[0]['automatic_apply_allowed']
