import { test, expect } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

test.describe("Vectorización (Automático / Actualización)", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("abre el asistente Automático", async ({ page }) => {
    await openModule(page, "auto");
    await expect(page.locator("#mod-auto")).toBeVisible();
  });

  test("abre el asistente de Actualización (usado para re-vectorizar puntual)", async ({ page }) => {
    await openModule(page, "upd");
    await expect(page.locator("#mod-upd")).toBeVisible();
  });

  // Flujo completo (preview → download-validate → confirm → job termina) y
  // Actualización de UN documento: depende de un curso real y tarda.
  test.fixme("preview + confirmar vectorización de un curso", async ({ page }) => {});
});
