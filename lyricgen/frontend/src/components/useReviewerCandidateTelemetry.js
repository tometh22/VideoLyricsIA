import { useEffect, useRef } from "react";

// Extends existing ProductEvent analytics. No transcript text is transmitted.
export default function useReviewerCandidateTelemetry(proposal, emit) {
  const root = useRef(null);
  const callback = useRef(emit);
  callback.current = emit;
  useEffect(() => {
    if (!proposal?.reviewer_assist || !root.current || typeof IntersectionObserver === "undefined") return;
    const visible = new Set(), shown = new Set(), examined = new Set();
    let active = null, lastAction = 0, seconds = 0, lastTick = performance.now();
    const report = (kind, id, extra = {}) => callback.current?.({
      kind, proposal_id: id, candidate_id: String(proposal.id),
      event_id: crypto.randomUUID(), ...extra,
    });
    const flush = () => { if (active && seconds > 0) report("active_seconds", active, { seconds }); seconds = 0; };
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        const id = entry.target.dataset.reviewerReceipt;
        if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
          visible.add(entry.target);
          if (!shown.has(id)) { shown.add(id); report("shown", id); }
        } else visible.delete(entry.target);
      });
    }, { threshold: [0, 0.5] });
    const element = root.current;
    element.querySelectorAll("[data-reviewer-receipt]").forEach(node => observer.observe(node));
    const interact = event => {
      const id = event.target.closest("[data-reviewer-receipt]")?.dataset.reviewerReceipt;
      if (!id) return;
      if (active !== id) { flush(); active = id; }
      lastAction = performance.now();
      if (!examined.has(id)) { examined.add(id); report("examined", id); }
    };
    element.addEventListener("pointerdown", interact);
    element.addEventListener("keydown", interact);
    const timer = setInterval(() => {
      const now = performance.now();
      if (active && [...visible].some(node => node.dataset.reviewerReceipt === active) && !document.hidden && document.hasFocus() && now - lastAction < 30000) {
        seconds += Math.min(1, (now - lastTick) / 1000);
      }
      lastTick = now;
      if (seconds >= 15) flush();
    }, 1000);
    return () => { flush(); clearInterval(timer); observer.disconnect();
      element.removeEventListener("pointerdown", interact); element.removeEventListener("keydown", interact); };
  }, [proposal?.id, Boolean(proposal?.reviewer_assist)]);
  return root;
}
