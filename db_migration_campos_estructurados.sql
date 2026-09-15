-- ============================================================================
-- db_migration_campos_estructurados.sql
-- Índice de campos estructurados (campo -> valor) extraído de las tablas
-- de cualquier documento vectorizado — reemplaza al router de metadata
-- basado en listas de palabras clave manuales que existía antes en
-- strategies/rerank.py (_METADATA_FIELD_PATTERNS, ya retirado) por uno
-- genérico que consulta los campos REALES que existen en cada colección.
--
-- No depende de Qdrant ni lo modifica — se reconstruye por colección
-- (DELETE + re-insert) sin tocar ningún vector. Ver campos_estructurados.py.
-- Idempotente: se puede correr dos veces sin romper nada.
-- ============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS coleccion_campos_estructurados (
    id                      BIGSERIAL PRIMARY KEY,
    qdrant_collection_name  VARCHAR(255) NOT NULL,
    document_filename       TEXT NOT NULL,
    document_type           VARCHAR(20),      -- pdf/docx (classify_file_type)
    chunk_type              VARCHAR(20) NOT NULL,  -- 'table' | 'table_row' (siempre viene de una tabla)
    field_raw               TEXT NOT NULL,    -- tal cual aparece: "14. Docente", "Ponderación"
    field_normalized        TEXT NOT NULL,    -- sin numeración/tildes/mayúsculas, para matching
    value_raw               TEXT,
    value_normalized        TEXT,
    section_heading         TEXT,             -- heading detectado (chunk_structured_blocks)
    page                    INTEGER,
    table_signature         TEXT,             -- repr de table_signature(), agrupa filas de la misma tabla
    is_repeated_template    BOOLEAN NOT NULL DEFAULT FALSE,  -- ver table_signature/repeated_signatures
    source_type             VARCHAR(20),      -- curso_propio/bibliografia (igual que en Qdrant)
    row_id                  TEXT,             -- agrupa los campos:valor de UNA misma fila de tabla
                                               -- (ej. tabla "Componente|Ponderación|..." con varias
                                               -- filas: cada fila comparte row_id) — permite resolver
                                               -- "¿cuánto vale examen sumativo?" buscando primero por
                                               -- VALOR ("examen sumativo") y luego el campo dentro de
                                               -- esa misma fila ("ponderación").
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lookup principal: "dame todos los valores de este campo en esta colección".
CREATE INDEX IF NOT EXISTS idx_campos_estructurados_lookup
    ON coleccion_campos_estructurados (qdrant_collection_name, field_normalized);

-- Para reconstrucción rápida (DELETE WHERE qdrant_collection_name = X antes
-- de re-poblar) y para listar los campos distintos de una colección (usado
-- por la detección genérica de campo en la pregunta).
CREATE INDEX IF NOT EXISTS idx_campos_estructurados_collection
    ON coleccion_campos_estructurados (qdrant_collection_name);

CREATE INDEX IF NOT EXISTS idx_campos_estructurados_row
    ON coleccion_campos_estructurados (qdrant_collection_name, row_id);

COMMIT;
