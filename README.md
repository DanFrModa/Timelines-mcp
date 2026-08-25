# TimelinesAI MCP Server

Servidor MCP (Model Context Protocol) que expone la API pública de TimelinesAI
—el inbox de WhatsApp para equipos— a Claude. Pensado para desplegarse en
Railway en **modo solo lectura**.

👉 **Los pasos de despliegue están en [DEPLOY-RAILWAY.md](./DEPLOY-RAILWAY.md).**

---

## Qué hace

Da a Claude 12 herramientas para leer y operar el inbox: chats, mensajes,
etiquetas, responsables, números conectados y equipo — más una herramienta
genérica, una de descubrimiento, y un resumen agregado del inbox.

| Herramienta | Endpoint |
|---|---|
| `timelines_whoami` | verifica el token, el workspace y las rejas |
| `timelines_request` | cualquier endpoint, cualquier método |
| `timelines_discover` | sondea rutas y reporta cuáles existen |
| `timelines_list_chats` | `GET /chats` con todos los filtros |
| `timelines_get_chat` | `GET /chats/{id}` |
| `timelines_list_messages` | `GET /chats/{id}/messages` |
| `timelines_send_message` | `POST /messages` o `/chats/{id}/messages` |
| `timelines_update_chat` | `PATCH /chats/{id}` |
| `timelines_manage_labels` | `GET/POST/PUT /chats/{id}/labels` |
| `timelines_list_whatsapp_accounts` | `GET /whatsapp_accounts` |
| `timelines_list_teammates` | `GET /workspace/teammates` |
| `timelines_activity_summary` | pagina `/chats` y cuenta todo (50 por página) |

---

## Variables de entorno

| Variable | Requerida | Default | Descripción |
|---|---|---|---|
| `TIMELINES_API_TOKEN` | sí | — | Token de la API (`tla_...`) |
| `TIMELINES_MCP_TRANSPORT` | en Railway | `stdio` | `http` para servidor remoto |
| `MCP_AUTH_TOKEN` | si `http` | — | Secreto que protege el endpoint. Mínimo 32 caracteres |
| `TIMELINES_READ_ONLY` | no | ver abajo | `1` bloquea toda escritura |
| `TIMELINES_ALLOW_SEND` | no | `0` | Reja aparte: **enviar mensajes de WhatsApp** |
| `TIMELINES_API_BASE` | no | `https://app.timelines.ai/integrations/api` | Para apuntar a otro host |
| `TIMELINES_MAX_CHARS` | no | `20000` | Truncado de respuestas |
| `TIMELINES_TIMEOUT` | no | `45` | Timeout en segundos |
| `PORT` | no | `8000` | Railway lo inyecta solo |

---

## Las tres rejas

Este MCP habla con personas reales. Un mensaje enviado por WhatsApp llega al
teléfono de alguien en segundos y **no se puede deshacer**. Por eso hay tres
candados independientes.

**1. `TIMELINES_READ_ONLY` — el default depende del transporte**

- **`stdio` (local):** escrituras **permitidas** por default.
- **`http` (remoto):** escrituras **bloqueadas** por default.

Olvidar la variable en un despliegue público lo deja en solo lectura.

**2. `TIMELINES_ALLOW_SEND` — la reja de envío**

Apagada por default en **los dos** transportes, incluso en local. Aunque
habilites las escrituras, enviar mensajes sigue bloqueado hasta que pongas
`TIMELINES_ALLOW_SEND=1`.

La razón es la asimetría: cambiar una etiqueta, reasignar un chat o cerrarlo son
acciones internas y reversibles. Mandar un WhatsApp a un cliente no lo es. No
tiene sentido que compartan el mismo interruptor.

**3. `confirm=true` — la reja por llamada**

Todo envío exige `confirm=true` además de lo anterior, igual que borrar un
archivo, reconfigurar un webhook o revocar el acceso de un compañero. La
instrucción de la herramienta es explícita: primero muéstrale al usuario el
destinatario exacto y el texto exacto, y solo con su visto bueno se confirma.

Cada rechazo dice **cuál** de las tres rejas lo detuvo.

---

## Autenticación del endpoint

