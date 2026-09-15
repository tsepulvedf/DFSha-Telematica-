# CLAUDE.md — DFSha

Memoria del proyecto entre sesiones. Si algo aquí contradice una idea "mejor" que se te
ocurra sobre la marcha, gana este documento: las decisiones ya están tomadas y discutidas.

DFSha es un sistema de archivos distribuido **basado en bloques**, arquitectura tipo HDFS,
opción cliente/servidor. Curso SI3007 / ST0263 (Sistemas Distribuidos).

Repositorio: https://github.com/tsepulvedf/DFSha-Telematica-  (**público**)

---

## 1. Decisiones de diseño firmes

No se cuestionan, se implementan.

1. **WORM con fachada CRUD.** Un bloque cerrado es inmutable y nunca se sobrescribe. Un
   "update" de archivo es copy-on-write: se escriben bloques nuevos y el metadato apunta a
   ellos. Desde el cliente la API se ve como CRUD completo.
2. **El DataNode no conoce rutas lógicas.** Almacena por `block_id` en layout plano con
   sharding por los dos primeros caracteres del id. El mapeo `ruta -> [block_id]` vive solo
   en el ControlNode. Consecuencia directa: `mv` y `rename` son metadato puro, O(1), sin
   tocar ningún byte en el DataNode.
3. **Colocación explícita, no hash.** El ControlNode elige el DataNode destino de cada
   bloque y **registra la elección** en el metadato (`block_replicas`). La ubicación jamás
   se recalcula por hash del id.
4. **Routing híbrido.** El ControlNode es routing tier solo para metadatos. Los bytes
   viajan **directo** entre cliente y DataNode; nunca atraviesan el ControlNode.
5. **CQRS en el ControlNode.** Comandos (mutan el namespace) y consultas (`ls`, `lookup`)
   viven en paquetes distintos: `commands/` y `queries/`. Hoy ambos golpean la misma base
   de datos; la separación existe para poder replicar el lado de lectura en la Etapa 3.
6. **Bloque de tamaño fijo configurable.** Default 64 MB (`DFSHA_BLOCK_SIZE`).
7. **Expiración por tiempo para estados transitorios.** Toda reserva de escritura tiene
   `expires_at`; un cliente caído no bloquea recursos indefinidamente. Este mecanismo es la
   base de los leases del RF3 en la Etapa 3.

Reglas transversales que se derivan de lo anterior:

- Acceso a datos **detrás de repositorios**. Nada de SQL suelto en los routers.
- Los routers de FastAPI solo traducen HTTP <-> casos de uso. Las reglas de dominio viven
  en `domain/`.
- Sin demonios ni tareas en background en la Etapa 1. Las comprobaciones de expiración son
  **perezosas**, en el momento en que se consulta o se pisa el nombre.

---

## 2. Hoja de ruta

- **Etapa 1 (hito-1, actual)** — RF1 + RF2, un ControlNode y un DataNode, JWT, SHA-256 por
  bloque, GC manual, logging estructurado.
- **Etapa 2 (hito-2)** — N DataNodes reales, gRPC ControlNode<->DataNode, heartbeat cada
  3 s con métricas de carga, block report incremental, política de colocación *power of d
  choices* (filtrar candidatos vivos con espacio -> tomar los 3 menos cargados -> elegir al
  azar entre esos) con restricción de dominios de falla.
- **Etapa 3 (hito-3)** — replicación R=3 con pipeline y commit W=2, re-replicación ante
  caída, ControlNode primary/standby con edit log, réplicas de lectura CQRS, RF3 con
  leases, mTLS y cifrado en reposo.
- **Etapa 4 (hito-4)** — benchmarks, informe, video.

Diseñar contra esta hoja de ruta, no adelantarla.

---

## 3. Alcance de la Etapa 1

### Sí entra

- **RF1** namespace: `ls`, `cd`, `mkdir`, `rmdir`, `rm`, `mv`, `stat`.
- **RF2** transferencia: `put` (particiona y sube) y `get` (descarga y reconstruye).
- Autenticación usuario/contraseña con JWT. Cada usuario ve solo su propio árbol.
- Verificación de integridad por bloque con SHA-256.
- Un ControlNode y un DataNode, cada uno con su API HTTP, en contenedores separados.
- Recolector manual de bloques huérfanos (`scripts/gc.py`).
- Logging estructurado JSON con tiempos, desde el primer commit.

