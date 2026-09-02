<!-- Regla de branches (CLAUDE.md): todo PR va a `staging`. main = PRODUCCIÓN
     (genly.pro, clientes UMG reales) y SOLO se toca con autorización
     explícita "esto va a producción" del usuario en la sesión actual. -->

## Qué



## Verificación

- [ ] Frontend: `npm run build` (corre `check:i18n`) + vitest completo
- [ ] Backend: `ruff --select F821` + pytest (incl. `test_tenant_isolation` si toca scoping)
- [ ] Toda key `t()` nueva existe en **es/en/pt** (`src/i18n.jsx`)

## Checklist de deploy

- [ ] **Base = `staging`** (nunca `main` sin autorización explícita de producción)
- [ ] Si toca `pipeline.py`/`ass_render.py`/`fx_compositor.py`: `golden-render` verde en staging (cambio visual intencional → re-bless documentado)
- [ ] Si toca backend: `edit-smoke` (post-deploy de staging) verde
- [ ] Rollback: este PR es revertible en aislamiento (sin migraciones destructivas)

### Promoción a producción (solo si aplica)

- [ ] Autorización explícita del usuario en esta sesión: "esto va a producción"
- [ ] `edit-smoke` corrido MANUALMENTE contra producción tras el deploy
