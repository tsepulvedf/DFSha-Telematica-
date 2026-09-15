#!/bin/bash
# Arranque de la replica de lectura.
#
# La primera vez clona el primario con `pg_basebackup`; despues arranca normal y sigue
# el WAL. La condicion de "primera vez" es que no haya PG_VERSION en el volumen, no una
# marca propia: si alguien borra el volumen, la replica se reconstruye sola.
#
# Por que no basta con `docker-entrypoint.sh postgres`: el initdb del entrypoint oficial
# crea una base VACIA e INDEPENDIENTE. Una replica no se inicializa, se copia.
set -euo pipefail

PRIMARIO="${DFSHA_PRIMARY_HOST:?falta DFSHA_PRIMARY_HOST}"
USUARIO="${DFSHA_REPLICATION_USER:?falta DFSHA_REPLICATION_USER}"
CLAVE="${DFSHA_REPLICATION_PASSWORD:?falta DFSHA_REPLICATION_PASSWORD}"
PUERTO="${DFSHA_PRIMARY_PORT:-5432}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
	echo "replica: volumen vacio, clonando desde $PRIMARIO:$PUERTO"

	mkdir -p "$PGDATA"
	# El volumen lo crea Docker como root; postgres se niega a arrancar con un PGDATA
	# que no sea suyo y con permisos 0700.
	chown -R postgres:postgres "$PGDATA"
	chmod 0700 "$PGDATA"

	# La clave va en .pgpass y no en la linea de comandos: los argumentos de un proceso
	# los ve cualquiera con `ps` dentro del contenedor. `pg_basebackup -R` escribe el
	# primary_conninfo del standby sin la clave, asi que este fichero tambien es lo que
	# permite que la replica reconecte despues de un reinicio.
	PGPASS="/var/lib/postgresql/.pgpass"
	printf '%s:%s:*:%s:%s\n' "$PRIMARIO" "$PUERTO" "$USUARIO" "$CLAVE" > "$PGPASS"
	chown postgres:postgres "$PGPASS"
	chmod 0600 "$PGPASS"

	echo "replica: esperando a que el primario acepte conexiones"
	until gosu postgres pg_isready -h "$PRIMARIO" -p "$PUERTO" -q; do
		sleep 1
	done

	# -Xs: trae el WAL en streaming durante la copia, para que el basebackup no falle si
	#      el primario recicla segmentos mientras copia.
	# -R : deja escritos standby.signal y primary_conninfo.
	# -w : nunca preguntar la clave por consola; este proceso no tiene consola.
	gosu postgres pg_basebackup \
		-h "$PRIMARIO" -p "$PUERTO" -U "$USUARIO" \
		-D "$PGDATA" -Fp -Xs -R -P -w

	echo "replica: clon completo"
fi

# `hot_standby=on` es lo que permite SERVIR CONSULTAS mientras reproduce el WAL, que es
# la unica razon por la que esta replica existe: el lado de lectura de CQRS.
exec docker-entrypoint.sh postgres -c hot_standby=on
