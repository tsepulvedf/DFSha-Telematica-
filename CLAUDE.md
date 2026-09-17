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
  La Etapa 3 añade dos más, cada una con su justificación escrita: el **renovador del
  lease de liderazgo** (Bloque A) y el **planificador de re-replicación** (Bloque B).
  **Ninguna de las cuatro puede tumbar su proceso**: todas capturan `Exception`,
  registran y siguen. Esa regla no se negocia al añadir la siguiente.

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
  **Hecho en la Etapa 3, Bloque B**: R=3, W=2.
- Pipeline de escritura entre DataNodes, quórum W, re-replicación automática.
  **Hecho en la Etapa 3, Bloque B.**
- Alta disponibilidad del ControlNode, edit log, failover, réplicas de lectura.
- mTLS, cifrado en reposo, 2FA, ACLs por grupo.
  **Hecho en la Etapa 3, Bloque C**: mTLS en el plano interno, cifrado extremo a extremo
  y ACLs con grupos. 2FA no entra: no esta en el enunciado.
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

### Decisiones de la Etapa 3 — Bloque B

1. **El pipeline necesita un buffer, y no es una chapuza.** El enunciado pide verificar
   el checksum **antes** de reenviar y reenviar **mientras** se escribe. Un SHA-256 no se
   puede verificar sin ver el ultimo byte, asi que reenviar en streaming puro y verificar
   antes de reenviar son incompatibles. La lectura que satisface las dos: recibir entero,
   verificar, y **escribir a disco y reenviar en paralelo**. El reenvio no espera al
   `fsync`, y lo que sale del nodo ya esta comprobado, asi que una corrupcion no se
   propaga por la cadena. El coste es un bloque en memoria por subida concurrente, que ya
   era el comportamiento del DataNode desde la Etapa 1.

   Dicho de otro modo, para el informe: **no se puede verificar y reenviar a la vez**, asi
   que hay que elegir que se solapa con que. Solapar el reenvio con la ESCRITURA (no con
   la recepcion) da las dos garantias a la vez y solo cuesta memoria.

2. **Todo lo bloqueante sale del bucle de eventos. Esto costo un interbloqueo.** La
   primera version hacia la escritura y el reenvio dentro del `async def`, lo que deja el
   bucle de eventos del nodo parado: **mientras escribe o espera, el nodo deja de aceptar
   peticiones**. Con dos subidas concurrentes eso es una espera circular:

       bloque A: cliente -> DN1 -> DN2 -> DN3
       bloque B: cliente -> DN2 -> DN1 -> DN4

   DN1 espera a DN2, DN2 espera a DN1, y ninguno puede atender al otro. Las dos subidas
   mueren por timeout. Se reprodujo en las pruebas de integracion, que pasaron de **248 s
   en timeouts a 50 s**. El trabajo bloqueante va ahora a un hilo del pool
   (`run_in_threadpool`).

   **Aviso para quien refactorice esto.** `_escribir_y_reenviar` parece una funcion
   sincrona suelta que "podria" volver a meterse en el handler para ahorrarse una
   indireccion, y el `run_in_threadpool` parece una ceremonia innecesaria alrededor de
   codigo que no es lento. Las dos lecturas son erroneas por el mismo motivo: **el
   problema no es que el trabajo tarde, es que mientras tarda el nodo no puede atender a
   nadie**, y quien le esta llamando es un nodo que a su vez no puede atenderle a el. Un
   solo nodo con una sola subida no reproduce nada; hacen falta dos nodos y dos subidas
   cruzadas, que es por lo que un refactor puede deshacerlo y ver la suite en verde
   localmente. Las pruebas que lo atrapan son las de `test_replication.py` con
   `parallel=2` y cuatro nodos.

3. **Un fallo aguas abajo no tumba la subida.** Si el nodo escribio bien pero el
   siguiente falla, responde 201 con un `acked` menor. Fallar la peticion convertiria W=3
   en el minimo de hecho, que es lo contrario de la decision de quorum. El hueco lo recoge
   la cola de re-replicacion.

4. **El quorum se decide en el `commit` y solo ahi.** El `X-DFSha-Replicas-Acked` que ve
   el cliente es informativo; la cuenta buena es la de `block_replicas`, que cada DataNode
   actualiza por su cuenta. Que no haya carreras depende de una decision de la Etapa 2: el
   aviso `/internal/v1/blocks/{id}/stored` es **sincrono y anterior al 201**, asi que
   cuando el cliente puede pedir el commit, el ControlNode ya sabe de esas copias.

5. **W < R deja el archivo sub-replicado, no roto.** Con 2 de 3 todavia tolera perder un
   nodo. Rechazar el commit pondria la durabilidad por encima de la disponibilidad, que es
   la eleccion contraria a la que hacen estos sistemas. Con menos de W el commit falla y el
   cliente reintenta: no hay medias tintas.

6. **Leer prueba las replicas por orden.** Sin relevo en la lectura, tener tres copias no
   servia para leer: perder el primer nodo del plan hacia fallar el `get` aunque los otros
   dos tuvieran los bytes. Una replica que devuelve bytes **corruptos** tambien se descarta
   y se pasa a la siguiente, que es exactamente el caso para el que existe tener mas de una
   copia.

