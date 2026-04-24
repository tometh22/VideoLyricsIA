# GenLy AI — Estado del Proyecto

> Última actualización: 2026-04-15

---

## Qué es GenLy AI

Plataforma SaaS de generación automática de lyric videos. Sube un MP3 → IA transcribe lyrics → genera video Full HD + YouTube Short + thumbnail → publica directo en YouTube con metadata SEO.

---

## Lo que está implementado (100% código, testeado)

### Backend (FastAPI + PostgreSQL)

| Módulo | Archivo | Qué hace |
|--------|---------|----------|
| **Database** | `backend/database.py` | SQLAlchemy ORM: 10 modelos (User, Job, Invoice, UserSettings, PasswordResetToken, EmailVerificationToken, AuditLog, BackgroundAsset, AIProvenance). Connection pooling. |
| **Auth** | `backend/auth.py` | JWT (HS256), bcrypt, login por username o email, registro público, password reset, email verification, plan usage tracking. Admins se crean con `ai_authorized=True`. |
| **Jobs** | `backend/jobs.py` | CRUD de jobs con PostgreSQL, thread-safe updates, admin queries. Soporte para status `pending_review`. |
| **Billing** | `backend/billing.py` | Stripe: checkout sessions, customer portal, cambio de plan con prorrateo, webhooks (checkout, subscription, invoice), historial de facturas. |
| **Admin** | `backend/admin.py` | Panel admin: stats globales, CRUD usuarios, cambiar plan, activar/desactivar, ver todos los jobs/facturas, audit log. Authorize/revoke AI por usuario. CRUD de backgrounds library. Vista de provenance. |
| **Emails** | `backend/emails.py` | SMTP: welcome, verificación email, password reset, job completado, alerta de uso (80%/100%), factura pagada. Templates HTML branded. |
| **Storage** | `backend/storage.py` | Capa abstracta: `LocalStorage` (disco) o `S3Storage` (AWS/DO Spaces/MinIO). Switch con env var. |
| **Pipeline** | `backend/pipeline.py` | Whisper → Gemini lyrics analysis → Veo 3.1 background → moviepy video → short → thumbnail → content validation → pending_review. Provenance tracking en cada llamada IA. Soporte para background humano/library. Config `SEND_ARTIST_TO_AI`. |
| **YouTube** | `backend/youtube_upload.py` | Upload con metadata IA vía Gemini. Provenance tracking con `job_id`. |
| **Provenance** | `backend/provenance.py` | **NUEVO**: Helper `record_ai_call()` con `ProvenanceRecorder`. Registra cada invocación IA con tool, prompt, datos enviados, duración, artefacto generado. Thread-safe. |
| **AI Providers** | `backend/ai_providers.py` | **NUEVO**: Abstracción de providers (VideoProvider, ImageProvider, TextProvider). Veo/Imagen/Gemini como default. Swappable via env var `VIDEO_PROVIDER`, `IMAGE_PROVIDER`, `TEXT_PROVIDER`. |
| **Content Validator** | `backend/content_validator.py` | **NUEVO**: Validación post-generación con Gemini Vision. Extrae frames, detecta personas/caras/texto/logos. Bloquea automáticamente si encuentra contenido prohibido. |
| **Compliance** | en `main.py` | **NUEVO**: Endpoints `/compliance/status` y `/compliance/data-policy`. Status de cada guideline UMG. Documentación de qué datos se envían a cada API. |
| **Rate Limiting** | en `main.py` | slowapi: 5 reg/min, 10 login/min, 30 upload/min. Desactivable para tests. |
| **Sentry** | en `main.py` | Init automático si `SENTRY_DSN` está configurado. |

### Frontend (React 18 + Vite + Tailwind)

| Componente | Archivo | Qué hace |
|------------|---------|----------|
| **LoginPage** | `components/LoginPage.jsx` | Login + registro público + forgot password + confirmación de reset. |
| **Landing** | `components/Landing.jsx` | Plan Free agregado a pricing. Botón de registro en cada plan. |
| **Sidebar** | `components/Sidebar.jsx` | Link a Admin (solo admins), badge del plan actual. |
| **Settings** | `components/Settings.jsx` | 3 tabs: YouTube / Billing / Account. Plan management, historial facturas, info cuenta. |
| **AdminPanel** | `components/AdminPanel.jsx` | 6 tabs: Overview / Users / Jobs / Invoices / Backgrounds / Compliance. CRUD usuarios con authorize/revoke AI. CRUD backgrounds library con upload/preview/delete. Dashboard de compliance con status por guideline. Stats incluyen pending_review. |
| **Dashboard** | `components/Dashboard.jsx` | Stats con pending_review y validation_failed. Thumbnails para jobs pending_review. Contador dinámico. |
| **UploadZone** | `components/UploadZone.jsx` | Selector de background con 3 modos: **IA Auto** (Veo genera), **Biblioteca** (galería de pre-aprobados), **Upload** (subir fondo propio). |
| **BatchProgress** | `components/BatchProgress.jsx` | Step "validation" en progress. Status pending_review (ámbar) y validation_failed (rojo) con iconos. |
| **JobDetail** | `components/JobDetail.jsx` | 4 tabs: Video / Short / Thumbnail / Provenance. Tab Provenance con timeline colapsable de cada llamada IA. Botones Aprobar/Rechazar para pending_review. Badges de status. Export de provenance. Panel YouTube solo para jobs aprobados. |
| **HistoryView** | `components/HistoryView.jsx` | Badges para pending_review y validation_failed. Click en pending_review para revisar. |
| **App** | `App.jsx` | Polling para pending_review/validation_failed. State de backgroundFile y backgroundId. Prop onJobUpdate para aprobar desde JobDetail. |
| **i18n** | `i18n.jsx` | +60 keys nuevas en ES/EN/PT (provenance, approval, validation, background selector, compliance). |

