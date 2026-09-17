# DFSha — sistema de archivos distribuido por bloques

**Hito 3 (en curso · Bloques A, B y C completos; falta TLS de cliente)** · SI3007 / ST0263 Sistemas Distribuidos

DFSha parte archivos en bloques de tamaño fijo, los reparte entre DataNodes y guarda todo
el metadato en un ControlNode. La arquitectura es de tipo HDFS, opción cliente/servidor:
el ControlNode dice *dónde* está cada bloque, pero **los bytes nunca pasan por él** — el
cliente habla directamente con los DataNodes.

El **Hito 1** entregó RF1 (namespace) y RF2 (transferencia) con un ControlNode y un
DataNode, autenticación JWT, integridad por bloque con SHA-256 y un recolector manual de
bloques huérfanos.

El **Hito 2** pasó a **N DataNodes reales**: un plano de control propio sobre gRPC con
heartbeats cada 3 s, detección de caídas en segundos, y una política de colocación
*power of d choices* que reparte los bloques según la carga real de cada nodo y su
dominio de falla.

El **Hito 3** quita los dos puntos únicos de fallo que quedaban.

El **ControlNode**: el metadato pasa a **PostgreSQL** (primario y réplica de lectura), el
esquema se versiona con **Alembic**, y hay **tres ControlNodes** tras un balanceador de
los que uno sostiene un **lease con época** — un token de aislamiento que impide que un
ControlNode congelado despierte y siga dando órdenes creyendo que todavía manda. Matar al
líder se recupera en menos de 6 segundos sin intervención.

Y los **datos**: cada bloque pasa a tener **tres copias en tres dominios de falla**,
subidas *en cadena* para que el cliente mande los bytes una sola vez. El `commit` pasa con
**dos** copias confirmadas y la tercera se completa después; si un nodo muere, la copia
que falta se **restaura sola** pasada una espera de gracia. Un archivo sobrevive a perder
dos de sus tres nodos.

Y la **seguridad**: el plano interno pasa a **TLS mutuo** con una CA propia, los archivos
se **cifran en el cliente** —el servidor guarda los bloques y no puede leerlos—, hay
**ACLs con grupos** para compartir, y cada petición de bloque lleva una **autorización
firmada** que el DataNode verifica por su cuenta. Más el **RF3**: `open`, lectura por
rango, `append` y bloqueo de archivos con lease.

> **Estado.** Los Bloques A (PostgreSQL, CQRS con réplica de lectura, elección de líder),
> B (replicación R=3 con pipeline, quórum W=2 y re-replicación) y C (mTLS, cifrado extremo
> a extremo, ACLs, token de bloque y RF3) están terminados y probados. Falta el TLS del
> tráfico de **cliente**, que es lo último del hito.

---

## Arranque rápido

Necesitas Docker y Python 3.11+. Debería llevarte menos de cinco minutos.

### 1. Crear el `.env` — obligatorio antes de nada

`docker compose up` **aborta** si no existe `.env` con los secretos rellenos:

```
falta DFSHA_JWT_SECRET; copia .env.example a .env
```

Es a propósito: los secretos no tienen valor por defecto en el código, porque uno por
defecto en un repositorio público es un hallazgo de seguridad.

```bash
git clone https://github.com/tsepulvedf/DFSha-Telematica-.git
cd DFSha-Telematica-
```

**bash / zsh / WSL:**

```bash
cp .env.example .env
sed -i "s|^DFSHA_JWT_SECRET=$|DFSHA_JWT_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')|" .env
sed -i "s|^DFSHA_PG_PASSWORD=$|DFSHA_PG_PASSWORD=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')|" .env
sed -i "s|^DFSHA_PG_REPLICATION_PASSWORD=$|DFSHA_PG_REPLICATION_PASSWORD=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')|" .env

# La URL de la base lleva dentro la contraseña que acabas de generar
sed -i "s|^DFSHA_DB_URL=.*|# DFSHA_DB_URL lo fija docker-compose.yml; esta linea solo vale sin Docker|" .env

grep -E '^DFSHA_(JWT_SECRET|PG_PASSWORD|PG_REPLICATION_PASSWORD)=.+' .env  # tres líneas
```

**PowerShell:**

```powershell
Copy-Item .env.example .env
$jwt = python -c "import secrets;print(secrets.token_urlsafe(48))"
$pg  = python -c "import secrets;print(secrets.token_urlsafe(24))"
$rep = python -c "import secrets;print(secrets.token_urlsafe(24))"
(Get-Content .env) `
  -replace '^DFSHA_JWT_SECRET=$', "DFSHA_JWT_SECRET=$jwt" `
  -replace '^DFSHA_PG_PASSWORD=$', "DFSHA_PG_PASSWORD=$pg" `
  -replace '^DFSHA_PG_REPLICATION_PASSWORD=$', "DFSHA_PG_REPLICATION_PASSWORD=$rep" | Set-Content .env

Select-String -Path .env -Pattern '^DFSHA_(JWT_SECRET|PG_PASSWORD|PG_REPLICATION_PASSWORD)=.+'  # tres
```

Rellenan las líneas vacías en su sitio, sin duplicar claves. En macOS el `sed -i`
del sistema pide un argumento: usa `sed -i ''` en lugar de `sed -i`.

`.env` está en `.gitignore`. No lo subas nunca.

### 2. Generar la CA y los certificados — también obligatorio

El plano interno usa **TLS mutuo**, y los servicios no arrancan sin su material:

```bash
python scripts/gen_certs.py
```

Deja en `certs/` la CA y un certificado por rol (`control`, `data`, `client`). **No se
versiona nada de esto**: `.gitignore` cubre `*.crt`, `*.key` y `certs/`.

El script **se niega a regenerar una CA existente** sin `--force`, y explica por qué:
volver a firmarla invalida todos los certificados emitidos, y un clúster a medio rotar
deja de hablar consigo mismo.

> Esto **sustituye a `DFSHA_INTERNAL_SECRET`**, que ya no existe. Un secreto compartido
> protege contra quien no lo conoce, pero no dice *quién* está al otro lado: cualquiera
> que lo tenga es todos a la vez. Con mTLS cada rol presenta su propio certificado.

### 3. Levantar el clúster

```bash
docker compose up --build -d
docker compose ps          # postgres x2, migrate, control-node-1..3, lb, data-node-1..4
curl http://localhost:8000/health
curl http://localhost:8001/health
```

Once servicios, en este orden de arranque:

| Servicio | Qué es |
|---|---|
| `postgres-primary` | El metadato. Todas las escrituras van aquí |
| `postgres-replica` | Réplica en streaming. Sirve el lado de consulta de CQRS |
| `migrate` | Aplica las migraciones y termina. Los ControlNodes esperan a que acabe |
| `control-node-1..3` | Tres instancias idénticas. Una sostiene el lease de líder |
| `lb` | nginx. Publica 8000 (REST del cliente), 8443 (plano interno) y 9000 (gRPC) |
| `data-node-1..4` | 8001–8004, cada uno con su volumen y su dominio de falla |

**El ControlNode no migra la base**: lo hace `migrate`, una sola vez. Con tres instancias
arrancando a la vez competirían por aplicar la misma migración. Cada ControlNode solo
comprueba al arrancar que la base está en la última revisión y falla pronto, con el
comando exacto, si no lo está.

Cada DataNode se registra solo por gRPC al arrancar, reintentando hasta que el
ControlNode responde, y a partir de ahí late cada 3 s. Late **contra el balanceador**, no
contra una instancia fija: así, matar al ControlNode líder no deja a ningún DataNode sin
camino.

### Ver la elección de líder

```bash
dfsha cluster          # última línea: lider, epoca y cuánto le queda al lease

