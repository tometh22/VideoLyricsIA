# Análisis de competencia — GenLy AI (lyric videos con IA para sellos)

> Fecha: 2026-05-20. Validado con research externo (fuentes citadas) + dogfooding propio de Rotor.
> Target de GenLy: sellos (UMG LATAM), producción de lyric videos a volumen.

---

## TL;DR

- **El mercado es real y grande.** Un major necesita muy por encima de 200 lyric videos/año (~1.200–15.000 global, ~150–450 LATAM). Producirlos a mano cuesta US$25–1.250 y 1–2.5 semanas c/u: ese costo/tiempo es lo que reprime el volumen, y es lo que GenLy elimina.
- **Competidor a vencer = Rotor (LyricFind)**, no Musixmatch. Rotor ya está adentro de UMG/Sony y vende producción de video a sellos. Pero su fondo es **stock/template compartido**, no generativo, y arrastra un **riesgo de derechos (ContentID)**.
- **Musixmatch** es competidor de video **débil**: su core es data de letras, el video es un add-on dependiente de Runway que no están invirtiendo, capado y social-first.
- **Wedge defendible de GenLy:** fondo **generativo único por canción + full 16:9 + ownership 100% + sin riesgo ContentID + a escala de sello**. Ningún competidor cubre esa intersección.

---

## Cuadro comparativo

| | **GenLy AI** | **Rotor (LyricFind)** | **Musixmatch** |
|---|---|---|---|
| Qué es | SaaS dedicado a lyric videos | Creador multi-formato de videos | Plataforma de data de letras |
| Dueño / respaldo | — | LyricFind (data de letras licenciadas) | TPG (private equity, ~unicornio) |
| En UMG/Sony hoy | No (target) | **Sí** (partner + CD Baby) | Sí, pero por publishing/royalties (no video) |
| Fondo | **Generativo único por canción (Vertex/Gemini)** | Stock compartido (9M clips) + 150 estilos | Generativo abstracto (Runway) |
| Formato | Full HD 16:9 + short + thumbnail | Full 16:9 (stock) + multi-formato | Clip social 30s, Canvas, full capado |
| Transcripción/sync | Whisper + corrección IA | Muy buena (prob. forced alignment c/ letra oficial LyricFind) | AI transcribe + experts |
| Batch / volumen | **Sí (100–1.000/mes)** | Sí (self-serve por créditos; enterprise por form) | Limitado / capado |
| Ownership del material | **100% del cliente** | Licencia limitada del stock (no es del cliente) | Dentro del ecosistema |
| Riesgo ContentID | Ninguno (material único) | **Sí** (clips compartidos) | n/d |
| Lyric video como producto | **El producto entero** | 1 de varios formatos | Feature #6 de ~10 |

---

## Pricing (con disclaimers de verificación)

### GenLy AI (B2B sello)
100 videos = $900/mo · 250 = $2.000 · 500 = $3.500 · 1.000 = $6.000. Overage +30%. Cada video = full HD + short + thumbnail, ownership 100%. (Fuente: landing/propuesta GenLy.)

### Rotor (confirmado por dogfooding 2026-05-20)
Créditos no expiran: 5=$44.99 ($9/cr) · 10=$79.99 ($8/cr) · 50=$299.99 ($6/cr). **Lyric video = 4 créditos → $24–36 c/u.** Free preview, se paga para descargar (watermark hasta pagar). Enterprise (Plug-In / Account / Affiliate) **detrás de form de ventas**, sin precio público.

### Musixmatch (según su pricing al 2026-05-20 — NO verificado por fuente externa, citar como tal)
- Artistas: Free · Basic $2.49 · Grow $7.99 · Plus $17.99 /mes. Video capado (5/10/20 fondos IA; 0/3/5 full YouTube).
- Labels: Free $0 · Premium escala por # artistas ($399/año @5 artistas → $7.999/año @150). Tabla de features marca **"full YouTube videos: up to 200"**.
- *Disclaimer: la página de pricing de Musixmatch está bloqueada a crawlers; estos números provienen de la web en vivo, no de fuente independiente.*