### UMG Compliance (implementado 2026-04-13/14)

Remediación completa frente a las "Guidelines for use of AI Image and Video Tools" (Oct 2025):

| Guideline | Requisito | Implementación |
|-----------|-----------|----------------|
| **1** | Herramientas aprobadas | Exclusivamente Vertex AI Enterprise. Config `VERTEX_ENTERPRISE_CONFIRMED`. Check en admin. |
| **3** | Sin herramientas prohibidas | Verificado: cero Midjourney/Sora/Dall-E/Runway/Hailuo. |
| **5** | Pre-autorización de contractors | Campo `ai_authorized` en User. Endpoints authorize/revoke. Bloqueo en upload/generate. |
| **6** | Uso limitado de IA + disclaimer copyright | IA solo en backgrounds. Export con disclaimer USCO por elemento. |
| **7** | Shots IA cortos | Clips de ~8s loopeados. Opción de fondo humano. |
| **9-10** | Input humano / fondo humano | Custom background upload. Biblioteca de fondos pre-aprobados. |
| **13-15** | Sin personas / validación | Prompts excluyen personas. Content Validator con Gemini Vision. Triple capa de protección. |
| **14** | No training con datos cliente | Vertex AI Enterprise no entrena. Data minimization. Endpoint `/compliance/data-policy`. |
| **16** | Clearance / workflow aprobación | Status `pending_review`. Approve/reject con audit log. Downloads bloqueados hasta aprobación. |
| **17** | Registros meticulosos IA vs humano | Tabla `ai_provenance`. Export JSON para copyright registration. Tab Provenance en UI. |

### Biblioteca de Backgrounds (implementado 2026-04-14)

| Feature | Detalle |
|---------|---------|
| **Admin CRUD** | Upload, preview, delete de fondos pre-aprobados desde AdminPanel > Backgrounds |
| **Modelo DB** | `BackgroundAsset`: name, filename, file_type, tags, uploaded_by, is_active |
| **Selector en UploadZone** | 3 modos: IA Auto / Biblioteca / Upload propio |
| **Bypass AI auth** | Usar fondo de biblioteca no requiere `ai_authorized` (no usa IA) |
| **Provenance** | Fondo de biblioteca se registra como `human-provided` en provenance |

### Documentos de Compliance

| Archivo | Qué es |
|---------|--------|
| `docs/GenLy_AI_Compliance_Report.md` | Reporte completo en markdown |
| `docs/pitch/compliance.html` | Deck de 14 slides (dark, formato pitch) |
| `docs/pitch/GenLy_AI_Compliance_Report.pdf` | PDF del deck de 14 slides |
| `docs/pitch/compliance_a4.html` | One-pager A4 formal (light, corporativo) |
| `docs/pitch/GenLy_AI_Compliance_A4.pdf` | PDF one-pager A4 para enviar a UMG |

### Infraestructura

| Archivo | Qué hace |
|---------|----------|
| `Dockerfile` | Multi-stage: Node 20 (frontend) → Python 3.11 (backend) con ffmpeg + ImageMagick. 4 workers uvicorn. |
| `docker-compose.yml` | PostgreSQL 16, Redis 7, app, nginx, backup service. Health checks en todo. |
| `nginx/nginx.conf` | Reverse proxy, gzip, rate limiting por zona, security headers, SPA fallback, 100MB upload. |
| `.env.example` | Todas las variables documentadas incluyendo UMG compliance vars. |
| `.github/workflows/ci.yml` | CI: pytest backend + build frontend + Docker build en main. |

### Tests

| Suite | Tests | Estado |
|-------|-------|--------|
| `tests/test_auth.py` | 14 | PASSED |
| `tests/test_admin.py` | 10 | PASSED |
| `tests/test_billing.py` | 5 | PASSED |
| `tests/test_settings.py` | 6 | PASSED |
| `tests/test_backgrounds.py` | 26 | PASSED |
| **Total** | **61** | **ALL PASSED** |

Tests de backgrounds cubren: CRUD backgrounds, permisos (admin/user), compliance endpoints, approval workflow, provenance endpoints, AI authorization (authorize/revoke/admin bypass/library bypass), bloqueo de upload/generate.

---

## Env vars nuevas (UMG Compliance)