# Matar al líder y ver el relevo
docker kill dfsha-control-node-2
sleep 8
dfsha cluster          # otro líder, y la época exactamente una más alta
```

La **época** es el número a mirar. Sube cada vez que alguien toma un lease vencido y no
baja nunca; si se repitiera, no serviría para nada. Ver «Elección de líder» más abajo.

### 3. Instalar el cliente

El cliente corre en tu máquina, no en un contenedor. Es el escenario que soporta la
Etapa 1 (ver [Dónde corre el cliente](#dónde-corre-el-cliente)).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 4. Reproducir el round-trip

```bash
export DFSHA_CONTROL_URL=http://localhost:8000

dfsha register ana               # pide la contraseña dos veces
dfsha login ana

dfsha cluster                    # los cuatro DataNodes, ALIVE, con su dominio

dfsha mkdir -p /datos/pruebas
dfsha ls /

# Un archivo de 50 MB con contenido reproducible, sin versionar binarios
python scripts/gen_testfile.py /tmp/original.bin --size 50MB
# anota el sha256 que imprime

dfsha put /tmp/original.bin /datos/pruebas/original.bin
dfsha stat /datos/pruebas/original.bin
dfsha get /datos/pruebas/original.bin /tmp/bajado.bin

sha256sum /tmp/original.bin /tmp/bajado.bin     # deben coincidir
```

Con el tamaño de bloque por defecto (64 MB) ese archivo cabe en un bloque. Para verlo
partido en 50 bloques, pásalo en la subida:

```bash
dfsha put --block-size 1048576 /tmp/original.bin /datos/pruebas/original.bin
dfsha stat /datos/pruebas/original.bin     # bloques: 50
dfsha cluster                              # los 50 repartidos entre los cuatro nodos
```

En `dfsha cluster` se ve el reparto: ningún nodo se lleva más de una fracción de los 50
bloques. Esa es la política de colocación funcionando, no el azar.

O levanta el clúster entero con bloques de 1 MB: `DFSHA_BLOCK_SIZE=1048576 docker compose
up --build -d`.

### 5. Ver el ciclo de borrado y el GC

Corre el GC **desde la raíz del repositorio**: encuentra los certificados en `certs/` por
su cuenta. No hace falta exportar nada.

```bash
dfsha rm /datos/pruebas/original.bin     # borrado lógico: los bloques siguen en disco
curl http://localhost:8001/health        # used_bytes todavía alto, block_count intacto

python scripts/gc.py --dry-run           # lista los huérfanos sin borrar nada
python scripts/gc.py                     # borrarlos de verdad

curl http://localhost:8001/health        # used_bytes y block_count de vuelta a cero
```

Si lo ejecutas desde otro directorio no encontrará `certs/`, y entonces hay que decirle
dónde está:

```bash
python scripts/gc.py   --tls-ca-cert certs/ca.crt --tls-cert certs/client.crt --tls-key certs/client.key
```

Presenta el certificado de **cliente**, no el del ControlNode, y eso importa: el GC pide la
lista de huérfanos y recibe con ella un **token de borrado por bloque**, firmado. No puede
fabricarlos él, así que quien decide qué es un huérfano sigue siendo el ControlNode.

---

## Arquitectura

```mermaid
flowchart LR
    CLI["Cliente (CLI)<br/>token + cwd en ~/.dfsha"]

    subgraph control["ControlNode"]
        REST["REST :8000<br/>api/ · routers"]
        GRPC["gRPC :9000<br/>ControlPlane"]
        CMD["commands/ · queries/<br/>CQRS"]
        PLACE["placement<br/>power of d choices"]
        DB[("SQLite<br/>metadato")]
        REST --> CMD
        GRPC --> CMD
        CMD --> PLACE
        CMD --> DB
        PLACE --> DB
    end

    subgraph datos["DataNodes"]
        DN1["data-node-1 :8001<br/>local-1"]
        DN2["data-node-2 :8002<br/>local-2"]
        DN3["data-node-3 :8003<br/>local-3"]
        DN4["data-node-4 :8004<br/>local-4"]
    end

    CLI -- "1· plan: que bloque,<br/>en que nodo" --> REST
    CLI == "2· bytes, directo<br/>PUT/GET /blocks/:id" ==> DN1
    CLI == " " ==> DN2
    CLI == " " ==> DN3
    CLI == " " ==> DN4

    DN1 -. "heartbeat 3s + block report<br/>(gRPC, stream bidireccional)" .-> GRPC
    DN2 -. " " .-> GRPC
    DN3 -. " " .-> GRPC
    DN4 -. " " .-> GRPC

    classDef bytes stroke-width:3px
    class CLI,DN1,DN2,DN3,DN4 bytes
```

La línea gruesa es el camino de los datos; la punteada, el plano de control. El
ControlNode no es un cuello de botella de ancho de banda: cada DataNode que se añade suma
capacidad de transferencia en lugar de saturar un nodo central.

### Dos direcciones por DataNode

Desde el Hito 3 un DataNode anuncia **dos** direcciones: una para el cliente y otra para
sus pares. Hasta el Hito 2 solo el cliente hablaba con los DataNodes, así que una bastaba;
ahora los DataNodes hablan **entre sí** (pipeline y re-replicación), y en `docker compose`
el cliente está fuera (`localhost:800N`) y los vecinos dentro (`data-node-N:8001`).

Con una sola dirección, DN1 reenviaba a `localhost:8002` y eso, dentro de un contenedor,
resuelve **al propio contenedor**. El síntoma era un `put` fallando con 409 «no alcanzan
el quórum» y 50 réplicas donde debería haber 150.

Esto **no** reabre la decisión del Hito 2 de no anunciar dos direcciones. Lo que allí se
rechazó fue que el ControlNode **infiriera** cuál usar según el origen de la petición, que
es una suposición sobre la red del cliente. Aquí no hay inferencia: las dos direcciones
son estáticas y su destinatario se sabe por la **estructura del mensaje** — el plan del
cliente lleva siempre la de cliente, la cadena del pipeline lleva siempre la de par.

### Replicación R=3: el cliente sube una vez

Con tres copias, la alternativa ingenua es que el cliente suba el mismo bloque tres veces.
DFSha lo sube **en cadena**: el cliente manda el bloque a la primera réplica del plan con
las otras dos en una cabecera, y cada DataNode lo reenvía al siguiente quitándose de la
lista. Con instancias pequeñas y un enlace doméstico, el ancho de subida del cliente es el
recurso más escaso, y multiplicarlo por tres es justo lo que no se puede permitir.

Dentro de cada nodo el orden es **verificar, luego escribir y reenviar a la vez**:

- **Verificar antes de reenviar** impide que una corrupción se propague por la cadena. Lo
  que sale de un nodo ya está comprobado.
- **Escribir y reenviar en paralelo** evita que la cadena sea la suma de las latencias de
  disco de tres nodos: el reenvío no espera al `fsync`.

Un fallo aguas abajo **no tumba la subida**. Si el primer nodo escribió bien y el tercero
falla, el cliente recibe `201` con un `X-DFSha-Replicas-Acked` menor. Fallar la petición
convertiría W=3 en el mínimo de hecho, que es lo contrario de lo que se decidió.

> **Un interbloqueo que costó encontrar, y que explica una línea del código.** La primera
> versión hacía la escritura y el reenvío dentro del handler asíncrono, lo que deja el
> bucle de eventos del nodo parado: mientras escribe, **el nodo deja de aceptar
> peticiones**. Con dos subidas concurrentes eso es una espera circular — DN1 esperando a
> DN2 y DN2 esperando a DN1 — y las dos mueren por *timeout*. Por eso todo el trabajo
> bloqueante sale a un hilo del pool. Las pruebas de integración pasaron de 248 s en
> timeouts a 50 s.

### Quórum W=2: legible con dos copias, completo con tres

El `commit` pasa con **dos** réplicas confirmadas. El archivo queda legible y la tercera se
completa en segundo plano.

Un archivo con 2 de 3 copias **no está roto**: todavía tolera perder un nodo. Rechazar su
commit pondría la durabilidad por encima de la disponibilidad, que es la elección contraria
a la que hacen estos sistemas. Con **menos de dos**, el commit falla y el cliente reintenta:
no hay medias tintas.

Quien decide el quórum es el ControlNode, contando filas de `block_replicas` — no la
cabecera que ve el cliente, que es informativa. Que eso no tenga carreras depende de una
decisión del Hito 2: el aviso de bloque almacenado es **síncrono y anterior** al `201`, así
que cuando el cliente puede pedir el commit, el ControlNode ya sabe de esas copias.

Se ve donde importa:

```bash
dfsha stat /video.mp4
  ...
  replicacion    FULLY_REPLICATED (3 de 3 copias por bloque)

