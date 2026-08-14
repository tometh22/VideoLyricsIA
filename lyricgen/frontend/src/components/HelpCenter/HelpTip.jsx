import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../../i18n";
import { useHelp } from "./HelpProvider";
import { findArticle } from "../../help/content";

// Inline "?" with a popover. Use as:
//   <HelpTip articleId="which-format" />            (text comes from i18n)
//   <HelpTip text="..." articleId="which-format" /> (explicit override)
//
// The popover is portal-mounted to <body> so it never gets clipped by
// `overflow:hidden` parents (UploadZone wraps sections in scroll containers).
export default function HelpTip({ articleId, text, className = "" }) {
  const { t } = useI18n();
  const { openHelp } = useHelp();
  const triggerRef = useRef(null);
  const popoverRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState({ top: 0, left: 0, placement: "top" });
  const id = useId();

  // Resolve copy: explicit `text` wins, else i18n key by articleId, else generic.
  const body =
    text ||
    (articleId ? t("help.tip." + articleId) : "") ||
    t("help.tip.generic") ||
    "";

  // Position above the trigger; flip below if not enough room.
  useLayoutEffect(() => {
    if (!open || !triggerRef.current || !popoverRef.current) return;
    const tRect = triggerRef.current.getBoundingClientRect();
    const pRect = popoverRef.current.getBoundingClientRect();
    const margin = 8;
    let top = tRect.top - pRect.height - margin;
    let placement = "top";
    if (top < margin) {
      top = tRect.bottom + margin;
      placement = "bottom";
    }
    let left = tRect.left + tRect.width / 2 - pRect.width / 2;
    left = Math.max(margin, Math.min(window.innerWidth - pRect.width - margin, left));
    setCoords({ top, left, placement });
  }, [open]);

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    function onDown(e) {
      if (
        triggerRef.current?.contains(e.target) ||
        popoverRef.current?.contains(e.target)
      ) return;
      setOpen(false);
    }
    function onKey(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const article = articleId ? findArticle(articleId) : null;
  const seeMoreLabel = t("help.tip.see_more") || "Ver más";

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={"hc-tip-trigger " + className}
        aria-label={t("help.tip.label") || "Mostrar ayuda"}
        aria-describedby={open ? id : undefined}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={(e) => {
          // Let users move into the popover without it disappearing.
          const next = e.relatedTarget;
          if (next instanceof Node && popoverRef.current?.contains(next)) return;
          setOpen(false);
        }}
        onFocus={() => setOpen(true)}
        onBlur={(e) => {
          const next = e.relatedTarget;
          if (next instanceof Node && popoverRef.current?.contains(next)) return;
          setOpen(false);
        }}
        onClick={(e) => {
          // Click toggles — useful on touch where hover is meaningless.
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ?
      </button>
      {open && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={popoverRef}
            id={id}
            role="tooltip"
            data-placement={coords.placement}
            className="hc-tip-popover"
            style={{ top: coords.top, left: coords.left }}
            onMouseLeave={() => setOpen(false)}
          >
            <div className="hc-tip-text">{body}</div>
            {article && (
              <button
                type="button"
                className="hc-tip-more"
                onClick={() => {
                  setOpen(false);
                  openHelp(articleId);
                }}
              >
                {seeMoreLabel} →
              </button>
            )}
          </div>,
          document.body
        )}
    </>
  );
}