```bash
# UMG Compliance
REQUIRE_REVIEW=true                    # Workflow de aprobación obligatorio
SEND_ARTIST_TO_AI=true                 # Enviar nombre de artista a Gemini (false para anonimizar)
VERTEX_ENTERPRISE_CONFIRMED=false      # Confirmar acuerdo enterprise con UMG

# AI Providers (defaults — no cambiar salvo que se necesite otro provider)
VIDEO_PROVIDER=veo                     # veo (default)
IMAGE_PROVIDER=imagen                  # imagen (default)
TEXT_PROVIDER=gemini                   # gemini (default)
```

---

## Planes y pricing

| Plan | Videos/mes | Precio USD/mes | Por video |
|------|-----------|----------------|-----------|
| Free | 5 | $0 | $0 |
| 100 | 100 | $900 | $9.00 |
| 250 | 250 | $2,000 | $8.00 |
| 500 | 500 | $3,500 | $7.00 |
| 1000 | 1,000 | $6,000 | $6.00 |
| Unlimited | ∞ | — | — |

Overage: +30% sobre precio unitario del plan.

---

## Pendientes (NO código — acciones operativas)

| # | Tarea | Tipo |
|---|-------|------|
| 1 | Confirmar con UMG que el contrato de Vertex AI califica como enterprise agreement → setear `VERTEX_ENTERPRISE_CONFIRMED=true` | Gestión |
| 2 | Enviar el compliance one-pager A4 (`GenLy_AI_Compliance_A4.pdf`) a UMG | Gestión |
| 3 | Cargar fondos pre-aprobados en la biblioteca de backgrounds (desde admin panel) | Contenido |
| 4 | Deploy a producción (hosting, dominio, keys) | Infra |

---

## Estructura de archivos (nuevos desde 2026-04-13)

```
VideoLyricsIA/
├── STATUS.md                              ← este archivo (actualizado)
├── docs/
│   ├── GenLy_AI_Compliance_Report.md      ← NUEVO: reporte compliance markdown
│   └── pitch/
│       ├── compliance.html                ← NUEVO: deck 14 slides
│       ├── compliance_a4.html             ← NUEVO: one-pager A4 corporativo
│       ├── GenLy_AI_Compliance_Report.pdf ← NUEVO: PDF deck
│       ├── GenLy_AI_Compliance_A4.pdf     ← NUEVO: PDF one-pager
│       ├── export_compliance_pdf.mjs      ← NUEVO: script export deck
│       ├── export_compliance_a4.mjs       ← NUEVO: script export A4
│       └── export_compliance_onepage.mjs  ← NUEVO: script export onepage
│
└── lyricgen/
    ├── assets/backgrounds/library/        ← NUEVO: carpeta para fondos pre-aprobados
    │
    ├── backend/
    │   ├── main.py                        ← actualizado (backgrounds, compliance, provenance, approval, AI auth)
    │   ├── database.py                    ← actualizado (+BackgroundAsset, +AIProvenance, +campos Job/User)
    │   ├── auth.py                        ← actualizado (ai_authorized en create_user)
    │   ├── jobs.py                        ← actualizado (completed_at para pending_review)
    │   ├── admin.py                       ← actualizado (backgrounds CRUD, authorize AI, provenance, stats)
    │   ├── pipeline.py                    ← actualizado (provenance, validation, background humano, SEND_ARTIST_TO_AI)
    │   ├── youtube_upload.py              ← actualizado (provenance con job_id)
    │   ├── provenance.py                  ← NUEVO
    │   ├── ai_providers.py                ← NUEVO
    │   ├── content_validator.py           ← NUEVO
    │   ├── .env.example                   ← actualizado (+REQUIRE_REVIEW, SEND_ARTIST_TO_AI, VERTEX_ENTERPRISE_CONFIRMED)
    │   └── tests/
    │       └── test_backgrounds.py        ← NUEVO: 26 tests (backgrounds, compliance, auth, provenance)
    │
    └── frontend/src/
        ├── App.jsx                        ← actualizado (polling, backgroundId, onJobUpdate)
        ├── i18n.jsx                       ← actualizado (+60 keys ES/EN/PT)
        └── components/
            ├── AdminPanel.jsx             ← actualizado (+Backgrounds tab, +Compliance tab, +authorize AI)
            ├── Dashboard.jsx              ← actualizado (+pending_review, +validation_failed)
            ├── UploadZone.jsx             ← actualizado (+selector 3 modos: IA/Biblioteca/Upload)
            ├── BatchProgress.jsx          ← actualizado (+validation step, +pending_review/validation_failed)
            ├── JobDetail.jsx              ← reescrito (+Provenance tab, +Approve/Reject, +badges)
            └── HistoryView.jsx            ← actualizado (+pending_review/validation_failed badges)
```

---

## Contexto del proyecto

- **Cliente target**: Universal Music (propuesta en revisión, compliance doc enviado)
- **Estado**: MVP funcional con compliance UMG completo, falta deploy a producción
- **Branch**: `claude/build-lyricgen-app-g8gYT`
- **Último trabajo**: Remediación UMG Guidelines (17 guidelines), biblioteca de backgrounds, documento compliance A4
