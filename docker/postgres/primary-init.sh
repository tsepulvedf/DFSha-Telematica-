#!/bin/bash
# Se ejecuta UNA sola vez, durante el initdb del primario.
#
# Hace dos cosas, y las dos hacen falta para que la replica pueda engancharse:
#   1. Crea el rol de replicacion. Es un rol aparte del de la aplicacion a proposito:
#      solo puede replicar, no leer ni escribir tablas. Si se filtra, no da acceso al
#      metadato.
#   2. Anade la linea de `pg_hba.conf` que autoriza conexiones de replicacion. El
#      pg_hba que genera el initdb no trae ninguna para la red, asi que sin esto la
#      replica recibe "no pg_hba.conf entry for replication connection" y nada explica
#      que la solucion esta aqui.
set -euo pipefail

USUARIO="${DFSHA_REPLICATION_USER:?falta DFSHA_REPLICATION_USER}"
CLAVE="${DFSHA_REPLICATION_PASSWORD:?falta DFSHA_REPLICATION_PASSWORD}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE ROLE "$USUARIO" WITH REPLICATION LOGIN PASSWORD '$CLAVE';
SQL

# `all` como origen se refiere a la red de compose, que no esta expuesta fuera del host.
# En AWS esto se restringe al CIDR de la subred privada; ver deploy/README.md.
cat >> "$PGDATA/pg_hba.conf" <<-HBA

	# --- DFSha: replicacion en streaming -------------------------------------
	host    replication    $USUARIO    all    scram-sha-256
HBA

echo "primary-init: rol de replicacion '$USUARIO' creado y pg_hba actualizado"
