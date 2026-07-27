import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../../i18n";
import {
  HELP_CONTENT,
  articlesIn,
  featuredArticles,
  findArticle,
  findCategory,
  tagsFor,
  ALLOWED_HTML_TAGS,
  ALLOWED_HTML_ATTRS,
} from "../../help/content";
import HelpAnimation from "./HelpAnimations";
import { startReplaySession } from "../OnboardingTour";

// Inline SVG icons for category cards. Keep them small (1.25rem-ish at
// render time) and stroke-based so they inherit currentColor.
const ICONS = {
  rocket:   <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09zM12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/></svg>,
  sparkles: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3v3M12 18v3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M3 12h3M18 12h3M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>,
  edit:     <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>,
  check:    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>,
  download: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>,
  card:     <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="5" width="20" height="14" rx="3"/><line x1="2" y1="10" x2="22" y2="10"/></svg>,
  wrench:   <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>,
};

// Sanitize the article body HTML by allowlist. Strings come from our own
// i18n file but we still strip surprises (script/iframe/event handlers).
// Built on the browser's DOMParser so we get a proper tree walk.
function sanitizeHtml(html) {
  if (!html) return "";
  if (typeof window === "undefined" || typeof DOMParser === "undefined") return "";
  const doc = new DOMParser().parseFromString(`<div id="r">${html}</div>`, "text/html");
  const root = doc.getElementById("r");
  if (!root) return "";
  walk(root);
  return root.innerHTML;

  function walk(node) {
    const kids = Array.from(node.childNodes);
    for (const c of kids) {
      if (c.nodeType === 1) {
        const tag = c.tagName.toLowerCase();
        if (!ALLOWED_HTML_TAGS.has(tag)) {
          // Replace disallowed element with its text content.
          c.replaceWith(...Array.from(c.childNodes));
          continue;
        }
        // Strip non-allowlisted attrs.
        for (const attr of Array.from(c.attributes)) {
          if (!ALLOWED_HTML_ATTRS.has(attr.name.toLowerCase())) {
            c.removeAttribute(attr.name);
          }
        }
        // Force safe href schemes.
        if (c.hasAttribute("href")) {
          const href = c.getAttribute("href") || "";
          if (!/^(https?:|mailto:|#help\/)/i.test(href)) {
            c.removeAttribute("href");
          } else if (/^https?:/i.test(href)) {
            // External links open in new tab.
            c.setAttribute("target", "_blank");
            c.setAttribute("rel", "noopener noreferrer");
          }
        }
        walk(c);
      } else if (c.nodeType === 8) {
        // Strip comments.
        c.remove();
      }
    }
  }
}

// Score-based search. Returns articles ordered most-relevant first.
function searchArticles(q, t, lang) {
  const ql = q.trim().toLowerCase();
  if (!ql) return [];
  const hits = [];
  for (const a of HELP_CONTENT.articles) {
    const title = (t(`help.article.${a.id}.title`) || "").toLowerCase();
    const summary = (t(`help.article.${a.id}.summary`) || "").toLowerCase();
    const body = (t(`help.article.${a.id}.body`) || "").toLowerCase();
    const tagList = tagsFor(a, lang).map(s => String(s).toLowerCase());
    let score = 0;
    if (title.includes(ql)) score += 10;
    if (tagList.some(tag => tag.includes(ql))) score += 5;
    if (summary.includes(ql)) score += 3;
    if (body.includes(ql)) score += 1;
    if (score > 0) hits.push({ a, score });
  }
  hits.sort((a, b) => b.score - a.score);
  return hits;
}

function highlight(text, q) {
  if (!q) return text;
  const re = new RegExp("(" + q.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&") + ")", "ig");
  const parts = String(text).split(re);
  return parts.map((p, i) =>
    re.test(p) ? <mark key={i} className="hc-mark">{p}</mark> : <span key={i}>{p}</span>
  );
}

// Article body with cross-link interception. Any <a href="#help/<id>">
// is rewired to navigate within the drawer instead of leaving the page.
function ArticleBody({ html, onNavigateArticle, onRunTour }) {
  const ref = useRef(null);
  const clean = useMemo(() => sanitizeHtml(html), [html]);

  useEffect(() => {
    if (!ref.current) return;
    const links = ref.current.querySelectorAll('a[href^="#help/"]');
    const handlers = [];
    links.forEach((a) => {
      const href = a.getAttribute("href") || "";
      const target = href.slice(6); // strip "#help/"
      const h = (e) => {
        e.preventDefault();
        if (target === "run-tour") { onRunTour?.(); return; }
        onNavigateArticle?.(target);
      };
      a.addEventListener("click", h);
      handlers.push([a, h]);
    });
    return () => handlers.forEach(([a, h]) => a.removeEventListener("click", h));
  }, [clean, onNavigateArticle, onRunTour]);

  return (
    <div
      ref={ref}
      className="hc-article-body"
      dangerouslySetInnerHTML={{ __html: clean }}
    />
  );
}

export default function HelpDrawer({ open, initialArticleId, onClose }) {
  const { t, lang } = useI18n();
  const [stack, setStack] = useState([{ view: "home" }]);
  const [query, setQuery] = useState("");
  const [feedback, setFeedback] = useState({});
  const bodyRef = useRef(null);
  const searchRef = useRef(null);

  // Reset internal state when the drawer reopens; jump straight to the
  // initial article if provided (HelpTip → "Ver más").
  useEffect(() => {
    if (open) {
      setStack(initialArticleId
        ? [{ view: "article", id: initialArticleId }]
        : [{ view: "home" }]);
      setQuery("");
      // Focus the search after the slide-in finishes so the keyboard caret
      // doesn't snap mid-animation.
      const t = setTimeout(() => searchRef.current?.focus(), 260);
      return () => clearTimeout(t);
    }
  }, [open, initialArticleId]);

  // Hydrate persisted feedback once on mount.
  useEffect(() => {
    try {
      const acc = {};
      for (const a of HELP_CONTENT.articles) {
        const v = localStorage.getItem("genly_help_fb_" + a.id);
        if (v === "yes" || v === "no") acc[a.id] = v;
      }
      setFeedback(acc);
    } catch {}
  }, []);

  const current = stack[stack.length - 1] || { view: "home" };

  const navigate = useCallback((node) => {
    setStack((s) => [...s, node]);
    setTimeout(() => { if (bodyRef.current) bodyRef.current.scrollTop = 0; }, 0);
  }, []);
  const goBack = useCallback(() => {
    setStack((s) => (s.length <= 1 ? s : s.slice(0, -1)));
  }, []);

  const submitFeedback = useCallback((id, v) => {
    setFeedback((f) => ({ ...f, [id]: v }));
    try { localStorage.setItem("genly_help_fb_" + id, v); } catch {}
  }, []);

  // ─── Render helpers ───
  const isSearching = query.trim().length >= 2;
  const searchHits = useMemo(
    () => (isSearching ? searchArticles(query, t, lang) : []),
    [isSearching, query, t, lang]
  );

  const drawerTitle = (() => {
    if (isSearching) return t("help.section.results") || "Resultados";
    if (current.view === "home") return t("help.drawer.title") || "Ayuda";
    if (current.view === "category") {
      const c = findCategory(current.id);
      return c ? (t(`help.category.${c.id}.title`) || c.id) : t("help.drawer.title");
    }
    if (current.view === "article") {
      const a = findArticle(current.id);
      return a ? (t(`help.article.${a.id}.title`) || a.id) : t("help.drawer.title");
    }
    return t("help.drawer.title") || "Ayuda";
  })();

  const showBack = isSearching ? false : (stack.length > 1);

  // ─── Views ───
  const HomeView = () => {
    const cats = [...HELP_CONTENT.categories].sort((a, b) => (a.order || 0) - (b.order || 0));
    const featured = featuredArticles();
    return (
      <>
        <p className="hc-section-title">{t("help.section.categories") || "Categorías"}</p>
        <div className="hc-cat-grid">
          {cats.map((c) => {
            const count = articlesIn(c.id).length;
            return (
              <button
                key={c.id}
                type="button"
                className="hc-cat-card"
                onClick={() => navigate({ view: "category", id: c.id })}
              >
                <span className="hc-cat-icon">{ICONS[c.icon] || ICONS.rocket}</span>
                <span className="hc-cat-name">{t(`help.category.${c.id}.title`) || c.id}</span>
                <span className="hc-cat-count">
                  {count} {count === 1
                    ? (t("help.count.one") || "artículo")
                    : (t("help.count.many") || "artículos")}
                </span>
              </button>
            );
          })}
        </div>

        {featured.length > 0 && (
          <>
            <p className="hc-section-title">{t("help.section.featured") || "Artículos destacados"}</p>
            <div className="hc-list">
              {featured.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="hc-art-item"
                  onClick={() => navigate({ view: "article", id: a.id })}
                >
                  <span className="hc-art-item-title">{t(`help.article.${a.id}.title`) || a.id}</span>
                  <span className="hc-art-item-summary">{t(`help.article.${a.id}.summary`) || ""}</span>
                </button>
              ))}
            </div>
          </>
        )}

        <p className="hc-section-title">{t("help.section.more") || "Más"}</p>
        <div className="hc-list">
          <button
            type="button"
            className="hc-art-item"
            onClick={() => {
              // Replay flow: clear flags + set session marker, then make sure
              // we land on /dashboard (the only route where DashboardTour
              // mounts). If we're already there, a reload is enough; otherwise
              // navigate via full-page redirect so React Router re-mounts the
              // routes and the tour wakes up.
              startReplaySession();
              onClose();
              if (typeof window !== "undefined") {
                if (window.location.pathname === "/dashboard") {
                  window.location.reload();
                } else {
                  window.location.href = "/dashboard";
                }
              }
            }}
          >
            <span className="hc-art-item-title">▶ {t("help.action.replay_tour") || "Ver tour interactivo"}</span>
            <span className="hc-art-item-summary">{t("help.action.replay_tour_sub") || "Reiniciar los tours guiados desde el principio."}</span>
          </button>
        </div>
      </>
    );
  };

  const CategoryView = () => {
    const c = findCategory(current.id);
    if (!c) return <EmptyView />;
    const arts = articlesIn(c.id);
    if (arts.length === 0) return <EmptyView />;
    return (
      <>
        <p className="hc-section-title">{t(`help.category.${c.id}.title`) || c.id}</p>
        <div className="hc-list">
          {arts.map((a) => (
            <button
              key={a.id}
              type="button"
              className="hc-art-item"
              onClick={() => navigate({ view: "article", id: a.id })}
            >
              <span className="hc-art-item-title">{t(`help.article.${a.id}.title`) || a.id}</span>
              <span className="hc-art-item-summary">{t(`help.article.${a.id}.summary`) || ""}</span>
            </button>
          ))}
        </div>
      </>
    );
  };

  const ArticleView = () => {
    const a = findArticle(current.id);
    if (!a) return <EmptyView />;
    const cat = findCategory(a.category);
    const title = t(`help.article.${a.id}.title`) || a.id;
    const summary = t(`help.article.${a.id}.summary`) || "";
    const body = t(`help.article.${a.id}.body`) || "";
    const fb = feedback[a.id];
    return (
      <>
        <div className="hc-art-head">
          {cat && <p className="hc-art-eyebrow">{t(`help.category.${cat.id}.title`) || cat.id}</p>}
          <h1 className="hc-art-title">{title}</h1>
          {summary && <p className="hc-art-summary">{summary}</p>}
        </div>
        {a.animation && <HelpAnimation name={a.animation} />}
        <ArticleBody
          html={body}
          onNavigateArticle={(id) => navigate({ view: "article", id })}
          onRunTour={() => {
            startReplaySession();
            onClose();
            if (typeof window !== "undefined") {
              if (window.location.pathname === "/dashboard") {
                window.location.reload();
              } else {
                window.location.href = "/dashboard";
              }
            }
          }}
        />
        <div className="hc-feedback">
          {fb
            ? <span className="hc-feedback-thanks">
                {fb === "yes"
                  ? (t("help.feedback.thanks_yes") || "¡Gracias!")
                  : (t("help.feedback.thanks_no") || "Gracias por avisar — vamos a mejorarlo.")}
              </span>
            : (
              <>
                <p className="hc-feedback-q">{t("help.feedback.q") || "¿Te sirvió este artículo?"}</p>
                <button type="button" className="hc-feedback-btn" onClick={() => submitFeedback(a.id, "yes")}>
                  👍 {t("help.feedback.yes") || "Sí"}
                </button>
                <button type="button" className="hc-feedback-btn" onClick={() => submitFeedback(a.id, "no")}>
                  👎 {t("help.feedback.no") || "No"}
                </button>
              </>
            )
          }
        </div>
      </>
    );
  };

  const SearchView = () => {
    if (searchHits.length === 0) {
      return (
        <div className="hc-empty">
          <p className="hc-empty-title">
            {(t("help.empty.no_results.title") || "Sin resultados").replace("{q}", query)}
          </p>
          <p>{t("help.empty.no_results.body") || "Probá otra palabra o explorá las categorías."}</p>
        </div>
      );
    }
    return (
      <>
        <p className="hc-section-title">
          {searchHits.length} {searchHits.length === 1
            ? (t("help.section.result_one") || "resultado")
            : (t("help.section.results") || "resultados")}
        </p>
        <div className="hc-list">
          {searchHits.map(({ a }) => (
            <button
              key={a.id}
              type="button"
              className="hc-art-item"
              onClick={() => { setQuery(""); navigate({ view: "article", id: a.id }); }}
            >
              <span className="hc-art-item-title">
                {highlight(t(`help.article.${a.id}.title`) || a.id, query)}
              </span>
              <span className="hc-art-item-summary">
                {highlight(t(`help.article.${a.id}.summary`) || "", query)}
              </span>
            </button>
          ))}
        </div>
      </>
    );
  };

  const EmptyView = () => (
    <div className="hc-empty">
      <p className="hc-empty-title">{t("help.empty.title") || "Sin contenido"}</p>
      <p>{t("help.empty.body") || ""}</p>
    </div>
  );

  return (
    <>
      <div
        className={"hc-backdrop" + (open ? " hc-backdrop-open" : "")}
        onClick={onClose}
        aria-hidden={!open}
      />
      <aside
        className={"hc-drawer" + (open ? " hc-drawer-open" : "")}
        role="dialog"
        aria-modal="true"
        aria-labelledby="hc-drawer-title"
        aria-hidden={!open}
      >
        <header className="hc-drawer-head">
          {showBack ? (
            <button
              type="button"
              className="hc-iconbtn"
              aria-label={t("help.action.back") || "Volver"}
              onClick={goBack}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
            </button>
          ) : <span style={{ width: "2rem" }} />}
          <h2 id="hc-drawer-title" className="hc-drawer-title">{drawerTitle}</h2>
          <button
            type="button"
            className="hc-iconbtn"
            aria-label={t("help.action.close") || "Cerrar"}
            onClick={onClose}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </header>

        <div className="hc-drawer-search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7" /><path d="M21 21l-4.35-4.35" />
          </svg>
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") setQuery(""); }}
            placeholder={t("help.drawer.search_placeholder") || "Buscar en la ayuda..."}
            aria-label={t("help.drawer.search_placeholder") || "Buscar en la ayuda"}
          />
        </div>

        <div ref={bodyRef} className="hc-drawer-body" tabIndex={-1}>
          {isSearching ? <SearchView /> :
            current.view === "home" ? <HomeView /> :
            current.view === "category" ? <CategoryView /> :
            current.view === "article" ? <ArticleView /> :
            <EmptyView />}
        </div>

        <footer className="hc-drawer-foot">
          {t("help.drawer.contact_cta") || "¿No encontraste lo que buscabas?"}{" "}
          <a href={"mailto:" + (t("help.drawer.contact_email") || "tomas@epical.digital")}>
            {t("help.drawer.contact_email") || "tomas@epical.digital"}
          </a>
        </footer>
      </aside>
    </>
  );
}
