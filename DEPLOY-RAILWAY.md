# Subir el MCP de TimelinesAI a Railway (modo solo lectura)

Al terminar tendrás una URL tipo `https://timelines-mcp-production.up.railway.app/mcp`
que puedes conectar desde Claude en web, móvil o escritorio, protegida con un
secreto propio.

**Modo:** solo lectura, y con el envío de mensajes apagado por separado. Claude
podrá leer y analizar el inbox, pero no mandarle nada a nadie.

---

## Antes de empezar

**1. Consigue el token de TimelinesAI**

En el dashboard de TimelinesAI, en los ajustes de API/integraciones. Empieza con
`tla_`. Si puedes crear uno dedicado para esto en vez de reusar el que ya tienes
en otro lado, mejor: así lo revocas sin romper nada más.

> A diferencia de Stripe, TimelinesAI no tiene tokens con permisos parciales.
> El token da acceso a todo el workspace: todas las conversaciones, con teléfonos
> y contenido. Por eso aquí las rejas del servidor son la única protección — y
> por eso el default es solo lectura.

**2. Genera el secreto que protegerá la URL**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Guarda esa cadena. Es la contraseña de tu endpoint. El servidor se niega a
arrancar si falta o si tiene menos de 32 caracteres.

---

## Paso 1 — Sube el código a GitHub

Desde `~/Desktop/Pushit/Timelines-mcp-railway`:

```bash
cd ~/Desktop/Pushit/Timelines-mcp-railway
git init
git add timelines_mcp.py requirements.txt Procfile railway.json .gitignore DEPLOY-RAILWAY.md README.md
git commit -m "TimelinesAI MCP server"
```

Crea un repo **privado** en GitHub y súbelo:

```bash
git remote add origin https://github.com/TU-USUARIO/timelines-mcp.git
git branch -M main
git push -u origin main
```

---

## Paso 2 — Crea el servicio en Railway

1. Railway → tu proyecto → **New** → **GitHub Repo**
2. Elige `timelines-mcp`
3. El primer arranque va a fallar porque faltan las variables. Es esperado.

---

## Paso 3 — Configura las variables

En el servicio → pestaña **Variables** → **Raw Editor** → pega esto:

```
TIMELINES_MCP_TRANSPORT=http
TIMELINES_API_TOKEN=PEGA_AQUI_TU_TOKEN
MCP_AUTH_TOKEN=el_secreto_que_generaste
TIMELINES_READ_ONLY=1
TIMELINES_ALLOW_SEND=0
```

`PORT` lo inyecta Railway solo, no la agregues. Sin comillas, sin espacios
alrededor del `=`.

> Las dos últimas son redundantes (ya son el default en HTTP), pero déjalas
> escritas para que sea explícito y nadie las quite por accidente.

Railway redespliega solo al guardar.

---

## Paso 4 — Genera la URL pública

Servicio → **Settings** → **Networking** → **Generate Domain**.

---

## Paso 5 — Verifica que está vivo y protegido

```bash
# 1. Health check (sin auth, debe responder "ok")
curl https://TU-DOMINIO.up.railway.app/healthz

# 2. Sin secreto: DEBE dar 404. Si da otra cosa, no sigas.
curl -i -X POST https://TU-DOMINIO.up.railway.app/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 3. Con secreto: debe listar 12 herramientas
curl -X POST https://TU-DOMINIO.up.railway.app/s/TU_MCP_AUTH_TOKEN/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

En los logs de Railway debes ver al arrancar:

```
[timelines-mcp] streamable-http on 0.0.0.0:XXXX  token=set  read_only=True  allow_send=False  sending_enabled=False
```

Si dice `sending_enabled=True`, **detente** — alguien abrió las dos rejas. El
servidor además imprime un `WARNING` explícito en ese caso.

---

## Paso 6 — Conéctalo a Claude

Los custom connectors de Claude **no aceptan un header fijo**: si el servidor
responde 401, Claude arranca un flujo OAuth que este servidor no implementa.
Por eso el secreto va en la ruta.

En Claude → Settings → **Connectors** → **Add custom connector**:

- **Nombre:** TimelinesAI
- **URL:** `https://TU-DOMINIO.up.railway.app/s/TU_MCP_AUTH_TOKEN/mcp`

Deja vacíos Client ID y Client Secret.

> ### ⚠️ La URL tiene que terminar exactamente en `/mcp`
>
> Si se corta al pegar —y se corta más seguido de lo que parece, porque es
> larguísima— Claude recibe un 404, asume que necesita autenticarse, se va a
> buscar un servicio de OAuth que no existe, y te muestra:
>
> *"Couldn't register with the sign-in service"*
>
> Ese error habla de OAuth pero casi siempre son dos caracteres perdidos.
> Después de pegar, haz clic al final del campo y presiona `End`: si lo último
> que ves es `/mcp`, quedó bien.

**Cuida esa URL como una contraseña.** Da acceso de lectura a todas las
conversaciones de WhatsApp del equipo. Compártela por gestor de contraseñas.

---

## Paso 7 — Primera prueba

Pídele a Claude, en este orden:

1. *"Corre timelines_whoami"* → debe decir `authenticated: true`, el workspace, y
   `sending_effectively_enabled: false`.
2. *"¿Cómo está el inbox?"* → usa `timelines_activity_summary`: sin leer, abiertos,
   por responsable, por etiqueta.
3. *"¿Qué chats están sin responder?"* → `timelines_list_chats` con `read=false`.

Si algo responde 403, corre `timelines_discover` — te dice qué endpoints alcanza
tu plan.

---

## Si algún día quieres que sí envíe

Necesitas **las dos** variables: `TIMELINES_READ_ONLY=0` y
`TIMELINES_ALLOW_SEND=1`. Y aun así cada envío exige `confirm=true`, con la
instrucción de mostrarte antes el destinatario y el texto exactos.

Piénsalo con calma. Un mensaje mal mandado a un cliente no se borra, y la API
manda desde la cuenta conectada más recientemente si no le especificas cuál —
así que un mensaje puede salir desde el número equivocado. Si lo habilitas,
pon siempre `whatsapp_account_phone` explícito.

---

## Si algo falla

**El deploy no arranca** → si dice `MCP_AUTH_TOKEN is required` o `is only N
characters`, falta la variable o es muy corta. Es a propósito.

**"Couldn't register with the sign-in service"** → la URL del connector. Revisa
que termine en `/mcp`.

**`timelines_whoami` dice `authenticated: false`** → token mal pegado, revocado,
o el plan no incluye la API pública. Pruébalo directo:

```bash
curl https://app.timelines.ai/integrations/api/workspace \
  -H "Authorization: Bearer TU_TOKEN"
```

**Un envío responde 200 pero el mensaje no llega** → la API a veces contesta 200
con `status:"error"` dentro. Este servidor lo detecta y lo reporta como error;
el `message` del cuerpo dice por qué (créditos agotados, número no conectado).

**Los filtros no devuelven nada** → `label` y `responsible` son coincidencia
exacta. Lista sin filtros primero y copia los valores tal cual salen.

**Las respuestas salen truncadas** → el tamaño de página está fijo en 50 y la API
ignora cualquier intento de bajarlo. Filtra más, o pide `fields` con solo las
claves que te interesan.
