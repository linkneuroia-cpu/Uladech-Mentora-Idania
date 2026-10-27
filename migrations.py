"""
migrations.py
==============
Control de qué `db_migration_*.sql` ya corrieron contra una base real
("¿cuál de los 15 archivos ya está aplicado en producción?" era, hasta
ahora, una pregunta sin respuesta verificable — cada uno se corría a mano
con `psql`). Reusa la misma conexión que el resto del gestor (`db.py`,
que ya resuelve `ENV=DESARROLLO|PRODUCCION` vía `settings.py`).

Idempotente a propósito: los `db_migration_*.sql` existentes ya usan
`CREATE TABLE IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS`, así que aplicar
dos veces el mismo archivo no rompe nada — pero igual se registra cada
uno una sola vez, para tener evidencia real de qué corrió y cuándo, en
vez de asumirlo.

Orden: alfabético por nombre de archivo (determinista). Los 15 archivos
actuales no tienen dependencias de orden entre sí (cada uno crea sus
propias tablas o agrega columnas a tablas ya existentes en db_schema.sql).
Los migration files NUEVOS deberían nombrarse con un prefijo numérico
(ej. `db_migration_0016_<nombre>.sql`) para que el orden quede explícito
y no dependa de que el alfabeto siga alcanzando.

Uso:
    python migrations.py auditar      # no aplica nada, solo informa
    python migrations.py aplicar      # aplica los pendientes, uno por uno
    python migrations.py aplicar --solo db_migration_x.sql
"""
import argparse
import glob
import os
import sys

import db
from settings import settings

_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    VARCHAR(255) PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _migration_files():
    base = os.path.dirname(os.path.abspath(__file__))
    return sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(base, "db_migration_*.sql"))
    )


def _ensure_ledger():
    db.execute(_LEDGER_SQL)


def _applied() -> set:
    rows = db.fetch_all("SELECT filename FROM schema_migrations")
    return {r["filename"] for r in rows}


def auditar():
    _ensure_ledger()
    applied = _applied()
    files = _migration_files()
    print(f"Entorno activo: {settings.ENV} ({settings.PG_HOST}:{settings.PG_PORT}/{settings.PG_DATABASE})\n")
    for f in files:
        estado = "APLICADA" if f in applied else "PENDIENTE"
        print(f"  [{estado:9}] {f}")
    pendientes = [f for f in files if f not in applied]
    print(f"\n{len(pendientes)} pendiente(s) de {len(files)} archivo(s) total.")
    return pendientes


def aplicar(solo: str = None):
    _ensure_ledger()
    applied = _applied()
    files = _migration_files()
    if solo:
        if solo not in files:
            print(f"'{solo}' no existe entre los db_migration_*.sql.", file=sys.stderr)
            sys.exit(1)
        files = [solo]

    pendientes = [f for f in files if f not in applied]
    if not pendientes:
        print("Nada pendiente.")
        return

    print(f"Entorno activo: {settings.ENV} ({settings.PG_HOST}:{settings.PG_PORT}/{settings.PG_DATABASE})")
    print(f"Se van a aplicar {len(pendientes)} archivo(s):")
    for f in pendientes:
        print(f"  - {f}")
    confirm = input("\n¿Continuar? (escribir 'si' para confirmar) ")
    if confirm.strip().lower() not in ("si", "sí", "s"):
        print("Cancelado, no se aplicó nada.")
        return

    base = os.path.dirname(os.path.abspath(__file__))
    for f in pendientes:
        path = os.path.join(base, f)
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        print(f"\n→ Aplicando {f} ...")
        db.execute(sql)
        db.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
            (f,),
        )
        print(f"  OK — {f} registrada.")

    print(f"\nListo: {len(pendientes)} migración(es) aplicada(s) y registrada(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("auditar", help="Lista cuáles están aplicadas/pendientes, sin tocar nada.")
    p_aplicar = sub.add_parser("aplicar", help="Aplica los pendientes (pide confirmación).")
    p_aplicar.add_argument("--solo", help="Aplicar un solo archivo puntual en vez de todos los pendientes.")
    args = parser.parse_args()

    if args.comando == "auditar":
        auditar()
    elif args.comando == "aplicar":
        aplicar(solo=args.solo)
