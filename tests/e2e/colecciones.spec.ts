import { test, expect } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

test.describe("Colecciones de Qdrant", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("abre el listado de colecciones", async ({ page }) => {
    await openModule(page, "qdrant-cols");
    await expect(page.locator("#mod-qdrant-cols")).toContainText(/colecci[oó]n|Curso_/i);
  });

  test("el dashboard muestra estado de Qdrant", async ({ page }) => {
    await openModule(page, "qdrant-dash");
    await expect(page.locator("#mod-qdrant-dash")).toContainText(/Qdrant|colecci[oó]n/i);
  });
});
