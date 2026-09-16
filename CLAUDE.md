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
- Las comprobaciones de expiración de reservas son **perezosas**, en el momento en que se
  consulta o se pisa el nombre; nunca un barrido en background.
- La Etapa 1 no tenía ninguna tarea en background. La Etapa 2 introduce dos, y solo dos:
  el bucle de heartbeat del DataNode y el evaluador de pertenencia del ControlNode.
  Ninguna puede tumbar su proceso: las dos capturan, registran y siguen.

---

## 2. Hoja de ruta

- **Etapa 1 (hito-1)** — RF1 + RF2, un ControlNode y un DataNode, JWT, SHA-256 por
  bloque, GC manual, logging estructurado.
- **Etapa 2 (hito-2, actual)** — N DataNodes reales, gRPC ControlNode<->DataNode,
  heartbeat cada 3 s con métricas de carga, block report incremental, maquina de estados
  ALIVE/SUSPECT/DEAD, politica de colocacion *power of d choices* con restriccion de
  dominios de falla, deteccion de divergencia sin borrado automatico, y despliegue en
  AWS.
- **Etapa 3 (hito-3)** — replicación R=3 con pipeline y commit W=2, re-replicación ante
  caída, ControlNode primary/standby con edit log, réplicas de lectura CQRS, RF3 con
  leases, mTLS y cifrado en reposo.
- **Etapa 4 (hito-4)** — benchmarks, informe, video.

Diseñar contra esta hoja de ruta, no adelantarla.

---

## 3. Alcance por etapa

### Etapa 1 — sí entra

- **RF1** namespace: `ls`, `cd`, `mkdir`, `rmdir`, `rm`, `mv`, `stat`.
- **RF2** transferencia: `put` (particiona y sube) y `get` (descarga y reconstruye).
- Autenticación usuario/contraseña con JWT. Cada usuario ve solo su propio árbol.
- Verificación de integridad por bloque con SHA-256.
- Un ControlNode y un DataNode, cada uno con su API HTTP, en contenedores separados.
- Recolector manual de bloques huérfanos (`scripts/gc.py`).
- Logging estructurado JSON con tiempos, desde el primer commit.

### Etapa 2 — sí entra

- gRPC para el plano de control: `Register`, `Heartbeat` (bidireccional), `BlockReport`.
- Heartbeat cada 3 s con métricas de carga, `fault_domain` y `boot_id`.
- Block report incremental en cada latido, completo cada 20 y en cada reconexión.
- Máquina de estados `ALIVE`/`SUSPECT`/`DEAD` con reincorporación por `boot_id`.
- Colocación *power of d choices* con restricción de dominios de falla.
- Una dirección anunciada por nodo, fijada por la configuración del despliegue.
- Detección de divergencia **sin borrado automático**.
- `GET /api/v1/cluster/status` y `dfsha cluster`.
- Compose con cuatro DataNodes y material de despliegue en AWS.

### No entra hasta la Etapa 3 — no lo implementes ni lo simules

- Replicación efectiva. `DFSHA_REPLICATION_FACTOR` sigue en **1**: la política soporta
  R>1 y está probada para ello, pero el default no cambia hasta la Etapa 3.
- Pipeline de escritura entre DataNodes, quórum W, re-replicación automática.
- Alta disponibilidad del ControlNode, edit log, failover, réplicas de lectura.
- mTLS, cifrado en reposo, 2FA, ACLs por grupo.
- RF3 (`open`/`read`/`write`/`lock`), leases, lecturas por rango.
- El GC se sigue corriendo a mano.

### Decisiones de la Etapa 2

1. **Estado derivado, no almacenado.** El estado de un DataNode se calcula del ultimo
   heartbeat cada vez que se consulta (`membership.state_for`). La columna `state` solo
   se persiste para detectar la transicion y emitir el evento; si las dos discrepan,
   manda lo derivado. Asi no puede haber deriva silenciosa.
2. **SUSPECT no toca las replicas.** Saca al nodo de la colocacion pero sus bloques se
   siguen sirviendo. Es lo que evita que un hipo de red cueste una re-replicacion entera
   en la Etapa 3.
3. **`boot_id` dentro del volumen de datos** (`node.json`, junto a los bloques). Perder
   el volumen pierde el `boot_id`, que es justo lo que el ControlNode necesita detectar
   para dar las replicas por perdidas. Guardarlo fuera mentiria en ese caso.
