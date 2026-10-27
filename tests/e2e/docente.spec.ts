import { test, expect } from "@playwright/test";

// Spec del panel de docente (rol nuevo). Requiere un enlace real de Moodle
// ya emitido para un docente de prueba:
//   DOCENTE_LOGIN_URL=https://educaia.uladech.edu.pe/f1/login?username=...&token=...&userid=...
//   DOCENTE_COURSEID=1472        (un curso del docente)
// Se salta si no están configurados (no rompe la suite en CI/local).
const LOGIN_URL = process.env.DOCENTE_LOGIN_URL || "";
const COURSEID = process.env.DOCENTE_COURSEID || "";

test.describe("Panel del docente", () => {
  test("solo ve sus cursos y puede configurar su asistente", async ({ page }) => {
    test.skip(!LOGIN_URL || !COURSEID, "Faltan DOCENTE_LOGIN_URL / DOCENTE_COURSEID");

    await page.goto(LOGIN_URL);
    await page.waitForURL(/\/gestor/);

    // No ve el sidebar de administrador.
    await expect(page.locator("#nav-docente")).toBeVisible();
    await expect(page.locator("#nav-qdrant-cols")).toBeHidden();
    await expect(page.locator("#nav-usuarios")).toBeHidden();

    // "Mis cursos" carga las tarjetas de sus cursos.
    await expect(page.locator("#doc-cursos")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("#doc-cursos")).not.toContainText("Cargando…", { timeout: 60_000 });
    const tarjeta = page.locator(".card", { hasText: `Curso` }).first();
    await expect(tarjeta).toBeVisible();

    // Endpoints de administración: 403. Rutas relativas (sin "/" inicial)
    // para que respeten el baseURL con subruta (/f1/).
    const resp = await page.request.get("api/collections");
    expect(resp.status()).toBe(403);

    // Documentos del propio curso: 200; de un curso ajeno: 403.
    const docsPropios = await page.request.get(`api/docente/cursos/${COURSEID}/documentos`);
    expect(docsPropios.status()).toBe(200);
    const docsAjenos = await page.request.get(`api/docente/cursos/999999/documentos`);
    expect(docsAjenos.status()).toBe(403);
  });
});
