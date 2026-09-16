// Request keys are generated once per user action and reused only for the
// retries belonging to that action.  They let the API replay an accepted
// mutation when the response was lost without turning a network retry into a
// second render.
export function requestIdempotencyKey(prefix = "request") {
  let suffix;
  try {
    suffix = globalThis.crypto?.randomUUID?.();
  } catch { /* older browsers / restricted crypto */ }
  suffix ||= `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${suffix}`;
}