4. **Un report incremental NUNCA marca MISSING.** El nodo solo manda lo que cambio, asi
   que la ausencia de un bloque no significa nada. Marcar MISSING con un incremental
   daria por perdido el disco entero en cada latido. Solo un report completo autoriza a
   concluir que falta algo. Blindado en `test_report_incremental_nunca_marca_missing`.
5. **Un report completo por cada reconexion.** El contador de "cada N latidos" vive en el
   DataNode; un stream que se rompe antes del latido N lo deja a medias, y con una red
   inestable podria no mandarse un completo nunca. El ControlNode manda `FullReportReq`
   en el primer latido de cada stream.
6. **La colocacion cuenta lo que ya asigno en la misma llamada.** Los bloques de un `put`
   se colocan todos en una unica llamada a `/files/create`, con una sola foto de carga.
   Sin ese recuento, con d=3 y cuatro nodos iguales el cuarto no entraria en la ventana ni
   una vez: el efecto manada que el power of d evita entre peticiones, reaparecido dentro
   de una.
7. **Nunca dos replicas en el mismo `data_node_id`.** Cuando no quedan dominios de falla
   libres se relaja el dominio y se avisa (`placement.domain_relaxed`), pero jamas se
   repite nodo: dos copias en el mismo disco se pierden juntas.
8. **El ControlNode no borra datos por una divergencia.** Un bloque de mas cuesta disco;
   uno de menos cuesta datos. Los huerfanos van al GC, que es manual, y las replicas
   ausentes a MISSING.
9. **Migracion de esquema: fallar pronto.** Un metadato de la Etapa 1 no tiene las
   columnas nuevas y `create_all` no las anade. Se detecta al arrancar y se falla con el
   comando exacto (`docker compose down -v`) y el aviso de que eso borra los datos.
10. **El evaluador de pertenencia no puede tumbar el ControlNode.** Captura `Exception`,
    registra y sigue; su intervalo es configurable.

### Costuras dejadas listas para N nodos

Aunque hoy haya un DataNode y una réplica, el diseño soporta N sin refactor:

- `block_replicas` con cardinalidad N desde ya (un bloque, varias filas).
- `data_nodes` con registro, capacidad y estado, aunque hoy tenga una fila.
- Interfaz `BlockPlacementPolicy` con un único método
  `select(block_size, replication_factor) -> list[DataNode]`, implementada por
  `SingleNodePlacement`. La Etapa 2 solo añade *power of d choices*.
- El ControlNode devuelve al cliente un **plan** de escritura/lectura que ya es una lista
  de réplicas por bloque, aunque hoy cada lista tenga un elemento.

Y las que la Etapa 2 deja para la Etapa 3:

- El stream de `Heartbeat` es **bidireccional** y el ControlNode ya empuja mensajes no
  solicitados (`FullReportReq`). Las ordenes de re-replicacion son un caso mas en el
  `oneof` de `ControlMessage`, no un transporte nuevo.
- El estado `MISSING` de una replica ya se detecta, se registra y se ve en los logs: es
  lo que disparara la recuperacion.
- `select(block_size, replication_factor)` no cambia de firma: la Etapa 3 solo sube el
  default de `DFSHA_REPLICATION_FACTOR`, que hoy es 1.

### Decisiones de la Etapa 3 — Bloque A

1. **PostgreSQL para todo menos las pruebas unitarias.** SQLite bloquea la base entera
   al escribir y con R=3 las escrituras de `block_replicas` se triplican; ademas un
   fichero local no lo comparten dos ControlNodes en maquinas distintas. Sin base
   compartida no hay alta disponibilidad. SQLite sobrevive donde sigue siendo mejor: en
   las pruebas rapidas, en memoria, sin nada que conservar.
2. **El esquema lo migra Alembic; el ControlNode solo comprueba.** Con tres instancias
   arrancando a la vez contra la misma base, migrar desde cada una seria una carrera.
   Migra el servicio `migrate`, de un solo uso; cada ControlNode verifica que la base
   esta en la ultima revision y falla pronto con el comando exacto si no. La regla de
   una linea: **PostgreSQL se migra, SQLite se crea con `create_all`**.
3. **`test_migracion_deja_el_mismo_esquema_que_los_modelos` es lo que hace util a
   Alembic.** Compara la base migrada contra `models.py` con `compare_metadata` y exige
   cero diferencias. Sin ella, anadir una columna y olvidar la revision pasa todas las
   pruebas (que usan `create_all`) y falla solo al arrancar contra PostgreSQL.
