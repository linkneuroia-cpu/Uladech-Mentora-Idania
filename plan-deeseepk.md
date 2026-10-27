# Plan para DeepSeek — Terminar la integración F1 ↔ Moodle (mod_secureurl / plugin idanIA)

> Documento autocontenido; no asume que leíste conversaciones previas.
> - Repo local: `C:\Users\MIGUEL\Desktop\Proyectos\Proyectos uladech\Funcionalidad 1\vectorization_v2-main`
> - Remoto git: `idania` → `https://github.com/linkneuroia-cpu/Uladech-Mentora-Idania.git`
>   (ramas `main`=producción, `develop`=preproducción — ver `deploy/README.md`)
> - Esta tarea **ya está implementada y probada en local**. Lo que falta es
>   verificación manual end-to-end, configurar el entorno real de destino,
>   y subir el commit — **no desplegar sin que Miguel lo confirme**.

## Objetivo de la tarea (contexto, ya resuelto)

F1 (`/f1/login` para el panel del docente, `/f1/asistente/{token}` para el
chat del alumno) confiaba en `username`/`userid`/`courseid` tal cual
llegaban en la URL — cualquiera podía escribirlos a mano y suplantar a
otra persona. El plugin idanIA de Moodle (mod_secureurl) ahora manda en su
lugar un **token cifrado AES-256-GCM** (formato `SU1.<kid>.<nonce>.<datos>`)
con `scope` (`"gestor"` para el login del docente, `"activity"` para el
chat), `sub`, `course`, `jti`, `iat`/`exp` (5 min) y los parámetros
elegidos en Moodle dentro de `p` (hoy: `p.username`+`p.userid` para el
gestor, `p.userid`+`p.courseid` para el chat).

**Decisión ya tomada con Miguel** (no repreguntarla): se confía **solo**
en `https://moodle5.linkneuroia.com` (el Moodle de prueba donde se está
validando el plugin) — **no** en `campus.uladech.edu.pe` (producción
real). El token es obligatorio de inmediato; se retiró por completo el
flujo viejo (sin período de transición).

## Qué ya está hecho (no repetir, solo verificar)

### Backend
- **`secureurl.py`** (nuevo, raíz del repo): copia tal cual de la
  librería de `Reporte de seguimiento_web/app/auth/secureurl.py`, con un
  único agregado mínimo: `issue_token()` acepta un parámetro opcional
  `extra: Mapping[str, Any]` para poder incluir `scope`/`course` de nivel
  superior al generar tokens de prueba (esos campos ya los lee
  `verify_token()` vía `claims.raw`, solo que el `issue_token()` original
  no tenía forma de agregarlos). No se tocó nada de la verificación.
- **`settings.py`**: nuevos campos `SECUREURL_APP_ID` (default `"f1"`),
  `SECUREURL_KEYS`, `MOODLE_FRAME_ANCESTOR` (default
  `"https://moodle5.linkneuroia.com"`), `SESSION_SAMESITE` (default
  `"lax"`), `SESSION_HTTPS_ONLY` (default `False`). Se **retiró**
  `DOCENTE_LOGIN_TOKEN` (quedaba un bypass temporal del 21-oct, ya
  reemplazado por este mecanismo real).
- **`auth.py`**: `verify_secureurl(token, *, scope, course=None)` —
  verifica el token, exige que `scope` coincida, si se pasa `course`
  exige que coincida también, y rechaza el reuso de `jti` (`JtiCache` en
  memoria, un solo proceso). Cualquier falla → el mismo
  `HTTPException(403, "Enlace no válido o caducado. Vuelve a abrirlo desde Moodle.")`,
  nunca se distingue el motivo hacia afuera.
