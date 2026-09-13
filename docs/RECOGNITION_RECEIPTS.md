# Stored recognition receipts

`recognition_receipts.make_receipt` packages an already completed Parakeet result
and its existing completion receipt. It performs no recognition and imports no
model or network client. Exact UTF-8 JSON bytes are retained in base64, including
unknown result fields; decoded objects are available through `decode_receipt`.
The package records model/revision, clip/window and parent audio identity.

An internal caller with a verified source binding may supply these packages in
`result['_recognition_receipts']` before `build_machine_evidence`. They persist
under `capture.recognition_receipts` in the existing v3 snapshot and participate
in its evidence hash. Absence leaves the old capture unchanged. Explicit null or
malformed evidence raises `ReceiptValidationError`; an empty list is permitted.
Finalization and validation require exact authoritative audio SHA/revision,
including validation before legacy normalization. Existing immutable editor
snapshot rules continue to apply: this interface is not a late attachment API.

The completion receipt hash must match the exact raw result bytes. Token pieces
must reconstruct raw text and clocks must fit the declared clip. These checks
establish integrity and internal consistency. The caller still owns verifying
the actual clip and parent audio; supplied hashes do not authenticate the
recognizer or establish acoustic correctness. Token clocks are not certified
phonetic word timings. No receipt is automatically used as selected text or
added to recognition attempt counters; this sidecar records already completed
external evidence rather than inventing another invocation.

Storage is bounded to eight supplied packages and four MiB of decoded JSON per
capture. Exact duplicate packages collapse in first-seen order. Conflicting
packages with identical result/receipt identity are rejected. Base64 increases
the serialized size; the four MiB cap describes decoded payload only.

This change does not install or invoke Parakeet, enable a text selector, create
review proposals, or modify human documents. A future producer must be scoped
and authorized separately. Selection/certificate provenance, source freshness,
approval/lock guards and complete-document quality validation remain required
before any candidate can affect editor content.