**Lectura:** a volumen de sello, GenLy ($5–8/video con 3 outputs + ownership + generativo) es competitivo y más barato por unidad que el self-serve de Rotor ($24–36). Rotor gana en precio de ENTRADA y librería; Musixmatch gana en sticker bajo pero capa el full video.

---

## Perfiles

### Rotor (LyricFind) — el competidor real
- **Fortalezas:** ya en UMG/Sony; librería de 9M clips + 150 estilos; transcripción/sync muy buena (probable forced alignment contra letras oficiales de LyricFind = moat de DATA); barato de entrada; multi-formato (Canvas, Album Motion, Music Video); créditos no expiran.
- **Debilidades vs GenLy:** fondo **stock compartido, no generativo** (uniqueness = recombinación, no bespoke); **riesgo de derechos**: el cliente no es dueño del stock y por clips compartidos el video puede recibir reclamos de **ContentID** en YouTube/FB (Rotor declara que no garantiza ni interviene); modelo self-serve por crédito/artista (enterprise opaco, tras form); watermark hasta pagar.

### Musixmatch — competidor de video débil
- **Core = data de letras** (distribución/sync/licensing/royalties). Roadmap 2025-26 todo en rights/data (Music Lens, Sentinel, deals AI con las 3 majors). **Cero inversión nueva en video desde el partnership con Runway (mar-2024).**
- Relación con UMG = vía UMPG (publishing), **no video**. Un deal de video con UMG es territorio abierto.
- Video = add-on social-first, capado, para artista indie. No es producción full-length de sello.

### Resto del landscape
- **Neural Frames / freebeat / CrePal:** generativos, pero **DIY-only, anti-enterprise**. No venden a sellos ni hacen batch.
- **Specterr / Vizzy:** visualizers/espectro, full-length sí pero **sin fondo generativo ni venta a sellos**.
- **LyricVideo.tv:** estudio humano para majors, broadcast, pero **manual, no escalable** por software.
- **Freelancers (Fiverr/Upwork):** $15–300/video, calidad variable, no escala.

---

## Wedge y posicionamiento

**La intersección que nadie ocupa bien:** fondo cinematográfico **generativo por IA** + full 16:9 + auto-sync + **batch a escala de sello** + **ownership 100% sin riesgo de ContentID**.

- Le ganamos a **Rotor** en unicidad del fondo, propiedad del material y seguridad de derechos para releases oficiales.
- Le ganamos a los **generativos DIY** en escala y venta a sello.
- Le ganamos a **Musixmatch** en volumen, foco y formato (full vs social capado).

**Mensaje a usar (NO "nadie sirve a sellos" — es falsable):**
> "Lyric videos cinematográficos generativos a escala de sello — fondo único por canción, 100% tuyo, sin riesgo de ContentID."

**Amenaza principal:** no es la feature, es la **distribución** de Rotor/LyricFind (relación UMG/Sony + data de letras licenciadas). Se compite con velocidad, calidad de fondo y foco.

---

## Acciones derivadas (técnicas/comerciales)
1. **Transcripción:** permitir pegar la letra oficial + forced alignment (no depender solo de Whisper) para igualar la precisión de Rotor en español/repeticiones/chorus.
2. **Pitch UMG:** liderar con ownership + sin ContentID + generativo único; cerrar con volumen (>200/año que Rotor self-serve no batchea y Musixmatch capa).
3. **Validar volumen UMG LATAM** con un contacto para fijar el tamaño del deal.

---

## Evidencia visual
Ver `docs/pitch/benchmark_fondos/benchmark.md` (frames reales GenLy vs Rotor vs Musixmatch).

## Fuentes
- Musixmatch/TPG: Bloomberg Law, BusinessWire, Music Business Worldwide, Wikipedia, Runway news, Billboard, Musically (Sentinel/Music Lens).
- Rotor/LyricFind: rotorvideos.com (incl. /rights), MBW "LyricFind acquires Rotor Videos", Music Week; + dogfooding propio 2026-05-20.
- Mercado: MBW (reporte anual UMG 2024), Luminate/SXSW, Billboard, Flowster.
- Landscape: Specterr, Neural Frames, LyricVideo.tv, Fiverr.
