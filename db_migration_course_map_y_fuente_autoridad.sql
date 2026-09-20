-- ============================================================================
-- db_migration_course_map_y_fuente_autoridad.sql
-- Incremento 1 del agente académico híbrido (Course Map + Navegación +
-- Structured Query Engine, ver plan de arquitectura de esta sesión):
--
-- 1) course_map_cache: caché de la estructura real del curso en Moodle
--    (secciones/actividades/urls/visibilidad), poblada desde
--    core_course_get_contents (ya en uso por definitions.get_course_resources
--    para vectorización — este cache es para NAVEGACIÓN, uso distinto).
--    Invalidación primaria: atada a ledger.sync_curso_archivos() cuando
--    detecta archivos nuevos/modificados/eliminados (el docente subió algo).
--    TTL como respaldo secundario para cursos sin sync reciente.
--
-- 2) fuente_autoridad: jerarquía de autoridad configurable entre tipos de
--    fuente (sílabo > sesión semanal > material docente > bibliografía >
--    otro), editable desde el panel sin deploy. Sin esto, dos fuentes con
--    igual "unicidad" (is_repeated_template=False) pero de distinto tipo
--    empatan arbitrariamente — el bug real que disparó este trabajo (un
--    Excel de notas ganándole al sílabo) es exactamente ese caso.
--
-- No toca Qdrant ni ninguna tabla existente. Idempotente.
-- ============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS course_map_cache (
    moodle_courseid  INTEGER PRIMARY KEY,
    map_json         JSONB NOT NULL,       -- CourseMap serializado (ver course_map.py)
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ttl_minutes      INTEGER NOT NULL DEFAULT 1440,  -- 24h, respaldo secundario del invalidado por ledger
    invalidated_at   TIMESTAMPTZ            -- seteado por el trigger de sync de archivos; NULL = vigente
);

CREATE TABLE IF NOT EXISTS fuente_autoridad (
    id          SERIAL PRIMARY KEY,
    source_type VARCHAR(50) NOT NULL UNIQUE,  -- 'silabo','sesion_semanal','material_docente','registro_calificaciones','bibliografia','otro'
    prioridad   INTEGER NOT NULL,             -- menor = mayor autoridad
    activo      BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Orden inicial recomendado (editable después desde el panel sin deploy).
-- 'registro_calificaciones' separado de 'material_docente' a propósito: es
-- exactamente la categoría que causó el bug real (Excel de notas con una
-- MUESTRA de semanas, no el total real de la unidad) — necesita quedar por
-- debajo de 'silabo' y 'sesion_semanal' explícitamente, no compartir
-- autoridad con material docente genérico.
INSERT INTO fuente_autoridad (source_type, prioridad) VALUES
    ('silabo', 10),
    ('sesion_semanal', 20),
    ('material_docente', 30),
    ('registro_calificaciones', 40),
    ('bibliografia', 50),
    ('otro', 60)
ON CONFLICT (source_type) DO NOTHING;

COMMIT;
