# Recorded endpoint false positive

`ambiguous_tail.npz` stores numerical features from the 2026-11-04 approved
correction replay: the spectral-only detector proposed 179.7949 s for a line
whose original and approved end were both 179.0600 s. No audio, lyrics,
credentials, or song identity are included in this fixture.

Arrays:

- `times`, `rms`, `spectrum`: a cropped `vocal-continuity-v1` feature track;
  the stem's measured 25.0625 ms offset has already been removed from times.
- `pitch_times`, `f0`, `voiced`, `probability`: local pYIN extraction (16 kHz,
  2048-sample window, 320-sample hop, 65–1000 Hz). `voiced` in this fixture
  already includes the 0.35 confidence floor; the gate also checks it.

The test first reproduces the unwanted extension, then verifies that the
periodicity gate rejects it and leaves the source segment unchanged. The
source calibration example was `ae673000b0f0-r141-40`. The gate itself never
receives that identifier or the approved endpoint.
