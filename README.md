# Gestor F1 — Mentora IdanIA

Asistente de IA con RAG híbrido (Qdrant + Postgres) integrado a Moodle,
con panel de administración y panel del docente.

## Trabajar en local

```
git checkout develop
git checkout -b feature/lo-que-sea     # o fix/..., hotfix/... sobre main
```

Copiar `.env.example` a `.env` y completar los valores reales (pedirlos,
no están en el repo). Con `ENV=DESARROLLO`, Postgres/Qdrant deberían
apuntar al servidor de preproducción (`80.190.77.138`) salvo que se
prefiera un Qdrant/Postgres propio en la máquina local.

Correr la app:
```
venv\Scripts\activate
python app.py          # http://localhost:8100
```

Pruebas:
```
pytest tests/ --ignore=tests/e2e -q          # unitarias, mockean DB/Moodle/Qdrant
npx playwright test                           # E2E — BASE_URL=... para apuntar a otro entorno
```

**Nunca** correr `tests/e2e/*-1440.spec.ts` contra producción — crean y
modifican datos reales.

## Despliegue

Ver `deploy/README.md` (flujo basado en Git, entornos, rollback,
migraciones).

## Estructura

- `app.py` — rutas FastAPI (gestor, panel docente, chat público).
- `rag_pipeline.py`, `query_router.py`, `strategies/` — pipeline RAG.
- `core.py`, `definitions.py`, `ledger.py` — ingesta/ETL desde Moodle.
- `assistants.py`, `auth.py`, `docentes.py`, `rds.py` — asistentes,
  usuarios/roles, docentes (ERP), RD.
- `frontend/` — interfaz del gestor y chat público.
- `doc/` — informes y documentos Word generados (no código).
- `tests/` — `test_*.py` (pytest, unitarias) y `e2e/` (Playwright).
- `tests/eval/` — datasets y resultados de evaluación de calidad del RAG.
- `migrations.py` + `db_migration_*.sql` — esquema de Postgres.
- `deploy/` — scripts de despliegue (ver su propio README).
