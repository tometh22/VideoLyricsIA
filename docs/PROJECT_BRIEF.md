# GenLy AI (VideoLyricsIA) — Resumen técnico para plan de negocio

> Documento de briefing para pasar a otro chat de Claude.
> Última actualización: 2026-04-25
> Branch: `claude/project-summary-business-plan-SSKYB`
> Repo: `tometh22/videolyricsia`

---

## 1. Qué es el producto

**GenLy AI** es una plataforma SaaS B2B que automatiza la creación de **lyric videos** para sellos discográficos y artistas. Flujo: el usuario sube un MP3 → IA transcribe lyrics → genera video Full HD (16:9) + YouTube Short (9:16) + thumbnail → publica directo en YouTube con metadata SEO optimizada.

- **Cliente target inicial:** Universal Music Group (UMG). Hay un compliance doc enviado y propuesta en revisión.
- **Estado actual:** MVP funcional (~10.000 LOC), 61 tests passing, falta deploy a producción.
- **Diferencial:** cumplimiento total de las "UMG Guidelines for use of AI Image and Video Tools" (Oct 2025) — 17 guidelines remediadas, con biblioteca de fondos pre-aprobados, content validator post-generación, workflow de aprobación obligatorio y registro de provenance IA vs humano para registro de copyright.

---

## 2. Stack técnico

### Backend (Python 3.11 / FastAPI)
- **Framework:** FastAPI 0.115 + Uvicorn (4 workers).
- **DB:** PostgreSQL 16 con SQLAlchemy 2.0. 10 modelos: User, Job, Invoice, UserSettings, PasswordResetToken, EmailVerificationToken, AuditLog, BackgroundAsset, AIProvenance.
- **Auth:** JWT HS256 + bcrypt. Login por user/email, registro público, reset de password, verificación email.
- **Queue:** Redis 7 + RQ. 3 réplicas de worker.
- **Storage:** capa abstracta — `LocalStorage` o `S3Storage` (AWS S3, Cloudflare R2, DO Spaces, MinIO).
- **Pagos:** Stripe (checkout, customer portal, webhooks, prorrateo de cambios de plan).
- **Email:** SMTP transaccional (welcome, verify, reset, job-complete, usage alert 80%/100%, invoice paid).
- **Observabilidad:** Sentry SDK + slowapi rate limiting (5 reg/min, 10 login/min, 30 upload/min).

### Pipeline IA (el core del costo operativo)
1. **Whisper** (OpenAI, modelo `turbo` con fallback a `large-v3`) → transcripción de lyrics. Corre **on-device** en el worker (GPU recomendada).
2. **Gemini 2.5 Flash** (Vertex AI Enterprise) → análisis de lyrics, generación de prompts visuales, metadata SEO de YouTube.
3. **Veo 3.1** (`veo-3.1-generate-001`, Vertex AI Enterprise) → generación de clips de ~8s loopeados como background. Rate-limit aware (50 req/min, cooldown 5s, retry hasta 5x).
4. **Imagen** (Vertex AI) → thumbnails.
5. **MoviePy** + **ffmpeg** + **ImageMagick** → composición final del video, short y thumbnail.
6. **Content Validator** (Gemini Vision) → extrae frames y bloquea automáticamente personas/caras/texto/logos.
7. **YouTube Data API v3** → upload con metadata generada por IA.

Toda llamada IA queda registrada en tabla `ai_provenance` con `tool_name`, prompt, datos enviados, duración, artefacto generado y `job_id`. Provenance exportable a JSON para registro USCO.

### Frontend (React 18 + Vite + Tailwind)
- ~3.500 LOC de componentes JSX.
- 10 componentes principales: LoginPage, Landing, Dashboard, UploadZone (con selector 3-modos: IA Auto / Biblioteca / Upload), BatchProgress, JobDetail (4 tabs: Video / Short / Thumbnail / Provenance), HistoryView, Settings (3 tabs: YouTube / Billing / Account), AdminPanel (6 tabs: Overview / Users / Jobs / Invoices / Backgrounds / Compliance), Sidebar.
- **i18n:** ES / EN / PT, ~60 keys nuevas relacionadas a compliance/provenance.

### Infraestructura
- **Docker multi-stage:** Node 20 (frontend build) → Python 3.11 (backend con ffmpeg + ImageMagick).
- **docker-compose:** PostgreSQL 16, Redis 7, API, 3 workers, nginx, backup service. Todos con healthchecks.
- **Nginx:** reverse proxy, gzip, rate limiting por zona, security headers, SPA fallback, upload 100MB.
- **CI:** GitHub Actions — pytest backend + build frontend + docker build en main.

---

## 3. Modelo de negocio

| Plan | Videos/mes | USD/mes | $/video |
|---|---|---|---|
| Free | 5 | $0 | $0 |
| Starter | 100 | $900 | $9.00 |
| Pro | 250 | $2.000 | $8.00 |
| Business | 500 | $3.500 | $7.00 |
| Scale | 1.000 | $6.000 | $6.00 |
| Unlimited | ∞ | a negociar | — |

Overage: **+30%** sobre el precio unitario del plan. Billing recurrente mensual vía Stripe Subscriptions con prorrateo en cambios de plan.

---

## 4. Costos operativos por video (referencia para el plan financiero)

Costos variables aproximados a investigar/validar para el plan:

- **Veo 3.1:** ~$0.35–0.75/segundo de video generado en Vertex AI. Con clips de 8s loopeados, ~$3–6 por background generado.
- **Gemini 2.5 Flash:** centavos por video (lyrics analysis + metadata + validation).
- **Imagen** (thumbnail): ~$0.02–0.04 por imagen.
- **Whisper:** corre local en GPU del worker, costo = compute, no API.
- **Storage** (S3/R2): MP3 input + video Full HD + Short + thumbnail ≈ 100–300 MB/job.
- **Egress:** YouTube upload + entregables al cliente.