4. **La promocion de la replica de PostgreSQL es manual** (`deploy/RUNBOOK-postgres.md`).
   Un failover automatico correcto necesita un arbitro externo, proteccion contra doble
   promocion y una forma de avisar a los clientes: es otro proyecto. No confundirlo con
   el liderazgo del ControlNode, que si es automatico porque el ControlNode no tiene
   estado propio.
5. **`DFSHA_DB_REPLICA_URL` vacia es un modo soportado**, no una degradacion: sin ella
   `read_engine is write_engine` y el sistema se comporta como en las etapas anteriores.
   Es deliberado, y es lo que convierte el recorte numero 1 del alcance en borrar una
   variable de entorno en vez de deshacer codigo.
6. **El rol de replicacion de PostgreSQL es distinto del de la aplicacion.** Solo puede
   replicar: filtrarlo no da acceso al metadato. Su clave va en un `.pgpass` con
   permisos 0600, nunca en la linea de comandos, que cualquiera ve con `ps`.

### Liderazgo: la epoca es un token de aislamiento

Varios ControlNodes sin estado contra la misma base. Uno sostiene el lease (tabla
`leadership`, una fila) y es el unico que evalua la pertenencia de los DataNodes,
programa re-replicaciones (Bloque B) y recolecta. **Todos** sirven lecturas, planes de
escritura y autenticacion, y **todos** aceptan heartbeats por gRPC: el heartbeat escribe
en la base compartida, no en la memoria del proceso que lo recibe.

El lease solo no basta. El escenario que obliga a la epoca:

    t=0   A toma el lease (epoca 7) y empieza a evaluar la pertenencia
    t=1   A se congela: pausa larga del recolector, particion, contenedor sin CPU
    t=7   el lease de A vence sin que A se entere
    t=8   B lo toma con epoca 8 y empieza a trabajar
    t=9   A despierta EN MEDIO de su operacion, convencido de que sigue mandando

Si lo unico que A comprobo fue "soy el lider" antes de empezar, en t=9 escribe, y hay dos
lideres marcando nodos muertos y programando la misma re-replicacion dos veces.

Las cuatro reglas que lo cortan:

1. **La epoca viaja con la operacion** (`Fencing(leader_id, epoch)`), no se consulta.
   **No existe ningun `soy_el_lider()`** consultable por separado: ese es exactamente el
   patron que deja pasar a A.
2. **Se verifica dentro de la misma transaccion que la escritura**, con `SELECT ... FOR
   UPDATE` sobre la fila del lease. Comprobar en una transaccion y escribir en otra deja
   una ventana entre ambas, y por esa ventana se cuela A.
3. **La epoca solo sube.** Nunca baja ni se reinicia.
4. **Recuperar el propio lease vencido tambien sube la epoca.** Si un lease vencido
   propio se tratara como renovacion, A volveria con la epoca 7 intacta y validaria el
   trabajo que empezo antes de la pausa. Entre la 7 y la 9 pudo pasar cualquier cosa.

`services/leadership.py` **propone** una epoca (vista local, puede estar obsoleta);
`commands/leadership.require_leadership` la **verifica**. Esa division es el diseno.

#### Aviso para quien refactorice esto

En algun momento alguien va a mirar este codigo y va a pensar que falta algo obvio:

```python
# NO. Esto deshace el Bloque A entero.
if not leadership.soy_el_lider():
    return
hacer_el_trabajo()
```

Parece una simplificacion evidente y es **la vulnerabilidad exacta** que la epoca existe
para cerrar. Dos motivos, y el segundo es el que no se ve:

1. **`soy_el_lider()` solo puede responder con la vista local**, que es precisamente lo
   que esta mal en el unico caso que importa: un proceso congelado cree que sigue
   mandando. Responder desde la base tampoco arregla nada, por el motivo siguiente.
2. **Entre la comprobacion y la escritura hay una ventana.** Aunque `soy_el_lider()`
   consultara la base, el lease puede cambiar de manos entre esa consulta y el `UPDATE`
   de mas abajo. La unica forma de cerrarla es que la comprobacion y la escritura esten
   en la misma transaccion y bajo el mismo cerrojo de fila, y eso obliga a que la epoca
   entre como **parametro** de la operacion, no como una pregunta previa.

