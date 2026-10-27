# Despliegue

Dos entornos, cada uno con su propio servidor, su propia base de datos y
su propio `deploy/.env.deploy*` (ninguno se versiona — ver `.gitignore`):

| Entorno | Servidor | Rama que corresponde | Compose |
|---|---|---|---|
| Producción | `10.0.0.92` | `main` | `docker-compose.yml` |
| Preproducción (desarrollo) | `80.190.77.138` (linkneuroia) | `develop` | `docker-compose.preprod.yml` |

Repo: `https://github.com/linkneuroia-cpu/Uladech-Mentora-Idania.git`
(remoto local `idania`).

## Flujo normal

```
git checkout develop
git checkout -b feature/lo-que-sea
# ... trabajar, commitear ...
git push idania feature/lo-que-sea
# validar, luego mergear a develop
git checkout develop && git merge feature/lo-que-sea
git push idania develop
deploy/deploy_git.sh preprod          # prueba en preproducción real
# si todo bien:
git checkout main && git merge develop
git push idania main
deploy/deploy_git.sh prod             # recién ahí, a producción
```

`deploy_git.sh` deja el servidor con un checkout real de git (no una copia
cruda del working directory) y construye la imagen Docker con el tag del
commit (`gestor-app:<sha-corto>`), no `:latest` — así cada deploy queda
identificado y es posible reconstruir exactamente esa versión después.

**Primera vez** en un servidor que todavía tiene el código viejo (subido
por `upload.sh`/`tar`): `deploy_git.sh` convierte esa carpeta en un
checkout de git con `git init` + `git checkout -f` — no toca `.env` ni
`temp_docs/` (no están versionados, `checkout -f` no los tocca). El resto
de archivos tracked quedan reemplazados por los del commit que se
despliega.

## Rollback

```
deploy/deploy_git.sh prod <tag-o-commit-anterior>
```

Si **no hubo migración de base de datos** entre esa versión y la actual,
esto alcanza — el servidor vuelve al código de ese commit, reconstruye la
imagen con ese tag y reinicia el contenedor.

Si **sí hubo una migración** (algún `db_migration_*.sql` nuevo), revertir
el código NO revierte el esquema. Antes de hacer rollback de código:
1. `python migrations.py auditar` (local, contra el entorno que
   corresponda vía `.env`/`ENV=`) — confirma qué migraciones están
   aplicadas.
2. Decidir si las columnas/tablas agregadas por esas migraciones pueden
   quedarse sin uso (lo normal, porque son `ADD COLUMN`/`CREATE TABLE IF
   NOT EXISTS` aditivas) o si hace falta revertirlas a mano con un
   `ALTER TABLE ... DROP COLUMN` puntual — **nunca automático**, revisar
   caso por caso.
3. Recién ahí, `deploy_git.sh` al commit anterior.

## Migraciones (`migrations.py`, en la raíz del repo)

```
python migrations.py auditar              # solo informa, no toca nada
python migrations.py aplicar               # aplica los pendientes (pide confirmación)
python migrations.py aplicar --solo db_migration_x.sql
```

Usa el mismo `ENV=DESARROLLO|PRODUCCION` del `.env` local para decidir
contra qué base conecta — revisar ese valor antes de correr `aplicar`.
Registra cada archivo aplicado en la tabla `schema_migrations`, para que
"¿esto ya corrió?" tenga una respuesta verificable en vez de ser una
suposición.

## Flujo viejo (`upload.sh` + `deploy.sh`, sin Git)

Se mantienen por compatibilidad, pero ya no son el camino recomendado:
suben el working directory local tal cual (tar/rsync), sin relación con
ningún commit. Usarlos deja al servidor "no versionado" otra vez — evitar
salvo emergencia puntual.
