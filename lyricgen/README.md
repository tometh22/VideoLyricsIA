# LyricGen

Aplicación web que automatiza la creación de lyric videos a partir de archivos MP3.

Genera automáticamente:
- **Lyric Video** Full HD (1920x1080) con letra sincronizada
- **YouTube Short** vertical (1080x1920) de 30 segundos del fragmento más energético
- **Thumbnail** JPG (1280x720) con nombre de artista y canción

## Requisitos previos

- Python 3.10+
- Node.js 18+
- FFmpeg instalado y disponible en PATH (`sudo apt install ffmpeg` / `brew install ffmpeg`)

## Instalación

### Backend

```bash
cd backend
pip install -r requirements.txt
```

> La primera vez que proceses un archivo, Whisper descargará el modelo "base" (~150 MB).

### Frontend

```bash
cd frontend
npm install
```

## Videos de fondo

Coloca archivos MP4 de video loop en:

```
assets/backgrounds/
  oscuro.mp4
  neon.mp4
  minimal.mp4
  calido.mp4
```

Si no se encuentra el fondo del estilo seleccionado, se usará `oscuro.mp4` como fallback.
Si tampoco existe, se generará un fondo negro sólido.

## Cómo correr

### Backend (puerto 8000)

```bash
cd backend
uvicorn main:app --reload
```

### Frontend (puerto 5173)

```bash
cd frontend
npm run dev
```

Abre **http://localhost:5173** en tu navegador.

## API

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/upload` | Sube MP3 + artista + estilo. Devuelve `{ job_id }` |
| GET | `/status/{job_id}` | Estado del job (processing/done/error) |
| GET | `/download/{job_id}/{file_type}` | Descarga archivo (video/short/thumbnail) |

## Notas

- Los jobs se almacenan en memoria (no hay base de datos).
- El procesamiento corre en un thread por job.
- Todo funciona offline después de la instalación inicial.