Por eso `evaluate_membership` recibe `fencing: Fencing` y llama a `require_leadership`
**dentro** de su propio `with uow:`. La incomodidad de tener que pasar la epoca por todas
partes no es un descuido de diseno: es la firma del tipo obligando a que nadie pueda
escribir sin ella. Si esa incomodidad desaparece en un refactor, la proteccion tambien.

La misma regla se aplica a todo lo que el Bloque B anade (programar re-replicacion) y a
lo que quede del recolector: si escribe y exige liderazgo, recibe `Fencing` y lo verifica
dentro de su transaccion.

Lo comprueban `test_el_lider_congelado_es_rechazado_y_no_escribe_nada` (rechazo **y**
cero escrituras) y `test_el_lider_congelado_que_recupera_el_lease_sigue_sin_validar_lo
_viejo`.

**Que se prueba donde, y por que.** SQLite no implementa `FOR UPDATE` y SQLAlchemy lo
omite en ese dialecto. En SQLite se prueba la **comparacion de epoca**; la **exclusion
mutua** solo puede probarse contra PostgreSQL de verdad, en
`tests/integration/test_leadership_postgres.py`, que se salta sin `DFSHA_TEST_PG_URL` y
corre en el CI, que levanta un servicio PostgreSQL para eso. Ocho hilos saliendo de una
barrera comun: gana exactamente uno.

Medido en el stack desechable: matar al lider da relevo en **5,8 s** con la epoca subiendo
de 2 a 3, dentro de los 10 s exigidos.

**`leadership.renewed` se emite a nivel DEBUG**, no INFO. Con el default de 2 s y tres
instancias son 1,5 lineas por segundo para siempre, y ahogarian los eventos que si
cuentan algo. `leadership.acquired`, `leadership.lost` y `leadership.epoch_rejected`, que
son los sucesos de verdad, van a INFO y WARNING.

### Enrutado CQRS: que consulta va a donde

La separacion `commands/` / `queries/` existe desde la Etapa 1. Aqui se cobra.

| Operacion | Destino | Por que |
|---|---|---|
| `ls`, `stat`, `open`, `cluster/status` | **replica** | Consultas puras. Son el grueso del trafico de lectura |
| Cualquiera de las anteriores con `X-DFSha-Read-LSN` por delante de la replica | **primario** | Read-your-writes: la replica todavia no tiene lo que este cliente escribio |
| Todos los comandos (`mkdir`, `rm`, `mv`, `create`, `commit`, `abort`) | **primario** | Escriben |
| `POST /internal/v1/blocks/{id}/stored` | **primario** | Escribe |
| `GET /internal/v1/gc/orphan-blocks` | **primario** | Es una consulta, pero su respuesta dispara un **borrado en disco**. Una replica retrasada podria listar un bloque que ya no toca borrar. La regla: una consulta cuya respuesta dispara una escritura destructiva no se sirve desde la replica |
| `auth/register`, `auth/login` | **primario** | `register` escribe; `login` lee credenciales recien creadas, que es el caso exacto de read-your-writes |

**Read-your-writes, en concreto.** La replicacion es asincrona: un `mkdir /a` seguido de
un `ls /` puede preguntarle a una replica que aun no reprodujo el `mkdir`. Eso no es un
poco de retraso, es mentirle al cliente sobre su propia escritura. El mecanismo:

1. Tras cada comando, un middleware del ControlNode devuelve `pg_current_wal_lsn()` en
   `X-DFSha-Write-LSN`. Va en middleware y no en cada caso de uso porque tiene que
   medirse **despues** del commit.
2. El cliente lo guarda en `session.json` (cada invocacion de `dfsha` es un proceso
   nuevo: en memoria no serviria) y lo reenvia como `X-DFSha-Read-LSN`.
3. El ControlNode compara con `pg_last_wal_replay_lsn()` de la replica. Si va por detras,
   la consulta se atiende desde el primario.

El coste cae **solo sobre los clientes que acaban de escribir**: quien no manda LSN va
derecho a la replica sin consulta adicional. Y todos los caminos de fallo caen del lado
seguro: un LSN mal formado, una replica inalcanzable o un `DFSHA_DB_REPLICA_URL` que por
error apunta a un primario acaban sirviendo desde el primario.

---

## 4. Stack

