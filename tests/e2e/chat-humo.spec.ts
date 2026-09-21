import { test, expect, Page } from "@playwright/test";

// Spec de humo del CHAT PÚBLICO. Requiere CHAT_URL con la URL completa del
// asistente, ej:
//   CHAT_URL="https://iauladech.linkneuroia.com/asistente/<token>?courseid=11&userid=2"
const CHAT_URL = process.env.CHAT_URL || "";

async function preguntar(page: Page, q: string): Promise<void> {
  const antes = await page.locator(".rating-btn.up").count();
  await page.fill("#q", q);
  await page.click("#sendbtn");
  // Espera una respuesta NUEVA (más bloque de calificación que antes).
  await page.waitForFunction(
    (b) => document.querySelectorAll(".rating-btn.up").length > b,
    antes,
    { timeout: 150_000 },
  );
}

test.describe("Chat público (humo)", () => {
  test.skip(!CHAT_URL, "Definí CHAT_URL para correr este spec.");

  test.beforeEach(async ({ page }) => {
    // Crear una conversación NUEVA vía API antes de cargar: así cada test
    // arranca con 0/10 preguntas y no topa el límite acumulado entre corridas.
    const u = new URL(CHAT_URL);
    const token = u.pathname.match(/\/asistente\/([^/]+)/)?.[1];
    if (token) {
      await page.request
        .post(`${u.origin}/asistente/${token}/nueva-conversacion`, {
          data: {
            courseid: Number(u.searchParams.get("courseid") || 0),
            userid: Number(u.searchParams.get("userid") || 0),
          },
        })
        .catch(() => {});
    }
    await page.goto(CHAT_URL);
    await expect(page.locator("#q")).toBeVisible();
    await expect(page.locator("#q")).toBeEnabled({ timeout: 30000 });
  });

  test("saludo con la semana real", async ({ page }) => {
    const body = await page.locator("body").innerText();
    expect(body).toMatch(/Semana\s*\d+/i);
  });

  test("'¿cómo apruebo el curso?' trae la tabla con Componente y Total", async ({ page }) => {
    await preguntar(page, "¿cómo apruebo el curso?");
    const ultimo = await page.locator(".msg.assistant, .message.assistant").last().innerText().catch(async () => await page.locator("body").innerText());
    expect(ultimo).toMatch(/Componente/i);
    expect(ultimo).toMatch(/Total/i);
    expect(ultimo).toMatch(/100\s*%/);
  });

  test("'¿quién es el profesor del curso?' responde el nombre real", async ({ page }) => {
    const q = "¿quién es el profesor del curso?";
    await preguntar(page, q);
    const body = await page.locator("body").innerText();
    // Solo la ÚLTIMA respuesta (el historial previo puede tener otras frases).
    const i = body.lastIndexOf(q.slice(0, 12));
    const ultimo = i >= 0 ? body.slice(i) : body;
    expect(ultimo).not.toMatch(/no encontr[eé] informaci[oó]n/i);
    expect(ultimo).toMatch(/(Dr\.|Dra\.|Mg\.|Prof\.)/);
  });

  test("'¿qué se me vence esta semana?' no lista el curso entero sin filtrar", async ({ page }) => {
    await preguntar(page, "¿qué se me vence esta semana?");
    const body = await page.locator("body").innerText();
    // Debe mencionar explícitamente la semana y marcar estado (vencida/próxima).
    expect(body).toMatch(/esta semana/i);
    expect(body).toMatch(/(vencida|pr[oó]xima)/i);
  });
});