- **`app.py`**:
  - Nuevo middleware `security_headers_mw`: agrega
    `Content-Security-Policy: frame-ancestors {MOODLE_FRAME_ANCESTOR}` y
    `Permissions-Policy` (micrófono/portapapeles permitidos para ese
    origen) en **todas** las respuestas; y una verificación mínima de
    CSRF por `Origin` en `POST/PUT/PATCH/DELETE` (si viene `Origin` y no
    es el propio de F1, 403).
  - `SessionMiddleware` ahora usa `settings.SESSION_SAMESITE`/
    `SESSION_HTTPS_ONLY` en vez de `"lax"` fijo.
  - `GET /login`: si hay `?token=`, se verifica con `scope="gestor"` y
    `username`/`userid` se toman del token (nunca de la URL); sin token,
    sigue el formulario de admin de siempre, sin cambios.
  - `GET /asistente/{token}`: el modo prueba (botón "Probar chat" del
    gestor) sigue igual que antes (no usa secureurl). El flujo real
    (alumno/docente desde Moodle) ahora exige `?token=` (scope="activity",
    y si el asistente es de un solo curso, se exige que `course` del
    token coincida con el curso del asistente), lo canjea una sola vez por
    una entrada en `request.session[f"chat_{token}"]`, y redirige (303) a
    la misma URL sin el token. Se quitaron por completo los parámetros
    `userid`/`documento`/`nombre` (ya no hay acceso anónimo).
  - Todos los endpoints hijos del chat (`/mensaje` ya no necesitaba
    cambios — validaba por `sesion_id`, no por courseid/userid de la URL;
    `/nueva-conversacion`, `/conversaciones`, `/conversaciones/{id}`,
    `/mensajes/{id}/calificar`, `/conversaciones/{id}/nombre`,
    `/conversaciones/{id}/carpeta`, `/carpetas` GET/POST,
    `/carpetas/{id}` DELETE) ya NO aceptan `courseid`/`userid` por
    query/body — los toman de `request.session[f"chat_{token}"]` vía el
    helper `_chat_identidad(request, token)`. Esto cierra un hueco real:
    antes cualquiera podía pedir `?courseid=X&userid=Y` con los IDs de
    otro alumno y leer su conversación.
  - Se eliminó `_userid_from_documento` (ya sin uso) y el import de
    `zlib` que solo ella usaba.

### Frontend
- **`frontend/asistente_chat.html`**: se eliminó por completo el bloque
  de identificación manual (`necesita_identificacion`, formulario de
  documento+nombre, y su CSS `.ident-*`) — ya no existe ningún camino sin
  token. Se quitaron `COURSEID`/`USERID` del JS y de todos los `fetch()`
  (ya no hace falta mandarlos: el backend los toma de la sesión).
- **`frontend/interfaz.html`**: el texto de ayuda en Administración →
  Usuarios ya no muestra el patrón de enlace viejo
  (`?username=...&token=<DOCENTE_LOGIN_TOKEN>&userid=...`); ahora explica
  que el acceso lo genera la actividad del plugin idanIA en Moodle.

### Pruebas
- **`tests/test_secureurl.py`** (nuevo, 20 pruebas): token válido de
  gestor, scope equivocado en cada ruta, token vencido/alterado/`aud`
  distinto, curso que no coincide, reuso de `jti`, `GET /login` sin
  token muestra el formulario normal, `GET /asistente/{token}` sin token
  → 403, con valores de URL distintos a los del token (gana el token),
  falta `p.courseid` → 400, cabecera CSP presente, configuración de
  `SessionMiddleware` (`same_site`/`https_only` vienen de `settings`),
  CSRF por `Origin`.
- **`tests/test_prueba_chat.py`**: se actualizó
  `test_prueba_sin_sesion_del_gestor_se_ignora` (sin sesión del gestor y
  sin token, ahora da 403, no 200 con un formulario — es el cambio de
  comportamiento esperado, no una regresión).
- **Suite completa:** `pytest tests/ --ignore=tests/e2e -q` → **61/61 en
  verde** (verificado antes de este hand-off).

### `.env` local
Se agregó al `.env` local (gitignorado, no tocar el de ningún servidor
real desde este archivo): `SECUREURL_APP_ID=f1`, `SECUREURL_KEYS=<el
valor real que Miguel ya dio en el chat — está en el .env local, no lo
repitas en ningún commit ni documento>`, `MOODLE_FRAME_ANCESTOR` (el
default ya alcanza).

## Qué falta (esto es lo que DeepSeek debe ejecutar)

### 1. Verificación manual end-to-end (recomendado antes de subir)

**Ojo con un problema real que ya apareció**: puede haber un proceso
`python app.py` **viejo** de una sesión anterior todavía corriendo en el
puerto 8100 en esta misma máquina (verificar con
`netstat -ano | findstr :8100` en PowerShell, o `netstat -ano | grep :8100`
en Git Bash). Si lo hay, **no lo mates sin confirmar con Miguel** qué es —
simplemente levanta tu propia instancia de verificación en otro puerto:
```bash
API_PORT=8101 python app.py
```
Con el servidor arriba (en el puerto que corresponda), generar tokens de
prueba con la clave real ya puesta en `.env` y probar:
```bash
python -c "
import base64, secureurl
key = base64.urlsafe_b64decode('<SECUREURL_KEYS sin el prefijo de versión, solo la clave>' + '==')
tok_gestor = secureurl.issue_token('f1', 1, key, {'username':'32919741','userid':14}, extra={'scope':'gestor'})
tok_activity = secureurl.issue_token('f1', 1, key, {'userid':8421,'courseid':1440}, extra={'scope':'activity','course':1440})
print(tok_gestor)
print(tok_activity)
"
```
Luego, contra el puerto donde quede el servidor de verificación:
- `GET /login` (sin token) → 200, formulario normal.
- `GET /login?token=<tok_activity>` → 403 (scope equivocado).
- `GET /login?token=<tok_gestor>` → 303 hacia `/gestor` sin `token` en la
  URL (puede fallar en 503/sin-cursos si el docente/ERP mockeado no
  existe en esta base — es esperable en local; lo importante es que NO
  dé 403 por el token en sí).
