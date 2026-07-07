// ─── Novedades (changelog) — fuente única de verdad ──────────────────────────
// Para anunciar una feature nueva: agregá una entrada ACÁ (más nueva primero).
// No hay que tocar componentes. Cada entrada alimenta: la campana (no-leídos),
// el panel de Novedades, y —si `featured: true`— un modal one-time al entrar.
//
// Campos:
//   id           string único y estable (se usa para el tracking de "visto").
//   date         "YYYY-MM-DD".
//   titleKey     clave i18n del título (en src/i18n.jsx, es/en/pt).
//   taglineKey   opcional, EL gancho de una línea — es lo único que el MODAL
//                muestra además del título (ver nota de diseño abajo).
//   bodyKey      clave i18n del cuerpo (fallback del panel si no hay
//                highlightKeys; el modal no lo usa).
//   highlightKeys opcional, array de claves — bullets de detalle, SOLO
//                visibles en el panel (WhatsNewPanel), nunca en el modal.
//   media        opcional, path en public/ (.mp4 = video loop; otro = imagen).
//                Sin media, el modal arma un hero de gradiente + `icon`.
//   icon         opcional, emoji para el hero cuando no hay `media` (default
//                "✨").
//   ctaKey       opcional, clave i18n del botón de acción.
//   ctaTo        opcional, ruta a la que navega el CTA (ej. "/new" = Crear videos).
//   featured     opcional, true → se muestra una vez como modal de anuncio.
//
// Para "tier-ear" (right-size): una feature menor = entrada SIN featured (solo
// aparece en el panel/campana). Una feature grande = featured:true (modal).
//
// DISEÑO DEL MODAL (revisión 07/07, world-class ≈ Linear/Figma/Stripe): el
// modal es el TEASER — visual + título + UNA línea de gancho + un CTA. Nada
// de bullets ni cuerpo largo ahí (eso hacía que se leyera como términos y
// condiciones). El detalle completo (highlightKeys/body) vive en el panel,
// que el usuario abre cuando quiere profundizar — no se pierde nada, solo se
// dosifica dónde aparece.
export const CHANGELOG = [
  {
    id: "motor-v2",
    date: "2026-07-07",
    titleKey: "announce.motor2_title",
    taglineKey: "announce.motor2_tagline",
    bodyKey: "announce.motor2_body",
    highlightKeys: [
      "announce.motor2_hl1",
      "announce.motor2_hl2",
      "announce.motor2_hl3",
      "announce.motor2_hl4",
    ],
    icon: "🎯",
    ctaKey: "announce.motor2_cta",
    ctaTo: "/new",
    featured: true,
  },
  {
    id: "estilo-mayuscula",
    date: "2026-07-06",
    titleKey: "announce.typocase_title",
    taglineKey: "announce.typocase_tagline",
    bodyKey: "announce.typocase_body",
  },
  {
    id: "escenas",
    date: "2026-06-30",
    titleKey: "announce.scenes_title",
    taglineKey: "announce.scenes_tagline",
    bodyKey: "announce.scenes_body",
    highlightKeys: [
      "announce.scenes_gift",
      "announce.scenes_hl1",
      "announce.scenes_hl2",
      "announce.scenes_hl3",
    ],
    media: "/escenas_demo.mp4",
    ctaKey: "announce.scenes_cta",
    ctaTo: "/new",
    // ya no featured: el modal one-time es de a una a la vez (find() toma la
    // primera featured del array); motor-v2 tomó la posta 07/07.
  },
];
