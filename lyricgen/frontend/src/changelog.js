// ─── Novedades (changelog) — fuente única de verdad ──────────────────────────
// Para anunciar una feature nueva: agregá una entrada ACÁ (más nueva primero).
// No hay que tocar componentes. Cada entrada alimenta: la campana (no-leídos),
// el panel de Novedades, y —si `featured: true`— un modal one-time al entrar.
//
// Campos:
//   id           string único y estable (se usa para el tracking de "visto").
//   date         "YYYY-MM-DD".
//   titleKey     clave i18n del título (en src/i18n.jsx, es/en/pt).
//   taglineKey   opcional, EL gancho de una línea del modal y el panel.
//   bodyKey      clave i18n del cuerpo (fallback del panel si no hay
//                highlightKeys; el modal no lo usa).
//   highlightKeys opcional, array de claves — bullets de detalle, SOLO
//                visibles en el panel (WhatsNewPanel), nunca en el modal.
//   modalFeatures opcional, array de { titleKey, bodyKey } — beneficios
//                explicados dentro del modal cuando un lanzamiento necesita
//                más contexto que el teaser corto.
//   visual       opcional: "control" | "transcription" | "textcase" |
//                "cinema" | "media". Los primeros cuatro renderizan demos
//                nativas sin assets.
//   media        opcional, path en public/ (.mp4 = video loop; otro = imagen).
//                Se usa cuando visual="media" o como fallback.
//   ctaKey       opcional, clave i18n del botón de acción.
//   ctaTo        opcional, ruta a la que navega el CTA (ej. "/new" = Crear videos).
//   featured     opcional, true → se muestra una vez como modal de anuncio.
//
// Para "tier-ear" (right-size): una feature menor = entrada SIN featured (solo
// aparece en el panel/campana). Una feature grande = featured:true (modal).
//
// DISEÑO DEL MODAL: por defecto es un teaser — visual + título + una línea de
// gancho + CTA. Un lanzamiento compuesto puede sumar `modalFeatures` para
// explicar 2-3 beneficios concretos sin convertir el anuncio en una ficha
// técnica. El historial largo (highlightKeys/body) sigue viviendo en el panel.
export const CHANGELOG = [
  {
    id: "editor-studio-agosto",
    date: "2026-08-12",
    titleKey: "announce.editor2_title",
    taglineKey: "announce.editor2_tagline",
    bodyKey: "announce.editor2_body",
    visual: "control",
    highlightKeys: [
      "announce.editor2_hl1",
      "announce.editor2_hl2",
      "announce.editor2_hl3",
    ],
    modalFeatures: [
      { titleKey: "announce.editor2_f1_title", bodyKey: "announce.editor2_f1_body" },
      { titleKey: "announce.editor2_f2_title", bodyKey: "announce.editor2_f2_body" },
      { titleKey: "announce.editor2_f3_title", bodyKey: "announce.editor2_f3_body" },
    ],
    ctaKey: "announce.editor2_cta",
    ctaTo: "/new",
    featured: true,
  },
  {
    id: "mas-control-julio-v2",
    date: "2026-07-22",
    titleKey: "announce.control_title",
    taglineKey: "announce.control_tagline",
    bodyKey: "announce.control_body",
    visual: "control",
    highlightKeys: [
      "announce.control_hl1",
      "announce.control_hl2",
      "announce.control_hl3",
    ],
    modalFeatures: [
      { titleKey: "announce.control_f1_title", bodyKey: "announce.control_f1_body" },
      { titleKey: "announce.control_f2_title", bodyKey: "announce.control_f2_body" },
      { titleKey: "announce.control_f3_title", bodyKey: "announce.control_f3_body" },
    ],
    ctaKey: "announce.control_cta",
    ctaTo: "/new",
    // ya no featured: el editor-studio-agosto tomó la posta 12/08.
  },
  {
    id: "motor-v2",
    date: "2026-07-07",
    titleKey: "announce.motor2_title",
    taglineKey: "announce.motor2_tagline",
    bodyKey: "announce.motor2_body",
    visual: "transcription",
    highlightKeys: [
      "announce.motor2_hl1",
      "announce.motor2_hl2",
      "announce.motor2_hl3",
    ],
    ctaKey: "announce.motor2_cta",
    ctaTo: "/new",
  },
  {
    id: "estilo-abc",
    date: "2026-07-06",
    titleKey: "announce.typocase_title",
    taglineKey: "announce.typocase_tagline",
    bodyKey: "announce.typocase_body",
    visual: "textcase",
    ctaKey: "announce.typocase_cta",
    ctaTo: "/new",
  },
  {
    id: "formato-cine",
    date: "2026-07-07",
    titleKey: "announce.cinema_title",
    taglineKey: "announce.cinema_tagline",
    bodyKey: "announce.cinema_body",
    visual: "cinema",
    ctaKey: "announce.cinema_cta",
    ctaTo: "/new",
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
    visual: "media",
    media: "/escenas_demo.mp4",
    ctaKey: "announce.scenes_cta",
    ctaTo: "/new",
    // ya no featured: el modal one-time es de a una a la vez (find() toma la
    // primera featured del array); motor-v2 tomó la posta 07/07.
  },
];
