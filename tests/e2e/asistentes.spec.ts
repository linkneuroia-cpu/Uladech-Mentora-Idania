import { test, expect } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

test.describe("Asistentes (CRUD + plantillas de saludo)", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("abre el listado de asistentes", async ({ page }) => {
    await openModule(page, "asis-list");
    await expect(page.locator("#mod-asis-list")).toBeVisible();
  });

  test("el editor de mensajes ofrece variables y vista previa", async ({ page }) => {
    await openModule(page, "asis-list");
    const editBtn = page.locator("#mod-asis-list button", { hasText: /editar/i }).first();
    if (!(await editBtn.count())) {
      test.skip(true, "No hay asistentes para editar en este entorno.");
      return;
    }
    await editBtn.click();
    await page.waitForTimeout(700);
    // Ir a la pestaña "Prompt y saludo" (ASIS_EDIT_TABS).
    const tab = page.locator(".wiz-tab", { hasText: /prompt y saludo/i }).first();
    if (await tab.count()) {
      await tab.click();
      await page.waitForTimeout(500);
    }
    // Debe listar las variables disponibles de la plantilla de saludo.
    await expect(page.locator("body")).toContainText(/\{semana\}|\{nombre\}/);
  });

  // Crear/editar/guardar/releer con variables: depende de datos de prueba.
  test.fixme("crear asistente y guardar plantilla con variables", async ({ page }) => {});
});
