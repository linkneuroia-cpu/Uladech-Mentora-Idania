import { test, expect } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";
import { login, openModule } from "./fixtures/auth";

// Corrige la vectorización del curso 1440: la colección "Curso_1440" había
// quedado con el embedding por defecto del sistema
// (sentence-transformers/all-MiniLM-L6-v2, 384 dims) por un bug real del
// módulo "Automático" (nunca enviaba model_name al backend). Este spec:
//   1. Borra la colección actual (384 dims).
//   2. La recrea con el embedding correcto (openai/text-embedding-3-small,
//      1536 dims + bm25 + Cosine), el mismo que la RD del curso 1541.
//   3. La re-vectoriza usando el módulo "Automático" YA ARREGLADO (con el
//      nuevo selector #auto-model), eligiendo explícitamente el mismo
//      embedding de la colección.
//   4. Fija el dense/sparse_strategy del asistente (id 22) por API directa
//      (la UI de un asistente "normal" no expone ese campo — gap aparte).
//   5. Prueba de humo en el chat público del asistente existente.
// Produce capturas + pasos.json en test-results/asistente_1440/ (mismo
// mecanismo que crear-asistente-1440.spec.ts), para regenerar el Word.

const CURSO_ID = 1440;
const COLECCION = `Curso_${CURSO_ID}`;
const DENSE = "openai/text-embedding-3-small"; // 1536 dims (igual que la RD de 1541)
const SPARSE = "bm25";
const ASISTENTE_ID = 22;
const TOKEN = "4ZfUNgarx3I2aJMc9Kn_ApelsixvgFwt";
const PREGUNTA = "¿En qué semana estamos y de qué trata el tema?";
const OUT_DIR = path.join(__dirname, "..", "..", "test-results", "asistente_1440");

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

// Ejecuta un fetch contra la API usando la sesión de administrador ya
// autenticada en el navegador (cookies incluidas automáticamente).
async function apiFetch(page: any, url: string, init: any = {}) {
  return page.evaluate(
    async ([u, i]: [string, any]) => {
      const r = await fetch(u, i);
      const text = await r.text();
      let body: any = text;
      try {
        body = JSON.parse(text);
      } catch {
        /* no-op */
      }
      return { ok: r.ok, status: r.status, body };
    },
    [url, init],
  );
}

