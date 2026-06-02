# RUNBOOK — Email deliverability (mails de GenLy cayendo a spam)

Diagnóstico y fix del reporte de cliente (2026-06-02): los mails
transaccionales de GenLy (`noreply@genly.ai` vía SMTP, `emails.py`)
llegan a la carpeta de spam de Gmail.

**Causa raíz**: el dominio `genly.ai` envía sin firma DKIM alineada y sin
política DMARC. Desde febrero 2024 Google exige SPF/DKIM alineados + DMARC
para entregar a inboxes de Gmail/Workspace; sin eso, el destino típico es
spam — sobre todo en clientes corporativos con filtros estrictos.

Relacionado: [`RUNBOOK_UMG.md`](RUNBOOK_UMG.md) (env vars de Railway, incl.
`SMTP_*`), [`RUNBOOK_EMERGENCY.md`](RUNBOOK_EMERGENCY.md).

---

## 1. Diagnóstico (estado encontrado 2026-06-02)

```bash
# SPF — solo incluye Firebase (los IPs de Google entran de rebote,
# porque _spf.firebasemail.com a su vez incluye _spf.google.com)
dig +short TXT genly.ai | grep spf
# "v=spf1 include:_spf.firebasemail.com ~all"

# DMARC — NO EXISTE ❌
dig +short TXT _dmarc.genly.ai
# (vacío)

# DKIM de Google Workspace — NO EXISTE ❌
# (solo están firebase1/firebase2, que firman los mails de Firebase Auth,
#  no los del backend)
dig +short TXT google._domainkey.genly.ai
# (vacío)

# MX — OK ✅ (Google Workspace)
dig +short MX genly.ai
```

Consecuencia: los mails del backend salen firmados con el dominio genérico
`*.gappssmtp.com` (firma DKIM **no alineada** con genly.ai) y sin DMARC →
spam.

---

## 2. Fix DNS (manual, ~15 min + propagación)

> Los tres pasos se hacen en el panel DNS de `genly.ai` y en Google Admin.
> Orden recomendado: DKIM primero (es lo que más pesa), después DMARC,
> después SPF.

### 2.1 Activar DKIM en Google Workspace (P0)

1. [Google Admin Console](https://admin.google.com) → **Apps → Google
   Workspace → Gmail → Autenticar correo electrónico**.
2. Seleccionar dominio `genly.ai` → **Generar nuevo registro** (2048 bits,
   selector `google`).
3. Publicar en el DNS de `genly.ai` el registro TXT que muestra la consola:
   - Host: `google._domainkey`
   - Valor: `v=DKIM1; k=rsa; p=MIIBIjANBg...` (el que genere la consola)
4. Esperar propagación (puede tardar hasta 48 h, normalmente < 1 h) y volver
   a la consola → **Iniciar autenticación**.

```bash
# Verificar
dig +short TXT google._domainkey.genly.ai
# Esperado: "v=DKIM1; k=rsa; p=MIIB..."
```

### 2.2 Publicar DMARC (P0)

Registro TXT en el DNS de `genly.ai`:

- Host: `_dmarc`
- Valor:

```
v=DMARC1; p=none; rua=mailto:tomas@epical.digital; adkim=r; aspf=r
```

`p=none` = solo monitorear (no afecta entrega, pero cumple el requisito de
Google y empieza a mandar reportes). **Después de 1–2 semanas** de reportes
limpios, subir a `p=quarantine`:

```
v=DMARC1; p=quarantine; rua=mailto:tomas@epical.digital; adkim=r; aspf=r
```

```bash
# Verificar
dig +short TXT _dmarc.genly.ai
```

### 2.3 Reforzar SPF (P1)

Reemplazar el TXT actual de SPF de `genly.ai` por uno que incluya a Google
de forma **directa** (hoy depende de que Firebase no cambie su SPF):

```
v=spf1 include:_spf.google.com include:_spf.firebasemail.com ~all
```

> ⚠️ Un dominio solo puede tener **UN** registro `v=spf1`. Editar el
> existente, no agregar otro. Los otros TXT (google-site-verification,
> firebase=...) no se tocan.

```bash
# Verificar
dig +short TXT genly.ai | grep spf
```

---

## 3. Fix de código (ya en `emails.py`)

PR `fix/email-deliverability`:

- Todos los mails salen como `multipart/alternative` con parte
  **texto plano + HTML** (HTML-only es señal clásica de spam).
- Headers `Date` y `Message-ID` (RFC 5322) en todos los envíos, con el
  `Message-ID` alineado al dominio del From.
- Tests en `tests/test_emails.py`.

---

## 4. Verificación end-to-end

1. Mandarse un mail real (registro de cuenta de prueba en prod, o trigger
   de password reset).
2. En Gmail: abrir el mail → ⋮ → **Mostrar original**. Verificar:

```
SPF:    PASS  con dominio genly.ai
DKIM:   PASS  con dominio genly.ai        ← clave: genly.ai, NO gappssmtp.com
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
- Pedirle al admin de su Workspace que agregue `genly.ai` a la allowlist de
  remitentes (Admin Console → Gmail → Spam, phishing y malware).
- Revisar reportes DMARC (`rua=`) por si hay otro origen enviando como
  genly.ai y quemando la reputación del dominio.

---

## 5. Nota sobre genly.pro (alertas de ops vía Resend)

`genly.pro` (usado por los scripts de preflight con Resend) ya tiene SPF +
DKIM (`resend._domainkey`) + DMARC `p=quarantine` ✅. No requiere cambios.
