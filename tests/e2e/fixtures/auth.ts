import { Page, expect } from "@playwright/test";

export const ADMIN_USER = process.env.ADMIN_USER || "admin";
export const ADMIN_PASS = process.env.ADMIN_PASS || "Uladech2026!";

/** Login del gestor (frontend/login.html: #username, #password, #btn). */
export async function login(
  page: Page,
  user: string = ADMIN_USER,
  pass: string = ADMIN_PASS,
): Promise<void> {
  // Sin barra inicial: con BASE_URL con subruta (ej. producción, montada
  // bajo /f1) una ruta absoluta ("/login") pisa toda la subruta y navega
  // al origen — bug real reproducido contra producción (404 de nginx).
  // Relativa sin barra se resuelve bien tanto ahí como en entornos sin
  // subruta (local, preprod), siempre que BASE_URL termine en "/".
  await page.goto("login");
  await page.fill("#username", user);
  await page.fill("#password", pass);
  await page.click("#btn");
  await page.waitForURL(/\/gestor/);
}

/** Abre un módulo del gestor por su nombre (ej. 'rd', 'auto', 'asis-list').
 *  El gestor es SPA: `showMod(name)` activa `#mod-<name>` y `#nav-<name>`. */
export async function openModule(page: Page, name: string): Promise<void> {
  await page.click(`#nav-${name}`);
  await expect(page.locator(`#mod-${name}`)).toBeVisible();
  await page.waitForTimeout(600);
}