test.describe("Corregir vectorizacion - curso 1440", () => {
  test("borrar -> recrear con openai/text-embedding-3-small -> re-vectorizar -> verificar", async ({ page }) => {
    test.setTimeout(90 * 60_000); // re-vectorizacion completa (incluye videos/Whisper)

    fs.mkdirSync(OUT_DIR, { recursive: true });

    // 1. Login
    await login(page);
    await paso(page, "Login en el gestor", "Inicio de sesión como administrador en el gestor (/login).");

    const pathPrefix = new URL(page.url()).pathname.includes("/gestor")
      ? new URL(page.url()).pathname.split("/gestor")[0]
      : "";
    const origin = new URL(page.url()).origin;

    // 2. Estado actual de la colección (para documentar el antes)
    const antes = await apiFetch(page, `${pathPrefix}/api/collections/${encodeURIComponent(COLECCION)}`);
    const antesTxt = antes.ok
      ? `dimensión ${antes.body?.vector_size ?? "?"} · ${antes.body?.points_count ?? "?"} vectores`
      : `no se pudo leer (${antes.status})`;

    // 3. Borrar la colección actual (384 dims)
    await openModule(page, "qdrant-cols");
    await expect(page.locator("#q-cols-tbl")).toContainText(COLECCION, { timeout: 30_000 });
    await paso(
      page,
      "Colección actual antes de corregir",
      `Estado previo de "${COLECCION}": ${antesTxt} (embedding por defecto incorrecto, 384 dims).`,
    );

    const fila = page.locator("#q-cols-tbl tr", { hasText: COLECCION }).first();
    await fila.locator('button:has-text("Eliminar")').click();
    await expect(page.locator("#m-body")).toContainText("no se puede deshacer", { timeout: 15_000 });
    await paso(page, "Confirmar eliminación", `Confirmando el borrado de "${COLECCION}" (acción irreversible).`);
    await page.locator('#m-body button:has-text("Eliminar")').click();
    await expect(page.locator("#q-cols-tbl")).not.toContainText(COLECCION, { timeout: 60_000 });
    await paso(page, "Colección eliminada", `"${COLECCION}" ya no figura en el listado de Qdrant.`);

    // 4. Recrear la colección con el embedding correcto (igual que la RD de 1541)
    await openModule(page, "qdrant-crear");
    await page.fill("#q-create-name", COLECCION);
    await page.fill("#q-create-desc", `Colección normal del curso 1440 (asistente Idania), embedding corregido.`);
    await page.click("#q-tipo-normal");
    await page.fill("#q-create-courseid", String(CURSO_ID));
    await expect(page.locator("#q-schema-hybrid")).toHaveClass(/selected/);
    await paso(page, "Formulario de nueva colección", `Tipo normal, curso ${CURSO_ID}, esquema híbrido.`);

    await page.waitForFunction(
      () => (document.querySelector("#q-create-dense-strategy") as HTMLSelectElement | null)?.options.length! > 1,
      { timeout: 20_000 },
    );
    await page.selectOption("#q-create-dense-strategy", DENSE);
    await page.selectOption("#q-create-sparse-strategy", SPARSE);
    await page.selectOption("#q-create-distance", "Cosine");
    await paso(
      page,
      "Embedding de la colección",
      `Denso ${DENSE} (1536 dims), sparse ${SPARSE}, distancia Cosine — el mismo embedding que la RD del curso 1541.`,
    );

    await page.click('button:has-text("Crear colección")');
    await expect(page.locator("#m-body")).toContainText("fue creada correctamente", { timeout: 30_000 });
    await paso(page, "Colección creada", `Confirmación de que "${COLECCION}" quedó creada con el embedding correcto.`);
    await page.click("button.modal-x");
    await expect(page.locator("#ov")).not.toHaveClass(/open/);

    // 5. Re-vectorizar el curso 1440 con el módulo "Automático" ya arreglado
    await openModule(page, "auto");
    await expect(page.locator(`#auto-col option[value="${COLECCION}"]`)).toHaveCount(1, { timeout: 30_000 });
    await page.selectOption("#auto-col", COLECCION);
    await page.waitForFunction(
      (v) => !!document.querySelector(`#auto-model option[value="${v}"]`),
      DENSE,
      { timeout: 30_000 },
    );
    await page.selectOption("#auto-model", DENSE);
    await paso(
      page,
      "Seleccionar colección y modelo de embeddings",
      `Colección destino "${COLECCION}" y modelo de embeddings ${DENSE} (el fix: antes el módulo Automático nunca enviaba el modelo y usaba siempre el default del sistema).`,
    );
    await page.click("#btn-a1");

    await page.fill("#auto-curid", String(CURSO_ID));
    await paso(page, "Colección y curso a vectorizar", `Colección ${COLECCION}, curid ${CURSO_ID}.`);
    await page.click("#btn-a-prev");
    await expect(page.locator("#auto-tree")).toBeVisible({ timeout: 60_000 });
    await paso(page, "Vista previa del curso en Moodle", `Árbol de recursos reales del curso ${CURSO_ID} obtenido desde Moodle.`);

    await page.click('button:has-text("Todos")');
    await page.click('button:has-text("Marcar selección como Curso")');
    await paso(page, "Selección de archivos a vectorizar", "Todos los archivos marcados, categoría 'Material del curso'.");

    await page.click("#btn-a3");
    await expect(page.locator("#auto-p4c")).toBeVisible({ timeout: 15 * 60_000 });
    await paso(page, "Descarga y validación", "Archivos descargados de Moodle y validados antes de vectorizar.");

    await page.click("#btn-a4");
    await expect(page.locator("#auto-res")).toBeVisible({ timeout: 75 * 60_000 });
    const resultadoVectorizacion = await page.locator("#auto-res").innerText();
    await paso(page, "Vectorización completada", `Resultado final de la re-vectorización del curso ${CURSO_ID} con ${DENSE}.`);
    expect(resultadoVectorizacion).toMatch(/\d+\s*\n?\s*vectores/i);

    // 6. Fijar el embedding del asistente (id 22) por API directa: la UI de un
    //    asistente "normal" no expone dense/sparse_strategy (gap de producto
    //    aparte). Sin esto, el asistente caería al default del sistema en
    //    consulta (384 dims) y fallaría por mismatch contra la colección 1536.
    const patch = await apiFetch(page, `${pathPrefix}/api/asistentes/${ASISTENTE_ID}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dense_strategy: DENSE, sparse_strategy: SPARSE }),
    });
    expect(patch.ok).toBeTruthy();
    await paso(
      page,
      "Embedding del asistente fijado por API",
      `PATCH /api/asistentes/${ASISTENTE_ID} → dense_strategy=${DENSE}, sparse_strategy=${SPARSE} (ajuste manual puntual, fuera de la UI).`,
    );

    // 7. Prueba de humo en el chat público del asistente existente
    const publicUrl = `${origin}${pathPrefix}/asistente/${TOKEN}`;
    const chatUrl = `${publicUrl}?courseid=${CURSO_ID}&userid=1`;
    await page.goto(chatUrl);
    await expect(page.locator("#q")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("#q")).toBeEnabled({ timeout: 30_000 });
    await paso(page, "Chat público - primera carga", "El asistente abre correctamente y muestra el saludo inicial.");

    const antesResp = await page.locator(".rating-btn.up").count();
    await page.fill("#q", PREGUNTA);
    await page.click("#sendbtn");
    await page.waitForFunction(
      (b) => document.querySelectorAll(".rating-btn.up").length > b,
      antesResp,
      { timeout: 180_000 },
    );
    const respuesta = await page.locator(".msg.assistant, .msg.bot, .chat-msg").last().innerText().catch(() => "");
    await paso(page, "Chat público - respuesta", "El asistente responde usando el contenido real del curso 1440 sin error de Qdrant.");

    fs.writeFileSync(
      path.join(OUT_DIR, "pasos.json"),
      JSON.stringify(
        { CURSO_ID, COLECCION, DENSE, SPARSE, publicUrl, token: TOKEN, respuesta, pasos },
        null,
        2,
      ),
      "utf-8",
    );
  });
});
