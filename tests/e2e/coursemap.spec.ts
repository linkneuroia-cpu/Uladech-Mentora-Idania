import { test, expect } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

test.describe("Course Map", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("abre el módulo de estructura del curso", async ({ page }) => {
    await openModule(page, "coursemap");
    await expect(page.locator("#mod-coursemap")).toContainText(/curso|estructura|courseid/i);
  });

  // Flujo completo: cargar un courseid, ver secciones/actividades y
  // "Sincronizar datos del aula" (sección General). Depende de Moodle real.
  test.fixme("cargar estructura + sincronizar sección General", async ({ page }) => {});
});
