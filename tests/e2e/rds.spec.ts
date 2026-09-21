import { test, expect } from "@playwright/test";
import { login, openModule } from "./fixtures/auth";

test.describe("Gestión RD", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("abre el módulo y lista las RD", async ({ page }) => {
    await openModule(page, "rd");
    await expect(page.locator("#mod-rd")).toContainText(/RD-/i);
  });

  // Flujo completo (crear RD, agregar courseid, activar/desactivar un curso
  // puntual con `activo_asistente`): requiere datos de prueba estables.
  test.fixme("crear RD + courseid + activar/desactivar curso", async ({ page }) => {
    await openModule(page, "rd");
    // TODO: completar con los ids reales del formulario de creación.
  });
});
