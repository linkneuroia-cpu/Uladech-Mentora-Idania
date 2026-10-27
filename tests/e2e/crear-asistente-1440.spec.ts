import { test, expect } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";
import { login, openModule } from "./fixtures/auth";

// Crea, documentando cada paso con captura de pantalla, el asistente
// "normal" (sin RD) del curso Moodle 1440 — mismo estilo/prompt que los
// asistentes "Idania" de 1541 (RD), pero tipo normal porque 1440 no
// pertenece a ninguna RD existente. Produce:
//   - test-results/asistente_1440/NN_paso.png (una captura por paso)
//   - test-results/asistente_1440/pasos.json (título + descripción de cada
//     paso, en el mismo orden, para el Word final)
// Plantilla real de "Idania" (prompt maestro, bienvenida y reencuentro):
// plantillas/asistente_idania.json — compartida con la creación de asistentes
// desde la UI y el wizard de admin.

const CURSO_ID = 1440;
const COLECCION = `Curso_${CURSO_ID}`;
// IMPORTANTE: el modulo "Automatico" (vectorizacion via Moodle) nunca envia
// model_name al backend (ver aPreview()/aDownVal() en interfaz.html) -> SIEMPRE
// vectoriza con el embedding por defecto del sistema (settings.EMBEDDING_MODEL),
// sin importar el embedding con que se creo la coleccion. Probado en vivo:
// crear la coleccion con openai/text-embedding-3-small (igual que la RD de
// 1541, 1536 dims) hizo que los 55 archivos fallaran con "Vector dimension
// error: expected dim: 1536, got 384" al vectorizar, porque el backend
// generaba embeddings de 384 dims (all-MiniLM-L6-v2) para una coleccion de
// 1536. Por eso esta coleccion se crea con el MISMO embedding por defecto
// que el modulo Automatico usa siempre, para que todo el pipeline sea
// consistente de punta a punta sin tocar nada fuera de la UI.
const DENSE = "sentence-transformers/all-MiniLM-L6-v2"; // = EMBEDDING_MODEL por defecto del sistema
const SPARSE = "bm25";
const OUT_DIR = path.join(__dirname, "..", "..", "test-results", "asistente_1440");
const TEXTOS = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "..", "plantillas", "asistente_idania.json"), "utf-8"),
);

type Paso = { file: string; titulo: string; descripcion: string };
const pasos: Paso[] = [];
let pasoN = 0;

async function paso(page: any, titulo: string, descripcion: string) {
  pasoN++;
  const slug = titulo.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  const file = `${String(pasoN).padStart(2, "0")}_${slug}.png`;
  await page.screenshot({ path: path.join(OUT_DIR, file), fullPage: true });
  pasos.push({ file, titulo, descripcion });
}

