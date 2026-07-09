# VideoLyricsIA

## Branches y deploys (REGLA CRÍTICA — leer antes de crear cualquier PR)

- `main` = **PRODUCCIÓN** (genly.pro). Railway y Vercel auto-deployan cada merge. Hay clientes reales (UMG Chile).
- `staging` = pre-producción (rama protegida, solo se toca vía PR).
- Todo fix, feature o experimento se abre como **PR contra `staging`**. Ahí termina el trabajo: se valida en el preview de staging.
- **PROHIBIDO crear o mergear PRs con base `main`**, salvo que el usuario diga explícitamente **"esto va a producción"** (o equivalente inequívoco) en la sesión actual. "ok", "dale", "mergea" **NO** autorizan prod.
  - Esto incluye PRs "espejo"/"[prod]" de fixes individuales y promociones de `staging` entero (head=staging). No los crees "por las dudas" ni los dejes abiertos.
  - La urgencia no es autorización: "este bug le pega a UMG en prod hoy" ≠ permiso para deployar. Reportá la urgencia y esperá la decisión del usuario.
- Antes de mergear cualquier PR, decí explícitamente a qué entorno deploya ("esto va a staging" / "esto va a PRODUCCIÓN").

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
