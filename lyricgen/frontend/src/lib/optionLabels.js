// Etiquetas legibles de las opciones de Movimiento y Efecto.
//
// Estaban duplicadas en TRES lugares: el cuerpo de render de UploadZone
// (MOVEMENT_META / EFFECTS), los mapas moveLabel/effectLabel de
// WizardLivePreview, y ahora las necesitaba la ficha del video. Tres copias de
// los mismos strings es cómo un mismo código termina llamándose distinto en dos
// pantallas — el problema que este trabajo entero viene arreglando.
//
// Los CÓDIGOS siguen en lib/catalogCodes.js (contrato de paridad con el
// backend, asertado por lib/renderParity.test.js). Acá sólo la capa de texto.
//
// Son funciones de `t` en vez de constantes porque i18n es un hook: el valor
// depende del idioma activo.

/** code → etiqueta de Movimiento. "" (Auto) incluido. */
export const MOVEMENT_LABELS = (t) => ({
  "": t("upload.movement_auto") || "Auto",
  estatico: t("upload.movement_estatico") || "Estático (cámara fija)",
  sutil: t("upload.movement_sutil") || "Sutil (mínimo movimiento)",
  estandar: t("upload.movement_estandar") || "Cinematográfico",
  "foto-parallax": t("upload.movement_foto_parallax") || "Foto fija",
  animado: t("upload.movement_animado") || "Animado (ilustración)",
});

/** code → etiqueta de Efecto. "" (Ninguno) incluido. */
export const EFFECT_LABELS = (t) => ({
  "": t("upload.effect_none") || "Ninguno",
  snow: t("upload.effect_snow") || "Nieve",
  rain: t("upload.effect_rain") || "Lluvia",
  stars: t("upload.effect_stars") || "Estrellas",
  light: t("upload.effect_light") || "Luces",
  bokeh: t("upload.effect_bokeh") || "Bokeh",
  // aurora está comentado en el picker (deshabilitado), pero jobs viejos lo
  // tienen persistido en render_params: la ficha tiene que poder nombrarlo en
  // vez de mostrar el código crudo.
  aurora: t("upload.effect_aurora") || "Aurora",
});

/**
 * code → nombre de la tipografía.
 *
 * No van por i18n (salvo "Auto"): son nombres de marca y no se traducen.
 * `FONT_BY_CODE` (components/fontCatalog) tiene el CSS y el weight pero NO el
 * label, así que sin este mapa la ficha del video mostraba el código crudo
 * ("poppins-bold"). Los códigos son el contrato con pipeline._FONT_CATALOGUE,
 * asertado por lib/renderParity.test.js.
 */
export const FONT_LABELS = (t) => ({
  "": t("upload.font_auto") || "Auto",
  fredoka: "Fredoka (redondeada)",
  quicksand: "Quicksand (suave)",
  nunito: "Nunito (amigable)",
  "jost-bold": "Jost (estilo Futura)",
  "montserrat-bold": "Montserrat",
  "poppins-bold": "Poppins",
  "outfit-bold": "Outfit (estilo Gilroy)",
  "roboto-bold": "Roboto",
  "bebas-neue": "Bebas Neue",
  "oswald-bold": "Oswald",
  anton: "Anton",
});
