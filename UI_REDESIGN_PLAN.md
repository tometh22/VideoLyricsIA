# Genly UI Redesign Plan

## Base auditada

El rediseño se implementa sobre `origin/staging` (`d777fe8`), no sobre la referencia histórica `origin/claude/build-lyricgen-app-g8gYT`.

- React 18, React Router 6, Vite 6 y Tailwind CSS 3.
- Aplicación y contratos de negocio concentrados en `src/App.jsx`.
- ES, EN y PT en `src/i18n.jsx`.
- Dashboard operativo, Historial con tabla/grilla, wizard con preview en vivo, settings avanzados y Admin modular ya existentes en staging.
- QA disponible: chequeos de i18n/fetch, Vitest y build Vite.

## Estrategia

1. Conservar íntegramente auth, endpoints, payloads, permisos, uploads, polling, wizard, edición, revisión, billing y administración de staging.
2. Migrar tokens y marca sin reemplazar componentes funcionales nuevos.
3. Aplicar la nueva composición a marketing, autenticación y shell.
4. Reutilizar los layouts amplios ya presentes en Dashboard, Historial y wizard.
5. Validar `npm run check`, tests y build.

## Riesgos

- `App.jsx`, `UploadZone.jsx` y `LyricsEditor.jsx` son componentes grandes y sensibles; no se reescriben.
- La marca fue entregada como lámina raster. Los assets preservan ese arte y no reconstruyen el logo con CSS.
- El stash `backup-redesign-built-on-stale-target-2026-07-11` conserva la implementación descartada sobre la base antigua y puede eliminarse cuando se apruebe esta migración.
