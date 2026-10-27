# Plan para DeepSeek — El asistente no reconoce el "Examen de la unidad" (curso 1440)

> Documento autocontenido; no asume que leíste conversaciones previas.
> - Repo: `C:\Users\MIGUEL\Desktop\Proyectos\Proyectos uladech\Funcionalidad 1\vectorization_v2-main`
> - Asistente afectado: id 22, curso Moodle 1440, colección `Curso_1440`.
>   Chat:
>   `https://educaia.uladech.edu.pe/f1/asistente/4ZfUNgarx3I2aJMc9Kn_ApelsixvgFwt?courseid=1440&userid=8421`
>   (8421 es la cuenta de prueba institucional).

## Reglas

- No hacer commit ni push salvo que Miguel lo pida.
- No copiar contraseñas ni tokens en archivos.
- No tocar `Server uladech.txt`.
- No revertir los cambios que ya están en el working tree.
- Descargas y consultas a Moodle: siempre con el token de webservice.
  Nunca iniciar sesión en Moodle con usuario y contraseña.
- **Las actividades ocultas para alumnos (`visible=0`) NO se muestran.**
  Es decisión de Miguel.
- Mantener el estilo del código: los comentarios explican el **porqué**
  (bug real, caso real). No dejar código muerto.

---

## 1. Qué pasó (evidencia real)

### Conversación

Sesión 159. Se obtuvo con el endpoint público
`GET /asistente/<token>/conversaciones/159?courseid=1440&userid=8421`.

| Fecha | Pregunta | Respuesta del asistente | Problema |
|---|---|---|---|
| 15-oct | "que contenido hay para esta semana?" | Tabla correcta de la Semana 7, con enlaces reales | — (entra por el intent `CURRENT_WEEK`) |
| 21-oct | "Ayudame con el caso para la semana 8" | Habla de la tabla de contenido del documento "Criterios…" de la semana 7 | Ignora la pregunta y retoma un tema viejo |
| 21-oct | "Que archivos puedo revisar para le semana 8?" | Tabla con "SESIÓN DE APRENDIZAJE N.° 8" y "Evaluación de la Unidad I" | Enlaces de la **semana 7** (`id=60298`, `id=78837`); "Evaluación de la Unidad I" es el **nombre de la sección**, no un documento; **omite el examen** |

### Lo que hay realmente en Moodle

Curso 1440, sección 8: "Semana 08. Evaluación de la Unidad I." Fuente:
`core_course_get_contents`.

| cmid | Tipo | Nombre | Visible | Fechas |
|---|---|---|---|---|
| 60300 | folder | SESIÓN DE APRENDIZAJE N.° 8 | sí | — |
| 80849 | resource | Examen de unidad | **no** (oculto) | — |
| 60301 | assign | **U1-Ex-Examen sumativo** | sí | Apertura / Cierre |
| 60302 | label | Unidad II: Diseña un recurso o experiencia digital… | sí | — |

El curso empezó el 1 de septiembre, así que el 21 de octubre corresponde a
la semana 8: la semana del examen.

## 2. Causas (en orden de impacto)

1. **El Query Router no reconoce "semana N + qué archivos/qué hay".**
   - La evidencia correcta ya existe: `query_router._format_section_contents_evidence`
     (cerca de la línea 419) devuelve la tabla real de una sección.
   - Pero solo se llega a ella de dos formas:
     - con `CURRENT_WEEK`, que exige "esta semana" o "semana actual";
     - con `NAVIGATION`, que exige un verbo de pedido de `_NAVIGATION_RE`
       ("dame", "envíame", "dónde está"…) **y** una palabra de
       `_SECTION_CONTENT_WORDS_RE`.
   - `_SECTION_CONTENT_WORDS_RE` no incluye "archivos", "actividades",
     "tareas", "evaluación" ni "examen".
   - `_CURRENT_WEEK_TOPIC_RE` excluye a propósito "semana + número".
   - Resultado: "¿qué archivos puedo revisar para la semana 8?" cae al RAG.
2. **El RAG no puede conocer el examen.**
   - "U1-Ex-Examen sumativo" es una tarea sin archivo, así que no hay nada
     que vectorizar.
   - "Examen de unidad" está oculto.
   - Solo el Course Map tiene esa información.
3. **El historial contamina la respuesta.**
   - `assistants.get_recent_history` (cerca de la línea 725) manda al LLM
     hasta 30 pares de la sesión, **sin límite de tiempo**. Esta sesión
     abarca del 15 al 21 de octubre.
   - Sin evidencia de la semana 8, el LLM copió el formato y **los
     enlaces** de la tabla de la semana 7 de un turno anterior.
   - En el turno 933 retomó una pregunta vieja.