- Revisar las cabeceras de la respuesta: debe traer
  `Content-Security-Policy: frame-ancestors https://moodle5.linkneuroia.com`.
- Con un asistente real existente en la base, repetir la prueba en
  `/asistente/{token_real}?token=<tok_activity con ese mismo course>`.

### 2. Confirmar el entorno real de destino

`MOODLE_FRAME_ANCESTOR` apunta a `https://moodle5.linkneuroia.com`, que
ya es el Moodle de prueba asociado al entorno de **preproducción**
existente (`80.190.77.138`, ver `docker-compose.preprod.yml` y
`deploy/.env.deploy.preprod`, ya en este repo). Es razonable asumir que
el destino de este cambio es ESE entorno, pero **no está confirmado
explícitamente por Miguel en esta tarea** — pregúntale antes de tocar
cualquier `.env` de un servidor real. **No** se toca el `.env` de
producción real (`10.0.0.92`) como parte de esto: el plan decidió
confiar solo en moodle5, no en `campus.uladech.edu.pe`.

### 3. Variables de entorno en el servidor real (una vez confirmado el destino)

En el `.env` de ESE servidor (nunca en el repo, nunca en un commit):
```
SECUREURL_APP_ID=f1
SECUREURL_KEYS=<el mismo valor que Miguel ya dio — pedírselo de nuevo si no está a mano, no inventarlo>
MOODLE_FRAME_ANCESTOR=https://moodle5.linkneuroia.com
SESSION_SAMESITE=none
SESSION_HTTPS_ONLY=true
```
`SESSION_SAMESITE=none` exige `SESSION_HTTPS_ONLY=true` (el navegador
descarta la cookie si no es `Secure`) — ese servidor debe estar detrás de
HTTPS real para que esto funcione (confirmar antes de aplicar).

### 4. Commit y push (seguir el flujo ya establecido en este repo)

Ver `deploy/README.md` para el detalle completo del flujo de ramas. En
resumen:
```bash
git checkout develop
git pull idania develop   # por si hay algo nuevo
git checkout -b fix/secureurl-moodle-idania
git add -A
git status   # confirmar que NO aparece ningún .env ni secreto
git commit -m "Integra F1 con mod_secureurl (plugin idanIA): login y chat verifican token cifrado de Moodle en vez de confiar en la URL"
git checkout develop
git merge --ff-only fix/secureurl-moodle-idania
git checkout main
git merge --ff-only develop
git push idania main develop fix/secureurl-moodle-idania
```
Antes del `git add -A`, correr `git status` y confirmar que **no** se
cuela ningún archivo de credenciales — `.env`, `Server uladech.txt`,
`Servidor linkneuroia.txt` ya están en `.gitignore`, pero conviene
revisar de nuevo dado que se tocaron `.env`/`settings.py` en esta tarea.

### 5. NO desplegar sin autorización explícita

No corras `deploy/deploy_git.sh` (ni ningún otro script de despliegue)
contra ningún servidor real como parte de esto. Eso lo decide Miguel por
separado, una vez que confirmen el punto 2.

## Pendientes fuera de este repo (informativo, no ejecutable desde acá)

- La actividad/filtro del plugin idanIA debe estar configurada en
  `moodle5.linkneuroia.com` apuntando a `https://.../f1/login` y
  `https://.../f1/asistente/{token}` con los parámetros `username`+
  `userid` (gestor) y `userid`+`courseid` (chat) — eso lo configura un
  administrador de Moodle, no este repo.
- Si más adelante se decide confiar también en `campus.uladech.edu.pe`
  (producción real), hay que agregarlo a `MOODLE_FRAME_ANCESTOR` (hoy
  solo acepta un origen — si se necesitan varios, ese campo pasaría a
  ser una lista, y el middleware `security_headers_mw` tendría que
  elegir o permitir varios `frame-ancestors`. No implementado todavía,
  a propósito).

## Checklist final
- [ ] Verificación manual end-to-end (punto 1) hecha y documentada.
- [ ] Confirmado con Miguel el servidor real de destino.
- [ ] Variables de entorno puestas en el `.env` de ESE servidor (no en
      el repo).
- [ ] Commit creado siguiendo el flujo de ramas, sin secretos.
- [ ] Push a `idania` (`main`, `develop` y la rama de la tarea).
- [ ] Ningún despliegue real ejecutado sin que Miguel lo pida.
