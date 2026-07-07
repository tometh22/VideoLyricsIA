# Runbook: Configurar OAuth de YouTube (conexión de canales self-service)

Guía paso a paso para el operador. Al terminar, cualquier tenant puede
conectar sus canales de YouTube desde Ajustes → YouTube sin tocar el
servidor.

## 1. Proyecto de Google Cloud

Entrá a https://console.cloud.google.com y seleccioná el **mismo proyecto**
que usa el token actual de YouTube (así el token legacy y el flujo nuevo
comparten la cuota diaria). Si no existe, creá uno.

## 2. Habilitar las APIs

**APIs & Services → Library**:
- Buscar **"YouTube Data API v3"** → Enable.
- Buscar **"YouTube Analytics API"** → Enable (la usa la fase de analytics;
  habilitarla ahora evita otro trámite después).

## 3. Pantalla de consentimiento OAuth

**APIs & Services → OAuth consent screen**:
1. User type: **External** (los dueños de canales no están en tu Workspace).
2. App name: ej. `Genly Publisher`. Support email y developer contact: tu email.
3. App domain + links de **Privacy Policy** y **Terms of Service** (obligatorios
   para publicar la app después — usá las páginas del sitio de Genly).

## 4. Scopes

En la misma pantalla → **Add or remove scopes** → pegar estos tres:

```
https://www.googleapis.com/auth/youtube.upload
https://www.googleapis.com/auth/youtube.readonly
https://www.googleapis.com/auth/yt-analytics.readonly
```

Son scopes **sensibles**: van a requerir verificación de Google (paso 6).

## 5. Test users (mientras la app esté en "Testing")

**⚠️ CRÍTICO**: en modo Testing, los refresh tokens **vencen a los 7 días**
— las conexiones se rompen solas cada semana. Sirve para desarrollo, es
inaceptable en producción.

Mientras tanto: agregá como **Test users** las cuentas de Google de cada
dueño de canal que vaya a conectar durante las pruebas (límite 100).

## 6. Publicar la app + verificación de Google (ARRANCAR YA)

**OAuth consent screen → Publish app → In production.**

Como los scopes son sensibles, Google exige **verificación**:
- Privacy policy accesible en el dominio de la app.
- Dominio verificado en **Google Search Console**.
- Un **video demo** corto del flujo OAuth (pantalla de consentimiento
  incluida) subido a YouTube (puede ser unlisted).
- Justificación de por qué se necesita `youtube.upload`.

Tarda **días a semanas** — iniciarlo cuanto antes. Hasta que se apruebe,
los usuarios ven una advertencia "app no verificada" (pueden continuar
vía "Advanced") y sigue aplicando el límite de 100 usuarios.

## 7. Crear las credenciales OAuth

**APIs & Services → Credentials → Create credentials → OAuth client ID**:
1. Application type: **Web application**. Nombre: `Genly API`.
2. **Authorized redirect URIs** — agregar EXACTAMENTE (esquema, host y path):
   - `https://<dominio-de-la-api>/youtube/oauth/callback` (producción)
   - `http://localhost:8000/youtube/oauth/callback` (desarrollo local)
   No hace falta "Authorized JavaScript origins" (el flujo es server-side).
3. Guardar → copiar **Client ID** y **Client Secret**.

## 8. Variables de entorno (Railway — servicio API y workers)

```
YOUTUBE_OAUTH_CLIENT_ID=<client id del paso 7>
YOUTUBE_OAUTH_CLIENT_SECRET=<client secret del paso 7>
YOUTUBE_OAUTH_REDIRECT_URI=https://<dominio-de-la-api>/youtube/oauth/callback
TOKEN_ENCRYPTION_KEY=<generar abajo>
FRONTEND_URL=https://<dominio-del-frontend>   # ya debería existir
```

Generar la clave de cifrado (una sola vez):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**⚠️ Guardá una copia de `TOKEN_ENCRYPTION_KEY` en el password manager.**
Si se pierde, todos los canales tienen que reconectarse. Para rotarla, ver
`backend/token_crypto.py` (soporta rotación sin downtime con
`TOKEN_ENCRYPTION_KEYS_OLD` + `scripts/rotate_token_keys.py`).

## 9. Cuota de la API

La cuota default es **10.000 unidades/día por proyecto** y cada subida de
video cuesta ~1.600 → **~6 videos/día**. Para volumen real:

**APIs & Services → YouTube Data API v3 → Quotas** → solicitar aumento vía
el **YouTube API Services compliance audit form** (linkeado desde esa
página). El trámite puede tardar semanas — iniciarlo junto con el paso 6.

## 10. Verificación final

1. Deploy con las env configuradas.
2. Ajustes → YouTube → "Conectar canal" → consentimiento de Google →
   volver a Ajustes con el canal listado.
3. Publicar un video de prueba en **No Listado** desde un job aprobado.

## Troubleshooting

| Síntoma | Causa probable |
|---|---|
| `redirect_uri_mismatch` en Google | La redirect URI del paso 7 no coincide EXACTAMENTE con `YOUTUBE_OAUTH_REDIRECT_URI` |
| "La conexión con YouTube no está configurada" (503) | Faltan `YOUTUBE_OAUTH_CLIENT_ID/SECRET` en el servicio |
| Canal pasa a "Requiere reconexión" cada ~7 días | La app OAuth sigue en modo Testing (paso 6) |
| `access_denied` al volver de Google | El usuario canceló, o su cuenta no está en Test users (modo Testing) |
| Subidas fallan con `quotaExceeded` | Cuota diaria agotada (paso 9); resetea a medianoche hora del Pacífico |
