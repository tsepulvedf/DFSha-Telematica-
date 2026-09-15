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

directories(id, parent_id NULL, name, owner_id, created_at, deleted_at NULL)
    UNIQUE(parent_id, name) solo sobre filas con deleted_at NULL
    UNIQUE(owner_id) solo sobre filas con parent_id NULL: una raiz por usuario
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

Dos desviaciones respecto al esquema y los contratos originales, ambas descubiertas al
implementar y ambas deliberadas:

1. **`directories.deleted_at`**: los directorios se borran de forma lógica. `rmdir -r`
   marca `DELETED` los archivos que contiene, y esas filas tienen que sobrevivir hasta
   que el GC recoja sus bloques; pero `files.directory_id` apunta a `directories`, así
   que borrar físicamente la fila del directorio violaría la clave foránea, y quitar la
   clave foránea dejaría el metadato sin quien lo sostenga. Un directorio con
   `deleted_at` es invisible y su nombre queda libre.
2. **`GET /gc/orphan-blocks` devuelve también `size`**: el ControlNode ya conoce el
   tamaño de cada bloque. Sin ese campo, el GC tendría que hacer una petición extra por
   bloque al DataNode solo para poder informar cuántos bytes liberó.

Reglas de dominio, a hacer cumplir en `domain/`:

- `rmdir` falla si el directorio no está vacío, salvo `--recursive`.
- Un archivo en estado `WRITING` no aparece en `ls` ni puede abrirse.
- **Reserva vencida**: archivo en `WRITING` con `expires_at` en el pasado = abandonado. No
  bloquea el nombre: si otro `create` pide la misma ruta, la reserva vencida se marca
  `DELETED` y se procede. Comprobación perezosa, nunca un barrido en background.
- Borrar es marcar `DELETED` y fijar `deleted_at`. Los bloques siguen en disco hasta el GC.
- **Copy-on-write sobre una ruta ocupada**: `create` sobre un nombre ya `COMMITTED` se
  permite. En el **commit**, el archivo viejo pasa a `DELETED` y el nuevo a `COMMITTED`
  **en una sola transacción**. Si se hiciera en dos pasos, una caída en el medio dejaría la
  ruta sin archivo visible. Los bloques del viejo quedan para el GC.
- `mv`: si `dst` es un directorio existente, mueve dentro conservando el nombre; si `dst`
  no existe y su padre sí, renombra; si `dst` es un archivo existente, 409 (nada de
  sobrescritura silenciosa). El padre nunca se crea implícitamente.
- El `UNIQUE(directory_id, name)` parcial sobre `COMMITTED` se declara con `sqlite_where` y
  `postgresql_where` **juntos en el mismo objeto `Index`**, para no amarrarnos a SQLite
  antes de la migración a PostgreSQL de la Etapa 3.
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

### Alcance de gRPC (Etapa 2) — material del informe

**gRPC solo entre ControlNode y DataNode.** Cliente<->ControlNode y cliente<->DataNode
siguen en REST, sin cambios. Es una decision razonada, no una concesion a medias:

- El **trafico de control** son mensajes frecuentes (uno cada 3 s por nodo), pequenos y de
  esquema fijo, sobre una conexion que conviene mantener abierta. Ahi Protobuf y HTTP/2
  pagan: menos bytes, menos handshakes, y un contrato con tipos que el compilador revisa.
- El **trafico de cliente** mueve bytes crudos, se depura con `curl`, y se beneficia de
  una API legible y de herramientas HTTP estandar. Meterlo en gRPC solo anadiria una capa
  de opacidad sobre un `PUT` de un bloque.

Solo migran tres RPC: `Register`, `Heartbeat` y `BlockReport`. **`POST
/internal/v1/blocks/{block_id}/stored` se queda en REST**, y la razon importa: el DataNode
lo llama de forma sincrona antes de responder 201 al cliente, de modo que cuando el
cliente ve su bloque subido, el ControlNode ya lo sabe. Si esa confirmacion viajara en el
block report incremental del heartbeat, un `commit` inmediato podria llegar hasta 3 s
antes que la noticia y fallar con 409 por una carrera. La latencia del commit no puede
quedar atada al periodo del heartbeat. Los dos endpoints del GC tambien siguen en REST,
porque los usa el script, no el DataNode.

### ControlNode `/internal/v1` — lo consumen el DataNode y el GC

```
POST /datanodes/register        {base_url, capacity_bytes} -> {data_node_id}
POST /blocks/{block_id}/stored  {data_node_id, size, checksum_sha256} -> 204
GET  /gc/orphan-blocks          -> {blocks:[{block_id, size, replicas:[{data_node_id, base_url}]}]}
POST /gc/confirm                {block_ids:[...]} -> 204
```

