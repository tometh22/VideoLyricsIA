import { useCallback, useEffect, useRef } from "react";

export default function useLatestReviewRequest() {
  const active = useRef(null);
  const cancel = useCallback(() => {
    active.current?.abort();
    active.current = null;
  }, []);
  useEffect(() => cancel, [cancel]);
  const start = useCallback(() => {
    cancel();
    const controller = new AbortController();
    active.current = controller;
    return {
      signal: controller.signal,
      current: () => active.current === controller && !controller.signal.aborted,
      finish: () => { if (active.current === controller) active.current = null; },
    };
  }, [cancel]);
  return { start, cancel, active };
}