7. **`DFSHA_REPLICATION_FACTOR=1` en las pruebas de las etapas anteriores, fijado a
   mano.** No es un parche: esas pruebas describen el comportamiento con una replica por
   bloque, y heredar R=3 no las haria mejores, las haria medir otra cosa. Quien necesita
   tres nodos, los levanta.

### Re-replicacion: tres frenos y una regla que costo un 404

La dispara el hueco entre las copias que un bloque deberia tener y las que tiene. **Solo
la programa el lider, con epoca verificada dentro de la transaccion**, por la misma razon
del Bloque A agravada: dos lideres programando a la vez no duplicarian un evento de log,
duplicarian el trafico de copia de un cluster que ya se esta recuperando de una caida.

**Los tres frenos**, y ninguno es opcional:

1. **Espera de gracia** (`DFSHA_REREPLICATION_GRACE_MS`, 5 min; 30 s para el video). No se
   programa nada hasta que el nodo lleva ese tiempo muerto. Reiniciar un contenedor tarda
   segundos, y copiar su disco entero por un reinicio no solo es caro: se **encadena**, la
   copia satura la red, otro nodo deja de latir a tiempo, y se dispara otra copia. Se mide
   desde el ultimo latido: como un nodo pasa a DEAD en `dead_after`, exigir
   `dead_after + gracia` de silencio es exactamente "gracia desde que entro en DEAD".
2. **Tope por destino** (`DFSHA_REREPLICATION_MAX_PER_NODE`, 2). Sin el, la recuperacion
   se concentra en el nodo mas vacio y lo tumba por saturacion — el nodo que precisamente
   se ofrecio como destino por estar libre.
3. **Prioridad por copias restantes.** Un bloque con **una sola** copia va antes que uno
   con dos. No es una optimizacion: uno esta a un fallo de desaparecer y el otro todavia
   tolera una caida. Si el cluster no da abasto, el orden en que se rinde decide si se
   pierden datos. El desempate es por `block_id` y no por antiguedad, para que dos
   ControlNodes que miren el mismo estado decidan lo mismo.

**El destino tira, el origen no empuja.** La orden le llega a quien tiene que hacer el
trabajo y puede negarse si no le cabe; el origen solo ve una descarga mas, que es lo que
ya sabe hacer, y no se le carga de escrituras mientras quiza sirve lecturas. Ademas no hay
endpoint nuevo: se reutiliza `GET /blocks/{id}`.

**La regla que costo un 404, y que conviene no volver a aprender.** Al despachar una copia
hay que **crear la fila PENDING en `block_replicas` para el nodo destino**. Es la decision
3 de la seccion 1 aplicada tambien aqui: el ControlNode elige el destino y **registra la
eleccion**. La primera version no lo hacia, y el sintoma fue este: el nodo destino copiaba
el bloque correctamente, avisaba con `/blocks/{id}/stored`, y recibia
`404 bloque desconocido en el metadato`, porque `mark_stored` solo sabe actualizar una fila
que ya existe. Resultado: la copia quedaba en su disco como **huerfana**, la tarea no se
cerraba nunca, y el bloque aparecia en los logs como `divergence.unknown_block` cada seis
segundos. Todo el mecanismo funcionaba salvo el registro de la decision.

**Las ordenes viajan por el `oneof` de `ControlMessage`** (`ReplicateBlock`,
`DeleteBlock`), que es la costura que la Etapa 2 dejo hecha al declarar el stream
bidireccional: un caso mas, no un transporte nuevo. Y se leen de la **base**, no de la
memoria: el stream de un nodo lo puede estar atendiendo una instancia que no es la lider.

**Entregar una orden NO exige liderazgo; decidirla si.** Negarse a entregar una orden ya
tomada porque la atiende otra instancia dejaria la copia esperando a que el lease cambiara
de manos. Lo mismo con cerrar una tarea cumplida: no es una decision, es registrar un
hecho que ya ocurrio.

**El DataNode ejecuta las ordenes fuera del hilo del heartbeat.** Copiar 64 MB tarda;
hacerlo en el hilo del stream dejaria de mandar latidos mientras dura, el ControlNode
daria por muerto justo al nodo que esta haciendo el trabajo, y la respuesta a eso seria
programar todavia mas copias.

**La tercera y ultima tarea en background.** La Etapa 2 fijo que solo hubiera dos y que
anadir una se justificara. Esta se justifica porque un hueco de replicacion no lo provoca
ninguna peticion: lo provoca que algo **deje** de pasar, y nadie va a preguntar por el. Va
aparte del evaluador de pertenencia porque sus cadencias son muy distintas (1 s frente a
5 s con una gracia de 5 minutos) y porque un fallo escaneando la replicacion no debe
impedir que se siga detectando que un nodo se cayo.

