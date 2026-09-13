"""Pure capture compatibility; no database, provider, or selector execution."""
import copy
import json
import unittest

import machine_evidence as candidate
from recognition_receipts import make_receipt,decode_receipt

ORIGINAL=[{'start':0.,'end':5.,'text':'unchanged selected text'}]


def packet():
    raw={'family':'nvidia_parakeet','model_id':'nvidia/parakeet-tdt-0.6b-v3',
        'revision':'541d1f99c6b0c3cd0b11a95167540bb8edefd82b','conditioning_texts':[],
        'text':['hello'],'raw_token_spans_relative':[[{'token':'hello','start':.1,'end':.3}]],
        'window':[0.,5.],'clip_sha256':'c'*64}
    encoded=json.dumps(raw).encode()
    import hashlib
    receipt=json.dumps({'completed':True,'result_sha256':hashlib.sha256(encoded).hexdigest()}).encode()
    return make_receipt(encoded,receipt,parent_audio_sha256='a'*64,audio_revision=1,clip_sha256='c'*64)


def finalize(evidence,**kw):
    return candidate.finalize_machine_evidence(evidence,original_segments=ORIGINAL,
        quality={},audio_sha256=kw.get('audio_sha256','a'*64),audio_revision=kw.get('audio_revision',1))


class CaptureTests(unittest.TestCase):
    def test_absent_and_empty_transport_do_not_create_recognition_attempt(self):
        result={'segments':copy.deepcopy(ORIGINAL)}
        absent=candidate.build_machine_evidence(result)
        empty=candidate.build_machine_evidence(dict(result,_recognition_receipts=[]))
        self.assertNotIn('recognition_receipts',absent['capture'])
        self.assertEqual(empty['capture'].pop('recognition_receipts'),[])
        absent.pop('captured_at');empty.pop('captured_at')
        self.assertEqual(absent,empty)

    def test_roundtrip_preserves_original_and_raw_result(self):
        p=packet();result={'segments':copy.deepcopy(ORIGINAL),'_recognition_receipts':[p]};before=copy.deepcopy(result)
        captured=candidate.build_machine_evidence(result)
        base=finalize(candidate.build_machine_evidence({'segments':ORIGINAL}))
        finalized=finalize(captured);durable=json.loads(json.dumps(finalized))
        candidate.validate_machine_evidence(durable,ORIGINAL)
        self.assertEqual(durable['capture']['recognition_receipts'],[p])
        self.assertEqual(decode_receipt(durable['capture']['recognition_receipts'][0]),decode_receipt(p))
        for key in ('hypotheses_by_family','pre_human','decisions'):
            self.assertEqual(finalized[key],base[key])
        self.assertEqual(finalized['capture']['recognition_attempt_count'],base['capture']['recognition_attempt_count'])
        self.assertEqual(result,before)

    def test_explicit_null_transport_is_rejected(self):
        with self.assertRaises(ValueError):candidate.build_machine_evidence({'segments':ORIGINAL,'_recognition_receipts':None})

    def test_missing_or_different_authoritative_audio_is_rejected(self):
        captured=candidate.build_machine_evidence({'segments':ORIGINAL,'_recognition_receipts':[packet()]})
        for kw in ({'audio_sha256':None},{'audio_sha256':'b'*64},{'audio_revision':2},
                   {'audio_revision':True},{'audio_revision':'1'},{'audio_sha256':'a'*64+'suffix'}):
            with self.assertRaises(ValueError):finalize(captured,**kw)

    def test_rehashed_snapshot_cannot_rebind_receipt_to_other_audio(self):
        e=finalize(candidate.build_machine_evidence({'segments':ORIGINAL,'_recognition_receipts':[packet()]}))
        e['pre_human']['audio_revision']=2
        e['evidence_sha256']=candidate.snapshot_hash({key:e[key] for key in ('hypotheses_by_family','capture','pre_human','decisions')})
        with self.assertRaisesRegex(candidate.MachineSnapshotMissing,'machine_recognition_receipt_invalid'):
            candidate.validate_machine_evidence(e,ORIGINAL)

    def test_ordinary_capture_still_validates_without_optional_field(self):
        e=finalize(candidate.build_machine_evidence({'segments':ORIGINAL}))
        candidate.validate_machine_evidence(e,ORIGINAL)
        self.assertNotIn('recognition_receipts',e['capture'])


if __name__=='__main__':unittest.main()