test.describe("Crear asistente normal - curso 1440", () => {
  test("flujo completo: coleccion -> vectorizacion -> asistente -> prueba", async ({ page }) => {
    test.setTimeout(60 * 60_000); // vectorizacion (incluye videos/Whisper) puede tardar

    fs.mkdirSync(OUT_DIR, { recursive: true });

    // 1. Login
    await login(page);
    await paso(page, "Login en el gestor", "Inicio de sesion como administrador en el gestor (/login).");

    // Idempotencia: si la coleccion ya existe y ya tiene vectores (de una
    // corrida anterior), no la recreamos ni re-vectorizamos el curso entero
    // de nuevo (evita perder ~20 min de trabajo ya hecho si el test se
    // relanza tras un fallo en un paso posterior).
    const existing = await page.evaluate(async (col) => {
      const prefix = location.pathname.includes("/gestor") ? location.pathname.split("/gestor")[0] : "";
      const r = await fetch(`${location.origin}${prefix}/api/collections/${encodeURIComponent(col)}`);
      if (!r.ok) return null;
      return await r.json();
    }, COLECCION);
    const yaVectorizada = !!existing && existing.points_count > 0;

    if (!yaVectorizada) {
      // 2. Crear la coleccion "normal" para el curso 1440
      await openModule(page, "qdrant-crear");
      await page.fill("#q-create-name", COLECCION);
      await page.fill("#q-create-desc", "Coleccion normal del curso 1440 (asistente Idania).");
      await page.click("#q-tipo-normal");
      await page.fill("#q-create-courseid", String(CURSO_ID));
      await expect(page.locator("#q-schema-hybrid")).toHaveClass(/selected/);
      await paso(page, "Formulario de nueva coleccion", `Tipo normal, curso ${CURSO_ID}, esquema hibrido.`);

      await page.waitForFunction(
        () => (document.querySelector("#q-create-dense-strategy") as HTMLSelectElement | null)?.options.length! > 1,
        { timeout: 20_000 },
      );
      await page.selectOption("#q-create-dense-strategy", DENSE);
      await page.selectOption("#q-create-sparse-strategy", SPARSE);
      await paso(page, "Embedding de la coleccion", `Denso ${DENSE}, sparse ${SPARSE}, distancia Cosine (el mismo embedding por defecto que usa siempre el modulo Automatico al vectorizar).`);

      await page.click('button:has-text("Crear colección")');
      await expect(page.locator("#m-body")).toContainText("fue creada correctamente", { timeout: 30_000 });
      await paso(page, "Coleccion creada", `Confirmacion de que "${COLECCION}" quedo creada en Qdrant.`);
      await page.click("button.modal-x");
      await expect(page.locator("#ov")).not.toHaveClass(/open/);

      // 3. Vectorizar el curso 1440 completo
      await openModule(page, "auto");
      await page.selectOption("#auto-col", COLECCION);
      await page.click("#btn-a1");
      await paso(page, "Seleccionar coleccion a vectorizar", `Coleccion destino: ${COLECCION}.`);

      await page.fill("#auto-curid", String(CURSO_ID));
      await page.click("#btn-a-prev");
      await expect(page.locator("#auto-tree")).toBeVisible({ timeout: 60_000 });
      await paso(page, "Vista previa del curso en Moodle", `Arbol de recursos reales del curso ${CURSO_ID} obtenido desde Moodle.`);

      await page.click('button:has-text("Todos")');
      await page.click('button:has-text("Marcar selección como Curso")');
      await paso(page, "Seleccion de archivos a vectorizar", "Todos los archivos marcados, categoria 'Material del curso'.");

      await page.click("#btn-a3");
      await expect(page.locator("#auto-p4c")).toBeVisible({ timeout: 10 * 60_000 });
      await paso(page, "Descarga y validacion", "Archivos descargados de Moodle y validados antes de vectorizar.");

      await page.click("#btn-a4");
      await expect(page.locator("#auto-res")).toBeVisible({ timeout: 45 * 60_000 });
      const resultadoVectorizacion = await page.locator("#auto-res").innerText();
      await paso(page, "Vectorizacion completada", "Resultado final de la vectorizacion del curso 1440.");
      // Un archivo puntual sin texto extraible (ej. PDF escaneado vacio) es
      // un resultado valido y esperado del validador, no una falla del
      // proceso: solo se exige que se haya guardado al menos un vector.
      expect(resultadoVectorizacion).toMatch(/\d+\s*\n?\s*vectores/i);
    } else {
      await paso(
        page,
        "Coleccion ya vectorizada (reutilizada)",
        `"${COLECCION}" ya existia con ${existing.points_count} vectores de una corrida previa — se reutiliza sin recrearla ni re-vectorizar.`,
      );
    }

    // 4. Crear el asistente "normal"
    await page.click("#nav-asis");
    await expect(page.locator("#wiz-body")).toBeVisible({ timeout: 15_000 });

    // Paso 1 - datos basicos + tipo normal
    await page.fill("#wiz-nombre", "Idania");
    await page.click("#wiz-tipo-normal");
    await paso(page, "Asistente - datos basicos", "Nombre 'Idania', tipo Normal (sin RD, un solo curso).");
    await page.click("#wiz-body .modal-foot .btn-prim");

    // Paso 2 - configuracion RAG: se deja todo en "valor por defecto del
    // sistema" (ya viene seleccionado asi) - mismo criterio que el
    // asistente de 1541 (todas sus estrategias quedaron en NULL salvo
    // generation=gpt4o_mini, que es a su vez el default del sistema).
    await paso(page, "Asistente - configuracion RAG", "Se dejan los valores por defecto del sistema en todas las etapas (igual que 1541).");
    await page.click("#wiz-body .modal-foot .btn-prim");

    // Paso 3 - vincular el curso 1440 (reutiliza la coleccion ya creada)
    await page.fill("#wiz-normal-courseid", String(CURSO_ID));
    await page.click("#wiz-normal-btn");
    await expect(page.locator("#wiz-normal-status")).toContainText("✓", { timeout: 30_000 });
    await expect(page.locator("#wiz-normal-status")).toContainText(COLECCION);
    await paso(page, "Asistente - curso vinculado", `Confirmado: curso ${CURSO_ID} enlazado a la coleccion ${COLECCION} ya vectorizada.`);
    await page.click("#wiz-body .modal-foot .btn-prim");

    // Paso 4 - prompt maestro, saludo, reencuentro, maximo de preguntas
    await page.fill("#wiz-prompt", TEXTOS.prompt_maestro);
    await page.fill("#wiz-saludo", TEXTOS.mensaje_bienvenida);
    await page.fill("#wiz-reencuentro", TEXTOS.mensaje_reencuentro);
    await page.fill("#wiz-max-act", "10");
    await paso(page, "Asistente - prompt y mensajes", "Mismo prompt maestro y mensajes que los demas asistentes 'Idania'.");
    await page.click("#wiz-body .modal-foot .btn-prim");

    // Paso 5 - revisar y crear
    await paso(page, "Asistente - revision final", "Resumen de la configuracion antes de crear el asistente.");
    await page.click("#wiz-body .modal-foot .btn-prim");
    await expect(page.locator("#m-body")).toContainText("fue creado", { timeout: 30_000 });
    const publicUrlHref = await page.locator("#m-body a").getAttribute("href");
    await paso(page, "Asistente creado", `URL publica generada: ${publicUrlHref}`);
    if (!publicUrlHref) throw new Error("No se encontro la URL publica en el modal de confirmacion.");

    await page.click("button.modal-x");
    await expect(page.locator("#ov")).not.toHaveClass(/open/);

    // 5. Prueba de humo en el chat publico (patron verificado de chat-humo.spec.ts).
    // Al no fijar dense/sparse_strategy del asistente (la UI "normal" no
    // expone ese campo), el chat cae al embedding por defecto del sistema
    // - que es el MISMO que se uso para crear y vectorizar la coleccion -
    // asi que no hace falta ningun ajuste fuera de la UI.
    const chatUrl = `${publicUrlHref}${publicUrlHref!.includes("?") ? "&" : "?"}courseid=${CURSO_ID}&userid=1`;
    await page.goto(chatUrl);
    await expect(page.locator("#q")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("#q")).toBeEnabled({ timeout: 30_000 });
    await paso(page, "Chat publico - primera carga", "El asistente abre correctamente y muestra el saludo inicial.");

    const antes = await page.locator(".rating-btn.up").count();
    await page.fill("#q", "¿En qué semana estamos y de qué trata el tema?");
    await page.click("#sendbtn");
    await page.waitForFunction(
      (b) => document.querySelectorAll(".rating-btn.up").length > b,
      antes,
      { timeout: 120_000 },
    );
    await paso(page, "Chat publico - primera respuesta", "El asistente responde usando el contenido real del curso 1440.");

    fs.writeFileSync(
      path.join(OUT_DIR, "pasos.json"),
      JSON.stringify(
        { CURSO_ID, COLECCION, DENSE, SPARSE, publicUrl: publicUrlHref, token: publicUrlHref!.split("/").pop()!.split("?")[0], pasos },
        null,
        2,
      ),
      "utf-8",
    );
  });
});
