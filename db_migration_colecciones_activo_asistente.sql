-- Activación por (RD, courseid), no solo por asistente completo. Hoy un
-- asistente ligado a una RD responde para CUALQUIER courseid que esa RD
-- tenga registrado en `colecciones` — no hay forma de habilitar solo
-- algunos cursos de una RD sin tocar los demás. DEFAULT TRUE preserva el
-- comportamiento actual de TODO lo que ya funciona hoy (nadie pierde
-- acceso solo por correr esta migración) — la desactivación puntual de
-- cursos específicos es un paso de datos aparte, no del default de la
-- columna.
ALTER TABLE colecciones ADD COLUMN IF NOT EXISTS activo_asistente BOOLEAN NOT NULL DEFAULT TRUE;