**El GC tiene ahora dos vias.** El script de `scripts/gc.py` sigue siendo el que pide el
enunciado; `--via-control-plane` encola los borrados como `DeleteBlock` y viajan por el
heartbeat. La diferencia no es de eficiencia: por esa via **no hace falta tener ruta hasta
los DataNodes**, solo hasta el ControlNode, que en AWS es el unico caso posible desde
fuera de la VPC porque los nodos anuncian su IP privada. A cambio el borrado es asincrono,
y por eso esa via **no confirma**: las filas del metadato se quitan en una pasada
posterior, cuando conste que el bloque ya no esta en ningun disco. El ControlNode no borra
metadato sobre una promesa.

### Dos direcciones por nodo: la del cliente y la de sus pares

Un fallo del Bloque B que solo se vio al validarlo en Docker, y que **estaba en dos
sitios a la vez**: el pipeline de escritura y la re-replicacion. En contenedores no
replicaba al subir *ni* se recuperaba de una caida.

El sintoma: `dfsha put` fallaba con 409 «no alcanzan el quorum de escritura (W=2)», y
`dfsha cluster` mostraba 50 bloques repartidos pero 50 replicas en total en vez de 150.
Cada bloque quedaba con una copia. En los logs del DataNode:

    "next_hop": "http://localhost:8002", "error": "ConnectError",
    "error_detail": "[Errno 111] Connection refused"

La causa: el plan llevaba las `advertise_url` (`localhost:800N`), que son validas **desde
el host** y no desde dentro de un contenedor. DN1 reenviaba a `localhost:8002` y eso
resolvia a **si mismo**. El trafico DataNode -> ControlNode si funcionaba, porque usa el
nombre de servicio (`lb:8000`), y por eso el fallo parecia parcial.

#### La decision

El DataNode anuncia **dos** direcciones:

- `DFSHA_DATANODE_ADVERTISE_URL` — alcanzable por el **cliente**. Sin cambios.
- `DFSHA_DATANODE_PEER_URL` — alcanzable por **otros DataNodes**. Vacia = la misma que la
  anterior, que es el despliegue donde cliente y nodos comparten red. No es un caso
  degradado: es el caso simple.

**Esto no contradice la decision de la Etapa 2, y la diferencia es fina pero es la que
hay que defender.** Lo que la Etapa 2 rechazo fue que **el ControlNode infiriera** la
direccion segun el **origen de la peticion**: eso metia en el plano de control una
suposicion sobre la topologia de red del cliente, que rompe con NAT, tuneles o varias
interfaces. Aqui **no hay ninguna inferencia**: las dos direcciones son **estaticas** y su
**destinatario se sabe por la estructura del mensaje**, no por quien llama. El plan del
cliente lleva siempre `advertise_url`; la cadena del pipeline lleva siempre `peer_url`. El
ControlNode no elige: manda las dos y cada una va en su sitio.

Y lo que lo fuerza es **un hecho nuevo**, no un cambio de opinion: en la Etapa 2 **solo el
cliente hablaba con los DataNodes**; en la Etapa 3 **los DataNodes hablan entre si**. El
conjunto de «quien tiene que alcanzar este nodo» crecio, y los dos grupos estan en redes
distintas. Una sola direccion solo puede ser correcta para los dos si comparten red, que
es justo lo que dejo de ser cierto.

#### Donde va cada una

| Camino | Direccion | Por que |
|---|---|---|
| Plan de escritura y de lectura (`replicas[].base_url`) | **cliente** | Los bytes van directos del cliente al DataNode |
| Cadena del pipeline (`pipeline[]` del plan) | **par** | La recorre un DataNode reenviando a otro |
| `source_base_url` de una orden de re-replicacion | **par** | El nodo destino descarga del origen |
| `dfsha cluster`, mensajes de error | **cliente** | Es la que una persona puede probar con `curl` |
| GC por REST (`/gc/orphan-blocks`) | **cliente** | Lo consume el script, que corre donde el cliente |
| GC por el canal de control (`DeleteBlock`) | *ninguna* | La orden viaja por el stream del propio nodo |

El **ControlNode no llama nunca a un DataNode**: todo su trafico va por el stream gRPC que
abre el nodo. Por eso no hay direccionamiento ControlNode -> DataNode que equivocar, y por
eso el inventario de arriba esta completo.

`peer_base_url` en la entidad `DataNode` es el **unico** punto de lectura de la direccion
de par. Si en algun camino nodo-a-nodo vuelve a aparecer `advertise_url`, es este mismo
fallo otra vez.

#### Por que las pruebas del Bloque B no lo atraparon

Hay que decirlo sin rodeos: **pasaron con esto roto**. `start_cluster` levanta los cuatro
DataNodes como hilos uvicorn **en el mismo proceso, sobre 127.0.0.1**, asi que la
direccion que alcanza el cliente y la que alcanza un vecino son literalmente la misma. El
reenvio funcionaba porque no habia forma de que no funcionara.

`test_el_cliente_sube_los_bytes_una_sola_vez` comprobaba que el bloque acababa en tres
discos. Eso era cierto, pero verificaba el **mecanismo** y no el **direccionamiento**: en
esa topologia no se pueden distinguir. Daba una garantia mas debil de la que parecia dar.

