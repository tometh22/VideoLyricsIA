import hashlib
import json
import math
import unittest

from recognition_receipts import (
    ReceiptValidationError,
    decode_receipt,
    make_receipt,
    validate_receipts,
)




class RecognitionReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = {"family":"nvidia_parakeet", "model_id":"nvidia/parakeet-tdt-0.6b-v3",
            "revision":"541d1f99c6b0c3cd0b11a95167540bb8edefd82b", "conditioning_texts":[],
            "window":[0.,12.7], "clip_sha256":"c"*64, "text":["héllo!"],
            "raw_token_spans_relative":[[{"token":"hé", "start":0., "end":.2},
                {"token":"llo", "start":.2, "end":.4}, {"token":"!", "start":.4, "end":.4}]],
            "elapsed_seconds":5.014647249998234, "opaque_extra":{"preserve":["all", "fields"]}}
        cls.result_raw = json.dumps(cls.result,ensure_ascii=False,indent=2).encode()
        cls.receipt_raw = json.dumps({"completed":True,"result_sha256":hashlib.sha256(cls.result_raw).hexdigest()}).encode()
        cls.parent_sha = "a"*64

    def packet(self):
        return make_receipt(
            self.result_raw,
            self.receipt_raw,
            parent_audio_sha256=self.parent_sha,
            audio_revision=0,
            clip_sha256=self.result["clip_sha256"],
        )

    def test_stored_bytes_round_trip_without_mutating_inputs(self):
        before = (self.result_raw, self.receipt_raw)
        packet = self.packet()
        self.assertEqual(decode_receipt(packet), self.result)
        self.assertEqual(before, (self.result_raw, self.receipt_raw))
        self.assertEqual(packet["result_sha256"], hashlib.sha256(self.result_raw).hexdigest())

    def test_build_validation_and_authoritative_binding(self):
        packet = self.packet()
        self.assertEqual(validate_receipts([packet]), [packet])
        self.assertEqual(
            validate_receipts([packet], audio_sha256=self.parent_sha, audio_revision=0),
            [packet],
        )
        with self.assertRaisesRegex(ReceiptValidationError, "audio_binding_mismatch"):
            validate_receipts([packet], audio_sha256="0" * 64, audio_revision=0)
        with self.assertRaisesRegex(ReceiptValidationError, "audio_binding_mismatch"):
            validate_receipts([packet], audio_sha256=self.parent_sha, audio_revision=1)
        with self.assertRaisesRegex(ReceiptValidationError, "authoritative_audio_sha256_invalid"):
            validate_receipts([packet], audio_sha256=None, audio_revision=None)

    def test_explicit_empty_is_valid_and_exact_duplicate_is_collapsed(self):
        self.assertEqual(validate_receipts([]), [])
        packet = self.packet()
        self.assertEqual(validate_receipts([packet, packet]), [packet])

    def test_duplicate_json_keys_nonfinite_and_hash_tampering_fail(self):
        duplicate = b'{"family":"nvidia_parakeet","family":"nvidia_parakeet"}'
        with self.assertRaisesRegex(ReceiptValidationError, "duplicate_json_key"):
            make_receipt(duplicate, self.receipt_raw, parent_audio_sha256=self.parent_sha,
                         audio_revision=0, clip_sha256=self.result["clip_sha256"])
        nonfinite = self.result_raw.replace(b'5.014647249998234', b'NaN')
        with self.assertRaisesRegex(ReceiptValidationError, "nonfinite_json_number"):
            make_receipt(nonfinite, self.receipt_raw, parent_audio_sha256=self.parent_sha,
                         audio_revision=0, clip_sha256=self.result["clip_sha256"])
        overflow = self.result_raw.replace(b'5.014647249998234', b'1e309')
        with self.assertRaisesRegex(ReceiptValidationError, "nonfinite_json_number"):
            make_receipt(overflow, self.receipt_raw, parent_audio_sha256=self.parent_sha,
                         audio_revision=0, clip_sha256=self.result["clip_sha256"])
        packet = self.packet()
        packet["result_sha256"] = "0" * 64
        with self.assertRaises(ReceiptValidationError):
            validate_receipts([packet])

    def test_wrong_raw_input_type_is_a_typed_error(self):
        with self.assertRaisesRegex(ReceiptValidationError, "result_bytes_required"):
            make_receipt(self.result_raw.decode(), self.receipt_raw,
                         parent_audio_sha256=self.parent_sha, audio_revision=0,
                         clip_sha256=self.result["clip_sha256"])

    def test_packet_window_is_stable_across_jsonb_negative_zero_normalization(self):
        result = dict(self.result)
        result.pop("raw_token_spans_absolute", None)
        result["window"] = [-0.0, 12.7]
        raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
        receipt = json.dumps({
            "result_sha256": hashlib.sha256(raw).hexdigest(),
            "completed": True,
        }).encode("utf-8")
        packet = make_receipt(
            raw, receipt, parent_audio_sha256=self.parent_sha, audio_revision=0,
            clip_sha256=result["clip_sha256"],
        )
        self.assertEqual(packet["window"], [0.0, 12.7])
        self.assertEqual(math.copysign(1.0, packet["window"][0]), 1.0)
        self.assertEqual(validate_receipts([packet]), [packet])

    def test_completed_receipt_must_hash_exact_raw_result(self):
        receipt = json.loads(self.receipt_raw)
        receipt["result_sha256"] = "0" * 64
        tampered = json.dumps(receipt).encode()
        with self.assertRaisesRegex(ReceiptValidationError, "receipt_result_sha256_mismatch"):
            make_receipt(self.result_raw, tampered, parent_audio_sha256=self.parent_sha,
                         audio_revision=0, clip_sha256=self.result["clip_sha256"])


if __name__ == "__main__":
    unittest.main()
