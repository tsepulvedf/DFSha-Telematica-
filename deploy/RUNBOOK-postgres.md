# Runbook — promoción manual de la réplica de PostgreSQL

> **Procedimiento manual, a propósito.** No hay failover automático de base de datos en
> DFSha y no lo habrá en esta etapa. Un failover automático **correcto** necesita un
> árbitro externo que decida quién es el primario, protección contra doble promoción
> (*split brain*) y una forma de que los clientes se enteren del cambio: es otro
> proyecto. Uno hecho a medias es peor que ninguno, porque promociona la réplica durante
> un corte de red de diez segundos y deja dos primarios aceptando escrituras divergentes.
>
> Lo que sí es automático es el liderazgo del **ControlNode** (lease con época, Bloque A).
> Son dos cosas distintas y conviene no confundirlas en el informe: el ControlNode es
> stateless y puede elegir líder solo; la base de datos tiene estado y no puede.

---

## Qué falla y qué sigue funcionando

Si el **primario** cae:

| Operación | Estado |
|---|---|
| `ls`, `stat`, `open`, `cluster` | **Funcionan**, las sirve la réplica |
| `get` de un archivo ya subido | **Funciona**: el plan de lectura es una consulta |
| `mkdir`, `rm`, `mv`, `put`, `commit` | **Fallan**: son escrituras |
| Heartbeats de los DataNodes | Fallan al persistir; los nodos siguen vivos y reintentan |
| Liderazgo del ControlNode | Nadie renueva el lease; el lease caduca y nadie lo toma |

O sea: **el clúster queda en solo lectura.** Eso es lo correcto. Escribir en dos sitios
sin coordinación es cómo se pierde el metadato.

---

## 1. Confirmar que el primario está caído de verdad

No promociones por un pico de latencia. Dos comprobaciones, desde otra máquina:

```bash
pg_isready -h <primario> -p 5432 -U dfsha          # esperado: no response / rechazo
docker inspect -f '{{.State.Status}}' dfsha-postgres-primary
```

Si el contenedor está `restarting`, **espera**. Un reinicio tarda segundos; promocionar
durante un reinicio deja dos primarios en cuanto el viejo vuelve.

## 2. Ver cuánto se pierde antes de decidir

En la réplica:

```bash
docker exec dfsha-postgres-replica \
  psql -U dfsha -d dfsha -c "SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn();"
```

Si los dos valores coinciden, la réplica reprodujo todo lo que recibió. Lo que no puedes
saber sin el primario es si el primario había escrito algo más que nunca llegó a
enviarse: **la replicación es asíncrona, así que una promoción puede perder las últimas
transacciones.** En DFSha eso significa, como mucho, unas pocas operaciones de metadato:
los bloques ya están en disco en los DataNodes y un archivo sin commit es una reserva
abandonada, que el sistema ya sabe recoger.

## 3. Detener los ControlNodes

Antes de promocionar, no después. Un ControlNode con conexiones abiertas al primario
viejo que vuelve es exactamente el escenario a evitar.

```bash
docker compose stop control-node-1 control-node-2 control-node-3
```

## 4. Promocionar

```bash
docker exec dfsha-postgres-replica gosu postgres pg_ctl promote -D /var/lib/postgresql/data
docker exec dfsha-postgres-replica psql -U dfsha -d dfsha -c "SELECT pg_is_in_recovery();"
```

`pg_is_in_recovery()` tiene que devolver `f`. Mientras devuelva `t`, sigue siendo una
réplica y rechazará cualquier escritura.

## 5. Apuntar los ControlNodes al primario nuevo

Cambia `DFSHA_DB_URL` para que apunte al host promovido y **vacía
`DFSHA_DB_REPLICA_URL`**: ya no hay réplica, y dejarla apuntando al nodo promovido haría
que las consultas fuesen al mismo sitio que los comandos sin que nadie lo notara.

```bash
# En el .env del despliegue
DFSHA_DB_URL=postgresql+psycopg://dfsha:...@<replica-promovida>:5432/dfsha
DFSHA_DB_REPLICA_URL=

docker compose up -d control-node-1 control-node-2 control-node-3
```

Comprueba que vuelven a escribir:

```bash
curl -s http://<balanceador>:8000/api/v1/cluster/leadership | jq
```

Tiene que aparecer un líder con una época **mayor** que la anterior: nadie renovó el
lease mientras la base estuvo caída, así que caducó y el primero en volver lo tomó.

## 6. Reconstruir una réplica nueva

El primario viejo **no puede volver como réplica sin reconstruirse**: su WAL divergió en
el momento de la promoción. O se clona de cero (`pg_basebackup`, que es lo que hace
`docker/postgres/replica-entrypoint.sh` con el volumen vacío) o se usa `pg_rewind`.

```bash
docker compose stop postgres-primary
docker volume rm dfsha_dfsha-pg-primary     # BORRA el volumen del primario viejo
# ... y se levanta como replica del nodo promovido
```

**Antes de borrar nada**, comprueba que el nodo promovido está sirviendo y que tiene los
datos. Borrar el volumen es irreversible.

---

## Prueba del runbook

Este procedimiento se prueba contra un stack desechable, nunca contra el entorno de
nadie:

```bash
docker compose -p dfsha-prueba up -d postgres-primary postgres-replica
# ... pasos 1 a 6 ...
docker compose -p dfsha-prueba down -v
```