### No entra — no lo implementes ni lo simules

- Replicación. En esta etapa R=1.
- Pipeline de escritura entre DataNodes, heartbeats, block reports, re-replicación.
- gRPC, mTLS, cifrado en reposo, 2FA, ACLs por grupo.
- Alta disponibilidad del ControlNode, edit log, failover.
- RF3 (`open`/`read`/`write`/`lock`), leases, lecturas por rango.
- Cualquier demonio o tarea en background. El GC se corre a mano.

### Costuras dejadas listas para N nodos

Aunque hoy haya un DataNode y una réplica, el diseño soporta N sin refactor:

- `block_replicas` con cardinalidad N desde ya (un bloque, varias filas).
- `data_nodes` con registro, capacidad y estado, aunque hoy tenga una fila.
- Interfaz `BlockPlacementPolicy` con un único método
  `select(block_size, replication_factor) -> list[DataNode]`, implementada por
  `SingleNodePlacement`. La Etapa 2 solo añade *power of d choices*.
- El ControlNode devuelve al cliente un **plan** de escritura/lectura que ya es una lista
  de réplicas por bloque, aunque hoy cada lista tenga un elemento.

---

## 4. Stack

Python 3.11+ · FastAPI + Uvicorn · Pydantic v2 (+ pydantic-settings) · SQLAlchemy 2.x +
SQLite · Typer + Rich · httpx · passlib[bcrypt] · pyjwt · structlog · pytest +
pytest-asyncio · Docker + docker-compose.

**No añadir dependencias sin preguntar.** En particular: nada de Prometheus,
OpenTelemetry ni exportadores de métricas. Logs JSON y nada más.

---

## 5. Modelo de metadatos

```
users(id, username UNIQUE, password_hash, created_at)

directories(id, parent_id NULL, name, owner_id, created_at)
    UNIQUE(parent_id, name)
    el root de cada usuario es una fila con parent_id NULL

files(id, directory_id, name, owner_id, size, block_size,
      state, created_at, committed_at, expires_at NULL, deleted_at NULL)
    state in {WRITING, COMMITTED, DELETED}
    UNIQUE(directory_id, name) solo sobre filas COMMITTED
    expires_at se fija al crear (estado WRITING) y se pone a NULL al hacer commit

blocks(block_id UUID PK, file_id, index, size, checksum_sha256)
    UNIQUE(file_id, index)

block_replicas(block_id, data_node_id, state, created_at)
    state in {PENDING, STORED}
    PK(block_id, data_node_id)

data_nodes(id, base_url, capacity_bytes, used_bytes, state, registered_at)
    state in {ALIVE, DEAD}
```

Reglas de dominio, a hacer cumplir en `domain/`:

- `rmdir` falla si el directorio no está vacío, salvo `--recursive`.
- Un archivo en estado `WRITING` no aparece en `ls` ni puede abrirse.
- **Reserva vencida**: archivo en `WRITING` con `expires_at` en el pasado = abandonado. No
  bloquea el nombre: si otro `create` pide la misma ruta, la reserva vencida se marca
  `DELETED` y se procede. Comprobación perezosa, nunca un barrido en background.
- Borrar es marcar `DELETED` y fijar `deleted_at`. Los bloques siguen en disco hasta el GC.
- Las rutas se normalizan y validan en un value object `Path`: rechaza `..`, rutas
  relativas sin cwd, nombres vacíos y caracteres de control.

---

## 6. Contratos de API

### ControlNode `/api/v1` — lo consume el cliente

```
POST /auth/register   {username, password}          -> 201
POST /auth/login      {username, password}          -> {access_token, expires_in}
```

Todo lo demás exige `Authorization: Bearer <token>`.

Namespace (RF1):

```
GET    /fs/ls?path=/a/b        -> {entries:[{name, type, size, created_at}]}
GET    /fs/stat?path=/a/b/c    -> {path, type, size, block_size, block_count, created_at}
POST   /fs/mkdir               {path, parents: bool}
DELETE /fs/rmdir?path=&recursive=
DELETE /fs/rm?path=
POST   /fs/mv                  {src, dst}
```

