// Help Center content map. Title/summary/body live in i18n.jsx under keys
// `help.article.<id>.title|summary|body` so every article supports ES/EN/PT.
// Edit copy in i18n.jsx; edit structure (categories, tags, animations) here.

export const HELP_CONTENT = {
  categories: [
    { id: "getting-started", icon: "rocket",   order: 1 },
    { id: "creating",        icon: "sparkles", order: 2 },
    { id: "lyrics",          icon: "edit",     order: 3 },
    { id: "review-approval", icon: "check",    order: 4 },
    { id: "formats-export",  icon: "download", order: 5 },
    { id: "billing-plan",    icon: "card",     order: 6 },
    { id: "troubleshoot",    icon: "wrench",   order: 7 },
  ],

  articles: [
    // ─── Primeros pasos ───
    { id: "first-video",       category: "getting-started", animation: "upload-flow", featured: true,
      tags: { es: ["primer", "video", "tutorial", "empezar"], en: ["first", "video", "tutorial", "start"], pt: ["primeiro", "video", "tutorial", "comecar"] } },
    { id: "interface-tour",    category: "getting-started", animation: null,
      tags: { es: ["interfaz", "tour", "navegacion"], en: ["interface", "tour", "navigation"], pt: ["interface", "tour", "navegacao"] } },
    { id: "account-plan",      category: "getting-started", animation: null,
      tags: { es: ["cuenta", "plan", "perfil"], en: ["account", "plan", "profile"], pt: ["conta", "plano", "perfil"] } },

    // ─── Creación de videos ───
    { id: "upload-files",      category: "creating", animation: null,
      tags: { es: ["subir", "audio", "mp3", "wav", "batch"], en: ["upload", "audio", "mp3", "wav", "batch"], pt: ["enviar", "audio", "mp3", "wav", "lote"] } },
    { id: "metadata",          category: "creating", animation: null,
      tags: { es: ["artista", "titulo", "idioma", "metadata"], en: ["artist", "title", "language", "metadata"], pt: ["artista", "titulo", "idioma", "metadata"] } },
    { id: "backgrounds",       category: "creating", animation: null,
      tags: { es: ["fondo", "background", "biblioteca", "ia"], en: ["background", "library", "ai", "generated"], pt: ["fundo", "biblioteca", "ia", "gerado"] } },
    { id: "style-settings",    category: "creating", animation: null,
      tags: { es: ["estilo", "genero", "tipografia", "animacion", "transicion"], en: ["style", "genre", "font", "animation", "transition"], pt: ["estilo", "genero", "fonte", "animacao", "transicao"] } },

    // ─── Editor de letras ───
    { id: "editor-overview",   category: "lyrics", animation: null,
      tags: { es: ["editor", "lyrics", "letras", "timeline"], en: ["editor", "lyrics", "timeline"], pt: ["editor", "letras", "timeline"] } },
    { id: "manual-sync",       category: "lyrics", animation: "editor-sync",
      tags: { es: ["sync", "manual", "sincronizar", "tiempo", "espacio"], en: ["sync", "manual", "timing", "space"], pt: ["sync", "manual", "tempo", "espaco"] } },
    { id: "editing-lines",     category: "lyrics", animation: null,
      tags: { es: ["editar", "lineas", "agregar", "borrar", "partir"], en: ["edit", "lines", "add", "delete", "split"], pt: ["editar", "linhas", "adicionar", "deletar"] } },
    { id: "approve-from-editor", category: "lyrics", animation: null,
      tags: { es: ["aprobar", "editor", "render"], en: ["approve", "editor", "render"], pt: ["aprovar", "editor", "render"] } },

    // ─── Revisión y aprobación ───
    { id: "job-states",        category: "review-approval", animation: null,
      tags: { es: ["estado", "queue", "render", "error", "pending"], en: ["state", "queue", "render", "error", "pending"], pt: ["estado", "fila", "render", "erro"] } },
    { id: "approve-reject",    category: "review-approval", animation: "approve-flow", featured: true,
      tags: { es: ["aprobar", "rechazar", "decision", "cuota"], en: ["approve", "reject", "decision", "quota"], pt: ["aprovar", "rejeitar", "decisao", "cota"] } },
    { id: "re-render",         category: "review-approval", animation: null,
      tags: { es: ["re-render", "regenerar", "cambios"], en: ["re-render", "regenerate", "changes"], pt: ["re-render", "regenerar", "mudancas"] } },

    // ─── Formatos y export ───
    { id: "which-format",      category: "formats-export", animation: null, featured: true,
      tags: { es: ["formato", "mp4", "prores", "short", "thumbnail"], en: ["format", "mp4", "prores", "short", "thumbnail"], pt: ["formato", "mp4", "prores", "short", "thumbnail"] } },
    { id: "prores-master",     category: "formats-export", animation: null,
      tags: { es: ["prores", "broadcast", "master", "422", "hq"], en: ["prores", "broadcast", "master", "422", "hq"], pt: ["prores", "broadcast", "master", "422", "hq"] } },
    { id: "google-drive",      category: "formats-export", animation: null,
      tags: { es: ["drive", "google", "transferir", "integracion"], en: ["drive", "google", "transfer", "integration"], pt: ["drive", "google", "transferir", "integracao"] } },
    { id: "download-all",      category: "formats-export", animation: "download-flow",
      tags: { es: ["descargar", "todo", "bundle", "zip"], en: ["download", "all", "bundle", "zip"], pt: ["baixar", "tudo", "pacote", "zip"] } },

    // ─── Plan y facturación ───
    { id: "usage-quota",       category: "billing-plan", animation: null,
      tags: { es: ["uso", "quota", "cuota", "plan", "mensual"], en: ["usage", "quota", "plan", "monthly"], pt: ["uso", "cota", "plano", "mensal"] } },
    { id: "billing-invoices",  category: "billing-plan", animation: null,
      tags: { es: ["facturacion", "factura", "stripe", "pago"], en: ["billing", "invoice", "stripe", "payment"], pt: ["faturamento", "fatura", "stripe", "pagamento"] } },

    // ─── Problemas comunes ───
    { id: "render-error",      category: "troubleshoot", animation: null,
      tags: { es: ["error", "render", "fallo", "retry"], en: ["error", "render", "fail", "retry"], pt: ["erro", "render", "falha"] } },
    { id: "editor-stuck",      category: "troubleshoot", animation: null,
      tags: { es: ["editor", "trabado", "no carga", "lyrics"], en: ["editor", "stuck", "not loading"], pt: ["editor", "travado", "nao carrega"] } },
  ],
};

