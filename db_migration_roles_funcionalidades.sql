-- ============================================================================
-- db_migration_roles_funcionalidades.sql
-- Roles (admin/docente), funcionalidades por asistente y documentos excluidos.
-- Aplicar a mano con psql (no hay herramienta de migraciones):
--   psql ... -f db_migration_roles_funcionalidades.sql
-- Antes: respaldar las tablas `usuarios` y `asistentes` (pg_dump).
-- ============================================================================

BEGIN;

-- ── usuarios: rol explícito + identidad de docente (login por enlace Moodle) ─
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS rol VARCHAR(20) NOT NULL DEFAULT 'admin';
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS documento VARCHAR(20) UNIQUE;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS moodle_userid INTEGER;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nombre VARCHAR(200);
-- Un docente entra solo por el enlace de Moodle: no tiene contraseña local.
ALTER TABLE usuarios ALTER COLUMN password_hash DROP NOT NULL;
UPDATE usuarios SET rol = CASE WHEN is_admin THEN 'admin' ELSE 'docente' END;
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_rol_check;
ALTER TABLE usuarios ADD CONSTRAINT usuarios_rol_check CHECK (rol IN ('admin','docente'));

-- ── asistentes: funcionalidades configurables por el docente ────────────────
-- TRUE por defecto = los asistentes que ya existen se comportan igual que antes.
ALTER TABLE asistentes ADD COLUMN IF NOT EXISTS func_notas      BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE asistentes ADD COLUMN IF NOT EXISTS func_documentos BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE asistentes ADD COLUMN IF NOT EXISTS func_navegacion BOOLEAN NOT NULL DEFAULT TRUE;

-- ── documentos que el docente decidió NO usar al responder ──────────────────
CREATE TABLE IF NOT EXISTS asistente_documentos_excluidos (
    asistente_id  INTEGER NOT NULL REFERENCES asistentes(id) ON DELETE CASCADE,
    filename      VARCHAR(500) NOT NULL,
    excluido_por  INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asistente_id, filename)
);

COMMIT;