Esto es crítico para definir **margen por plan** — al precio actual ($6–9/video) hay que validar que el COGS quede idealmente bajo $2.50–3.50/video.

---

## 5. Necesidades de Claude API

El proyecto **hoy NO usa Claude API** — usa Gemini en Vertex AI por requisito de UMG (Vertex AI Enterprise no entrena con datos del cliente). **Claude entra del lado del builder**, no del producto:

- **Desarrollo y mantenimiento del código** (Claude Code en Sonnet 4.6 / Opus 4.7) → backend FastAPI complejo + pipeline IA + frontend React.
- **Posible uso futuro como provider intercambiable:** la abstracción `ai_providers.py` (`TextProvider`, `VideoProvider`, `ImageProvider`) ya soporta swap por env var. Claude podría sumarse como `TEXT_PROVIDER=claude` para SEO/metadata si UMG lo aprueba (necesitaría su propio enterprise agreement con Anthropic con cláusula no-training).

---

## 6. Necesidades de servidor / infra

Dos cargas muy distintas:

- **API + DB + Redis** → liviana, cualquier VPS de 4 vCPU / 8 GB RAM alcanza (Hetzner, DO, Vultr).
- **Workers de pipeline IA** → pesados, requieren:
  - GPU para Whisper (idealmente NVIDIA con ≥8GB VRAM, ej. RTX 4000 / L4 / A10).
  - CPU + RAM para moviepy/ffmpeg (encoding video Full HD).
  - Egress alto (subida a YouTube + storage).
  - Considerar **GPU on-demand** (RunPod, Lambda Labs, Modal, Fly.io GPU) en vez de always-on para optimizar costos en etapa temprana.

Storage objeto recomendado: **Cloudflare R2** (sin egress fees) ya está cableado vía las env vars `R2_*`.

---

## 7. Compliance y posicionamiento legal (relevante para el plan)

- Toda la generación pasa por **Vertex AI Enterprise** (acuerdo no-training).
- Triple capa anti-personas: prompts excluyentes + content validator con Gemini Vision + biblioteca de fondos humanos como bypass.
- Workflow de aprobación obligatorio (`pending_review` → approve/reject) bloquea downloads.
- Provenance JSON exportable para registro de copyright USCO.
- Documentos entregables a UMG: deck de 14 slides en PDF + one-pager A4 corporativo.

Esto es un **moat regulatorio real** para vender a majors (UMG, Sony, Warner) — y es lo que justifica la propuesta a Universal.

---

## 8. Para el plan de negocio + LLC

Datos relevantes para que el otro chat decida:

- **Geografía operativa:** vende a sellos discográficos globales, principal en US.
- **Servicios externos a contratar (con créditos potenciales en LLC formation services tipo Stripe Atlas, Firstbase, Doola, Clerky):**
  - Stripe (pagos, ya integrado).
  - AWS o Google Cloud (Vertex AI obligatorio para UMG).
  - Cloudflare R2 (storage).
  - SendGrid / Postmark / Resend (SMTP).
  - Sentry (errores).
  - Anthropic Claude (desarrollo/builder).
  - Dominio + email empresarial.
- **Estructura sugerida a evaluar:** Delaware C-Corp si hay intención de levantar capital / vender a majors; LLC si es bootstrap. Stripe Atlas suele dar créditos AWS + Stripe + Notion + AWS Activate ($5K–25K).
- **Branch actual de trabajo:** `claude/project-summary-business-plan-SSKYB`.
- **Repo:** `tometh22/videolyricsia`.

---

## 9. Métricas de tamaño del codebase

- **Backend:** ~5.700 LOC Python en 18 archivos (incluye `main.py` 1.256 LOC, `pipeline.py` 1.671 LOC).
- **Frontend:** ~3.500 LOC JSX en 10 componentes principales.
- **Tests:** 61 tests pasando (auth, admin, billing, settings, backgrounds).
- **Total:** ~10.000 LOC + Dockerfiles + nginx config + docs de compliance.

---

## 10. Pendientes operativos (no son código)

| # | Tarea | Tipo |
|---|-------|------|
| 1 | Confirmar con UMG que el contrato de Vertex AI califica como enterprise agreement → setear `VERTEX_ENTERPRISE_CONFIRMED=true` | Gestión |
| 2 | Enviar el compliance one-pager A4 a UMG | Gestión |
| 3 | Cargar fondos pre-aprobados en la biblioteca de backgrounds | Contenido |
| 4 | Deploy a producción (hosting, dominio, keys, LLC formada) | Infra |

---

## 11. Preguntas concretas para el otro chat

1. ¿Qué modelo de Claude conviene para el desarrollo continuo del producto (Sonnet 4.6 vs Opus 4.7), considerando que es un codebase de ~10K LOC con pipeline IA complejo?
2. ¿Qué stack de hosting recomienda para un MVP con clientes enterprise (UMG)? Separación API vs workers GPU.
3. ¿Stripe Atlas / Firstbase / Doola / Clerky? Comparar créditos en AWS, GCP, Stripe, Notion, OpenAI/Anthropic.
4. ¿Delaware C-Corp o LLC? Considerando venta a majors y posible levantamiento de capital.
5. Estructura de costos detallada por plan (5 / 100 / 250 / 500 / 1.000 videos/mes) con márgenes proyectados.
6. Plan de negocio con: GTM para sellos, pricing strategy, runway, hitos de levantamiento, defensibilidad por compliance.
