import { useState, useEffect } from "react";

const API = "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const DEFAULT_SETTINGS = {
  titleFormat: "{artista} - {cancion} (Letra/Lyrics)",
  descriptionHeader: "",
  descriptionFooter: "",
  mandatoryTags: "",
  hashtags: "#lyrics #letra",
  metadataLanguage: "es",
  defaultPrivacy: "unlisted",
  channelName: "",
};

export default function Settings({ onBack }) {
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/settings`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (data && Object.keys(data).length) setSettings({ ...DEFAULT_SETTINGS, ...data }); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    try {
      await fetch(`${API}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(settings),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {}
  };

  const update = (key, value) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  if (loading) return null;

  return (
    <div className="w-full max-w-2xl animate-fade-in">
      <div className="flex items-center gap-3 mb-8">
        <button onClick={onBack}
          className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-2xl font-bold">Configuracion</h1>
          <p className="text-sm text-gray-500">Template de YouTube y preferencias</p>
        </div>
      </div>

      <div className="space-y-6">
        {/* YouTube Templates */}
        <div className="glass rounded-2xl p-6">
          <h3 className="font-semibold mb-1 flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24">
              <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
            </svg>
            Template de YouTube
          </h3>
          <p className="text-xs text-gray-500 mb-5">Configura como se generan los metadatos de cada video.</p>

          <div className="space-y-4">
            {/* Title format */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Formato del titulo</label>
              <input
                type="text"
                value={settings.titleFormat}
                onChange={(e) => update("titleFormat", e.target.value)}
                className="input-field text-sm"
                placeholder="{artista} - {cancion} (Letra/Lyrics)"
              />
              <p className="text-[10px] text-gray-600 mt-1">
                Variables: {"{artista}"}, {"{cancion}"}. Ej: {"{artista}"} - {"{cancion}"} | Lyric Video Oficial
              </p>
            </div>

            {/* Description header */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Encabezado de descripcion (fijo)</label>
              <textarea
                value={settings.descriptionHeader}
                onChange={(e) => update("descriptionHeader", e.target.value)}
                rows={3}
                className="input-field text-sm resize-none"
                placeholder="Texto que aparece al inicio de todas las descripciones. Ej: Escucha en todas las plataformas..."
              />
              <p className="text-[10px] text-gray-600 mt-1">
                Se agrega antes de la descripcion generada por IA. Variables: {"{artista}"}, {"{cancion}"}
              </p>
            </div>

            {/* Description footer */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Pie de descripcion (fijo)</label>
              <textarea
                value={settings.descriptionFooter}
                onChange={(e) => update("descriptionFooter", e.target.value)}
                rows={3}
                className="input-field text-sm resize-none"
                placeholder="Texto legal, links a redes, etc. Ej: © 2026 Universal Music. Seguinos en Instagram..."
              />
              <p className="text-[10px] text-gray-600 mt-1">
                Se agrega al final de todas las descripciones.
              </p>
            </div>

            {/* Mandatory tags */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Tags obligatorios</label>
              <input
                type="text"
                value={settings.mandatoryTags}
                onChange={(e) => update("mandatoryTags", e.target.value)}
                className="input-field text-sm"
                placeholder="Universal Music, VEVO, Warner Music"
              />
              <p className="text-[10px] text-gray-600 mt-1">
                Separados por coma. Se agregan a los tags generados por IA.
              </p>
            </div>

            {/* Hashtags */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Hashtags (se muestran arriba del titulo)</label>
              <input
                type="text"
                value={settings.hashtags}
                onChange={(e) => update("hashtags", e.target.value)}
                className="input-field text-sm"
                placeholder="#lyrics #letra #UniversalMusic"
              />
              <p className="text-[10px] text-gray-600 mt-1">
                YouTube muestra los primeros 3 hashtags arriba del titulo del video.
              </p>
            </div>

            {/* Metadata language */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Idioma de metadata (titulo, descripcion, tags)</label>
              <select
                value={settings.metadataLanguage}
                onChange={(e) => update("metadataLanguage", e.target.value)}
                className="input-field text-sm appearance-none cursor-pointer"
              >
                <option value="es">Espanol</option>
                <option value="en">English</option>
                <option value="pt">Portugues</option>
                <option value="fr">Francais</option>
                <option value="it">Italiano</option>
                <option value="de">Deutsch</option>
              </select>
            </div>

            {/* Default privacy */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Privacidad por defecto</label>
              <select
                value={settings.defaultPrivacy}
                onChange={(e) => update("defaultPrivacy", e.target.value)}
                className="input-field text-sm appearance-none cursor-pointer"
              >
                <option value="unlisted">No listado (para revision)</option>
                <option value="private">Privado</option>
                <option value="public">Publico</option>
              </select>
            </div>
          </div>
        </div>

        {/* Channel info */}
        <div className="glass rounded-2xl p-6">
          <h3 className="font-semibold mb-1">Canal de YouTube</h3>
          <p className="text-xs text-gray-500 mb-4">Canal conectado para subir videos.</p>
          <div>
            <label className="block text-xs font-medium text-gray-400 mb-1.5">Nombre del canal</label>
            <input
              type="text"
              value={settings.channelName}
              onChange={(e) => update("channelName", e.target.value)}
              className="input-field text-sm"
              placeholder="Nombre del canal de YouTube"
            />
          </div>
          <div className="flex items-center gap-2 mt-4">
            <div className="w-2 h-2 rounded-full bg-accent" />
            <span className="text-xs text-gray-500">Canal autorizado y conectado</span>
          </div>
        </div>

        {/* Save */}
        <div className="flex items-center gap-4">
          <button onClick={handleSave} className="btn-primary py-3 px-8">
            Guardar configuracion
          </button>
          {saved && (
            <span className="text-sm text-accent animate-fade-in flex items-center gap-1.5">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12"/>
              </svg>
              Guardado
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
