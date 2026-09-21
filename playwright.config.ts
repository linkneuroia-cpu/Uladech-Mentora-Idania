import { defineConfig } from "@playwright/test";

// Suite E2E del gestor + chat público de F1 (Idania).
// Base por defecto: http://localhost:8100 (desarrollo). Para apuntar a
// preproducción:  BASE_URL=https://iauladech.linkneuroia.com npx playwright test
// Credenciales del gestor: ADMIN_USER / ADMIN_PASS (default admin / la de .env).
// Chat público (spec de humo): CHAT_URL con la URL completa del asistente.
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:8100",
    ignoreHTTPSErrors: true,
    headless: true,
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
});