4. **No se validan los enlaces generados.**
   - `rag_pipeline.answer_query` (cerca de la línea 688) no revisa si las
     URLs de la respuesta estaban en la evidencia del turno.
5. **El vocabulario de exámenes es rígido.**
   - `moodle_tools._MODNAME_WORDS` mapea "examen" → `quiz`, pero en este
     curso el examen es una tarea (`assign`).
   - `find_activity` no entiende que "U1" es "Unidad I" ni que "Ex" es
     "examen".
   - `_DUE_DATES_RE` no cubre "¿cuándo es el examen?".
6. **Las fechas se pierden.**
   - `moodle_calendar` solo funciona con la BD de Moodle (preproducción) o
     con un WS de calendario. En producción queda `unavailable`.
   - `core_course_get_contents` sí trae `dates` (Apertura/Cierre) por
     módulo, pero `course_map._fetch_course_map_from_moodle` (cerca de la
     línea 103) no las guarda.
7. **Ocultos y etiquetas.**
   - `_format_section_contents_evidence` lista también las actividades con
     `visible=0` y las etiquetas (`label`).

---

## 3. Cambios

### A. `course_map.py`: guardar fechas

- Agregar a `Activity` el campo `dates: List[Dict[str, Any]] = field(default_factory=list)`.
  - Se llena con `m.get("dates")` en el formato `[{label, timestamp}]`,
    normalizando `label` sin los dos puntos: `Apertura`, `Cierre`,
    `Vencimiento`.
- `CourseMap.from_json` debe tolerar cachés viejos que no tienen `dates`
  (valor por defecto `[]`).
- Invalidar el caché viejo: agregar una constante `CACHE_VERSION` dentro
  de `map_json`, de modo que, si no coincide, se vuelva a pedir a Moodle.
  Así no hace falta ninguna migración de base de datos.
- Helper `activity_due(a) -> Optional[int]`: devuelve el timestamp de
  `Cierre` o `Vencimiento`.

### B. `query_router.py`

**1. Nuevo intent `SECTION_CONTENTS`**

- Se activa cuando la pregunta tiene una referencia a una semana **y** una
  palabra de contenido.
- Referencias a semana:
  - `semana\s*(\d+)` → esa semana.
  - "próxima semana" o "semana que viene" → semana actual + 1.
  - "semana pasada" o "semana anterior" → semana actual − 1.
  - La semana actual se obtiene con `cm.get_current_week_number`.
- Palabras de contenido, en un nuevo `_SECTION_ASK_RE`: `archivos?`,
  `materiales?`, `recursos?`, `documentos?`, `actividades?`, `tareas?`,
  `evaluaci[oó]n(es)?`, `ex[aá]men(es)?`, `lecturas?`, `contenidos?`,
  `qu[eé]\s+hay`, `qu[eé]\s+(puedo\s+)?revis(o|ar)`, `qu[eé]\s+toca`,
  `qu[eé]\s+(debo|tengo\s+que)\s+(hacer|revisar|estudiar)`.
- Responde con `_format_section_contents_evidence(course_id, N)`.
- Va en `_classify` **después** de `DUE_DATES`, `CURRENT_WEEK`,
  `PROGRESS`, `GRADES`, `EVALUATION_CRITERIA` y `STUDY_HELP`, y **antes**
  de `TEACHER_INFO` y `NAVIGATION`.
- Pertenece a la funcionalidad **navegación**: agregarlo al conjunto de
  intents de navegación que se apagan con `func_navegacion`.

**2. Nuevo intent `ASSESSMENTS`**

- Se activa con `ex[aá]men|evaluaci[oó]n\s+(de\s+(la\s+)?)?unidad|sumativ[oa]|parcial|final`.
  Debe ganar a `SECTION_CONTENTS` solo si **no** hay una referencia a una
  semana.
- Busca en el Course Map las actividades `assign`, `quiz` o `workshop`
  visibles cuyo nombre o sección coincida, usando la normalización de
  alias del punto C.
- Si la pregunta nombra una unidad (`unidad\s+([ivx\d]+)`), filtra por
  esa unidad: en el nombre (`U1`) o en el nombre de la sección
  ("Evaluación de la Unidad I").
- Devuelve una tabla Nombre · Tipo · Semana · Apertura · Cierre · Enlace.
  Las fechas se toman de `Activity.dates` y se formatean con
  `cm._format_date_es`.
