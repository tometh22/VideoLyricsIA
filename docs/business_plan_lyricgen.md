# GenLy AI — Plan de Negocio y Costos Operativos

## Resumen ejecutivo

GenLy AI es una plataforma que genera automáticamente lyric videos (Full HD), YouTube Shorts (vertical) y thumbnails a partir de un archivo MP3. Está diseñada para operar a escala para discográficas como Universal Music Group.

**Propuesta de valor:** Un editor humano cobra entre $50 y $200 USD por lyric video. GenLy AI produce 100 videos por mes por menos de lo que cuesta uno solo hecho a mano.

---

## Qué genera la plataforma (por cada canción)

| Output | Formato | Resolución |
|--------|---------|------------|
| Lyric Video | MP4 | 1920x1080 (Full HD) |
| YouTube Short | MP4 | 1080x1920 (Vertical, 30s) |
| Thumbnail | JPG | 1280x720 |

---

## Stack tecnológico

| Componente | Tecnología | Licencia | Costo |
|------------|-----------|----------|-------|
| Transcripción de audio | OpenAI Whisper (local) | MIT (open source) | $0 |
| Generación de video de fondo | Google Veo 3 API | Pay-as-you-go | ~$0.35/video |
| Composición de video | MoviePy + FFmpeg | Open source | $0 |
| Tipografías | Google Fonts (Montserrat, Roboto, Oswald, etc.) | SIL Open Font License | $0 |
| Backend | Python + FastAPI | Open source | $0 |
| Frontend | React + Tailwind CSS | Open source | $0 |

---

## Costos operativos mensuales

### Escenario A: Cliente provee su Google Cloud (recomendado)

La app corre en la infraestructura del cliente. Solo se paga el consumo de API.

| Concepto | 100 videos/mes | 200 videos/mes | 500 videos/mes |
|----------|---------------|----------------|----------------|
| Google Veo 3 API | $35 | $70 | $175 |
| Infraestructura | $0 | $0 | $0 |
| Licencias de software | $0 | $0 | $0 |
| **Total mensual** | **$35** | **$70** | **$175** |

### Escenario B: Nosotros hosteamos todo

La app corre en nuestro servidor cloud. El cliente accede vía web.

| Concepto | 100 videos/mes | 200 videos/mes | 500 videos/mes |
|----------|---------------|----------------|----------------|
| Google Veo 3 API | $35 | $70 | $175 |
| Servidor cloud (VM) | $50 | $50 | $80 |
| Almacenamiento (outputs) | $5 | $10 | $20 |
| Dominio + SSL | $1 | $1 | $1 |
| **Total mensual** | **$91** | **$131** | **$276** |

### Escenario C: Todo en Google Cloud Platform

Infraestructura y API en el mismo proveedor.

| Concepto | 100 videos/mes | 200 videos/mes | 500 videos/mes |
|----------|---------------|----------------|----------------|
| Google Veo 3 API | $35 | $70 | $175 |
| Cloud Run / Compute Engine | $30 | $30 | $50 |
| Cloud Storage | $5 | $10 | $20 |
| Dominio + SSL | $1 | $1 | $1 |
| **Total mensual** | **$71** | **$111** | **$246** |

---

## Costo por video

| Volumen mensual | Escenario A | Escenario B | Escenario C |
|----------------|-------------|-------------|-------------|
| 100 videos | $0.35 | $0.91 | $0.71 |
| 200 videos | $0.35 | $0.66 | $0.56 |
| 500 videos | $0.35 | $0.55 | $0.49 |
| 1,000 videos | $0.35 | $0.44 | $0.42 |

---

## Comparativa con producción tradicional

| Método | Costo por video | Tiempo por video | Escalabilidad |
|--------|----------------|-----------------|---------------|
| Editor humano freelance | $50 - $200 | 2-4 horas | Baja |
| Estudio de producción | $150 - $500 | 1-2 días | Media |
| **GenLy AI (100 videos/mes)** | **$0.35 - $0.91** | **~5 minutos** | **Alta** |

**Ahorro estimado a 100 videos/mes:**
- vs. freelance ($100 promedio): **$9,909/mes** de ahorro (99% reducción)
- vs. estudio ($300 promedio): **$29,909/mes** de ahorro (99.7% reducción)

---

## Costos por única vez

| Concepto | Costo |
|----------|-------|
| Desarrollo de la plataforma | A definir |
| Configuración de Google Cloud | $0 |
| Registro de dominio (anual) | $12/año |
| Configuración de YouTube API (futuro) | $0 |
| Google Workspace | No necesario |
| Licencias de software | $0 |

---

## Propiedad intelectual y licencias

| Componente | Licencia | Uso comercial | Monetización en YouTube |
|------------|---------|--------------|------------------------|
| Videos de fondo (Veo 3) | Output propiedad del usuario | Si | Si |
| Tipografías (Google Fonts) | SIL Open Font License | Si, sin restricciones | Si |
| Audio (provisto por cliente) | Propiedad del cliente | Si | Si |
| Software de la plataforma | Open source (MIT, Apache) | Si | N/A |

**Ningún componente tiene restricciones de uso comercial ni riesgo de reclamos de terceros.**

---

## Funcionalidades actuales

- Subida de MP3 individual o en batch
- Transcripción automática de letras (Whisper AI)
- Generación de video de fondo único por canción (Google Veo 3)
- Composición de lyric video Full HD con tipografía profesional
- Generación automática de YouTube Short (selección de chorus)
- Generación de thumbnail con frame del video
- Historial persistente de jobs con preview inline
- Interfaz web profesional con sidebar y gestión de batch

## Funcionalidades planificadas

- Flujo de aprobación (reviewer aprueba antes de publicar)
- Subida directa a YouTube vía API
- Generación automática de títulos, descripciones y tags con IA
- Programación de publicación
- Dashboard de métricas

---

## Requisitos técnicos

**Para Escenario A (local):**
- Mac o Linux con Python 3.10+
- 8 GB RAM mínimo
- Conexión a internet (para Veo 3 API)
- Cuenta de Google Cloud con billing activo

**Para Escenario B/C (cloud):**
- VM con 4 vCPUs, 8 GB RAM
- 50 GB de almacenamiento
- Cuenta de Google Cloud con billing activo

---

*Documento generado el 31 de marzo de 2026.*
*Todos los precios en USD. Costos de API estimados según pricing de Google Cloud al momento de redacción.*
