/**
 * Panel "Variante" de un look (wizard paso 4 y editor). Contrato:
 * .context/variants_contract.md.
 *
 * El look define el MOVIMIENTO; la variante define fuentes y colores según
 * el género. Acá el operador:
 *   - ve el género detectado y lo corrige;
 *   - pide "Otra variante" (= variante resuelta + 1) o "Restaurar" (vuelve
 *     todo a automático: variante y overrides de fuentes/colores);
 *   - pisa "Letra principal" / "Letra destacada" y los colores "Texto",
 *     "Secundario" y "Destacado" (cada uno con "usar el de la variante").
 *
 * Es controlado: no guarda estado propio. `values` viene del wizard/editor
 * y cada cambio sale por `onChange(field, value)` con la key camelCase.
 * Lo resuelto (`resolved`, de hooks/useLookVariant) sólo se MUESTRA.
 */
import { useEffect } from "react";
import Listbox from "./Listbox";
import {
  LOOK_OVERRIDE_RESET, catalogGenre, contrastWarnings, ensureFontLoaded,
  fontCss, genreFamily, normalizeHexOrEmpty, normalizeLyricVariant, orderedFonts,
} from "../lib/lyricLookVariants";

const fill = (template, vars) =>
  Object.entries(vars).reduce((s, [k, v]) => s.replace(new RegExp(`\\{${k}\\}`, "g"), () => String(v)), String(template));

/** ¿Hay algo que "Restaurar" pueda limpiar? */
export function hasLookOverrides(values) {
  const v = values || {};
  return Object.entries(LOOK_OVERRIDE_RESET).some(([k, reset]) => {
    const cur = v[k] == null ? "" : v[k];
    if (k === "lyricColor") return String(cur || "#FFFFFF").toUpperCase() !== reset;
    return cur !== reset;
  });
}