- Si no encuentra nada: "En el aula no hay un examen visible para esa
  unidad todavía" (texto fijo). **No** cae al RAG.
- Pertenece a la funcionalidad **navegación**.

**3. `_format_section_contents_evidence`**

- Excluir las actividades con `visible=False` o `uservisible=False`, y las
  de tipo `label`.
- Agregar una columna **Fechas** ("Abre dd/mm · Cierra dd/mm") cuando
  alguna actividad de la sección tenga `dates`.
- Mantener la instrucción de "lista completa y real" y agregar: "No uses
  enlaces de otras semanas ni del historial".

**4. `_DUE_DATES_RE` y su respuesta**

- Agregar a la regex:
  - `cu[aá]ndo\s+es\s+(el|la)\s+(ex[aá]men|evaluaci[oó]n)`
  - `cu[aá]ndo\s+(abre|cierra|se\s+cierra)`
  - `hasta\s+cu[aá]ndo`
- En `_format_due_dates_evidence`: si `moodle_calendar.get_due_dates`
  devuelve `unavailable` o `not_found`, usar como respaldo las fechas de
  `Activity.dates` del Course Map (solo actividades visibles). La fuente
  sigue siendo Moodle real; **nunca** inventar fechas.

### C. `moodle_tools.py`

- `_MODNAME_WORDS`: `"examen"` y `"evaluacion"` deben mapear a
  `{"assign","quiz"}`. Cambiar el valor a un conjunto y ajustar su uso en
  `find_first_activity`.
- En `_content_words`, normalizar alias antes de comparar, en ambos lados
  (consulta y nombre de la actividad):
  - `\bu\s*-?\s*(\d+)\b` → `unidad <n>`;
  - romanos ↔ arábigos para unidad (`i` → `1`, `ii` → `2`… hasta `x`);
  - `\bex\b` → `examen`;
  - `\bef\b` → `evaluacion formativa`;
  - `\baf\b` → `actividad formativa`.

  Con esto, "examen de la unidad I" coincide con "U1-Ex-Examen sumativo".
- `find_activity`: ignorar las actividades ocultas (`visible=False`) y las
  etiquetas.

### D. Historial acotado

Archivos: `assistants.py` y `app.py`.

- `get_recent_history(sesion_id, max_pairs=6, max_age_hours=6)`: agregar
  a la consulta SQL `AND created_at >= now() - interval '<max_age_hours> hours'`
  (pasarlo como parámetro).
- En `/asistente/{token}/mensaje`, llamar con esos valores por defecto.
- El saludo de reencuentro no cambia. Solo cambia lo que se manda al LLM.

### E. Guardia de enlaces (`rag_pipeline.py`)

- Después de `generator.generate(...)`:
  1. Extraer las URLs del campus de la respuesta con la regex
     `https?://campus\.uladech\.edu\.pe/[^\s)\]]+`.
  2. Comprobar si cada URL aparece en el texto o en el payload de algún
     candidato del turno (`candidates`).
  3. Si no aparece:
     - en un enlace markdown `[texto](url)`, dejar solo `texto`;
     - si la URL está suelta, quitarla.
  4. Registrar las URLs quitadas en `config_used["urls_removidas"]`.
- En `SYSTEM_PROMPT_TEMPLATE`, agregar la regla: "Usa solo enlaces que
  aparezcan en la evidencia de esta pregunta; nunca copies enlaces de
  respuestas anteriores."

### F. Que el nombre de una sección no se convierta en "documento" (`rag_pipeline.py`)

**Por qué apareció "Evaluación de la Unidad I":**

- Es el título real de la sección 8 en Moodle: "Semana 08. Evaluación de la
  Unidad I."
- Al vectorizar, cada fragmento guarda ese título como `section_name`
  (`core.py`, cerca de la línea 437: `week_label = resource.get("section_name")`).
- `build_generation_prompt` (cerca de la línea 371) presenta cada fragmento
  así:
  `[Fuente: <archivo>, semana/sección: Semana 08. Evaluación de la Unidad I., fragmento N]`.
  El modelo tomó ese rótulo y lo listó como una fila "Documento".
- Los enlaces no estaban en ningún fragmento, porque los fragmentos no
  llevan URL. El modelo imitó la tabla "Nombre | Tipo | Enlace" de la
  semana 7 que estaba en el historial y copió sus enlaces **por posición**:
  - fila 1 → `60298` (carpeta de la sesión 7);
  - fila 2 → `78837` (Guía N.° 7).

**Cambio:**