Python 3.11+ · FastAPI + Uvicorn · Pydantic v2 (+ pydantic-settings) · SQLAlchemy 2.x ·
**PostgreSQL** (SQLite solo en pruebas) · **Alembic** · Typer + Rich · httpx ·
passlib[bcrypt] · pyjwt · structlog · pytest + pytest-asyncio · Docker + docker-compose ·
nginx como balanceador.

Dependencias anadidas en la Etapa 3, con permiso explicito: `alembic`, `psycopg[binary]`
y (en el Bloque C) `cryptography`. **Se descarto `argon2-cffi`**: la clave del usuario se
deriva con PBKDF2-HMAC-SHA256 a 600 000 iteraciones, que esta en la biblioteca estandar.
Argon2id seria preferible por su resistencia a hardware especializado (GPU y ASIC); se
eligio PBKDF2 por no anadir dependencia. Es un limite reconocido, no una omision.

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
DFSHA_DATANODE_CAPACITY_BYTES=
DFSHA_WRITE_TTL_SECONDS=600      # vencimiento de reservas en WRITING
DFSHA_LOG_LEVEL=INFO

# --- Etapa 2 ---
DFSHA_DATANODE_ADVERTISE_URL=http://localhost:8001   # alcanzable por el CLIENTE
DFSHA_DATANODE_FAULT_DOMAIN=local-1                  # cadena opaca
DFSHA_CONTROL_GRPC_URL=control-node:9000
DFSHA_GRPC_PORT=9000
DFSHA_HEARTBEAT_INTERVAL_MS=3000
DFSHA_FULL_REPORT_EVERY_N=20
DFSHA_SUSPECT_AFTER_MS=10000     # menor que DEAD, o el servicio no arranca
DFSHA_DEAD_AFTER_MS=30000
DFSHA_MEMBERSHIP_INTERVAL_MS=1000
DFSHA_REPLICATION_FACTOR=1       # la Etapa 3 sube este default
DFSHA_PLACEMENT_D=3
DFSHA_MIN_FREE_BYTES=134217728
```

`DFSHA_DATANODE_BASE_URL` de la Etapa 1 pasó a llamarse `DFSHA_DATANODE_ADVERTISE_URL`,
sin alias: mantener dos nombres para lo mismo envejece mal. Un `.env` de la Etapa 1 falla
al arrancar con un mensaje claro.

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

Etapa 2:

- `node.registered` — `data_node_id`, `advertise_url`, `fault_domain`, `boot_id`,
  `rejoin_kind`
- `node.state_changed` — `from`, `to`, `data_node_id`, `fault_domain`, `silence_seconds`
- `heartbeat.received` — `data_node_id`, `sequence`, `used_bytes`, `lag_ms`
- `placement.selected` — `block_id`, `candidates`, `chosen`, `fault_domains`
- `placement.domain_relaxed`, `placement.insufficient_candidates`
- `divergence.unknown_block`, `divergence.missing_block`

Etapa 3 (Bloque A):

- `leadership.acquired` — `leader_id`, `epoch`, `previous_leader`, `self_recovery`
- `leadership.lost` — `leader_id`, `epoch`
- `leadership.renewed` — **a nivel DEBUG**; ver la nota de la seccion de liderazgo
- `leadership.epoch_rejected` — `epoch`, `current_epoch`, `expired`. El lider congelado
- `query.routed_to_primary` — `reason`, `client_lsn`, `replica_lsn`

---

## 9. Cliente CLI

Comandos: `login`, `register`, `ls`, `cd`, `pwd`, `mkdir`, `rmdir`, `rm`, `mv`, `stat`,
`put <local> <remoto>`, `get <remoto> <local>`, `cluster`.

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
│   ├── domain/      # entidades y reglas: Path, File, Block, User, Lease
│   ├── repositories/
│   └── services/    # auth, placement, leadership, read_routing
├── data_node/
│   ├── main.py, config.py, api/, storage.py
│   ├── heartbeat.py     # cliente gRPC del plano de control
│   └── runtime.py       # carga instantanea que alimenta el heartbeat
└── client/
    ├── cli.py, session.py, chunker.py, transfer.py
alembic/{env.py,versions/}      # migraciones del metadato (Etapa 3)
tests/{unit,integration}/
scripts/{gen_testfile.py,gc.py}
docker/{control_node,data_node,client}.Dockerfile
docker/{nginx/dfsha.conf,postgres/*.sh}
deploy/RUNBOOK-postgres.md      # promocion manual de la replica
```
