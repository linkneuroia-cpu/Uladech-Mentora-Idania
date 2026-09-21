import { test, expect } from "@playwright/test";
import { login, ADMIN_USER } from "./fixtures/auth";

test.describe("Login del gestor", () => {
  test("login válido redirige al gestor", async ({ page }) => {
    await login(page);
    await expect(page).toHaveURL(/\/gestor/);
    // El gestor tiene la navegación lateral con los módulos.
    await expect(page.locator("#nav-rd")).toBeVisible();
  });

  test("login inválido muestra el error", async ({ page }) => {
    await page.goto("/login");
    await page.fill("#username", ADMIN_USER);
    await page.fill("#password", "clave-incorrecta-xyz");
    await page.click("#btn");
    await expect(page.locator("#err")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("sin sesión, /gestor redirige a /login", async ({ page }) => {
    await page.goto("/gestor");
    await expect(page).toHaveURL(/\/login/);
  });
});
