# RUNBOOK — Email deliverability (mails de GenLy cayendo a spam)

Diagnóstico y fix del reporte de cliente (2026-06-02): los mails
transaccionales de GenLy (SMTP vía Google Workspace, dominio **genly.pro**,
`emails.py`) llegan a la carpeta de spam de Gmail.

**Causa raíz**: el DKIM de Google Workspace para genly.pro está roto — la
key publicada en el DNS (GoDaddy) **no coincide** con la key que tiene la
consola de Google Admin, y el estado en Admin es *"No se autentica el correo
electrónico"*. Los mails salen firmados con el dominio genérico
`gappssmtp.com` (no alineado), y como el DMARC de genly.pro es
**`p=quarantine`** (estricto), Gmail los manda a spam.

Relacionado: [`RUNBOOK_UMG.md`](RUNBOOK_UMG.md) (env vars de Railway, incl.
`SMTP_*`), [`RUNBOOK_EMERGENCY.md`](RUNBOOK_EMERGENCY.md).

---

## 1. Diagnóstico (estado encontrado 2026-06-02)

```bash
# SPF — OK ✅ (incluye Google vía el include de GoDaddy)
dig +short TXT genly.pro | grep spf
# "v=spf1 include:dc-aa8e722993._spfm.genly.pro ~all"
dig +short TXT dc-aa8e722993._spfm.genly.pro
# "v=spf1 include:_spf.google.com ~all"

# DMARC — existe, política ESTRICTA ✅⚠️
dig +short TXT _dmarc.genly.pro
# "v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:dmarc_rua@onsecureserver.net;"
# p=quarantine = si la autenticación no alinea → spam. Por eso es crítico
# que DKIM esté sano.

# DKIM de Google Workspace — PUBLICADO PERO ROTO ❌
dig +short TXT google._domainkey.genly.pro
# Devuelve una key "v=DKIM1;k=rsa;p=MIIB..." que NO coincide con la que
# muestra Google Admin Console → Gmail NO firma con genly.pro.
# (Probable causa: alguien regeneró la key en la consola y nunca se
#  actualizó el DNS.)

# MX — OK ✅ (Google Workspace)
dig +short MX genly.pro

# DNS hosteado en GoDaddy:
dig +short NS genly.pro
# ns71.domaincontrol.com / ns72.domaincontrol.com
```

> Nota histórica: `genly.ai` NO es dominio de correo (no existe
> `noreply@genly.ai`). El default viejo de `SMTP_FROM` en `emails.py`
> apuntaba ahí — corregido a `noreply@genly.pro` en el mismo PR que este
> runbook. En Railway, `SMTP_FROM` debe ser una dirección @genly.pro.

---

## 2. Fix DNS (manual, ~10 min + propagación)

### 2.1 Re-sincronizar la key DKIM (P0 — esto es el fix)

1. [Google Admin Console](https://admin.google.com) → **Apps → Google
   Workspace → Gmail → Autenticar correo electrónico**.
2. Dominio seleccionado: **genly.pro**.
3. Copiar el **valor completo del registro TXT** que muestra la consola
   (`v=DKIM1; k=rsa; p=MIIB...`).
   ⚠️ **NO apretar "Generar nuevo registro"** — eso rota la key y
   desincroniza de nuevo el DNS.
4. En **GoDaddy** → DNS de genly.pro → editar el registro TXT existente
   con nombre **`google._domainkey`** → reemplazar el valor por el copiado
   en el paso 3.
5. Esperar propagación (normalmente < 1 h, hasta 48 h) y verificar:

```bash
dig +short TXT google._domainkey.genly.pro
# Debe coincidir EXACTAMENTE con lo que muestra la consola de Google.
```

6. Volver a la consola de Google → **"INICIAR LA AUTENTICACIÓN"**.
   El estado debe pasar a "Se está autenticando el correo electrónico".

### 2.2 SPF y DMARC

Ya están bien configurados para genly.pro. **No tocar.**

Si en algún momento se cambia de proveedor de envío (ej. SendGrid/Resend
para mail transaccional), agregar su include al SPF y su selector DKIM.

---

## 3. Fix de código (ya en `emails.py`)

PR `fix/email-deliverability`:

- Todos los mails salen como `multipart/alternative` con parte
  **texto plano + HTML** (HTML-only es señal clásica de spam).
- Headers `Date` y `Message-ID` (RFC 5322) en todos los envíos, con el
  `Message-ID` alineado al dominio del From.
- Default de `SMTP_FROM` corregido: `noreply@genly.pro` (antes apuntaba a
  genly.ai, que no es dominio de correo).
- Tests en `tests/test_emails.py`.

**Verificar en Railway** que `SMTP_FROM` esté seteado a una dirección
@genly.pro que sea el `SMTP_USER` o un alias registrado de esa cuenta
(Gmail reescribe el From si no es un alias verificado).

---

## 4. Verificación end-to-end

1. Mandarse un mail real (registro de cuenta de prueba en prod, o trigger
   de password reset).
2. En Gmail: abrir el mail → ⋮ → **Mostrar original**. Verificar:

```
SPF:    PASS  con dominio genly.pro
DKIM:   PASS  con dominio genly.pro       ← clave: genly.pro, NO gappssmtp.com
DMARC:  PASS
```

3. Test externo independiente: <https://www.mail-tester.com> — crear un
   usuario de prueba con el mail que da la página y disparar un welcome
   email. Objetivo: score ≥ 9/10.
4. Registrar el dominio en
   [Google Postmaster Tools](https://postmaster.google.com) para monitorear
   reputación y tasa de spam en el tiempo.

### Si el cliente sigue viendo spam después del fix

- Los mails ya marcados como spam entrenan al filtro: pedirle al cliente que
  marque **"No es spam"** en 1–2 mails — eso re-entrena su filtro personal.
- Pedirle al admin de su Workspace que agregue `genly.pro` a la allowlist de
  remitentes (Admin Console → Gmail → Spam, phishing y malware).
- Revisar reportes DMARC (`rua=`) por si hay otro origen enviando como
  genly.pro y quemando la reputación del dominio.

---

## 5. Resend (alertas de ops)

Los scripts de preflight (`uptime_ping.py`, `daily_smoke.py`) mandan
alertas vía Resend con `RESEND_FROM=noreply@genly.pro`. Resend tiene su
propio selector DKIM (`resend._domainkey.genly.pro`) ya publicado ✅ y el
SPF/DMARC del dominio lo cubren. No requiere cambios.
