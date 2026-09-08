-- ============================================================================
-- db_migration_colecciones_migracion.sql
-- Agrega el estado de "migración" (re-sincronización con Moodle) a cada
-- colección: modo manual/periódica, intervalo, última corrida y su
-- resultado. Consumido por el scheduler (ver migracion.py) y expuesto en
-- Gestión Qdrant → Colecciones.
-- Idempotente: se puede correr dos veces sin romper nada.
-- ============================================================================
BEGIN;

ALTER TABLE colecciones ADD COLUMN IF NOT EXISTS migracion_modo VARCHAR(10) NOT NULL DEFAULT 'manual';
ALTER TABLE colecciones ADD COLUMN IF NOT EXISTS migracion_intervalo_minutos INTEGER;
ALTER TABLE colecciones ADD COLUMN IF NOT EXISTS migracion_estado VARCHAR(20);
ALTER TABLE colecciones ADD COLUMN IF NOT EXISTS migracion_ultima_at TIMESTAMPTZ;
ALTER TABLE colecciones ADD COLUMN IF NOT EXISTS migracion_ultimo_resultado TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'colecciones_migracion_modo_check'
    ) THEN
        ALTER TABLE colecciones
            ADD CONSTRAINT colecciones_migracion_modo_check CHECK (migracion_modo IN ('manual', 'periodica'));
    END IF;
END $$;

COMMIT;
