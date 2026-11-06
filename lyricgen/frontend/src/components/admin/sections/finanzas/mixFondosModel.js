// Cuentas del "Mix de fondos": el contrato con Universal pide 50% de video
// lyrics con fondo Veo Lite y 50% con foto fija + efecto. El backend agrupa
// lo publicado por campaña; acá se suma sólo lo tildado.

export const STORAGE_KEY = "genly.admin.mixFondos";

export function summarize(groups = [], selected = new Set(), meta = 0) {
  const picked = groups.filter((g) => selected.has(g.key));
  const sum = (field) => picked.reduce((a, g) => a + (g[field] || 0), 0);
  const veo = sum("veo");
  const foto = sum("foto");
  const sinDato = sum("sin_dato");
  const videos = sum("videos");
  const modelos = {};
  for (const g of picked) {
    for (const [k, v] of Object.entries(g.veo_models || {})) modelos[k] = (modelos[k] || 0) + v;
  }
  const clasificados = veo + foto;
  const out = {
    grupos: picked.length,
    videos,
    veo,
    foto,
    sinDato,
    pctVeo: clasificados ? veo / clasificados : null,
    pctFoto: clasificados ? foto / clasificados : null,
    modelos,
    // Veo que no es Lite (Fast u otro modelo): el contrato dice Lite.
    veoNoLite: (modelos.fast || 0) + (modelos.otro || 0),
    // Para empatar sobre lo ya entregado, sin generar más del tipo que sobra.
    faltanFoto: Math.max(0, veo - foto),
    faltanVeo: Math.max(0, foto - veo),
    meta: null,
  };
  const total = Number(meta) || 0;
  if (total > 0) {
    const mitad = total / 2;
    const restantes = Math.max(0, total - clasificados);
    out.meta = {
      total,
      restantes,
      foto: Math.max(0, Math.ceil(mitad - foto)),
      veo: Math.max(0, Math.ceil(mitad - veo)),
      // Si ya hay más de la mitad de un tipo, el 50/50 de la meta es imposible.
      alcanzable: veo <= mitad && foto <= mitad,
    };
  }
  return out;
}

export function loadSaved() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    return { selected: Array.isArray(data.selected) ? data.selected : null, meta: Number(data.meta) || 0 };
  } catch {
    return null;
  }
}

export function save(selected, meta) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ selected: [...selected], meta: Number(meta) || 0 }));
  } catch {
    // Sin localStorage la selección vive sólo en la pestaña.
  }
}