La cobertura se corrigio en `tests/integration/test_addressing.py`, en dos niveles:

1. **Contrato**: que el plan separa las dos direcciones y que el cliente copia la cadena
   **tal cual** en vez de deducirla de `replicas`. Es la assercion que habria fallado
   desde el primer dia y no necesita red. La version para la re-replicacion vive en
   `tests/unit/test_rereplication_leadership.py`, y va ahi por un motivo concreto: en un
   cluster vivo el planificador despacha la copia y el DataNode la completa en
   milisegundos, asi que la prueba perderia la carrera contra el propio sistema.
2. **Escenario invertido** (`advertise_muerta=True`): un cluster donde la direccion de
   cliente **no responde desde ningun sitio**. Es la inversion del caso de Docker y tiene
   la misma propiedad util: cualquier camino nodo-a-nodo que use la direccion equivocada
   falla. Permite reproducir en un solo proceso un fallo que solo se manifestaba con dos
   redes. Misma tactica que `test_report_incremental_nunca_marca_missing` de la Etapa 2:
   montar el escenario donde la implementacion incorrecta produce un sintoma visible.

#### Un segundo fallo que destapo esa prueba

Al escribir la comprobacion de contrato de la re-replicacion aparecio otro: **el dominio
asignaba dos destinos para un bloque al que le faltaban dos copias, pero la cola solo
guarda una tarea viva por bloque** (indice unico parcial). Los dos despachos caian sobre
la misma fila y el segundo pisaba al primero: quedaba una copia programada en vez de dos,
y una fila PENDING en `block_replicas` apuntando a un destino que nunca recibiria la
orden.

`assign_targets` respeta ahora el mismo invariante que el esquema (`max_per_block=1`): un
bloque al que le faltan dos copias **recupera una por pasada**. Ademas de correcto es
preferible, por el mismo motivo que los otros tres frenos: reparte la recuperacion en el
tiempo en vez de concentrarla.

#### Consecuencia para el Bloque C

Los certificados tienen que cubrir **los nombres de par** (`data-node-N` en compose, las
**IP privadas** en AWS), porque son los que el nodo que llama verifica contra el SAN. Y
tambien los de cliente, porque el mismo proceso sirve las dos cosas. `gen_certs.py
--hosts` esta para eso, y desde aqui los nombres de par **no son opcionales**: sin ellos
el pipeline falla en el handshake en vez de en el `connect`, que es un fallo distinto con
el mismo efecto.

### Sobre-replicacion tras una reincorporacion: estado esperable

Observado en la validacion en Docker del Bloque B. `dfsha stat` mostro
`FULLY_REPLICATED (3-4 de 3 copias por bloque)`. **No es una anomalia.** La secuencia:

1. Un DataNode muere. El evaluador marca sus replicas `MISSING` — **no las borra**, que es
   la decision 8 de la Etapa 2.
2. Pasada la gracia, la re-replicacion pone una copia nueva en otro nodo. El bloque vuelve
   a tener 3 copias vivas, mas la fila `MISSING` del caido.
3. El nodo **vuelve** con su disco intacto y manda un report completo. `apply_block_report`
   trata un report completo como la verdad sobre ese disco, asi que la fila `MISSING`
   vuelve a `STORED`.
4. El bloque se queda con **4 copias** y R=3.

El estado de replicacion se deriva del bloque **peor** replicado, asi que el archivo sigue
siendo `FULLY_REPLICATED`: tener de mas en un bloque no empeora a ninguno. El rango
`3-4` se muestra a proposito — un numero plano escondería que los bloques no estan todos
igual.

**Que la fila `MISSING` sobreviva es lo que hace que una reincorporacion normal NO cueste
una re-replicacion.** Esta sobre-replicacion es la otra cara de esa misma propiedad: la
reincorporacion llego tarde, cuando la copia ya se habia repuesto.

#### Y NO se limpia sola. Esto corrige la intuicion natural

Parece que el GC lo recogera, y **no lo hace**: el GC recoge bloques de archivos `DELETED`
y de reservas vencidas (`list_orphans`), y este bloque pertenece a un archivo `COMMITTED` y
vivo. **La cuarta copia se queda.** Cuesta disco hasta que alguien decida quitarla; no
cuesta correccion, y la durabilidad solo mejora.

Tampoco es un huerfano ni una divergencia: `divergence.unknown_block` es para un bloque que
el metadato **no asocia** a ese nodo, y aqui si lo asocia, porque la fila nunca se borro.

Quitar la copia sobrante de forma automatica violaria la decision 8 de la Etapa 2 —el
ControlNode no borra datos por una divergencia— y la violaria en el peor sitio posible:
para deshacer una redundancia que el propio sistema acaba de conseguir. Si algun dia se
recortan las copias sobrantes, tiene que ser un mecanismo nuevo y deliberado, con su
propia decision escrita.

Fijado en `tests/unit/test_sobrereplicacion.py`, que ademas blinda lo que **no** debe
pasar: no se encola re-replicacion, no se cuenta como sub-replicado ni critico, no se
registra divergencia, y el GC no lo lista.

