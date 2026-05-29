import { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import HelpDrawer from "./HelpDrawer";
import "./HelpCenter.css";

// Context shape: { isOpen, openHelp(articleId?), closeHelp() }.
// Components anywhere in the tree call useHelp() to pop the drawer.
const HelpContext = createContext({
  isOpen: false,
  openHelp: () => {},
  closeHelp: () => {},
});

export function useHelp() {
  return useContext(HelpContext);
}

// Provider mounts the drawer into a portal at the end of <body> so the
// slide-in doesn't get clipped by ancestors with `overflow:hidden` (the
// editor sets that on its main panel) and z-index stacks predictably.
export function HelpProvider({ children }) {
  const [state, setState] = useState({ open: false, initialArticleId: null });
  const prevFocusRef = useRef(null);

  const openHelp = useCallback((articleId) => {
    // Remember the focused element so we can restore on close (a11y).
    prevFocusRef.current = (typeof document !== "undefined") ? document.activeElement : null;
    setState({ open: true, initialArticleId: typeof articleId === "string" ? articleId : null });
  }, []);

  const closeHelp = useCallback(() => {
    setState({ open: false, initialArticleId: null });
    // Defer focus restore so the drawer fade-out doesn't fight the focus.
    setTimeout(() => {
      const el = prevFocusRef.current;
      if (el && typeof el.focus === "function") {
        try { el.focus(); } catch {}
      }
    }, 50);
  }, []);

  // Global keyboard handlers:
  //  - "?" toggles the drawer when no input/textarea has focus.
  //  - "Escape" closes the drawer when it's open.
  useEffect(() => {
    function onKey(e) {
      if (e.key === "Escape" && state.open) {
        e.preventDefault();
        closeHelp();
        return;
      }
      if (e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const tag = (document.activeElement && document.activeElement.tagName) || "";
        const isEditable = document.activeElement?.isContentEditable;
        if (tag === "INPUT" || tag === "TEXTAREA" || isEditable) return;
        e.preventDefault();
        if (state.open) closeHelp(); else openHelp();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state.open, openHelp, closeHelp]);

  // Lock body scroll while the drawer is open. Restored on close.
  useEffect(() => {
    if (typeof document === "undefined") return;
    if (state.open) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = prev; };
    }
  }, [state.open]);

  const value = { isOpen: state.open, openHelp, closeHelp };
  const portalTarget = (typeof document !== "undefined") ? document.body : null;

  return (
    <HelpContext.Provider value={value}>
      {children}
      {portalTarget && createPortal(
        <HelpDrawer
          open={state.open}
          initialArticleId={state.initialArticleId}
          onClose={closeHelp}
        />,
        portalTarget
      )}
    </HelpContext.Provider>
  );
}
