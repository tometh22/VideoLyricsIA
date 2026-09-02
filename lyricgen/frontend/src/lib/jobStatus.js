// Single source of truth for job status classification on the frontend.
//
// Why this module exists (audit 2026-07-27): the sets of "terminal" and
// "active" statuses used to live hardcoded and DUPLICATED across App.jsx
// (`pollJob` TERMINAL, the history root poller `ACTIVE`) and BatchProgress
// (`allDone`, the single-song hero gate). They drifted: `pollJob` never
// listed `bg_preview_done`, so when the backend closed the SSE on that
// status (main.py TERMINAL set) the poll never resolved; and the
// single-song hero treated "not done/pending_review" as "still generating",
// so ANY terminal-but-not-successful status (error, validation_failed,
// rejected, or a future status) froze the "Construyendo tu video" screen
// forever. Centralizing the sets makes the view a TOTAL function over
// status and keeps the frontend in lockstep with the backend.
//
// TERMINAL_STATUSES mirrors the backend SSE terminal set exactly
// (lyricgen/backend/main.py — search: "ghost jobs that never advance").
// Keep them in sync: adding a status on the backend without adding it here
// reintroduces the infinite-poll / frozen-hero class of bug.

export const TERMINAL_STATUSES = Object.freeze([
  "done",
  "pending_review",
  "error",
  "rejected",
  "validation_failed",
  "transcription_failed",
  "bg_preview_done",
  "bg_preview_failed",
]);

// In-flight states the worker moves through. Mirrors the history root
// poller's original ACTIVE set (App.jsx).
export const ACTIVE_STATUSES = Object.freeze([
  "processing",
  "queued",
  "editing",
  "transcribing",
  "transcribing_queued",
  "background_generating",
  "rendering",
]);

// Terminal AND successful → open the editor / show the "listo" state.
const SUCCESS_STATUSES = Object.freeze(["done", "pending_review"]);

// Terminal but NOT successful → the single-song view must render a dead-end
// error card with an escape hatch, never the generating spinner.
const ERROR_STATUSES = Object.freeze([
  "error",
  "validation_failed",
  "rejected",
  "transcription_failed",
  "bg_preview_failed",
]);

const TERMINAL_SET = new Set(TERMINAL_STATUSES);
const ACTIVE_SET = new Set(ACTIVE_STATUSES);
const SUCCESS_SET = new Set(SUCCESS_STATUSES);
const ERROR_SET = new Set(ERROR_STATUSES);

export const isTerminalStatus = (status) => TERMINAL_SET.has(status);
export const isActiveStatus = (status) => ACTIVE_SET.has(status);
export const isSuccessStatus = (status) => SUCCESS_SET.has(status);
export const isErrorStatus = (status) => ERROR_SET.has(status);