dfsha cluster
  ...
  12 bloques sub-replicados (menos de 3 copias) · 1 con UNA sola copia — a un fallo de perderse
```

Los críticos se cuentan aparte de los sub-replicados porque **no cuestan lo mismo**: uno
con dos copias todavía tolera una caída; uno con una sola está a un fallo de desaparecer.

> **Puede aparecer `FULLY_REPLICATED (3-4 de 3)`, y es normal.** Si un nodo muere, se
> repone su copia y luego el nodo **vuelve** con su disco intacto, sus réplicas se
> readmiten y el bloque queda con una copia de más. Es la otra cara de la propiedad que
> hace que una reincorporación normal no cueste una re-replicación.
>
> Esa copia extra **no la recoge el GC**, que solo recoge bloques de archivos borrados y
> reservas vencidas: el bloque pertenece a un archivo vivo. Cuesta disco, no corrección.
> Quitarla automáticamente significaría que el ControlNode borra datos por su cuenta, que
> es justo lo que el diseño no hace.

### Re-replicación: tres frenos

Cuando un nodo muere de verdad, sus copias se rehacen solas. El mecanismo tiene más
capacidad de hacerse daño a sí mismo que ningún otro del sistema —reacciona a una caída
moviendo gigabytes, justo cuando el clúster ya va justo—, así que lleva tres frenos:

| Freno | Por defecto | Qué evita |
|---|---|---|
| Espera de gracia | 5 min desde `DEAD` | Copiar el disco entero de un nodo por un **reinicio de contenedor**, que tarda segundos. Es el error clásico, y se encadena: la copia satura la red, otro nodo deja de latir a tiempo, y se dispara otra copia |
| Tope por destino | 2 copias a la vez | Que la recuperación se concentre en el nodo más vacío y lo **tumbe por saturación** — el nodo que se ofreció como destino justo por estar libre |
| Prioridad | por copias restantes | Que un bloque con **una sola** copia espere detrás de uno con dos. Si el clúster no da abasto, el orden en que se rinde decide si se pierden datos |

**El destino tira, el origen no empuja.** La orden llega a quien tiene que hacer el
trabajo y puede negarse si no le cabe; el origen solo ve una descarga más, que es lo que ya
sabe hacer. Las órdenes viajan por el stream de heartbeat, en el `oneof` que el Hito 2
dejó preparado: un caso más, no un transporte nuevo.

La cola vive en PostgreSQL, no en la memoria del líder — si viviera en memoria se perdería
justo cuando más falta hace, que es cuando el líder cambia de manos — y **solo el líder la
programa**, con su época verificada dentro de la transacción. Dos líderes programando a la
vez no duplicarían un log: duplicarían el tráfico de copia de un clúster que ya se está
recuperando de algo.

Para verlo en vivo, con la gracia bajada:

```bash
DFSHA_REREPLICATION_GRACE_MS=30000 docker compose up -d
dfsha put ./archivo.bin /archivo.bin
docker kill dfsha-data-node-2
dfsha stat /archivo.bin     # UNDER_REPLICATED (2 de 3)
# ... 30 s ...
dfsha stat /archivo.bin     # FULLY_REPLICATED (3 de 3)
```

### El GC, ahora por dos vías

`scripts/gc.py` sigue siendo el recolector manual que pide el enunciado. La novedad es
`--via-control-plane`, que encola los borrados como órdenes que viajan por el heartbeat.

La diferencia no es de eficiencia: por esa vía **no hace falta tener ruta hasta los
DataNodes**, solo hasta el ControlNode. En AWS es el único caso posible desde fuera de la
VPC, porque los nodos anuncian su IP privada. A cambio el borrado es asíncrono, así que esa
vía **no confirma**: las filas del metadato se quitan en una pasada posterior, cuando
conste que el bloque ya no está en ningún disco. El ControlNode no borra metadato sobre una
promesa.

### Elección de líder: la época es un token de aislamiento

Los tres ControlNodes son idénticos y **ninguno guarda estado**: el token JWT lleva la
identidad y el cwd vive en el cliente. Por eso el balanceador puede repartir sin sesiones
pegajosas, y por eso una petición puede caer en cualquiera de los tres.

Pero hay trabajo que **no** puede hacer más de uno a la vez: evaluar qué DataNodes están
muertos y (en el Bloque B) programar re-replicaciones. Dos instancias haciéndolo en
paralelo marcarían el mismo nodo muerto dos veces y copiarían los mismos bloques por
duplicado. Para eso está el lease: una fila en PostgreSQL que una instancia sostiene
renovándola cada 2 s, y que caduca a los 6 s si deja de renovarla.

**Un lease solo no basta**, y este es el punto que merece el espacio:

```
t=0   A toma el lease (época 7) y empieza a evaluar la pertenencia
t=1   A se congela — una pausa larga del recolector de basura, una partición
      de red, un contenedor al que el planificador dejó sin CPU
t=7   el lease de A vence sin que A se entere
t=8   B lo toma con época 8 y empieza a trabajar
t=9   A despierta EN MEDIO de su operación, convencido de que sigue mandando
```

Si lo único que A comprobó fue «¿soy el líder?» antes de empezar, en `t=9` **escribe**. Y
entonces hay dos líderes dando órdenes contradictorias sobre el mismo clúster.

Lo que lo corta es que A lleve su época encima y se compruebe contra la almacenada
**dentro de la misma transacción que la escritura**. En `t=9` la fila dice 8, A trae 7, y
la operación se aborta entera sin dejar nada escrito. De ahí que en el código **no exista
ningún `soy_el_lider()`** consultable por separado: esa función es exactamente el patrón
que deja pasar a A.

Dos consecuencias que parecen detalles y no lo son:

- **La época solo sube.** Nunca baja ni se reinicia.
- **Recuperar el propio lease vencido también sube la época.** Si A vuelve y el lease
  está libre, lo toma con la 9, no con la 7. Entre una y otra pudo pasar cualquier cosa,
  y su trabajo a medio camino sigue invalidado. Es lo correcto.

Medido en un stack desechable: matar al líder da relevo en **5,8 s**, con la época
subiendo de 2 a 3.

```bash
curl -s localhost:8000/api/v1/cluster/leadership -H "Authorization: Bearer $TOKEN"
{"leader_id":"12801511-...","epoch":3,"is_self":false,
 "instance_id":"5c71586c-...","expires_in_seconds":4.25, ...}