export default function LookVariantPanel({
  t: translate,
  catalog,
  resolved = null,
  detected = null,
  loading = false,
  values = {},
  onChange,
  disabled = false,
  // Estilo del contenedor: el wizard y el editor tienen paddings distintos.
  className = "",
  testIdPrefix = "variant",
}) {
  // `t` local: el fallback se usa también cuando el traductor devuelve la
  // key cruda (tests). Se llama `t` para que check:i18n vea las keys.
  const t = (key, fallback) => {
    const v = translate ? translate(key) : "";
    return v && v !== key ? v : fallback;
  };
  const set = (field, value) => onChange && onChange(field, value);

  const operatorGenre = values.lyricGenre || "";
  const genreLabel = (id) => catalogGenre(catalog, id)?.label || id;
  const resolvedGenre = resolved && resolved.genre_source !== "operator" ? resolved.genre : "";
  const autoGenre = resolvedGenre || detected?.genre || "";
  let genreStatus;
  if (operatorGenre) genreStatus = null;
  else if (autoGenre) genreStatus = fill(t("upload.variant_genre_detected", "Detectado: {genre}"), { genre: genreLabel(autoGenre) });
  else if (loading) genreStatus = t("upload.variant_genre_detecting", "Detectando…");
  else genreStatus = t("upload.variant_genre_unknown", "Sin detectar");

  const family = resolved?.family || genreFamily(catalog, operatorGenre || autoGenre);
  const fonts = orderedFonts(catalog, family);
  useEffect(() => {
    for (const f of fonts) ensureFontLoaded(f.css_family, f.weight);
  }, [fonts]);

  const genreOptions = [
    { code: "", label: t("upload.variant_genre_auto", "Automático") },
    ...((catalog && catalog.genres) || []).map((g) => ({ code: g.id, label: g.label })),
  ];

  const fontOptions = (slot) => {
    const rf = resolved?.fonts?.[slot];
    const name = rf ? (fonts.find((f) => f.id === rf.id)?.label || rf.css_family || rf.id) : "";
    return [
      {
        code: "",
        label: name
          ? fill(t("upload.variant_font_default", "De la variante ({font})"), { font: name })
          : t("upload.variant_font_default_plain", "De la variante"),
        css: rf ? fontCss(rf.css_family) : undefined,
        weight: rf?.weight,
      },
      ...fonts.map((f) => ({
        code: f.id,
        label: f.label || f.css_family || f.id,
        css: fontCss(f.css_family),
        weight: f.weight,
      })),
    ];
  };

  const variantNum = resolved ? normalizeLyricVariant(resolved.variant) : normalizeLyricVariant(values.lyricVariant);
  const nextVariant = () => {
    const base = variantNum === "" ? 0 : variantNum;
    set("lyricVariant", base + 1);
  };
  const restore = () => {
    for (const [k, v] of Object.entries(LOOK_OVERRIDE_RESET)) set(k, v);
  };

  const colors = resolved?.colors || {};
  const inkOverride = (() => {
    const c = normalizeHexOrEmpty(values.lyricColor);
    return c && c !== "#FFFFFF" ? c : "";
  })();
  const colorRows = [
    { field: "lyricColor", key: "ink", label: t("upload.variant_color_ink", "Texto"), value: inkOverride, resetTo: "#FFFFFF" },
    { field: "lyricAltColor", key: "alt", label: t("upload.variant_color_alt", "Secundario"), value: normalizeHexOrEmpty(values.lyricAltColor), resetTo: "" },
    { field: "lyricAccentColor", key: "accent", label: t("upload.variant_color_accent", "Destacado"), value: normalizeHexOrEmpty(values.lyricAccentColor), resetTo: "" },
  ];

  const warnings = contrastWarnings(resolved);
  const warningText = (w) => ({
    low_contrast_ink: t("upload.variant_warning_low_contrast_ink", "El color de texto tiene poco contraste: puede no leerse."),
    low_contrast_alt: t("upload.variant_warning_low_contrast_alt", "El color secundario tiene poco contraste: puede no leerse."),
    low_contrast_accent: t("upload.variant_warning_low_contrast_accent", "El color destacado tiene poco contraste: puede no leerse."),
    accent_same_as_text: t("upload.variant_warning_accent_same_as_text", "El color destacado es igual al del texto: la palabra clave no va a resaltar."),
  }[w] || t("upload.variant_warning_low_contrast", "Poco contraste: la letra puede no leerse."));

  const canRestore = hasLookOverrides(values);

  return (
    <div className={className} data-testid={`${testIdPrefix}-panel`}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-[11px] text-gray-300 font-medium">{t("upload.variant_section", "Variante")}</p>
        {loading && (
          <span className="text-[10px] text-gray-500" data-testid={`${testIdPrefix}-loading`}>
            {t("upload.variant_loading", "Calculando…")}
          </span>
        )}
      </div>
      <p className="text-[10px] text-gray-600 mt-0.5 mb-3">
        {t("upload.variant_desc", "Fuentes y colores según el género de la canción. El movimiento lo define el look.")}
      </p>

      <div className="space-y-3">
        {/* Género */}
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-gray-600 shrink-0 w-20">{t("upload.variant_genre_label", "Género")}</span>
          <Listbox
            value={operatorGenre}
            onChange={(v) => {
              // Otro género = otro pool de variantes: el número elegido para
              // el anterior no significa nada en el nuevo.
              set("lyricGenre", v);
              if (values.lyricVariant !== "" && values.lyricVariant != null) set("lyricVariant", "");
            }}
            options={genreOptions}
            className="flex-1"
            ariaLabel={t("upload.variant_genre_label", "Género")}
            disabled={disabled}
          />
        </div>
        {genreStatus && (
          <p className="ml-[5.5rem] -mt-1.5 text-[10px] text-gray-400" data-testid={`${testIdPrefix}-genre-status`}>
            {genreStatus}
          </p>
        )}

        {/* Variante */}
        <div className="rounded-lg bg-black/20 ring-1 ring-white/[0.06] px-3 py-2">
          <p className="text-[11px] text-gray-200" data-testid={`${testIdPrefix}-description`}>
            {variantNum !== "" && (
              <span className="text-gray-500 mr-1.5">
                {fill(t("upload.variant_number", "Variante {n}"), { n: variantNum })}
              </span>
            )}
            {resolved?.description || (loading ? t("upload.variant_loading", "Calculando…") : "—")}
          </p>
          <div className="flex items-center gap-3 mt-2">
            <button
              type="button"
              onClick={nextVariant}
              disabled={disabled}
              data-testid={`${testIdPrefix}-next`}
              className="px-2.5 py-1 rounded-md text-[11px] font-medium bg-brand/20 text-brand ring-1 ring-brand/40 hover:bg-brand/30 transition-colors disabled:opacity-40"
            >
              {t("upload.variant_next", "Otra variante")}
            </button>
            <button
              type="button"
              onClick={restore}
              disabled={disabled || !canRestore}
              data-testid={`${testIdPrefix}-restore`}
              className="text-[10px] text-gray-400 hover:text-white underline-offset-2 hover:underline transition-colors disabled:opacity-40 disabled:no-underline"
            >
              {t("upload.variant_restore", "Restaurar")}
            </button>
          </div>
        </div>

        {/* Fuentes */}
        {[
          ["lyricFontMain", "main", t("upload.variant_font_main", "Letra principal")],
          ["lyricFontKey", "key", t("upload.variant_font_key", "Letra destacada")],
        ].map(([field, slot, label]) => (
          <div className="flex items-center gap-2" key={field} data-testid={`${testIdPrefix}-font-${slot}`}>
            <span className="text-[11px] text-gray-600 shrink-0 w-20">{label}</span>
            <Listbox
              value={values[field] || ""}
              onChange={(v) => set(field, v)}
              options={fontOptions(slot)}
              className="flex-1"
              ariaLabel={label}
              disabled={disabled}
            />
          </div>
        ))}

        {/* Colores */}
        <div className="flex items-start gap-2">
          <span className="text-[11px] text-gray-600 shrink-0 w-20 pt-1">{t("upload.variant_colors_label", "Colores")}</span>
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {colorRows.map((row) => {
              const shown = row.value || normalizeHexOrEmpty(colors[row.key]) || "#FFFFFF";
              return (
                <div key={row.field} className="flex flex-col gap-0.5" data-testid={`${testIdPrefix}-color-${row.key}`}>
                  <label className="flex items-center gap-1.5 text-[11px] text-gray-400 cursor-pointer">
                    <input
                      type="color"
                      value={shown.toLowerCase()}
                      onChange={(e) => set(row.field, e.target.value.toUpperCase())}
                      disabled={disabled}
                      className="w-7 h-7 rounded cursor-pointer bg-transparent border-0 p-0"
                      aria-label={row.label}
                    />
                    <span>{row.label}</span>
                  </label>
                  {row.value ? (
                    <button
                      type="button"
                      onClick={() => set(row.field, row.resetTo)}
                      disabled={disabled}
                      data-testid={`${testIdPrefix}-color-${row.key}-reset`}
                      className="text-left text-[10px] text-gray-500 hover:text-white underline-offset-2 hover:underline transition-colors"
                    >
                      {t("upload.variant_color_use_variant", "usar el de la variante")}
                    </button>
                  ) : (
                    <span className="text-[10px] text-gray-600">{t("upload.variant_color_from_variant", "de la variante")}</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
        {warnings.length > 0 && (
          <div className="flex flex-wrap gap-1.5" role="status">
            {warnings.map((w) => (
              <span
                key={w}
                data-testid={`${testIdPrefix}-contrast-warning`}
                data-warning={w}
                className="px-2 py-0.5 rounded-full text-[10px] bg-amber-500/15 text-amber-200 ring-1 ring-amber-500/30"
              >
                {warningText(w)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
