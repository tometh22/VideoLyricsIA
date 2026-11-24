# Individual and campaign lyric preview parity

The persisted individual lyric review now uses the same NativeLyricPreview as campaign review. The editor renders it into the wizard column through a portal, alongside the existing audio-player portal. The editor remains the owner of the masked live segments, selected line, audio clock and typography settings. Raw App/store snapshots are never used for the native track. The preview waits for durable hydration; API/renderer failures retain the native retry action.

The initial upload/configuration wizard still offers illustrative mood/movement samples before an editor job exists. The native track verifies lyric composition, cuts, fonts and dimensions; it does not generate or certify backgrounds, effects, title cards or a final video. Existing standalone drag/layout tools are unchanged.

## Validation

- Production build and 59 focused frontend checks passed.
- Four Chromium scenarios cover individual live edits, switching editing modes, initial hydration privacy, campaign layout, real WASM rendering and retry. They use synthetic API fixtures and bundled fonts.
- Staging documents and original source objects were read without updating customer rows. Four distinct private recordings (126.47–350.85 seconds) cover missing legacy IDs, ten repeated phrase occurrences, 128 lines and one overlapping interval. Their persisted snapshots were composed by the checked-out backend and replayed locally with their original audio, HTTP byte-range support and browser libass. Four replay scenarios passed audio decoding, mid-song playback, preview readiness, local-only typing/save and exact preservation of line count/start/end.
- Real recordings, lyrics, snapshots, tracks and browser evidence are private workspace artifacts; none are committed. All replay writes terminate in a local mock API, never in staging. No AI, transcription, generation, approval, customer save or infrastructure change was performed.

This is functional validation on representative sources, not a human certification of transcription or acoustic alignment. No new release/version reservation or staging deployment is included in this follow-up; assign the release version when publication is coordinated.