```

`is_self` dice si te atendió el líder o una de las otras dos; `instance_id` dice cuál de
las tres te atendió. Con un balanceador delante es la única forma de saberlo.

**Qué exige liderazgo y qué no.** Lo exigen el evaluador de pertenencia, la
re-replicación (Bloque B) y el recolector. **No** lo exigen las consultas de metadatos,
los planes de escritura y lectura, la autenticación, ni el servidor gRPC que recibe
heartbeats: los DataNodes pueden latir contra cualquier instancia, porque el heartbeat
escribe en la base compartida y no en la memoria del proceso que lo recibe.

### CQRS: la réplica de lectura, y por qué no te miente

Las consultas (`ls`, `stat`, `open`, `cluster/status`) van a la réplica de PostgreSQL;
todo lo que escribe va al primario. La separación `commands/` y `queries/` existe en el
código desde el Hito 1 esperando este momento.

El problema de hacerlo es que la replicación es **asíncrona**. Un `mkdir /a` seguido de un
`ls /` puede preguntarle a una réplica que todavía no ha reproducido el `mkdir`, y eso no
es «un poco de retraso»: es mentirle al cliente sobre su propia escritura.

La solución es *read-your-writes* con el LSN del WAL:

1. Tras cada comando, el ControlNode devuelve `pg_current_wal_lsn()` en
   `X-DFSha-Write-LSN`. Va en un middleware y no en cada caso de uso porque tiene que
   medirse **después** del commit.
2. El cliente lo guarda en `~/.dfsha/session.json` — cada invocación de `dfsha` es un
   proceso nuevo, así que en memoria no serviría — y lo reenvía como `X-DFSha-Read-LSN`.
3. El ControlNode lo compara con `pg_last_wal_replay_lsn()` de la réplica. Si va por
   detrás, la consulta se atiende desde el primario.

El coste cae **solo sobre los clientes que acaban de escribir**: quien no manda LSN va
derecho a la réplica sin consulta adicional. Y todos los caminos de fallo caen del lado
seguro — un LSN mal formado, una réplica inalcanzable, o un `DFSHA_DB_REPLICA_URL` que por
error apunta a un primario acaban sirviendo desde el primario.

`GET /internal/v1/gc/orphan-blocks` es una consulta y **se queda en el primario** a
propósito: su respuesta dispara un borrado en disco. La regla, escrita en `CLAUDE.md`: una
consulta cuya respuesta dispara una escritura destructiva no se sirve desde la réplica.

**La promoción de la réplica ante caída del primario es manual**, y está en
[`deploy/RUNBOOK-postgres.md`](deploy/RUNBOOK-postgres.md). No confundirla con la elección
de líder: el ControlNode no tiene estado y puede elegir líder solo; una base de datos sí
lo tiene y no puede.

### Por qué gRPC solo en el plano de control

Es una decisión razonada, no una concesión a medias, y es material del informe.

| | Plano de control (ControlNode↔DataNode) | Plano de datos y cliente |
|---|---|---|
| **Transporte** | **gRPC** | **REST** |
| Cómo es el tráfico | mensajes pequeños, frecuentes (uno cada 3 s por nodo), de esquema fijo | bytes crudos, transferencias grandes y esporádicas |
| Qué se gana | Protobuf y HTTP/2: menos bytes, conexión persistente, contrato con tipos que el compilador revisa | depurable con `curl`, herramientas HTTP estándar, API legible |

Tres RPC viajan por gRPC: `Register`, `Heartbeat` (stream bidireccional) y `BlockReport`.

**`POST /internal/v1/blocks/{block_id}/stored` se queda en REST**, y la razón importa: el
DataNode la llama de forma síncrona antes de responder `201` al cliente, de modo que
cuando el cliente ve su bloque subido, el ControlNode ya lo sabe. Si esa confirmación
viajara en el block report del heartbeat, un `commit` inmediato podría adelantarse hasta
3 s a la noticia y fallar con `409` por una carrera. La latencia del commit no puede
quedar atada al periodo del latido.

### Estado de un DataNode

El estado se **deriva** del último heartbeat cada vez que se consulta, no se lee de una
columna que alguien tenga que acordarse de actualizar. Así no puede quedar
desincronizado.

| Estado | Cuándo | En la colocación | Sus réplicas |
|---|---|---|---|
| `ALIVE` | latido en los últimos 10 s | candidato | legibles |
| `SUSPECT` | sin latir 10 s | **fuera** | **siguen legibles** |
| `DEAD` | sin latir 30 s | fuera | marcadas `MISSING` |

Que `SUSPECT` no toque las réplicas es lo que evita que un hipo de red cueste una
re-replicación entera cuando llegue la Etapa 3.

Los tres umbrales son configurables, y **son agresivos a propósito**: con un latido cada
3 s, 10 s son tres perdidos y 30 s son diez. Se eligieron para que la transición quepa en
la demostración del hito; en producción serían mucho más largos, porque declarar muerto a
un nodo vivo es caro.

**Reincorporación por `boot_id`.** El `boot_id` se guarda en `node.json`, dentro del mismo
volumen que los bloques, y eso es el mecanismo, no un detalle: si el nodo vuelve con el
mismo `boot_id`, conserva su disco y sus réplicas se recuperan tras el primer report
completo; si vuelve con otro, perdió el volumen y sus réplicas pasan a `MISSING`, porque
los bytes ya no están.

### Cómo se elige el DataNode de cada bloque

`LeastLoadedPlacement`, en este orden:

1. **Filtrar**: solo `ALIVE` con `disk_free_bytes ≥ block_size + DFSHA_MIN_FREE_BYTES`.
2. **Ordenar** por ocupación (`used/capacity`), desempatando por escrituras en vuelo.
3. **Elegir al azar entre los `d` primeros** (d=3), no el primero. Coger siempre el más
   vacío provoca efecto manada: todas las escrituras concurrentes van al mismo nodo hasta
   el siguiente heartbeat, que es cuando el ControlNode se entera de que ya no está vacío.
4. **Dominios de falla**: cada réplica en un dominio distinto. Si no quedan libres, se
   relaja con un aviso (`placement.domain_relaxed`), pero **nunca dos réplicas en el mismo
   nodo**: dos copias en el mismo disco se pierden juntas.

Si no hay candidatos suficientes, el error dice cuántos se pidieron, cuántos había y por
qué se descartó cada nodo. Nunca se colocan en silencio menos réplicas de las pedidas.

La carga sale del **último heartbeat**, no del contador del ControlNode, que es solo una
caché. Y la política cuenta lo que ya asignó dentro de la misma llamada: sin eso, los 50
bloques de un `put` se colocarían con una única foto de carga y, con `d=3` y cuatro nodos
iguales, el cuarto no entraría en la ventana ni una sola vez.

### Divergencia: el ControlNode nunca borra datos

| Situación | Qué hace |
|---|---|
| El nodo reporta un bloque que el metadato no conoce | huérfano: se registra y queda para el GC |
| El metadato espera un bloque que el nodo no reporta | la réplica pasa a `MISSING` y se registra |

La asimetría es deliberada: un bloque de más cuesta disco, uno de menos cuesta datos. Ante
la duda, el sistema se queda con el bloque y avisa a un humano.

**Un report incremental nunca marca `MISSING`.** En un incremental el nodo manda solo lo
que cambió; que un bloque no aparezca significa que no se tocó, no que no esté. Solo un
report completo autoriza a concluir que falta algo. El ControlNode pide uno al abrir cada
stream de heartbeat, porque justo tras una reconexión es cuando su visión puede haber
quedado vieja.

### Escritura en tres fases

Un bloque, una vez cerrado, es inmutable (WORM). Por eso una escritura no puede ser una
sola llamada:

```mermaid
sequenceDiagram
    participant C as Cliente
    participant N as ControlNode
    participant D as DataNode

    C->>N: POST /files/create {path, size}
    N-->>C: 201 plan: block_ids + a qué DataNode va cada uno<br/>(archivo en WRITING, con expires_at)
    loop cada bloque, hasta --parallel a la vez
        C->>D: PUT /blocks/{id} + X-DFSha-Checksum
        D->>D: verifica SHA-256 y escribe .tmp + os.replace
        D->>N: POST /internal/v1/blocks/{id}/stored
        D-->>C: 201
    end
    C->>N: POST /files/{id}/commit
    N-->>C: 200 {path, size, block_count}<br/>(COMMITTED; la versión anterior pasa a DELETED)