### Trampa de httpx 0.28: `cert=` se ignora si `verify` es una ruta

**Si tocas cualquier cliente HTTP con certificado, lee esto antes.** No es una
peculiaridad nuestra: es el comportamiento de una version concreta de una libreria, y
muerde en silencio.

`httpx.Client(verify="ca.crt", cert=("cli.crt", "cli.key"))` **descarta el certificado de
cliente**. Medido con httpx 0.28.1 contra un servidor con `ssl_cert_reqs=CERT_REQUIRED`:

| Como se construye el cliente | Resultado |
|---|---|
| `verify=<ruta>` + `cert=(crt, key)` | **el servidor cierra la conexion** |
| `verify=SSLContext(solo CA)` + `cert=(crt, key)` | 200 |
| `verify=SSLContext(CA + cert cargado)` | 200 |

`verify=<str>` esta deprecado en 0.28 y su camino construye el `SSLContext` por su cuenta
**ignorando `cert`**. El unico aviso que emite httpx es sobre `verify`; sobre el
certificado que acaba de tirar, ninguno.

El sintoma es un error de transporte —«Server disconnected without sending a response»—
que parece del servidor y es del cliente.

**Por eso hay un unico sitio donde se construye el contexto de cliente**,
`common/tls.client_ssl_context`, y lo usan el DataNode, el recolector y las pruebas. Si
aparece un `verify=<ruta>` junto a un `cert=` en algun sitio nuevo, es este fallo otra vez.

#### El patron, que es lo que va al informe

Los **dos** fallos serios de la Etapa 3 han sido el mismo tipo de cosa: **configuracion
que aparenta estar puesta y no lo esta**, y ninguno se detecto leyendo el codigo.

| Fallo | Que parecia | Que era | Como se detecto |
|---|---|---|---|
| Direccionamiento (Bloque B) | El plan llevaba la direccion de cada replica | Llevaba la del **cliente** en un camino entre nodos | Validando en **Docker**, no en las pruebas |
| `cert=` de httpx (Bloque C) | El cliente presentaba su certificado | httpx lo **descartaba** sin avisar | Midiendo tres combinaciones, no asumiendo |

Los dos pasaban por caminos que en el entorno de prueba no se distinguen del correcto: el
primero porque los nodos compartian espacio de red; el segundo porque todas las pruebas de
rechazo pasaban —el servidor cerraba la conexion, que es justo lo que se esperaba de un
intruso— y solo fallaba el camino bueno.

La leccion, y es material del informe: **una prueba que solo comprueba que lo malo se
rechaza no comprueba que lo bueno funciona**, y en seguridad las dos mitades hacen falta.
De ahi que `test_mtls.py` empiece por el caso bueno con un comentario que lo dice: si ese
falla, los demas no prueban nada.

### ACLs: cuatro reglas, una funcion, y ningun «denegar»

**No hay reglas de denegacion.** Solo concesiones, y la ausencia de concesion es
denegacion. Es la decision que mantiene el modelo explicable: en cuanto existe un
«denegar» hay que definir que gana cuando un ancestro permite y un descendiente niega, y
en que orden se evaluan los grupos, y ahi es donde estos sistemas dejan de poder contarse
en un parrafo.

Tres permisos **ordenados**: `READ < WRITE < ADMIN`. Que esten ordenados y no sean un
conjunto de banderas es lo que permite que la comprobacion sea un `>=` y no una tabla.

Las cuatro reglas viven en **una sola funcion**, `domain/acl.resolve`, pura y probada sin
base de datos:

1. **La herencia va HACIA ARRIBA.** Se sube por el arbol hasta la primera regla
   aplicable. No se propagan permisos a los hijos al conceder, porque eso se corrompe en
   cuanto se mueve un directorio: el hijo llevaria permisos de un padre que ya no es el
   suyo. Y `mv` es metadato puro (decision 2 de la seccion 1), asi que mover tiene que
   seguir siendo O(1).
2. **Gana la regla MAS CERCANA, aunque conceda MENOS.** Es la unica forma que tiene un
   modelo sin denegaciones de acotar el alcance: si se tomara el maximo de todo el camino,
   una concesion amplia arriba haria imposible dar menos permiso abajo.
3. **El MAXIMO entre lo concedido al usuario y a sus grupos**, que es la consecuencia
   directa de no tener denegaciones: los permisos solo pueden sumar.
4. **El propietario es ADMIN de su arbol, y NO como un caso especial cosido aparte**:
   entra por la misma funcion. Eso es lo que hace que todas las pruebas de las Etapas 1 y
   2 —cada usuario en su propio arbol— sigan describiendo el mismo comportamiento sin
   tocar ni una.

**El permiso minimo se pasa como argumento, no se comprueba despues.** `directory_for(uow,
user_id, path, minimum)` resuelve y exige a la vez. Un `resolve()` que devolviera el
permiso para que el llamante lo comparara seria el mismo patron que `soy_el_lider()`: una
consulta que alguien acabara olvidandose de mirar. Por eso subir un archivo pide `WRITE`
sobre el directorio destino, que es lo que separa a quien puede leer un directorio
compartido de quien puede meter cosas en el.

