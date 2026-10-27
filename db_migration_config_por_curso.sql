-- ============================================================================
-- db_migration_config_por_curso.sql
-- Configuración y exclusiones de documentos POR CURSO, para que lo que edite
-- un docente no modifique el asistente global ni la RD (un asistente de RD
-- comparte todas sus aulas).
--
-- Aplicar con psql (respaldar antes usuarios/asistentes y las tablas
-- asistente_*):
--   psql ... -f db_migration_config_por_curso.sql
-- Idempotente.
-- ============================================================================

BEGIN;

-- ── asistente_documentos_excluidos: agregar el curso a la clave ─────────────
ALTER TABLE asistente_documentos_excluidos ADD COLUMN IF NOT EXISTS moodle_courseid INTEGER;

-- Backfill: para asistentes "normales", el curso del asistente; si no tiene
-- (asistente institucional sin curso), 0.
UPDATE asistente_documentos_excluidos e
SET moodle_courseid = COALESCE(a.moodle_courseid, 0)
FROM asistentes a
WHERE a.id = e.asistente_id AND e.moodle_courseid IS NULL;

UPDATE asistente_documentos_excluidos SET moodle_courseid = 0 WHERE moodle_courseid IS NULL;

ALTER TABLE asistente_documentos_excluidos ALTER COLUMN moodle_courseid SET DEFAULT 0;
ALTER TABLE asistente_documentos_excluidos ALTER COLUMN moodle_courseid SET NOT NULL;

ALTER TABLE asistente_documentos_excluidos
    DROP CONSTRAINT IF EXISTS asistente_documentos_excluidos_pkey;
ALTER TABLE asistente_documentos_excluidos
    ADD CONSTRAINT asistente_documentos_excluidos_pkey
    PRIMARY KEY (asistente_id, moodle_courseid, filename);

-- ── asistente_curso_config: overrides por curso ────────────────────────────
CREATE TABLE IF NOT EXISTS asistente_curso_config (
    asistente_id            INTEGER NOT NULL REFERENCES asistentes(id) ON DELETE CASCADE,
    moodle_courseid         INTEGER NOT NULL,
    func_notas              BOOLEAN NOT NULL DEFAULT TRUE,
    func_documentos         BOOLEAN NOT NULL DEFAULT TRUE,
    func_navegacion         BOOLEAN NOT NULL DEFAULT TRUE,
    max_actividades_sesion  INTEGER NOT NULL DEFAULT 10,
    activo                  BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by              INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asistente_id, moodle_courseid)
);

COMMIT;