// Featured articles shown on drawer home. Cap to a sensible count even
// if the article list grows.
export const FEATURED_LIMIT = 3;

// HTML tags allowed in the body — anything else is stripped before render.
// The body strings come from our own i18n file, but we still sanitize so
// a typo (or future translator handoff) can't introduce <script> tags.
export const ALLOWED_HTML_TAGS = new Set([
  "p", "br", "strong", "em", "a", "kbd", "code",
  "ol", "ul", "li",
  "table", "thead", "tbody", "tr", "th", "td",
  "dl", "dt", "dd",
  "h4", "span", "mark",
]);
export const ALLOWED_HTML_ATTRS = new Set([
  "href", "class", "data-article",
]);

// Resolve a category by id (linear scan — list is tiny).
export function findCategory(id) {
  return HELP_CONTENT.categories.find(c => c.id === id) || null;
}
export function findArticle(id) {
  return HELP_CONTENT.articles.find(a => a.id === id) || null;
}
export function articlesIn(catId) {
  return HELP_CONTENT.articles.filter(a => a.category === catId);
}
export function featuredArticles() {
  return HELP_CONTENT.articles.filter(a => a.featured).slice(0, FEATURED_LIMIT);
}

// Resolve the per-language tag list for an article. Falls back to ES.
export function tagsFor(article, lang) {
  if (!article || !article.tags) return [];
  return article.tags[lang] || article.tags.es || [];
}