`/compartido-conmigo` es un directorio **virtual**: no existe en `directories`, se compone
al vuelo con las concesiones que apuntan a este usuario. No es una fila porque no tiene
dueno ni padre, y darle uno obligaria a inventar reglas para renombrarlo o borrarlo.

### Cifrado extremo a extremo: tres niveles de clave

El servidor **nunca ve una clave**. Con el metadato entero, la base de datos y todos los
bloques del disco, no puede descifrar un byte. Eso es lo que distingue cifrar en el
cliente de cifrar en el servidor, y es la afirmacion que
`test_con_el_metadato_entero_y_el_disco_no_se_descifra_sin_la_contrasena` comprueba por su
efecto.

    contrasena del usuario
        |  PBKDF2-HMAC-SHA256, 600 000 iteraciones, con users.kdf_salt
        v
    clave maestra  ------ no sale NUNCA del cliente
        |  AES-256-GCM, con el file_id como dato autenticado
        v
    clave de archivo  --- una por archivo; lo que se guarda es su ENVOLTURA
        |  AES-256-GCM, nonce = HMAC-SHA256(clave, "file_id:index")[:12]
        v
    bloque cifrado en el DataNode

Lo que el servidor guarda son **dos columnas inofensivas por separado**: `users.kdf_salt`,
que no es secreta —su trabajo es que dos usuarios con la misma contrasena tengan claves
distintas, por eso puede viajar en el login— y `files.wrapped_key`, que sin la clave
maestra es ruido.

**Una clave por archivo y no una por usuario**, aunque sea mas trabajo: asi compartir un
archivo puede llegar a ser entregar su clave, sin dar acceso a todo lo demas.

**El `file_id` entra en la envoltura como dato autenticado.** Sin eso, alguien con acceso
de escritura al metadato podria intercambiar envolturas y hacer que el cliente descifrara
el archivo equivocado con una clave que si valida. No rompe la criptografia; rompe la
integridad de lo que el usuario cree estar leyendo.

**La envoltura viaja en el COMMIT y no en el create**, y no es una preferencia: se envuelve
con el `file_id`, que en el momento de crear todavia no existe. Ademas es metadato que
tiene que quedar durable justo cuando el archivo se hace visible, que es la definicion del
commit.

**`wrapped_key` vacia = archivo SIN cifrar**, y el cliente lo **mira** en vez de suponerlo.
Es lo que permite que un metadato migrado desde la Etapa 2 siga siendo utilizable.

#### El nonce es DETERMINISTA, y eso es lo contrario de un descuido

`nonce = HMAC-SHA256(clave_archivo, "file_id:index")[:12]`. No se guarda en ninguna parte:
se vuelve a derivar al descifrar.

Repetir un nonce con la misma clave en GCM no degrada la seguridad, la **elimina**: revela
el XOR de los dos textos claros y permite falsificar mensajes. Un nonce aleatorio podria
repetirse **sin que nada lo detecte**. Aqui la unicidad esta garantizada por construccion:
`(file_id, index)` es unico por el `UNIQUE(file_id, index)` de `blocks`, y los bloques **no
se reescriben nunca** (WORM, decision 1 de la seccion 1). La propiedad criptografica se
apoya en una decision de diseno que ya estaba tomada en la Etapa 1.

Pasa por HMAC en vez de concatenar los valores en claro para que quien mire el disco no
pueda deducir la posicion de un bloque a partir de su nonce.

Efecto util: descifrar el bloque 1 con los bytes del 0 **falla**, en vez de devolver
basura. Un reordenamiento no pasa desapercibido.

#### El checksum va sobre el texto CIFRADO

El cliente cifra **y luego** calcula el SHA-256. Asi el DataNode verifica integridad sin
tener la clave, y —lo que importa para el Bloque B— **el pipeline puede seguir comprobando
antes de reenviar**. Con el checksum del texto claro, el DataNode no podria comprobar nada
y habria que fiarse del cliente.

Las dos comprobaciones no son redundantes: el SHA-256 lo verifica quien **no** tiene la
clave y protege contra corrupcion accidental; la etiqueta de GCM solo la puede verificar
quien **si** la tiene, y protege contra alteracion deliberada.

#### `files.size` es el claro; `blocks.size` es lo ALMACENADO

GCM anade 16 bytes por bloque (`crypto.TAG_BYTES`). Los dos tamanos dejan de coincidir, y
hay que decidir que guarda cada columna:

| Columna | Significa | Por que |
|---|---|---|
| `files.size` | bytes **claros** | Es el tamano del archivo que el usuario subio, y el que `ls` y `stat` tienen que mostrar |
| `blocks.size` | bytes **en disco** | Es contra lo que el cliente comprueba lo que descarga, y lo que cuentan la colocacion, la cuota y el GC |

Por eso el cliente declara `cipher_overhead` en `/files/create`: el ControlNode planifica
ahi los bloques y necesita saber cuanto ocupara cada uno **de verdad** antes de elegir
destinos. Y por eso `download_blocks` recibe `plain_size`: el archivo destino se reserva
con el tamano claro mientras los offsets se calculan restando el sobrecoste.