El protocolo MCP no trae autenticación propia. En modo `http`, este servidor
exige `Authorization: Bearer <MCP_AUTH_TOKEN>` en cada request, o el secreto
embebido en la ruta (`/s/<secreto>/mcp`) para los connectors de Claude.
`/healthz` es la única ruta pública.

El servidor **se niega a arrancar** si `MCP_AUTH_TOKEN` falta o tiene menos de
32 caracteres.

---

## Correr en local

```bash
pip install -r requirements.txt

# stdio (para Claude Desktop)
TIMELINES_API_TOKEN=tla_xxx python timelines_mcp.py

# http (como en Railway)
TIMELINES_MCP_TRANSPORT=http \
TIMELINES_API_TOKEN=tla_xxx \
MCP_AUTH_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))") \
PORT=8000 python timelines_mcp.py
```

Al arrancar imprime en qué modo quedó:

```
[timelines-mcp] streamable-http on 0.0.0.0:8000  token=set  read_only=True  allow_send=False  sending_enabled=False
```

---

## Notas sobre la API de TimelinesAI

Verificado contra la referencia pública (`https://timelines.ai/docs/public-api-reference/overview`):

- **Base:** `https://app.timelines.ai/integrations/api`, auth `Authorization: Bearer <tla_...>`.
- **Los cuerpos van en JSON**, no form-encoded.
- **Las respuestas vienen envueltas:** `{"status":"ok","data":{...}}`. Y hay
  fallos que llegan con **HTTP 200 pero `status:"error"`** — este servidor los
  trata como error, no como éxito, porque si no un envío fallido se leería como
  enviado.
- **Los errores traen detalle por campo:** `{"status":"error","message":...,
  "error_code":...,"errors":[{"fields":["phone"],"msg":"..."}]}`. Se muestran
  tal cual en el mensaje de error.
- **Los filtros de varios valores van separados por coma** en un solo parámetro
  (`label=vip,enterprise`), no repetidos ni con corchetes. Pasar una lista de
  Python produce esa forma.
- **El tamaño de página es fijo en 50 y no se puede cambiar.** Verificado
  contra la API en vivo el 2026-08-25: `limit`, `per_page`, `page_size`, `size`,
  `count`, `take` y `rows` se ignoran todos, y cada página llega con 50
  registros. El único parámetro que hace algo es `page`, y `has_more_pages` en
  la respuesta dice si hay otra. Por eso las herramientas no exponen un
  `per_page`: sería un parámetro que aparenta ajustar y no ajusta nada.
- **Para reducir el tamaño de una respuesta**, entonces, no queda bajar la
  página: hay que filtrar más, o usar `fields` para quedarte solo con las claves
  que necesitas.
- **Los teléfonos van en formato internacional** con `+`: `+5215512345678`. El
  modelo lo valida antes de salir a la red y limpia espacios y guiones.
- **`text` tiene tope de 2000 caracteres**; las etiquetas 64, los nombres de
  chat 256.
- **Si omites `whatsapp_account_phone`**, TimelinesAI manda desde la cuenta
  conectada más recientemente — que rara vez es la que el usuario tiene en
  mente. Con más de un número conectado, conviene ser explícito.
- **Los envíos se espacian ~2 segundos** entre uno y otro por política de
  WhatsApp, y cada mensaje consume créditos (1 texto, 2 con adjunto; los
  fallidos se reembolsan).
- **No hay endpoint de agregación.** Por eso `timelines_activity_summary`
  pagina y cuenta del lado del servidor MCP, y avisa con `complete=false`
  cuando el tope de páginas cortó el conteo.

---

## Seguridad

- Los secretos van en variables de entorno, nunca en el código. El `.gitignore`
  bloquea archivos `.env`.
- **Un token de TimelinesAI da acceso a todo el workspace**: todas las
  conversaciones de WhatsApp del equipo, con sus teléfonos y su contenido. Es
  información de clientes reales — trátala como tal.
- Un solo token compartido significa cero trazabilidad por persona.
- Para cortar el acceso de golpe: revoca el token en el dashboard de
  TimelinesAI — el servidor queda inútil al instante.