Transferencia (RF2). Escritura en tres fases para respetar WORM: reservar el plan, subir
directo a los DataNodes, y recién entonces commit.

```
POST /files/create   {path, size, block_size?}
     -> 201 {file_id, block_size, expires_at,
             blocks:[{block_id, index, size,
                      replicas:[{data_node_id, base_url}]}]}

POST /files/{file_id}/commit
     -> 200 {path, size, block_count}
     409 si algún bloque no está en estado STORED
     410 si la reserva venció (expires_at en el pasado)

POST /files/{file_id}/abort   -> 204

GET  /files/open?path=/a/b/c
     -> {file_id, size, block_size,
         blocks:[{block_id, index, size, checksum_sha256,
                  replicas:[{data_node_id, base_url}]}]}
```

### ControlNode `/internal/v1` — lo consumen el DataNode y el GC

```
POST /datanodes/register        {base_url, capacity_bytes} -> {data_node_id}
POST /blocks/{block_id}/stored  {data_node_id, size, checksum_sha256} -> 204
GET  /gc/orphan-blocks          -> {blocks:[{block_id, replicas:[{data_node_id, base_url}]}]}
POST /gc/confirm                {block_ids:[...]} -> 204
```

`GET /gc/orphan-blocks` devuelve bloques de archivos `DELETED` y de reservas vencidas.
`POST /gc/confirm` borra esas filas del metadato una vez el script confirmó que los bloques
ya no están en disco.

Autenticación interna: secreto compartido por header `X-DFSha-Internal-Secret`. En la
Etapa 3 esto pasa a gRPC con mTLS.

### DataNode `/api/v1` — lo consume el cliente

```
PUT    /blocks/{block_id}    body: bytes crudos
                             header X-DFSha-Checksum: <sha256 hex>
       -> 201, o 422 si el checksum no coincide, 409 si el block_id ya existe
       el DataNode notifica al ControlNode antes de responder 201
GET    /blocks/{block_id}    -> bytes crudos, header X-DFSha-Checksum
DELETE /blocks/{block_id}    -> 204
GET    /health               -> {status, used_bytes, capacity_bytes, block_count}
```

Los bloques son inmutables: reescribir un `block_id` existente es 409.

`used_bytes` y `block_count` se calculan del estado real en disco, no de un contador en
memoria. En la Etapa 2 `/health` se convierte en el heartbeat y la política de colocación
depende de esos números, así que tienen que ser fiables desde ya.

### Layout del DataNode

```
$DFSHA_DATA_DIR/blocks/<2 primeros chars del block_id>/<block_id>.blk
$DFSHA_DATA_DIR/blocks/<2 primeros chars del block_id>/<block_id>.meta
```

`.meta` es JSON con `size`, `checksum_sha256`, `created_at`. Escritura atómica: a `.tmp` en
el mismo directorio y luego `os.replace`.

---

## 7. Configuración

Todo por variables de entorno vía Pydantic Settings, documentadas en `.env.example`:

```
DFSHA_BLOCK_SIZE=67108864        # 64 MB default
DFSHA_CONTROL_URL=http://localhost:8000
DFSHA_DB_URL=sqlite:///./dfsha.db
DFSHA_JWT_SECRET=
DFSHA_JWT_TTL_SECONDS=3600
DFSHA_INTERNAL_SECRET=
DFSHA_DATA_DIR=/var/lib/dfsha
DFSHA_DATANODE_CAPACITY_BYTES=
DFSHA_WRITE_TTL_SECONDS=600      # vencimiento de reservas en WRITING
DFSHA_LOG_LEVEL=INFO
```

`DFSHA_JWT_SECRET` y `DFSHA_INTERNAL_SECRET` **no tienen default en el código**. Si faltan,
el servicio falla al arrancar con un mensaje claro. Un secreto por defecto en un repo
público es un hallazgo de seguridad, y este proyecto se evalúa en seguridad.

---

## 8. Logging estructurado