Esto costo el ultimo fallo del Bloque C, y es del mismo tipo que los otros dos: no se veia
leyendo el codigo, porque **cada mitad era coherente consigo misma**. El metadato decia el
tamano claro, el disco tenia el cifrado, y la comprobacion de tamano del cliente —que
existe desde la Etapa 1 y era correcta— fallaba en el cruce. El sintoma tampoco apuntaba
ahi: el relevo de replicas del Bloque B capturaba el `StorageError`, agotaba las replicas y
reportaba «ninguna de las 1 replicas pudo servir el bloque», que parece un problema de
disponibilidad. **Nadie escribe un 16 a mano**: sale de `crypto.TAG_BYTES`.

#### El limite reconocido, enunciado con precision

El modelo es **«el servidor nunca ve la clave»**, no «la clave nunca toca el disco». La
clave maestra se deriva en el login y se guarda en `~/.dfsha/session.json` para que `put` y
`get` no pidan la contrasena en cada invocacion —cada `dfsha` es un proceso nuevo, en
memoria no serviria—. Quien no quiera eso tiene `dfsha login --ask-password`, que no la
guarda y la pide en cada operacion.

El otro limite, ya escrito en la seccion 4: **Argon2id seria preferible** a PBKDF2 por su
resistencia a GPU y ASIC. Se eligio PBKDF2 por estar en la biblioteca estandar. Cambiarlo
es cambiar `derive_master_key` y nada mas.

Y uno que conviene decir en voz alta: **cambiar la contrasena no re-cifra nada**. La clave
maestra cambiaria y las envolturas existentes dejarian de abrirse. Re-envolverlas exige
tener la contrasena vieja y la nueva a la vez, es decir, hacerlo **durante** el cambio y
desde el cliente. No esta implementado, y el CLI lo dice al fallar en vez de dejar un
archivo ilegible sin explicacion.

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
users(id, username UNIQUE, password_hash, kdf_salt, created_at)
    kdf_salt: sal del KDF del CLIENTE. NO es secreta y viaja en el login; su
    trabajo es que dos usuarios con la misma contrasena tengan claves distintas

directories(id, parent_id NULL, name, owner_id, created_at, deleted_at NULL)
    UNIQUE(parent_id, name) solo sobre filas con deleted_at NULL
    UNIQUE(owner_id) solo sobre filas con parent_id NULL: una raiz por usuario
    el root de cada usuario es una fila con parent_id NULL

files(id, directory_id, name, owner_id, size, block_size,
      state, created_at, committed_at, expires_at NULL, deleted_at NULL,
      wrapped_key, key_algo)
    state in {WRITING, COMMITTED, DELETED}
    UNIQUE(directory_id, name) solo sobre filas COMMITTED
    expires_at se fija al crear (estado WRITING) y se pone a NULL al hacer commit
    size son bytes CLAROS: el tamano del archivo tal y como el usuario lo subio
    wrapped_key: clave del archivo envuelta con la maestra del usuario. Se fija
    en el COMMIT, porque se envuelve con el file_id. Vacia = SIN CIFRAR, que es
    como se reconocen los archivos de las Etapas 1 y 2

blocks(block_id UUID PK, file_id, index, size, checksum_sha256)
    UNIQUE(file_id, index) -- ademas de un invariante, es lo que garantiza que
    el nonce derivado de (file_id, index) no se repita nunca
    size son los bytes ALMACENADOS: con cifrado, los claros mas los 16 de la
    etiqueta de GCM. NO coincide con files.size; ver "Cifrado extremo a extremo"
    checksum_sha256 es el del texto CIFRADO, que es lo unico que el DataNode ve

block_replicas(block_id, data_node_id, state, created_at)
    state in {PENDING, STORED, MISSING}
    PK(block_id, data_node_id)
    la re-replicacion TAMBIEN inserta filas aqui al despachar una copia: el
    ControlNode elige el destino y registra la eleccion (ver Bloque B)

leadership(id=1, leader_id NULL, epoch, acquired_at, renewed_at, expires_at)
    una sola fila, sembrada por la migracion 0002
    epoch solo sube; es el token de aislamiento del Bloque A

rereplication_tasks(id, block_id, kind, state, source_node_id, target_node_id,
                    replicas_at_schedule, created_at, dispatched_at, sent_at,
                    expires_at, attempts, last_error)
    kind in {REPLICATE, DELETE}
    state in {PENDING, IN_FLIGHT, DONE, FAILED}
    UNIQUE(block_id) solo sobre filas PENDING o IN_FLIGHT: una tarea viva por
    bloque, que es lo que impide programar la misma copia dos veces cuando dos
    lideres se solapan durante un relevo

groups(id, name, owner_id, created_at)
    UNIQUE(owner_id, name): el nombre es unico POR DUENO, no globalmente

group_members(group_id, user_id, added_at)
    PK(group_id, user_id). Grupos PLANOS: un grupo no contiene a otro

