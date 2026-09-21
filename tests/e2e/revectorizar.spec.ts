import { test, expect, Locator, Page } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

// Nombres de archivo reales (tildes, "ñ", etc.) pueden llegar representados
// en distinta forma de normalización Unicode (NFC vs NFD) según el paso
// intermedio (env var de shell, JSON.parse, el propio HTML) — visualmente
// idénticos pero distintos byte a byte, lo que rompe un match de texto
// exacto/hasText de Playwright (bug real reproducido en producción: un
// documento con tilde no se encontraba en la lista aunque estaba ahí).
// Se normaliza a NFC de ambos lados antes de comparar.
async function clickByNormalizedText(
  page: Page, all: Locator, needle: string, sub?: string,
): Promise<void> {
  const target = needle.normalize("NFC");
  const count = await all.count();
  for (let i = 0; i < count; i++) {
    const el = all.nth(i);
    const text = (await el.innerText()).normalize("NFC");
    if (text.includes(target)) {
      await (sub ? el.locator(sub) : el).click();
      return;
    }
  }
  throw new Error(`No se encontró ningún elemento con texto que incluya "${needle}" (${count} candidatos revisados).`);
}

// Re-vectoriza puntualmente documentos ya indexados, re-descargándolos desde
// Moodle (mismo archivo, mismo nombre) vía el asistente de "Actualización"
// del gestor — así pasan por el pipeline de extracción vigente sin tocar el
// resto del curso ni requerir subida manual. Lista de objetivos configurable
// por env REVECTORIZAR_TARGETS (JSON); por defecto, el censo de calidad
// (coleccion_campos_estructurados con field_normalized largo/corrupto)
// conocido para producción (Curso_1472 / Curso_1541).
type Target = { collection: string; filename: string; curid: number };

const DEFAULT_TARGETS: Target[] = [
  { collection: "Curso_1472", filename: "LIBRO.pdf", curid: 1472 },
  { collection: "Curso_1472", filename: "SILABO INTRODUCCIÓN A LA ODONTOLOGÍA.pdf", curid: 1472 },
  { collection: "Curso_1472", filename: "Sistema de notación dentaria.pdf", curid: 1472 },
  { collection: "Curso_1472", filename: "MANUAL DE ESPECIALIDDES ODONTOLOGICAS.pdf", curid: 1472 },
  { collection: "Curso_1541", filename: "SILABO_FILOSOFIA_DE_LA_EDUCACION_2026II_ALINEADO_CORREGIDO.pdf", curid: 1541 },
  { collection: "Curso_1541", filename: "Filosofía, ética y educación para la ciudadanía en la era digital.pdf", curid: 1541 },
  { collection: "Curso_1541", filename: "SILABO_FILOSOFIA_DE_LA_EDUCACION_2026II_ALINEADO_CORREGIDO.docx", curid: 1541 },
  { collection: "Curso_1541", filename: "Material_Lectura_Semana_1_Filosofia_de_la_Educacion_2026II.docx", curid: 1541 },
];

const TARGETS: Target[] = process.env.REVECTORIZAR_TARGETS
  ? JSON.parse(process.env.REVECTORIZAR_TARGETS)
  : DEFAULT_TARGETS;

test.describe("Re-vectorización puntual (Actualización vía Moodle)", () => {
  for (const t of TARGETS) {
    test(`reemplaza "${t.filename}" en ${t.collection} desde Moodle (curid ${t.curid})`, async ({ page }) => {
      // Documentos con muchas imágenes/fórmulas caen a OCR (pix2tex/EasyOCR,
      // CPU-only en el servidor) por región — confirmado en vivo que un solo
      // PDF puede tardar bastante más de 15 min. Timeout generoso para no
      // fallar el test por un documento simplemente lento, no roto.
      test.setTimeout(45 * 60_000);

      await login(page);
      await openModule(page, "upd");

      // Paso 1 — colección (tipo de contenido queda en "Material del curso",
      // que es el default seleccionado — todos los objetivos son curso_propio).
      await page.selectOption("#upd-col", t.collection);
      await page.click("#btn-u1");

      // Paso 2 — documento a reemplazar.
      await expect(page.locator("#upd-dl")).toBeVisible({ timeout: 30_000 });
      await clickByNormalizedText(page, page.locator(".di"), t.filename);
      await page.click("#btn-u2");

      // Paso 3 — fuente: desde Moodle (re-descarga el mismo archivo real).
      await page.click("#src-moodle");
      await page.click("#btn-u3-moo");

      // Paso 3b — curid + árbol de módulos, seleccionar el archivo real.
      await page.fill("#upd-curid", String(t.curid));
      await page.click("#btn-upd-consult");
      await expect(page.locator("#upd-mtree")).toBeVisible({ timeout: 60_000 });

      await clickByNormalizedText(page, page.locator(".tree-row"), t.filename, 'input[type="radio"]');

      // Paso 4 — descarga + validación.
      await page.click("#btn-u3b");
      await expect(page.locator("#upd-p4c")).toBeVisible({ timeout: 3 * 60_000 });

      if (await page.locator("#upd-p4-error-block").isVisible()) {
        const msg = await page.locator("#upd-sg").innerText();
        throw new Error(`Validación falló para "${t.filename}": ${msg}`);
      }

      // Paso 5 — confirmar reemplazo y esperar a que el job termine.
      await page.click('button:has-text("Confirmar reemplazo")');
      await expect(page.locator("#upd-res")).toBeVisible({ timeout: 40 * 60_000 });
      const resultText = await page.locator("#upd-res").innerText();
      expect(resultText).toContain("Documento reemplazado");
    });
  }
});