`GET /gc/orphan-blocks` devuelve bloques de archivos `DELETED` y de reservas vencidas.
`POST /gc/confirm` borra esas filas del metadato una vez el script confirmó que los bloques
ya no están en disco.

Autenticación interna: secreto compartido por header `X-DFSha-Internal-Secret`,
comparado en tiempo constante. En la Etapa 3 esto pasa a gRPC con mTLS.

### DataNode `/api/v1` — lo consume el cliente

```
PUT    /blocks/{block_id}    body: bytes crudos
                             header X-DFSha-Checksum: <sha256 hex>
       -> 201, o 422 si el checksum no coincide, 409 si el block_id ya existe
       el DataNode notifica al ControlNode antes de responder 201
GET    /blocks/{block_id}    -> bytes crudos, header X-DFSha-Checksum
DELETE /blocks/{block_id}    -> 204
GET    /health               -> {status, used_bytes, capacity_bytes, block_count,
                                disk_free_bytes}
```

Los bloques son inmutables: reescribir un `block_id` existente es 409.

`used_bytes` y `block_count` se calculan del estado real en disco, no de un contador en
memoria; `used_bytes` suma solo los bytes de los `.blk`, ignorando los `.meta`. En la
Etapa 2 `/health` se convierte en el heartbeat y la política de colocación depende de esos
números, así que tienen que ser fiables desde ya.

`disk_free_bytes` sale de `shutil.disk_usage`, **nunca** de `capacity_bytes - used_bytes`.
Si el disco se llena por logs, la base de datos u otro contenedor, la resta miente y la
política de colocación de la Etapa 2 mandaría bloques a un nodo que no puede recibirlos.

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
DFSHA_DATANODE_BASE_URL=http://localhost:8001
DFSHA_DATANODE_CAPACITY_BYTES=
DFSHA_WRITE_TTL_SECONDS=600      # vencimiento de reservas en WRITING
DFSHA_LOG_LEVEL=INFO
```

`DFSHA_DATANODE_BASE_URL` no estaba en la lista original y la exige el contrato: el
DataNode tiene que decirle al ControlNode con que URL anunciarse, porque los bytes van
directos y el ControlNode se limita a repetirsela al cliente.

**Decision de la Etapa 1: el escenario soportado es el cliente en el HOST**, con
`http://localhost:8001`. Un unico DataNode solo puede registrar una URL, y "alcanzable"
significa cosas distintas desde el host y desde dentro de la red de compose. La variable
la lee el DataNode al registrarse, nunca el cliente, asi que ponerla en el servicio
`client` de compose no tiene ningun efecto.

El contenedor `client` de compose tiene dos limitaciones conocidas, ambas comprobadas
en ejecucion y documentadas en el README. El escenario soportado es el cliente del host;
el contenedor es solo una comodidad:

1. `put`, `get` y el GC fallan con "Connection refused", porque el plan trae `localhost`
   y ahi `localhost` es el propio contenedor del cliente. Los comandos de namespace si
   funcionan.

   **Sigue vigente al cerrar la Etapa 2, y es una decision, no un olvido.** La Etapa 1
   anoto aqui que la Etapa 2 lo resolveria; al disenar la Etapa 2 se decidio lo
   contrario, porque con N DataNodes el coste real quedo a la vista: soportarlo exigiria
   que **cada** nodo anunciara dos direcciones distintas (la del host y la de la red de
   compose) y que el ControlNode **eligiera entre ellas segun el origen de cada
   peticion**. Eso mete en el plano de control una inferencia sobre la topologia de red
   del cliente, que es justo el tipo de magia que rompe de formas dificiles de
   diagnosticar cuando aparecen NAT, tuneles o varias interfaces. La Etapa 2 fija
   **una sola direccion anunciada por nodo** (`DFSHA_DATANODE_ADVERTISE_URL`), elegida
   por la configuracion del despliegue. Quien quiera revertir esta decision en la
   Etapa 3 debe saber que el precio es ese, no un ajuste de una linea.

2. La sesion no sobrevive entre invocaciones de `docker compose run --rm client`, pese
   al volumen montado en `/home/dfsha/.dfsha`, que es donde `DFSHA_HOME` apunta. Si
   alguien lo retoma: el cliente ya dice por pantalla donde guardo la sesion y que
   fichero busco al no encontrarla, que es la mitad del diagnostico.

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
- `.gitattributes` fuerza LF en `*.py`, `*.sh` y Dockerfiles: sin eso, un checkout desde
  Windows rompe los builds de Docker.
- La ruta de trabajo de un integrante contiene un espacio (`F:\DFSha telematica`). Todas
  las rutas en Dockerfiles, `docker-compose.yml` y scripts van **entrecomilladas**.

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