acl_entries(id, directory_id, principal_type, principal_id, permission,
            granted_by, granted_at)
    principal_type in {1=USER, 2=GROUP}
    permission   in {1=READ, 2=WRITE, 3=ADMIN}, guardado como entero para que
                 el orden de potencia sea el del propio dato
    SOLO concesiones: no hay denegaciones. Se cuelgan de un DIRECTORIO, nunca de
    un archivo; ver "ACLs: cuatro reglas"

data_nodes(id, advertise_url, peer_url, capacity_bytes, used_bytes, state,
           registered_at, fault_domain, boot_id, stat_*)
    state in {ALIVE, DEAD}   (SUSPECT se deriva, no se guarda)
    advertise_url: alcanzable por el CLIENTE, UNIQUE
    peer_url:      alcanzable por OTROS DATANODES; vacia = la misma. Sin UNIQUE,
                   porque vacia en varios nodos a la vez es legitimo
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
POST /files/create   {path, size, block_size?, cipher_overhead?}
     -> 201 {file_id, block_size, expires_at,
             blocks:[{block_id, index, size,
                      replicas:[{data_node_id, base_url}],
                      pipeline:[peer_url, ...]}]}
     size son bytes CLAROS; cipher_overhead lo que el cifrado del cliente anade
     a cada bloque. El `size` de cada bloque del plan ya los lleva sumados: es
     lo que ocupara en disco

POST /files/{file_id}/commit  {wrapped_key?, key_algo?}
     -> 200 {path, size, block_count}
     409 si algún bloque no está en estado STORED
     410 si la reserva venció (expires_at en el pasado)

POST /files/{file_id}/abort   -> 204

GET  /files/open?path=/a/b/c
     -> {file_id, size, block_size, wrapped_key, key_algo,
         blocks:[{block_id, index, size, checksum_sha256,
                  replicas:[{data_node_id, base_url}]}]}
     wrapped_key vacia = archivo sin cifrar, y el cliente lo MIRA en vez de
     suponerlo: es lo que mantiene legibles los archivos de la Etapa 2
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

   **Matiz de la Etapa 3, y conviene leerlo entero antes de creer que esto cambio.** El
   DataNode anuncia ahora DOS direcciones, pero por un motivo distinto y sin volver a la
   inferencia que aqui se rechazo: la segunda es para OTROS DATANODES, que en la Etapa 2
   no hablaban entre si. El ControlNode sigue sin elegir segun el origen de la peticion.
   Ver "Dos direcciones por nodo" mas abajo.

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

Etapa 3 (Bloque B):

- `replication.pipeline` — `block_id`, `nodes`, `acked`, `duration_ms`, `ok`
- `replication.partial` — la cadena no confirmo todas las replicas planificadas
- `replication.quorum_met` / `replication.quorum_failed` — `quorum`, `min_replicas`,
  `under_replicated_blocks`
- `rereplication.scheduled` — `block_id`, `current_replicas`, `critical`, `epoch`
- `rereplication.dispatched` — `source`, `target`, `critical`
- `rereplication.completed` / `rereplication.failed`
- `rereplication.expired` — `requeued`, `gave_up`
- `block.replica_failed` — una replica no sirvio la lectura; se prueba la siguiente

---

## 9. Cliente CLI

Comandos: `login`, `register`, `ls`, `cd`, `pwd`, `mkdir`, `rmdir`, `rm`, `mv`, `stat`,
`put <local> <remoto>`, `get <remoto> <local>`, `cluster`.

Etapa 3: `share`, `unshare`, `shared` y los de grupos, y `login --ask-password`, que no
guarda la clave maestra en disco y la pide en cada `put` y `get`.

`login` **deriva la clave maestra en el cliente** y no la envia a ninguna parte. `put`
genera una clave por archivo, la envuelve y cifra los bloques antes de subirlos; `get`
abre la envoltura y descifra. Ver "Cifrado extremo a extremo".

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
│   ├── crypto.py        # las tres capas de clave (Bloque C)
│   └── tls.py           # UNICO sitio donde se construye un contexto TLS de cliente
├── control_node/
│   ├── main.py, config.py
│   ├── api/         # routers FastAPI, solo traducción HTTP <-> casos de uso
│   ├── commands/    # lado escritura CQRS
│   ├── queries/     # lado lectura CQRS
│   ├── domain/      # entidades y reglas: Path, File, Block, User, Lease, acl
│   ├── repositories/
│   └── services/    # auth, placement, leadership, read_routing, access, shared
├── data_node/
│   ├── main.py, config.py, api/, storage.py
│   ├── heartbeat.py     # cliente gRPC del plano de control
│   └── runtime.py       # carga instantanea que alimenta el heartbeat
└── client/
    ├── cli.py, session.py, chunker.py, transfer.py
alembic/{env.py,versions/}      # migraciones del metadato (Etapa 3)
tests/{unit,integration}/
scripts/{gen_testfile.py,gc.py,gen_certs.py}
docker/{control_node,data_node,client}.Dockerfile
docker/{nginx/dfsha.conf,postgres/*.sh}
deploy/RUNBOOK-postgres.md      # promocion manual de la replica
```