```

Si el cliente se cae en medio, no pasa nada: la reserva vence a los
`DFSHA_WRITE_TTL_SECONDS` y deja de bloquear el nombre. Sus bloques quedan a la vista
del GC.

### Decisiones que explican el código

| Decisión | Consecuencia visible |
|---|---|
| El DataNode no conoce rutas lógicas | `mv` y `rename` son O(1) y no transfieren un solo byte |
| Colocación explícita, no por hash | El ControlNode elige el destino y lo **registra**; añadir nodos no reubica nada |
| WORM con fachada CRUD | Sobrescribir un archivo escribe bloques nuevos; el viejo pasa a `DELETED` en la misma transacción |
| CQRS | `commands/` y `queries/` separados, con trazas distintas, para replicar el lado de lectura en la Etapa 3 |
| Expiración de reservas | Un cliente caído no bloquea un nombre para siempre; es la base de los leases del RF3 |
| Borrado lógico + GC manual | `rm` responde al instante; los bytes se liberan cuando se corre `scripts/gc.py` |

---

## Endpoints

### ControlNode `/api/v1` — lo consume el cliente

| Método | Ruta | Cuerpo / parámetros | Respuesta |
|---|---|---|---|
| `POST` | `/auth/register` | `{username, password}` | `201` |
| `POST` | `/auth/login` | `{username, password}` | `{access_token, expires_in}` |
| `GET` | `/cluster/status` | — | `{nodes:[…], replication_factor, suspect_after_ms, dead_after_ms}` |
| `GET` | `/fs/ls` | `?path=/a/b` | `{entries:[{name, type, size, created_at}]}` |
| `GET` | `/fs/stat` | `?path=/a/b/c` | `{path, type, size, block_size, block_count, created_at}` |
| `POST` | `/fs/mkdir` | `{path, parents}` | `201` |
| `DELETE` | `/fs/rmdir` | `?path=&recursive=` | `204` |
| `DELETE` | `/fs/rm` | `?path=` | `204` |
| `POST` | `/fs/mv` | `{src, dst}` | `204` |
| `POST` | `/files/create` | `{path, size, block_size?}` | `201 {file_id, block_size, expires_at, blocks:[…]}` |
| `POST` | `/files/{file_id}/commit` | — | `200 {path, size, block_count}` · `409` si falta algún bloque · `410` si la reserva venció |
| `POST` | `/files/{file_id}/abort` | — | `204` |
| `GET` | `/files/open` | `?path=/a/b/c` | `{file_id, size, block_size, blocks:[…]}` |

Todo salvo `/auth/*` exige `Authorization: Bearer <token>`.

### ControlNode, plano de control — gRPC `:9000`

Solo lo consume el DataNode. Definición en
[`control.proto`](src/dfsha/common/proto/control.proto); el código se genera con
`python scripts/gen_proto.py` y no se versiona.

| RPC | Tipo | Para qué |
|---|---|---|
| `Register` | unario | Alta del nodo: URL anunciada, dominio de falla, `boot_id`, capacidad |
| `Heartbeat` | **stream bidireccional** | Latido cada 3 s con métricas y report incremental. El ControlNode responde `Ack` y, si su visión no cuadra, `FullReportReq` |
| `BlockReport` | unario | Report completo, fuera de banda porque puede ser grande |

El stream es bidireccional desde ya aunque hoy el ControlNode solo empuje esos dos
mensajes: es por donde la Etapa 3 mandará las órdenes de re-replicación, y añadirlas será
un caso más en el `oneof`, no rehacer el transporte.

### ControlNode `/internal/v1` — DataNode y GC

Exige la cabecera `X-DFSha-Internal-Secret`, comparada en tiempo constante. En la Etapa 3
pasa a mTLS.

| Método | Ruta | Cuerpo | Respuesta |
|---|---|---|---|
| `POST` | `/blocks/{block_id}/stored` | `{data_node_id, size, checksum_sha256}` | `204` |
| `GET` | `/gc/orphan-blocks` | — | `{blocks:[{block_id, size, replicas:[…]}]}` |
| `POST` | `/gc/confirm` | `{block_ids:[…]}` | `204` |

### DataNode `/api/v1` — lo consume el cliente

| Método | Ruta | Detalle |
|---|---|---|
| `PUT` | `/blocks/{block_id}` | Bytes crudos + `X-DFSha-Checksum`. `201`; `422` si el checksum no cuadra; `409` si el bloque ya existe |
| `GET` | `/blocks/{block_id}` | Bytes crudos + `X-DFSha-Checksum` |
| `DELETE` | `/blocks/{block_id}` | `204`, idempotente |
| `GET` | `/health` | `{status, used_bytes, capacity_bytes, block_count, disk_free_bytes, data_node_id, fault_domain, boot_id}` |

Los bloques son inmutables: reescribir un `block_id` existente es `409`, nunca una
sobrescritura.

Documentación interactiva: <http://localhost:8000/docs> y <http://localhost:8001/docs>.

---

## Cliente

```
dfsha register <usuario>              dfsha ls [ruta]          dfsha put <local> <remoto>
dfsha login <usuario>                 dfsha cd <ruta>          dfsha get <remoto> <local>
dfsha logout                          dfsha pwd                dfsha cluster
dfsha mkdir [-p] <ruta>               dfsha rm <ruta>          dfsha mv <origen> <destino>
dfsha rmdir [-r] <ruta>               dfsha stat <ruta>
```

Compartir (Bloque C):

```
dfsha share <ruta> <usuario|grupo> <read|write|admin>     dfsha shared
dfsha unshare <ruta> <usuario|grupo>                      dfsha acl <ruta>
dfsha group create|add|remove|list
```

RF3:

```
dfsha lock [--shared] <ruta>          dfsha read <ruta> --offset N --length N
dfsha unlock <ruta>                   dfsha append <remoto> <local>
dfsha locks <ruta>
```

- `cd` y `pwd` operan sobre un directorio de trabajo **del lado del cliente**, guardado
  junto al token en `~/.dfsha/session.json`. El ControlNode es stateless.
- `put` y `get` aceptan `--parallel N` (por defecto 4) para transferir bloques a la vez.
- `-v` emite los logs JSON de tiempos por stdout.
- `login` **deriva la clave de cifrado aquí** y no la manda a ninguna parte. Con
  `--ask-password` no la guarda en disco y la pide en cada `put` y `get`.
- `read` escribe el tramo pedido por la salida estándar, así que se encadena con `head`,
  `jq` o lo que sea. Descarga **solo los bloques que tocan el tramo**.
- `lock` vuelve a ejecutarse para **renovar**: el bloqueo vence solo si el proceso muere.

### `dfsha cluster`: por qué `bloq.` y `repl.` son dos columnas

Parecen redundantes y no lo son: **`bloq.` es lo que el nodo dice tener en su disco, y
`repl.` lo que el ControlNode cree que ese nodo le sirve.** Salen de sitios distintos, y
su diferencia es precisamente la señal.

Se ve al matar un nodo. En `SUSPECT` mantiene sus réplicas —sale de la colocación pero
sigue sirviendo lo que tiene—, así que las dos columnas siguen cuadrando. Al pasar a
`DEAD`, **`repl.` cae a 0 mientras `bloq.` se queda en 14**: los bytes siguen ahí, pero
el ControlNode ya no cuenta con ellos. Esa asimetría es la transición, visible sin abrir
un solo log.

La misma diferencia aparece por divergencia —un `.blk` borrado a mano baja `bloq.` y deja
`repl.` alto— y por eso la columna se pinta en amarillo cuando las dos no coinciden.

> **Git Bash en Windows**: MSYS reescribe los argumentos que empiezan por `/` y convierte
> `/datos/pruebas` en `C:/Program Files/Git/datos/pruebas` antes de que la CLI los vea.
> Usa PowerShell, CMD o WSL, o antepón una barra más: `dfsha cd //datos/pruebas`.

### Dónde corre el cliente

**La Etapa 1 soporta el cliente en el host.** Es el escenario del arranque rápido y el
único verificado de punta a punta.

Hay que elegir porque los bytes viajan directos entre cliente y DataNode: el ControlNode
se limita a repetirle al cliente la URL con la que el DataNode se registró, y un solo
DataNode solo puede registrar una. "Alcanzable" significa cosas distintas desde el host y
desde dentro de la red de compose, y ninguna URL sirve para las dos.

El interruptor es una sola variable, y va **siempre en el servicio del DataNode**:

| Escenario | `DFSHA_DATANODE_ADVERTISE_URL` en cada `data-node-N` | Estado |
|---|---|---|
| Cliente en el host | `http://localhost:800N` | **Soportado** (por defecto) |
| Cliente en un contenedor | `http://data-node-N:8001` | Limitación conocida, ver abajo |

No sirve de nada ponerla en el servicio `client`: el cliente nunca lee esa variable,
recibe la dirección dentro del plan que le devuelve el ControlNode.

Ojo con la asimetría del compose, que es deliberada: el DataNode se **anuncia** como
`localhost:800N` (lo consume el cliente, desde fuera) pero habla con el ControlNode por
`control-node:9000` y `control-node:8000` (tráfico interno, por dentro de la red).

Con N DataNodes el coste de soportar los dos escenarios a la vez queda más claro que en
la Etapa 1: **cada** nodo tendría que anunciar dos direcciones y el ControlNode elegir
entre ellas según el origen de cada petición, metiendo en el plano de control una
inferencia sobre la topología de red del cliente. Esa complejidad no se paga.

#### Limitaciones conocidas del servicio `client` de compose

El contenedor `client` existe para poder usar la CLI sin instalar Python, y trae el
`ENTRYPOINT` ya puesto, así que los comandos se invocan **sin repetir `dfsha`**:

```bash
docker compose run --rm client ls /        # correcto
docker compose run --rm client dfsha ls /  # NO: 'dfsha' sería el nombre de la ruta
```

Tiene dos limitaciones, ambas comprobadas en ejecución. Las dos desaparecen usando el
cliente del host, que es el escenario soportado.

**1. `put`, `get` y el GC no funcionan desde ahí.** Fallan con
`[Errno 111] Connection refused` en el primer bloque, porque el plan trae
`http://localhost:8001` y dentro de ese contenedor `localhost` es el propio contenedor
del cliente. Los comandos de namespace —`ls`, `mkdir`, `rmdir`, `rm`, `mv`, `stat`— sí
funcionan: son metadato puro contra el ControlNode. El síntoma es un error de conexión,
no de autenticación ni de ruta.

**2. La sesión no sobrevive entre invocaciones.** Tras un `login` correcto, el siguiente
`docker compose run` responde `no has iniciado sesion`. Cada `run` es un contenedor
nuevo, y aunque hay un volumen `dfsha-client-home` montado en `/home/dfsha/.dfsha`
—donde el cliente escribe, según `DFSHA_HOME`—, el `session.json` no reaparece.

No se ha arreglado en la Etapa 1: el escenario soportado es el cliente del host, donde la
sesión persiste en `~/.dfsha/session.json` sin nada de por medio, y el contenedor solo
es una comodidad. Lo que sí se hizo fue dejar de esconder el fallo: `login` imprime ahora
dónde guardó la sesión y el error de "no has iniciado sesión" dice qué fichero miró, así
que un vistazo a esas dos líneas basta para localizar el problema.

Si necesitas la CLI en contenedor con sesión persistente, encadena los comandos en una
sola invocación, que sí comparte el sistema de ficheros:

```bash
docker compose run --rm --entrypoint sh client -c "dfsha login ana --password X && dfsha ls /"
```

Para transferir desde un contenedor hay que cambiar de escenario, y entonces deja de
funcionar el cliente del host:

```bash
DFSHA_DATANODE_BASE_URL=http://data-node-1:8001 docker compose up -d --build
```

Corre el GC desde el host, que es donde sí funciona en ambos casos:

```bash
export DFSHA_CONTROL_URL=http://localhost:8000
python scripts/gc.py
```

Esto no es una carencia que haya que arreglar en la Etapa 1: con N DataNodes, la Etapa 2
lo resuelve en el sitio correcto, haciendo que el ControlNode anuncie a cada cliente la
dirección visible desde donde está.

---

## Configuración

Todo por variables de entorno; `.env.example` las lista todas.

| Variable | Por defecto | Para qué |
|---|---|---|
| `DFSHA_BLOCK_SIZE` | `67108864` (64 MB) | Tamaño de bloque |
| `DFSHA_CONTROL_URL` | `http://localhost:8000` | Dónde está el ControlNode |
| `DFSHA_DB_URL` | `sqlite:///./dfsha.db` | Metadato. En Docker, PostgreSQL primario. SQLite solo para pruebas |
| `DFSHA_DB_REPLICA_URL` | vacío | Réplica de lectura. **Vacío = todo al primario**, y es un modo soportado |
| `DFSHA_PG_PASSWORD` | — **obligatorio** | Clave del rol de aplicación de PostgreSQL |
| `DFSHA_PG_REPLICATION_PASSWORD` | — **obligatorio** | Clave del rol de replicación, que solo puede replicar |
| `DFSHA_JWT_SECRET` | — **obligatorio** | Firma de los tokens |
| `DFSHA_JWT_TTL_SECONDS` | `3600` | Vida del token |
| `DFSHA_TLS_CA_CERT` | — **obligatorio** | La CA contra la que todos se validan |
| `DFSHA_TLS_CERT` / `DFSHA_TLS_KEY` | — **obligatorio** | El certificado de este rol y su clave |
| `DFSHA_INTERNAL_PORT` | `8443` | Puerto del plano interno, separado del de cliente |
| `DFSHA_FILE_LOCK_TTL_MS` | `120000` | Vida de un bloqueo de archivo (RF3) sin renovar |
| `DFSHA_DATA_DIR` | `/var/lib/dfsha` | Dónde guarda bloques el DataNode |
| `DFSHA_DATANODE_ADVERTISE_URL` | `http://localhost:8001` | URL con la que se anuncia el DataNode, **alcanzable por el cliente** |
| `DFSHA_DATANODE_PEER_URL` | vacío | URL **alcanzable por otros DataNodes** (pipeline y re-replicación). Vacío = la misma que la anterior |
| `DFSHA_DATANODE_FAULT_DOMAIN` | `local-1` | Dominio de falla: cadena opaca, solo se compara igualdad |
| `DFSHA_CONTROL_GRPC_URL` | `lb:9000` | Dónde escucha el plano de control (el balanceador, no una instancia) |
| `DFSHA_DATANODE_CAPACITY_BYTES` | libre en disco | Capacidad anunciada |
| `DFSHA_GRPC_PORT` | `9000` | Puerto gRPC del ControlNode |
| `DFSHA_HEARTBEAT_INTERVAL_MS` | `3000` | Cada cuánto late un DataNode |
| `DFSHA_FULL_REPORT_EVERY_N` | `20` | Cada cuántos latidos manda un report completo |
| `DFSHA_SUSPECT_AFTER_MS` | `10000` | Silencio tras el que un nodo sale de la colocación |
| `DFSHA_DEAD_AFTER_MS` | `30000` | Silencio tras el que sus réplicas se dan por no disponibles |
| `DFSHA_MEMBERSHIP_INTERVAL_MS` | `1000` | Cada cuánto se evalúan las transiciones de estado |
| `DFSHA_LEASE_TTL_MS` | `6000` | Vida del lease de líder sin renovar |
| `DFSHA_LEASE_RENEW_MS` | `2000` | Cada cuánto renueva el líder. Debe ser menor que el TTL o el servicio no arranca |
| `DFSHA_WRITE_QUORUM` | `2` | Réplicas confirmadas que exige el `commit`. No puede ser mayor que R o el servicio no arranca |
| `DFSHA_REREPLICATION_GRACE_MS` | `300000` | Espera desde que un nodo entra en `DEAD` antes de copiar sus bloques |
| `DFSHA_REREPLICATION_MAX_PER_NODE` | `2` | Copias simultáneas hacia el mismo destino |
| `DFSHA_REREPLICATION_MAX_PER_PASS` | `8` | Copias despachadas por pasada del planificador |
| `DFSHA_REREPLICATION_INTERVAL_MS` | `5000` | Cada cuánto corre el planificador |
| `DFSHA_ORDER_WORKERS` | `2` | Copias simultáneas que un DataNode acepta ejecutar |
| `DFSHA_REPLICATION_FACTOR` | `3` | Copias por bloque |
| `DFSHA_PLACEMENT_D` | `3` | Tamaño de la ventana del *power of d choices* |
| `DFSHA_MIN_FREE_BYTES` | `134217728` | Margen de disco que un nodo debe conservar para ser candidato |
| `DFSHA_WRITE_TTL_SECONDS` | `600` | Vencimiento de las reservas de escritura |
| `DFSHA_LOG_LEVEL` | `INFO` | Nivel de log |

Los secretos **no tienen valor por defecto en el código** y deben tener al menos 16
caracteres. Un secreto por defecto en un repositorio público es un hallazgo de seguridad.

`DFSHA_INTERNAL_SECRET` **ya no existe**: lo sustituyó el mTLS del Bloque C. Un `.env`
viejo que todavía lo tenga no estorba —simplemente se ignora— pero le faltarán las tres
variables de TLS, y entonces el servicio no arranca y lo dice.

Los tres ficheros de TLS **no tienen valor por defecto**, igual que el secreto de JWT: un
plano interno que arranca sin autenticación porque se olvidó una variable es peor que uno
que no arranca. Y el DataNode además comprueba que los ficheros **existen**, para que el
fallo sea «no existe `certs/data.crt`» y no un error de handshake diez segundos después.

---

## Logs

Una línea JSON por evento a stdout, con `timestamp`, `level`, `service`, `event` y
`duration_ms`. No son decorativos: son los datos de los benchmarks de la Etapa 4.

```bash
docker compose logs -f control-node | jq 'select(.event | startswith("metadata"))'
dfsha -v put /tmp/original.bin /datos/x.bin | jq 'select(.event == "block.upload")'
```

| Evento | Campos propios |
|---|---|
| `block.upload` / `block.download` | `block_id`, `size_bytes`, `data_node_id`, `ok` |
| `file.put` / `file.get` | `file_id`, `size_bytes`, `block_count`, `parallel` |
| `metadata.command` / `metadata.query` | `operation` |
| `node.registered` | `data_node_id`, `advertise_url`, `fault_domain`, `boot_id`, `rejoin_kind` |
| `node.state_changed` | `from`, `to`, `data_node_id`, `fault_domain`, `silence_seconds` |
| `heartbeat.received` | `data_node_id`, `sequence`, `used_bytes`, `lag_ms` |
| `placement.selected` | `block_id`, `candidates`, `chosen`, `fault_domains` |
| `placement.domain_relaxed` · `placement.insufficient_candidates` | por qué se relajó o falló |
| `divergence.unknown_block` · `divergence.missing_block` | `data_node_id`, `block_id`, `source` |

La separación CQRS se ve en las trazas: los comandos y las consultas emiten eventos
distintos.

---

## Pruebas

```bash
pip install -e ".[dev]"
python scripts/gen_proto.py    # genera el codigo del .proto (no se versiona)
pytest -q                      # toda la suite
pytest tests/unit -q           # rápido, sin red
pytest tests/integration -q    # levanta ControlNode y DataNode reales en puertos reales

# Cuatro pruebas se SALTAN sin PostgreSQL. Ver abajo: no son opcionales, son las que
# no se pueden fingir.
DFSHA_TEST_PG_URL="postgresql+psycopg://dfsha:...@localhost:5432/dfsha" pytest -q
```

Las de integración arrancan servidores uvicorn de verdad en hilos y usan el cliente real,
para ejercitar el camino que un cliente simulado se saltaría: metadato contra el
ControlNode, bytes contra el DataNode.

Cubren, entre otros: round-trip de 50 MB en 50 bloques con SHA-256 idéntico, archivo
vacío y de un byte, checksum incorrecto rechazado con `422` y `commit` fallando con
`409`, aislamiento entre usuarios, `mv` sin tocar el DataNode, reserva vencida con `410`,
y el ciclo completo del GC con `used_bytes` restaurado.

Y los criterios de la Etapa 2, con cuatro DataNodes reales: los cuatro registrados y
`ALIVE` con su dominio, 50 bloques repartidos sin que ninguno se lleve más del 60 %, un
nodo con menos capacidad recibiendo una fracción menor, el ciclo completo de caída
(`SUSPECT` → `DEAD` → deja de recibir bloques → el `get` falla nombrando bloque y nodo →
reincorporación), y un `.blk` borrado a mano que acaba en `MISSING` sin que el ControlNode
borre nada.

Y los del Hito 3. **Bloque A**: que la migración de Alembic y `models.py` describen
**exactamente** el mismo esquema (`compare_metadata` con cero diferencias), el enrutado de
consultas con sus cuatro caminos de fallo, y el lease de líder.

**Bloque B**, con cuatro DataNodes reales: cada bloque en tres nodos y tres dominios; el
cliente hablando con **un** nodo por bloque mientras los otros dos reciben su copia de la
cadena; un nodo caído durante la subida que no impide el `commit` y deja el archivo
`UNDER_REPLICATED`; el archivo legible con **dos de tres nodos caídos** y el SHA-256
intacto; y el ciclo completo de recuperación —matar un nodo, espera de gracia,
re-replicación, vuelta a `FULLY_REPLICATED`— comprobando además que el archivo se sigue
bajando bien desde la copia nueva.

Y la otra mitad, que es la que no se ve si solo se prueba el camino feliz:
`test_un_reinicio_rapido_NO_dispara_una_copia`. Un nodo que se reinicia y vuelve enseguida
no debe costar ni una copia; sin ese freno, cada despliegue rodante movería el disco
entero.

Los umbrales van comprimidos en las pruebas (1,5 s y 3 s) para que la suite no tarde
minutos; la aritmética de los valores reales está cubierta por las unitarias de
`test_membership.py`, con reloj inyectado. Lo mismo con el lease: reloj inyectado, sin
esperas reales.

### Las cuatro pruebas que necesitan PostgreSQL de verdad

`tests/integration/test_leadership_postgres.py` se salta si no hay `DFSHA_TEST_PG_URL`,
para que `pytest` siga corriendo en un portátil sin Docker. **No son un extra.** La
exclusión mutua del lease depende de `SELECT ... FOR UPDATE`, que SQLite no implementa y
SQLAlchemy omite silenciosamente en ese dialecto: en SQLite se prueba la comparación de
época, pero que dos instancias compitiendo no ganen las dos solo se puede comprobar contra
PostgreSQL. Ocho hilos saliendo de una barrera común; gana exactamente uno.

El CI levanta un servicio PostgreSQL para eso, así que en cada push sí se comprueban.

### La prueba que importa del Bloque A

`test_el_lider_congelado_es_rechazado_y_no_escribe_nada` reproduce el escenario completo:
A toma el lease, se congela, B lo toma con la época siguiente, y A despierta a mitad de su
operación. Comprueba las dos mitades, y la segunda es la que suele olvidarse: que A fue
rechazado, **y** que no dejó ni una escritura detrás.

---

## Estructura

```
src/dfsha/
├── common/          DTOs compartidos, checksum, errores, logging
│   ├── crypto.py    las tres capas de clave del cifrado extremo a extremo
│   ├── blocktoken.py  autorización por bloque que el DataNode verifica solo
│   ├── tls.py       único sitio donde se construye un contexto TLS de cliente
│   └── proto/       control.proto (el codigo generado no se versiona)
├── control_node/
│   ├── api/         routers: sólo traducción HTTP ↔ casos de uso
│   ├── commands/    lado escritura (CQRS)
│   ├── queries/     lado lectura (CQRS)
│   ├── domain/      Path, File, Block, reglas, partición, pertenencia, acl, filelock
│   ├── repositories/  modelos SQLAlchemy, interfaces, unidad de trabajo
│   └── services/    auth, placement, resolver, pertenencia, access, shared
├── data_node/       storage.py (layout en disco), heartbeat.py (cliente gRPC) + api/
└── client/          cli.py, session.py, chunker.py, transfer.py
alembic/versions/    migraciones del metadato (0001–0007)
scripts/             gc.py, gen_testfile.py, gen_proto.py, gen_certs.py,
                     verificar_pruebas.py, demo/
certs/               la CA y los certificados (NO se versiona)
deploy/              material de despliegue en AWS
tests/               unit/, integration/
```

`CLAUDE.md` guarda las decisiones de diseño, la hoja de ruta por etapas y los contratos.

### Dos herramientas que no son del sistema sino de cómo se verifica

- **`scripts/demo/`** — cuatro guiones que reproducen los escenarios del hito y
  **comprueban el resultado**. Cada uno lleva un *control positivo* junto a la
  comprobación negativa, porque «no aparece el texto claro» y «un `grep` mal escrito» se
  parecen demasiado. Ver [`scripts/demo/README.md`](scripts/demo/README.md).
- **`scripts/verificar_pruebas.py`** — rompe a propósito cada protección de seguridad y
  exige que las pruebas que dicen fijarla **caigan**. Encontró tres pruebas que pasaban
  por un camino distinto del que su nombre anunciaba. Una prueba que nunca has visto
  fallar no sabes si prueba algo.

---

## Despliegue en AWS

Seis instancias `t3.micro` —PostgreSQL, un ControlNode y cuatro DataNodes en dos zonas de
disponibilidad— con su grupo de seguridad propio. Los pasos exactos, las reglas de red y
qué cambia en cada instancia están en **[`deploy/README.md`](deploy/README.md)**.

> **Sin ejecutar todavía.** El material está escrito y revisado, pero nadie lo ha corrido
> en una cuenta de AWS. El despliegue local con `docker compose` sí está verificado de
> punta a punta.

## Alcance de esta etapa

**Entra**, y está terminado y probado:

| Bloque | Qué |
|---|---|
| **A** | PostgreSQL con réplica de lectura, migraciones con Alembic, tres ControlNodes y elección de líder por lease con **época** |
| **B** | Replicación **R=3** con pipeline encadenado, quórum **W=2** en el commit, re-replicación automática con tres frenos |
| **C** | **mTLS** en el plano interno, **cifrado extremo a extremo**, **ACLs** con grupos, **token de bloque**, y **RF3** (`open`, lectura por rango, `append`, `lock` con lease) |

**Falta para cerrar el hito**: TLS para el tráfico de **cliente**. Hoy el cliente habla
con el ControlNode y con los DataNodes en HTTP plano; el plano interno ya va cifrado y
autenticado por los dos lados.

Que esa pieza vaya al final es deliberado: era la primera de la lista de recortes acordada
al empezar, junto con los grupos de las ACLs. El cifrado de los archivos **no depende de
ella** —los bloques viajan ya cifrados desde el cliente, así que un observador de la red
tampoco los lee—; lo que falta es proteger el **metadato** en tránsito y el token JWT.

### Límites conocidos, escritos a propósito

Ninguno es un olvido. Están razonados en `CLAUDE.md` y se defienden como decisiones:

- **PBKDF2 en vez de Argon2id** para derivar la clave del usuario. Argon2id resiste mejor
  el hardware especializado; PBKDF2 está en la biblioteca estándar y no añade dependencia.
- **La clave maestra se guarda en la sesión.** El modelo es «el servidor nunca ve la
  clave», no «la clave nunca toca el disco». `login --ask-password` no la guarda.
- **No hay revocación de tokens de bloque.** Retirar un permiso corta la *emisión*; lo ya
  emitido vale hasta caducar, y por eso la vida son 10 minutos. Consultar al ControlNode
  en cada petición de bloque devolvería el plano de control al camino de los datos.
- **Cambiar la contraseña no re-cifra nada.** Las envolturas existentes dejarían de
  abrirse. El CLI lo dice al fallar en vez de dejar un archivo ilegible sin explicación.
- **La promoción de la réplica de PostgreSQL es manual** (`deploy/RUNBOOK-postgres.md`).
  Un failover de base de datos automático y correcto es otro proyecto; uno a medias es
  peor que ninguno.
- **La sobre-replicación tras una reincorporación no se limpia sola.** Un bloque puede
  quedar con 4 copias y R=3. Cuesta disco, no corrección, y quitarla automáticamente
  violaría la regla de que el ControlNode no borra datos por una divergencia.
- **`append` reescribe el bloque de cola.** Añadir un byte puede reescribir hasta un
  bloque entero (64 MB con el default). El coste está acotado por el tamaño de bloque, no
  por el del archivo; la alternativa dejaba miles de bloques diminutos.