- En `build_generation_prompt`, cambiar el rótulo `semana/sección: X` por
  `sección de Moodle (no es un documento): X`.
- En `SYSTEM_PROMPT_TEMPLATE`, agregar: "Las tablas Nombre/Tipo/Enlace solo
  se arman con la evidencia «Estructura real del curso (Moodle)». Nunca
  listes como documento el nombre de una sección."

**Prueba:**

- Con candidatos RAG cuyo `section_name` es "Semana 08. Evaluación de la
  Unidad I.", el prompt generado contiene el rótulo nuevo y ya no contiene
  `semana/sección:`.

---

## 4. Pruebas

### Nuevo `tests/test_query_router_examenes.py`

Usar un Course Map simulado (mock de `cm.get_course_map`) con la sección 8
real de la tabla de arriba, incluido el recurso oculto 80849 y la etiqueta.

| Pregunta | Intent esperado | Debe contener | No debe contener |
|---|---|---|---|
| "Que archivos puedo revisar para le semana 8?" | `SECTION_CONTENTS` (8) | 60300, 60301, fechas | 80849, la etiqueta, `id=60298`, `id=78837` |
| "Ayudame con el caso para la semana 8" | `SECTION_CONTENTS` (8) o `STUDY_HELP`; documentar cuál y por qué | contenido real de la semana 8 | contenido de la semana 7 |
| "¿cuándo es el examen de la unidad?" | `DUE_DATES` o `ASSESSMENTS` | "U1-Ex-Examen sumativo" con su cierre real | — |
| "dame el examen de la unidad I" | `ASSESSMENTS` | enlace a 60301 | — |
| "¿qué examen hay esta semana?" | `CURRENT_WEEK` (sin regresión), con la tabla de la semana actual | — | — |
| `func_navegacion=False` + "Que archivos puedo revisar para la semana 8?" | mensaje fijo de funcionalidad deshabilitada | — | — |

### Otras pruebas

- **Guardia de enlaces:**
  - con una respuesta que tiene `[x](https://campus.uladech.edu.pe/mod/resource/view.php?id=78837)`
    y candidatos sin esa URL, queda `x` y la URL aparece en
    `urls_removidas`;
  - si la URL sí está en los candidatos, se mantiene.
- **`get_recent_history`:** con mensajes de hace 7 días y de hace 10
  minutos, solo vuelven los recientes, con un máximo de 6 pares.
- **Alias:** `_content_words("U1-Ex-Examen sumativo")` y
  `_content_words("examen de la unidad I")` comparten `examen` y `unidad`
  / `1`.
- **Regresión:**
  - todas las pruebas existentes del router y del pipeline (`tests/`);
  - las 157 consultas reales usadas antes para validar regresiones del
    router, si están en `tests/eval/`: la proporción de intents no debe
    cambiar salvo en los casos nuevos (documentar las diferencias).

## 5. Despliegue y verificación en producción

1. Ejecutar `bash deploy/upload.sh && bash deploy/deploy.sh`. Este cambio
   no tiene migraciones de base de datos; el caché de Course Map se
   invalida solo con `CACHE_VERSION`.
2. Abrir el chat con una **sesión nueva**: modo prueba desde el panel del
   docente, o el botón "Nueva conversación".
3. Preguntar "Que archivos puedo revisar para la semana 8?". Debe salir una
   tabla con:
   - SESIÓN DE APRENDIZAJE N.° 8 (`id=60300`);
   - U1-Ex-Examen sumativo (`id=60301`) con apertura y cierre;
   - nada de "Examen de unidad" (oculto) ni enlaces de la semana 7.
4. Preguntar "¿Cuándo es el examen de la unidad?". Debe dar la fecha de
   cierre real de la tarea 60301.
5. Informar a Miguel:
   - el resultado de las pruebas;
   - cualquier diferencia de intents en la regresión;
   - capturas de las dos respuestas.

## Checklist

- [ ] "semana N + archivos/qué hay" responde con la tabla real del Course
      Map.
- [ ] Los exámenes (`assign`/`quiz`) se reconocen por nombre y por unidad,
      con fechas reales.
- [ ] No se muestran actividades ocultas ni etiquetas.
- [ ] El historial que se manda al LLM está acotado: 6 h y 6 pares.
- [ ] No quedan enlaces del campus que no estén en la evidencia del turno.
- [ ] El nombre de una sección ya no aparece como "documento" (rótulo
      nuevo en el prompt).
- [ ] Las pruebas nuevas y las existentes pasan.
- [ ] No se hizo commit ni push sin pedido de Miguel.
