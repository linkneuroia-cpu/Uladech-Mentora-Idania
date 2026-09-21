import { test, expect } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

const CM_COURSEID = process.env.CM_COURSEID || "11";
const CM_COLLECTION = process.env.CM_COLLECTION || "Curso_11";

test.describe("Course Map", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("abre el módulo de estructura del curso", async ({ page }) => {
    await openModule(page, "coursemap");
    await expect(page.locator("#mod-coursemap")).toContainText(/curso|estructura|courseid/i);
  });

  test("CRUD manual de 'Datos generales del aula' (Parte 2.5)", async ({ page }) => {
    await openModule(page, "coursemap");
    await page.fill("#cm-courseid", CM_COURSEID);
    await page.fill("#cm-collection", CM_COLLECTION);
    await page.click("button:has-text('Cargar estructura')");
    await expect(page.locator("#cm-wrap")).toBeVisible({ timeout: 60_000 });

    const campo = "E2E_PRUEBA";
    const valor = "valor de prueba e2e";
    await page.fill("#cm-new-field", campo);
    await page.fill("#cm-new-value", valor);
    await page.click("#cm-add-field");
    await expect(page.locator("#cm-general")).toContainText(campo, { timeout: 30_000 });

    // Borrar el campo manual (aceptar el confirm).
    page.on("dialog", (d) => d.accept());
    const row = page.locator("#cm-general tr", { hasText: campo });
    await row.locator("button[data-act='del']").click();
    await expect(page.locator("#cm-general")).not.toContainText(campo, { timeout: 30_000 });
  });

  // Sincronizar datos del aula + confirmar que un campo manual sobrevive.
  test.fixme("sincronizar sección General preservando los campos manuales", async ({ page }) => {});
});
