# LyricGen — Propuesta Comercial

## Plataforma de generación automática de Lyric Videos

---

## El problema

Las discográficas necesitan producir lyric videos a escala para su catálogo. El proceso actual depende de editores freelance que:

- Tardan 2-4 horas por video
- No escalan más allá de 20-30 videos por semana
- Producen calidad inconsistente
- No están disponibles 24/7
- No pueden absorber picos de demanda (lanzamientos simultáneos)

Para 100 videos mensuales, el proceso manual requiere coordinar múltiples freelancers, revisar cada entrega, y gestionar plazos. Para 500+ videos (escala regional), se vuelve inviable.

---

## La solución

**LyricGen** es una plataforma SaaS que genera automáticamente lyric videos profesionales a partir de un archivo MP3.

Por cada canción, la plataforma genera:

| Output | Formato | Uso |
|--------|---------|-----|
| Lyric Video | Full HD 1920x1080 | YouTube, plataformas de streaming |
| YouTube Short | Vertical 1080x1920, 30s | YouTube Shorts, Instagram Reels, TikTok |
| Thumbnail | 1280x720 JPG | Portada del video en YouTube |

### Proceso (menos de 5 minutos por canción):

1. Se sube el MP3 a la plataforma
2. La IA transcribe y sincroniza las letras automáticamente
3. La IA analiza la temática de la canción y genera un fondo visual acorde
4. Se genera el lyric video, el short y el thumbnail
5. El equipo revisa, descarga y publica

### Capacidades clave:

- **Transcripción inteligente**: Las letras se extraen del audio y se corrigen automáticamente con IA
- **Fondos únicos por canción**: Cada video tiene un fondo visual generado por IA que refleja el mood de la canción (nunca se repite)
- **3 estilos visuales**: Video cinematográfico, fotografía artística, o ilustración — la IA elige según el género
- **Batch processing**: Se pueden subir y procesar múltiples canciones a la vez
- **Revisión de lyrics**: Pantalla de edición para corregir letras antes de generar el video
- **Selector de idioma**: Español, inglés, portugués, francés, italiano, alemán
- **Historial completo**: Todos los videos generados quedan disponibles para re-descarga
- **Preview inline**: Vista previa del video, short y thumbnail sin necesidad de descargar

---

## Planes y precios

### Generación de contenido

| Plan | Videos/mes | Precio | Por video |
|------|-----------|--------|-----------|
| **100** | 100 | **USD 800/mes** | $8.00 |
| **250** | 250 | **USD 1,750/mes** | $7.00 |
| **500** | 500 | **USD 3,000/mes** | $6.00 |
| **1,000** | 1,000 | **USD 5,000/mes** | $5.00 |

Cada video incluye: Lyric Video (Full HD) + YouTube Short (vertical) + Thumbnail.

### Módulo de publicación en YouTube (add-on)

Subida automática a YouTube con metadata generada por IA.

| Feature | Detalle |
|---------|---------|
| Subida directa | El video se publica en el canal del cliente sin intervención manual |
| Título optimizado | Generado por IA, optimizado para SEO de YouTube |
| Descripción completa | Con créditos, links a plataformas, hashtags |
| Tags y keywords | Generados automáticamente por género, artista, idioma |
| Thumbnail | Se sube como portada del video |
| Programación | Se puede definir fecha y hora de publicación |
| Flujo de aprobación | Preview → aprobación interna → publicación |

| Plan | Con módulo YouTube |
|------|-------------------|
| **100 videos** | **USD 1,200/mes** (+$400) |
| **250 videos** | **USD 2,500/mes** (+$750) |
| **500 videos** | **USD 4,200/mes** (+$1,200) |
| **1,000 videos** | **USD 7,000/mes** (+$2,000) |

### Condiciones generales

- Contrato mínimo: 6 meses
- Facturación mensual en USD
- IVA no incluido
- Videos no utilizados en el mes no se acumulan
- El módulo YouTube requiere autorización OAuth del canal del cliente (setup único)

*Los planes son por territorio. Para multi-territorio (ej: Argentina + México + Colombia), se cotiza plan combinado con descuento.*

---

## Comparativa

| | Editor freelance | Estudio de producción | **LyricGen** |
|---|---|---|---|
| Costo por video | $10 - $15 | $150 - $500 | **$8 o menos** |
| Tiempo por video | 2-4 horas | 1-2 días | **5 minutos** |
| 100 videos | 2-3 semanas | 1-2 meses | **1 día** |
| Escala a 500/mes | Inviable | Muy costoso | **Mismo plan** |
| Calidad consistente | Variable | Alta | **Alta y consistente** |
| Disponibilidad | Horario laboral | Horario laboral | **24/7** |
| Fondo único por canción | Repite templates | Personalizado | **IA genera uno único** |
| Idiomas | Depende del editor | Depende del editor | **6 idiomas automáticos** |

### Ahorro estimado (100 videos/mes):

| vs. | Costo mensual actual | Con LyricGen | Ahorro |
|-----|---------------------|-------------|--------|
| Freelancer ($12/video) | $1,200 | $800 | **$400/mes ($4,800/año)** |
| Estudio ($200/video) | $20,000 | $800 | **$19,200/mes** |

---

## Propiedad intelectual

| Componente | Propiedad |
|------------|-----------|
| Videos de fondo generados por IA | **Del cliente** — propiedad comercial completa |
| Tipografías utilizadas | **Licencia libre** — SIL Open Font License, uso comercial sin restricciones |
| Audio (MP3 provisto por el cliente) | **Del cliente** |
| Lyric video final | **Del cliente** — puede monetizar, distribuir y publicar sin restricciones |

**El cliente es dueño al 100% de todo el contenido generado.** No hay regalías, ni atribución requerida, ni restricciones de uso. Los videos pueden monetizarse en YouTube, plataformas de streaming, redes sociales, TV, o cualquier medio.

---

## Tecnología

La plataforma utiliza infraestructura de Google Cloud (Vertex AI) con las siguientes garantías:

- **SLA 99.9%** de disponibilidad
- **Encriptación** de datos en tránsito y en reposo
- **Aislamiento de datos** por cliente — cada cliente tiene su instancia separada
- **Cumplimiento** con regulaciones de protección de datos
- **Escalabilidad automática** — la plataforma crece con la demanda

---

## Próximas funcionalidades (roadmap)

| Feature | Estado |
|---------|--------|
| Flujo de aprobación interno | En desarrollo |
| Subida directa a YouTube vía API | Planificado Q2 2026 |
| Generación automática de títulos y descripciones para YouTube | Planificado Q2 2026 |
| Dashboard de métricas y analytics | Planificado Q3 2026 |
| Integración con sistemas internos del cliente | Bajo demanda |

---

## Cómo empezar

1. **Firma de contrato** — Plan Argentina, 6 meses
2. **Setup** (1-2 días hábiles) — Configuración de la instancia del cliente
3. **Capacitación** (1 hora) — Demo de la plataforma al equipo
4. **Producción** — El equipo comienza a generar videos

---

## Contacto

**LyricGen**
Plataforma de generación automática de lyric videos con inteligencia artificial

*Propuesta válida por 30 días a partir de la fecha de emisión.*
*Fecha: 1 de abril de 2026*