`common/logging.py` configura structlog para emitir **JSON por línea** a stdout con
`timestamp`, `level`, `service`, `event` y los campos propios de cada evento. No es
decorativo: son los datos de los benchmarks de la Etapa 4.

Eventos obligatorios, todos con `duration_ms`:

- `block.upload` — `block_id`, `size_bytes`, `data_node_id`, `duration_ms`, `ok`
- `block.download` — mismos campos
- `file.put` — `file_id`, `size_bytes`, `block_count`, `parallel`, `duration_ms`
- `file.get` — mismos campos
- `metadata.command` / `metadata.query` — `operation`, `duration_ms` (la separación CQRS
  tiene que verse también en las trazas)

---

## 9. Cliente CLI

Comandos: `login`, `register`, `ls`, `cd`, `pwd`, `mkdir`, `rmdir`, `rm`, `mv`, `stat`,
`put <local> <remoto>`, `get <remoto> <local>`.

- `cd` y `pwd` operan sobre un cwd **del lado del cliente**, persistido junto al token en
  `~/.dfsha/session.json`. El ControlNode no guarda sesión: es stateless.
- Las rutas relativas se resuelven contra el cwd antes de enviarse.
- `put`: consulta tamaño -> `/files/create` -> lee el archivo local en streaming por
  bloques sin cargarlo entero en memoria -> sube cada bloque al DataNode del plan ->
  commit. Si algo falla, `abort` en un bloque `finally`.
- `get`: `/files/open` -> descarga los bloques en orden de `index` -> verifica checksum de
  cada uno -> concatena en streaming al archivo destino.
- Barra de progreso con Rich en `put` y `get`.
- `--parallel N` (default 4) para subir y bajar bloques concurrentemente.

---

## 10. Recolector de bloques huérfanos

`scripts/gc.py`, a mano, sin demonio ni scheduler. Soporta `--dry-run`.

1. `GET /internal/v1/gc/orphan-blocks` -> bloques de archivos `DELETED` y reservas vencidas.
2. Por cada bloque, `DELETE /blocks/{block_id}` en cada réplica.
3. `POST /internal/v1/gc/confirm` para que el ControlNode borre las filas.
4. Resumen: bloques borrados, bytes liberados, fallos.

Por qué en la Etapa 1 y no después: sin GC, el `used_bytes` de cada DataNode queda inflado
por bloques muertos, y ese número es la entrada de la política de colocación de la Etapa 2.

---

## 11. Higiene del repositorio

El repositorio es **público**. No negociable:

- `.gitignore` cubre `.env`, `*.db`, `*.blk`, `*.meta`, `data/`, `__pycache__/`, `.venv/`,
  `.pytest_cache/`, `*.egg-info/`, `.dfsha/`.
- `.env.example` lleva nombres de variables con valores vacíos o de ejemplo evidentes,
  **nunca secretos reales**.
- Nada de archivos de prueba binarios versionados. Para eso está `scripts/gen_testfile.py`.
- `.github/workflows/tests.yml` corre `pytest` en cada push y pull request.

### Ramas

- Se trabaja en `etapa-N`, nunca directo sobre `main`.
- Al terminar la etapa: merge a `main` + tag `hito-N`.
- Proyecto en equipo: **nunca** `push --force` ni reescritura de historia publicada.
- Commits pequeños, formato conventional commits. Push a `etapa-N` al terminar cada
  componente, no todo acumulado al final.

---

## 12. Estructura

```
src/dfsha/
├── common/          # DTOs compartidos, checksum, errores, logging
├── control_node/
│   ├── main.py, config.py
│   ├── api/         # routers FastAPI, solo traducción HTTP <-> casos de uso
│   ├── commands/    # lado escritura CQRS
│   ├── queries/     # lado lectura CQRS
│   ├── domain/      # entidades y reglas: Path, File, Block, User
│   ├── repositories/
│   └── services/    # auth, placement
├── data_node/
│   ├── main.py, config.py, api/, storage.py
└── client/
    ├── cli.py, session.py, chunker.py, transfer.py
tests/{unit,integration}/
scripts/{gen_testfile.py,gc.py}
docker/{control_node,data_node,client}.Dockerfile
```
