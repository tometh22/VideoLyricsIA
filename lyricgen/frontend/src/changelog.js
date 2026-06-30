// ─── Novedades (changelog) — fuente única de verdad ──────────────────────────
// Para anunciar una feature nueva: agregá una entrada ACÁ (más nueva primero).
// No hay que tocar componentes. Cada entrada alimenta: la campana (no-leídos),
// el panel de Novedades, y —si `featured: true`— un modal one-time al entrar.
//
// Campos:
//   id        string único y estable (se usa para el tracking de "visto").
//   date      "YYYY-MM-DD".
//   titleKey  clave i18n del título (en src/i18n.jsx, es/en/pt).
//   bodyKey   clave i18n del cuerpo.
//   media     opcional, path en public/ (.mp4 = video loop; otro = imagen).
//   ctaKey    opcional, clave i18n del botón de acción.
//   ctaTo     opcional, ruta a la que navega el CTA (ej. "/new" = Crear videos).
//   featured  opcional, true → se muestra una vez como modal de anuncio.
//
// Para "tier-ear" (right-size): una feature menor = entrada SIN featured (solo
// aparece en el panel/campana). Una feature grande = featured:true (modal).
export const CHANGELOG = [
  {
    id: "escenas",
    date: "2026-06-30",
    titleKey: "announce.scenes_title",
    taglineKey: "announce.scenes_tagline",
    bodyKey: "announce.scenes_body",
    highlightKeys: [
      "announce.scenes_hl1",
      "announce.scenes_hl2",
      "announce.scenes_hl3",
    ],
    media: "/escenas_demo.mp4",
    ctaKey: "announce.scenes_cta",
    ctaTo: "/new",
    featured: true,
  },
];
