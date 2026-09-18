# Despliegue de DFSha en AWS — Etapa 3

> **Sin ejecutar.** Estos pasos están escritos y revisados, pero nadie los ha corrido
> todavía en una cuenta de AWS. Lo que sí está verificado de punta a punta es el
> despliegue local con `docker compose`, incluido el TLS de cliente, y las 640 pruebas
> automatizadas (más 4 que solo corren contra PostgreSQL). Esta nota se borra cuando alguien
> complete el despliegue y confirme el resultado.
>
> Al revisarlos antes de ejecutarlos aparecieron **cuatro fallos** que habrían parado el
> despliegue: una URL que salía vacía por la interpolación de compose, `R=1` en el ejemplo
> del ControlNode, un Ubuntu cuyo Python no instala el cliente, y el paso de `git clone`
> sin rama. Están explicados en su sitio. La lección es la de toda la etapa: **lo que no
> se ha ejecutado nunca está sin probar**, por muy revisado que esté.

**Seis** instancias `t3.micro`, repartidas en dos zonas de disponibilidad para que la
restricción de dominios de falla sea física y no una etiqueta:

| Instancia | Zona | Qué corre | Puertos (todos solo al grupo, salvo el 22) |
|---|---|---|---|
| `dfsha-db` | `us-east-1b` | PostgreSQL, **ControlNode B** y **nginx** | 5432, 8000, 8443, 9000 |
| `dfsha-control` | `us-east-1b` | **ControlNode A** y el cliente | 8000, 8443, 9000 |
| `dfsha-data-1` | `us-east-1b` | DataNode | 8001 |
| `dfsha-data-2` | `us-east-1b` | DataNode | 8001 |
| `dfsha-data-3` | `us-east-1d` | DataNode | 8001 |
| `dfsha-data-4` | `us-east-1d` | DataNode | 8001 |

```
                       dfsha-db
    cliente  ──https──►  nginx :8000 ──https (verificado)──► ControlNode B (misma instancia)
 (en dfsha-control)       │   :8443, :9000 (mTLS, nivel 4)   └────────────► ControlNode A (dfsha-control)
                          ▲
    DataNodes ── heartbeat y confirmaciones ──┘            ambos ──► PostgreSQL (dfsha-db)
    cliente ──── bytes, https, directos ──► DataNodes ──► pipeline entre DataNodes (https)
```

## Por qué así: dos ControlNodes y el balanceador en `dfsha-db`

**Lo que vale del Bloque A es el relevo del líder, y para enseñarlo hacen falta dos cosas,
no una.** Dos ControlNodes, para que haya a quién pasar el lease. Y un **balanceador**, para
que los DataNodes y el cliente no dependan de uno concreto. Sin balanceador, cada DataNode
apunta a **una** IP de ControlNode: matar al que atiende el tráfico deja el clúster sin plano
de control aunque el otro tome el lease. Se vería el traspaso, con la época subiendo, pero
no que **el servicio continúa**, que es la mitad de la demostración. En local lo hace `lb`;
aquí lo hace nginx en `dfsha-db`.

**Una séptima instancia no aporta nada que esto no dé.** Lo que faltaba no era una máquina,
era el balanceador.

### «Una instancia, un servicio», y por qué `dfsha-db` corre tres

La regla existe y sigue valiendo, pero **nació por los DataNodes**: cada subida concurrente
guarda un bloque entero en memoria (hasta 64 MB), y dos DataNodes en 1 GB acaban en OOM a
mitad de un `put`; además, compartir máquina anularía los dominios de falla.

Nada de eso aplica a un ControlNode:

- **No tiene estado propio.** Todo vive en PostgreSQL. Ponerlo junto a la base no vuelve a
  juntar el metadato en un disco, que era el motivo para separar la base.
- **Los bytes de los archivos no lo atraviesan nunca** (decisión 4 de `CLAUDE.md`): no
  guarda bloques en memoria, y su consumo no crece con el tamaño de las subidas.

Medido en el compose local, en reposo: **ControlNode 90–95 MiB, PostgreSQL 62 MiB, nginx
20 MiB**. Con Ubuntu y Docker (~300 MB) y PostgreSQL con carga (hasta ~200 MB), `dfsha-db`
queda en **~500–650 MB de los ~950 útiles**. El swapfile de 1 GB del paso 3 es la red de
seguridad, no el plan.

### El límite, dicho antes de la demostración

**Matar la instancia `dfsha-db` tumba el clúster entero.** PostgreSQL es el **punto único
de fallo aceptado**, con promoción manual de una réplica (decisión 4 del Bloque A;
[`RUNBOOK-postgres.md`](RUNBOOK-postgres.md)); aquí ni siquiera hay réplica, porque sería
otra instancia. nginx está en la misma máquina, así que comparte su suerte. Lo que se
demuestra es **la caída de un ControlNode**, no la de la base, y conviene que esté dicho
antes de enseñarlo y no descubierto durante la sustentación.

### El TLS de cliente (C2), encendido

En el compose local viene **apagado**, porque el modo por defecto es el de quien clona y
hace `docker compose up`. AWS es el despliegue **real**, y aquí el tráfico cruza redes: el
JWT, el metadato y los tokens de bloque viajan cifrados. Los bytes de los archivos ya
viajaban cifrados extremo a extremo en los dos modos.

No hay un fichero de override como en local: aquí C2 está encendido siempre, así que va
directamente en los `.env` (`DFSHA_CLIENT_TLS_*`) y en las URL, que son `https://`.

### Todo valor, escrito

Los compose de `deploy/` **no interpolan nada**: todo valor que cambia por despliegue está
escrito en el `.env` de cada instancia, y `environment:` solo lleva lo estructural. La
versión anterior componía una URL con `${...}`, que compose lee del `.env` de `deploy/` y no
del de la raíz: salía `https://:8443`, pisaba la correcta, y ningún `put` habría confirmado.
Por la misma razón `DFSHA_DATANODE_PEER_URL` va escrita aunque coincida con la de cliente:
un valor escrito se comprueba leyendo el fichero; uno vacío obliga a razonar qué significa.

---

## 1. Grupo de seguridad `dfsha-cluster`

Este es el punto donde es fácil hacer trampa abriendo todo a `0.0.0.0/0` y quedarse
tranquilo. No lo hagas.

El plano de control lleva **TLS mutuo** y el de cliente **TLS**, así que un puerto expuesto
ya no es el desastre que era. Aun así el grupo sigue cerrado, por dos motivos que conviene
saber defender:

1. **Defensa en profundidad.** El TLS protege contra quien no tiene certificado; la red
   protege contra quien encuentre un fallo en el TLS. Quitar una capa porque la otra
   funciona es como quitar el cinturón porque el coche tiene airbag.
2. **PostgreSQL no lleva TLS.** El 5432 se protege **solo** con la red y con la contraseña
   del rol. Ahí no hay segunda capa, y por eso ese puerto no sale nunca del grupo.

Crea **un solo** grupo de seguridad, `dfsha-cluster`, y aplícalo a las seis instancias.
Las reglas con el propio grupo como origen son las que permiten que los miembros hablen
entre sí sin abrir nada hacia fuera.

### Entrada

| Puerto | Origen | Quién lo usa |
|---|---|---|
| 22 | **tu IP**, `x.x.x.x/32` | SSH. Nunca `0.0.0.0/0` |
| 8000 | el propio grupo | El cliente (en `dfsha-control`) hacia nginx; nginx hacia el ControlNode A |
| 8443 | el propio grupo | Plano interno con mTLS: confirmación de bloques y GC. DataNodes → nginx → ControlNodes |
| 9000 | el propio grupo | gRPC del plano de control, con mTLS. Igual que el 8443 |
| 8001 | el propio grupo | Cliente → DataNodes (los bytes), y **DataNode → DataNode**: pipeline y re-replicación |
| 5432 | el propio grupo | PostgreSQL. **Nunca hacia fuera**: aquí no hay TLS |

**Ningún puerto de la aplicación se abre a tu IP**, y es deliberado: el cliente corre
**dentro** del clúster, en `dfsha-control` (ver el paso 7). Abrir el 8000 a tu IP no
bastaría: los bytes van directos a los DataNodes por su IP privada, así que harían falta
cinco túneles y que los nodos anunciaran `localhost`, lo que rompe la comunicación entre
ellos. Es la tensión de una sola dirección anunciada que documenta `CLAUDE.md`.

**El 8001 entre miembros no es opcional.** Si falta, las subidas fallan con «no alcanzan el
quórum de escritura», un síntoma que apunta a capacidad y no a red.

### Salida

Todo permitido (`0.0.0.0/0`). Las instancias necesitan salir para `apt`, para las imágenes
base de Docker y para clonar el repositorio.

### Lo que NO se abre, y por qué importa

- **5432 fuera del grupo.** Es el único servicio sin TLS. Con el metadato se lee el árbol
  entero de todos los usuarios, y aunque los bloques estén cifrados, las claves envueltas y
  las sales están ahí.
- **Nada de la aplicación a Internet.** Todo lleva TLS, pero no hay ninguna razón para
  ponerlo a prueba: nadie de fuera del clúster tiene nada que hacer en esos puertos.

```bash
# Con la CLI de AWS, si la tienes configurada.
SG=$(aws ec2 create-security-group --group-name dfsha-cluster \
  --description "DFSha cluster" --vpc-id <VPC_ID> --query GroupId --output text)
MI_IP=$(curl -s https://checkip.amazonaws.com)/32

aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "$MI_IP"

# Todo lo demas, solo entre miembros del grupo.
for PUERTO in 8000 8001 8443 9000 5432; do
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp \
    --port "$PUERTO" --source-group "$SG"
done
```

---

## 2. Lanzar las instancias

Seis `t3.micro` con **Ubuntu Server 24.04 LTS**, el grupo `dfsha-cluster` y tu par de
claves. Reparte las zonas según la tabla de arriba: la zona se elige en la subred, así que
necesitas una subred en `us-east-1b` y otra en `us-east-1d`.

**24.04 y no 22.04**: el cliente exige Python 3.11 o superior (`requires-python` en
`pyproject.toml`) y 22.04 trae 3.10, así que el `pip install` del paso 7 fallaría. 24.04
trae 3.12. Por lo mismo no se propone Amazon Linux 2023, que trae 3.9.

Disco: 8 GB por defecto llega para la demostración. Si vas a subir archivos grandes, sube
los DataNodes a 20 GB y fija `DFSHA_DATANODE_CAPACITY_BYTES`.

**Apunta la IP privada de cada instancia**: es lo que va en los certificados, en los `.env`
y en las URL anunciadas.

---

## 3. Instalar Docker y el swap (en las seis)

```bash
ssh -i <tu-clave.pem> ubuntu@<IP_PUBLICA>

sudo apt-get update
sudo apt-get install -y ca-certificates curl git jq
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Swap de 1 GB: la red de seguridad de 1 GB de RAM. Imprescindible en dfsha-db (tres
# servicios) y util en los DataNodes (un bloque en memoria por subida concurrente) y al
# construir las imagenes en la propia instancia.
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Para no tener que usar sudo en cada comando. Requiere volver a entrar por SSH.
sudo usermod -aG docker ubuntu
exit
```

```bash
ssh -i <tu-clave.pem> ubuntu@<IP_PUBLICA>
free -h                  # la fila Swap tiene que decir 1.0Gi
# --branch main: sin ella se clona la rama por defecto de GitHub, que fue etapa-1
# hasta el hito 3 y habria desplegado la primera etapa en las seis instancias.
git clone --branch main https://github.com/tsepulvedf/DFSha-Telematica-.git
cd DFSha-Telematica-
```

---

## 4. La CA y los certificados

**El paso que más fácil es hacer mal.** No hay ningún secreto compartido que copiar en las
seis máquinas: hay una CA y un certificado por rol.

### Generar, EN TU MÁQUINA, con las seis IP privadas dentro

```bash
python scripts/gen_certs.py --hosts \
  <IP_PRIV_DB>,<IP_PRIV_CONTROL>,<IP_PRIV_DATA_1>,<IP_PRIV_DATA_2>,<IP_PRIV_DATA_3>,<IP_PRIV_DATA_4>
```

**Las seis, y la de `dfsha-db` la primera en importancia.** Cada nodo que llama verifica el
certificado del otro contra la dirección que marca:

- Los DataNodes marcan **la IP de `dfsha-db`** (nginx) y, a través de nginx en nivel 4, les
  contesta un ControlNode con `control.crt`. Esa IP tiene que estar en su SAN.
- El cliente marca también **la IP de `dfsha-db`** y ahí nginx presenta `control.crt`.
- Los DataNodes se llaman entre sí por **IP privada** (pipeline) y presentan `data.crt`.

Sin ellas, el fallo es un *handshake* en vez de un `connect`: distinto, con el mismo
efecto, y bastante más confuso. `--hosts` añade las IP a todos los roles, y los nombres por
defecto (`lb` entre ellos, que es el que verifica nginx hacia los ControlNodes) se
conservan.

Si ya generaste certificados antes de conocer las IP, hay que repetirlo con `--force` y
**redistribuir los de las seis**. `gen_certs.py` se niega a regenerar una CA sin ese
argumento a propósito: refirmarla invalida todo lo emitido.

### Repartir: cada instancia recibe SOLO lo suyo

```bash
# Los dos ControlNodes: dfsha-db (B y nginx) y dfsha-control (A y el cliente)
for IP in <IP_PUB_DB> <IP_PUB_CONTROL>; do
  scp -i <clave.pem> certs/ca.crt certs/control.crt certs/control.key ubuntu@$IP:/tmp/
  ssh -i <clave.pem> ubuntu@$IP 'sudo mkdir -p /etc/dfsha/certs \
    && sudo mv /tmp/{ca.crt,control.crt,control.key} /etc/dfsha/certs/ \
    && sudo chown 1000:1000 /etc/dfsha/certs/* && sudo chmod 600 /etc/dfsha/certs/*.key \
    && sudo chmod 644 /etc/dfsha/certs/*.crt'
done

# Cada DataNode (los cuatro)
for IP in <IP_PUB_DATA_1> <IP_PUB_DATA_2> <IP_PUB_DATA_3> <IP_PUB_DATA_4>; do
  scp -i <clave.pem> certs/ca.crt certs/data.crt certs/data.key ubuntu@$IP:/tmp/
  ssh -i <clave.pem> ubuntu@$IP 'sudo mkdir -p /etc/dfsha/certs \
    && sudo mv /tmp/{ca.crt,data.crt,data.key} /etc/dfsha/certs/ \
    && sudo chown 1000:1000 /etc/dfsha/certs/* && sudo chmod 600 /etc/dfsha/certs/*.key \
    && sudo chmod 644 /etc/dfsha/certs/*.crt'
done
```

**`chown 1000:1000`, explícito.** Los contenedores del ControlNode y del DataNode corren como
el usuario `dfsha`, uid 1000, y una clave con `600` solo la lee su dueño. Sin el `chown`
funcionaría **por coincidencia**: `scp` deja los ficheros a nombre de `ubuntu`, que en la AMI
también es el uid 1000. Una coincidencia no se documenta como paso. nginx lee la clave como
root, así que no le afecta.

**`ca.key` no se copia a ninguna instancia.** Solo firma, y firmar se hace en tu máquina. En
el compose local se monta el directorio entero por comodidad; aquí no.

---

## 5. Configurar

### Los secretos

Dos, y los **dos ControlNodes comparten los dos**: un token emitido por uno lo valida el
otro, porque nginx reparte entre ambos.

```bash
python -c "import secrets; print('DFSHA_JWT_SECRET=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
```

No los pases por el historial de comandos de una instancia compartida ni los subas al
repositorio.

### El `.env` de los ControlNodes: EL MISMO en `dfsha-db` y en `dfsha-control`

```bash
cp deploy/.env.control.example .env
nano .env
```

Rellena los dos secretos, pon la contraseña también dentro de `DFSHA_DB_URL`, y sustituye
`<IP_PRIVADA_DB>` y `<IP_PRIVADA_CONTROL>`. Todo lo demás ya viene escrito: R=3, W=2, la
gracia de re-replicación a 30 s para la demostración, los tiempos del lease y **C2
encendido**.

Hazlo en una instancia y **copia el fichero** a la otra, en vez de editarlo dos veces.
Después comprueba que son idénticos:

```bash
sha256sum .env        # en las dos instancias: el mismo hash
```

`dfsha-db` usa además `POSTGRES_PASSWORD` (el contenedor de PostgreSQL) y
`DFSHA_CONTROL_A_IP` (nginx); en `dfsha-control` sobran y no molestan.

### El `.env` de cada DataNode

```bash
cp deploy/.env.datanode.example .env
nano .env
```

Los valores que **cambian en cada DataNode**:

| Variable | `dfsha-data-1` | `dfsha-data-2` | `dfsha-data-3` | `dfsha-data-4` |
|---|---|---|---|---|
| `DFSHA_DATANODE_ADVERTISE_URL` | `https://<IP_PRIV_1>:8001` | `https://<IP_PRIV_2>:8001` | `https://<IP_PRIV_3>:8001` | `https://<IP_PRIV_4>:8001` |
| `DFSHA_DATANODE_PEER_URL` | `https://<IP_PRIV_1>:8001` | `https://<IP_PRIV_2>:8001` | `https://<IP_PRIV_3>:8001` | `https://<IP_PRIV_4>:8001` |
| `DFSHA_DATANODE_FAULT_DOMAIN` | `us-east-1b` | `us-east-1b` | `us-east-1d` | `us-east-1d` |

Y los que son **iguales en los cuatro**, todos ya escritos en el ejemplo salvo la IP:

```
DFSHA_CONTROL_GRPC_URL=<IP_PRIV_DB>:9000
DFSHA_CONTROL_INTERNAL_URL=https://<IP_PRIV_DB>:8443
DFSHA_TLS_CA_CERT=/etc/dfsha/certs/ca.crt
DFSHA_TLS_CERT=/etc/dfsha/certs/data.crt
DFSHA_TLS_KEY=/etc/dfsha/certs/data.key
DFSHA_CLIENT_TLS_CERT=/etc/dfsha/certs/data.crt
DFSHA_CLIENT_TLS_KEY=/etc/dfsha/certs/data.key
```

**El plano de control apunta a `dfsha-db`, no a un ControlNode.** Es nginx: así matar a
cualquiera de los dos ControlNodes no deja a ningún DataNode sin camino.

**`DFSHA_DATANODE_PEER_URL` va escrita aunque coincida con la de cliente.** Coinciden porque
el cliente corre dentro de la VPC, en la misma red que los nodos (en el compose local no,
y por eso allí difieren). Vacía significaría lo mismo, pero habría que saberlo, y ese
razonamiento sobre qué significa una ausencia es el que falló en el Bloque B. Escrita, se
comprueba leyendo el `.env`. Y con C2 tiene que llevar `https://` igual que la otra.

**IP privada, no pública.** En AWS Academy las públicas cambian cada vez que se reinicia la
instancia, y una URL anunciada que deja de resolver convierte cada `put` en un fallo de
conexión sin explicación aparente. La privada es estable dentro de la VPC.

---

## 6. Arrancar, en este orden

### `dfsha-db`: PostgreSQL, migración, ControlNode B y nginx

```bash
docker compose -f deploy/docker-compose.db.yml up -d postgres
docker compose -f deploy/docker-compose.db.yml ps        # postgres: healthy

# La migracion, UNA vez y desde aqui: el ControlNode no migra, solo comprueba al arrancar
# que la base esta en la ultima revision y, si no, falla con el comando exacto.
docker compose -f deploy/docker-compose.db.yml run --rm --entrypoint "" control-node \
  alembic upgrade head

docker compose -f deploy/docker-compose.db.yml up --build -d
docker compose -f deploy/docker-compose.db.yml ps        # los tres healthy
```

Que `lb` salga `healthy` ya dice bastante: su sonda entra por la puerta interna de nginx y
llega a un ControlNode por HTTPS **verificando la CA**. Si el certificado o la CA estuvieran
mal, no llegaría a `healthy`.

### `dfsha-control`: ControlNode A

```bash
docker compose -f deploy/docker-compose.control.yml up --build -d
docker compose -f deploy/docker-compose.control.yml ps   # healthy
```

La sonda del contenedor pregunta en HTTPS, con la misma variable que enciende TLS en el
servidor (`common/healthcheck.py`): no pueden discrepar.

### Cada DataNode

```bash
docker compose -f deploy/docker-compose.datanode.yml up --build -d
docker compose -f deploy/docker-compose.datanode.yml logs --tail 20   # node.registered
```

---

## 7. El cliente, en `dfsha-control`

**La opción soportada es el cliente dentro del clúster**, en la instancia del ControlNode A.
No hay otra: el cliente sube y baja los bytes **directamente** de los DataNodes por su IP
privada, así que tiene que estar en la VPC. Desde tu portátil harían falta cinco túneles y
que los nodos anunciaran `localhost`, lo que rompe la comunicación entre ellos (ver el
paso 1).

```bash
sudo apt-get install -y python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

export DFSHA_CONTROL_URL=https://<IP_PRIV_DB>:8000
export DFSHA_TLS_CA_CERT=/etc/dfsha/certs/ca.crt
dfsha register ana && dfsha login ana
```

La URL es la de **nginx en `dfsha-db`**, no `localhost`: así el cliente sigue funcionando
aunque se mate el ControlNode de esta misma instancia.

### Comprobar

```bash
dfsha cluster
```

Los cuatro DataNodes `ALIVE`, dos en `us-east-1b` y dos en `us-east-1d`, con las
direcciones en `https://`, y un líder con su época.

```bash
python scripts/gen_testfile.py /tmp/prueba.bin --size 50MB
dfsha put /tmp/prueba.bin /prueba.bin --block-size 1048576
dfsha stat /prueba.bin          # FULLY_REPLICATED (3 de 3)
dfsha get /prueba.bin /tmp/bajado.bin && sha256sum /tmp/prueba.bin /tmp/bajado.bin
```

Si el `stat` dice menos de 3, o el `put` falla con «no alcanzan el quórum de escritura»,
los sospechosos por orden:

1. **El 8001 cerrado al propio grupo**: los DataNodes no pueden reenviarse entre sí.
2. **Un certificado sin la IP privada del nodo en el SAN**: los logs del DataNode hablan de
   *handshake* o `CERTIFICATE_VERIFY_FAILED`, no de `ConnectError`.
3. **Una `DFSHA_DATANODE_PEER_URL` con `http://`**: con C2 el vecino solo habla HTTPS.

El primer error de la cadena vale más que el último: `replication.forward_failed` en los
logs del DataNode dice la causa; el 409 del cliente solo la resume.

---

## 8. Demostrar la caída de un DataNode

```bash
# En dfsha-data-3
docker compose -f deploy/docker-compose.datanode.yml stop

# En dfsha-control, repetido cada pocos segundos
dfsha cluster
dfsha stat /prueba.bin
```

A los ~10 s el nodo pasa a `SUSPECT` y a los ~30 s a `DEAD`; el archivo pasa a
`UNDER_REPLICATED` y se sigue leyendo. Pasada la gracia (30 s en este `.env`), la
re-replicación repone la copia en otro nodo y el archivo vuelve a `FULLY_REPLICATED`. Al
volver a arrancar el nodo se reincorpora con el mismo `boot_id`; algunos bloques pueden
quedar con 4 copias, que es esperable (`CLAUDE.md`, «Sobre-replicación tras una
reincorporación»).

---

## 9. Demostrar el relevo del líder, a mano por SSH

`scripts/demo/failover_del_lider.py` **no sirve en AWS**: identifica al líder y lo mata con
`docker exec` y `docker stop` sobre contenedores **locales**. Aquí los dos ControlNodes
están en máquinas distintas, así que la secuencia es manual. Tres terminales:
**T1** en `dfsha-control` (el cliente), **T2** en `dfsha-control`, **T3** en `dfsha-db`.

El control positivo es el mismo que en el guion, y es lo que hace honesta la demostración:
**se mata al que es líder**, no a uno al azar. Si se matara al otro, que el clúster siga no
demostraría nada.

**0. Preparar (T1).** El token, para preguntar por el liderazgo:

```bash
TOKEN=$(python -c "import json,os; print(json.load(open(os.path.expanduser('~/.dfsha/session.json')))['token'])")
LIDER() { curl -s --cacert /etc/dfsha/certs/ca.crt -H "Authorization: Bearer $TOKEN" "$1/api/v1/cluster/leadership" | jq -c "$2"; }
```

**1. Quién manda (T1).** Se pregunta a cada ControlNode **directamente**, no a través de
nginx, porque nginx elige a quién pregunta:

```bash
LIDER https://<IP_PRIV_DB>:8000 '{leader_id, epoch}'     # el estado del lease
LIDER https://localhost:8000    '{is_self, epoch}'       # ¿es A?
```

Y en **T3**, en `dfsha-db`, el otro (publicado solo en loopback, para esto):

```bash
curl -s --cacert /etc/dfsha/certs/ca.crt -H "Authorization: Bearer <TOKEN>" \
  https://localhost:8010/api/v1/cluster/leadership | jq -c '{is_self, epoch}'   # ¿es B?
```

**Mira:** exactamente uno de los dos dice `is_self: true`. Apunta la época.

**2. Matar al líder, sin avisar.** `docker kill`, no `stop`:

```bash
docker kill dfsha-control-node-a     # en T2, si el lider es A
docker kill dfsha-control-node-b     # en T3, si el lider es B
```

**Por qué `kill`.** Una parada ordenada (`docker stop`) **suelta** el lease al salir
(`leadership.released`), y el otro lo toma en el siguiente ciclo de renovación: se ve un
traspaso limpio, con `previous_leader: null`. Con `kill` no hay liberación: el lease tiene
que **caducar** (TTL de 6 s), que es lo que pasa en una caída de verdad. `stop` sirve para
enseñar después la variante ordenada.

**3. El servicio no se interrumpe (T1), inmediatamente:**

```bash
dfsha ls /
dfsha get /prueba.bin /tmp/otra.bin && sha256sum /tmp/otra.bin
```

**Mira:** los dos responden. nginx reintenta en el ControlNode vivo; lecturas, planes y
autenticación no necesitan liderazgo.

**4. El relevo, con la época subiendo (T1):**

```bash
watch -n 1 "curl -s --cacert /etc/dfsha/certs/ca.crt -H 'Authorization: Bearer $TOKEN' \
  https://<IP_PRIV_DB>:8000/api/v1/cluster/leadership | jq -c '{leader_id, epoch, expires_in_seconds}'"
```

**Mira:** en menos de ~10 s cambia `leader_id` y **la época sube en uno**. Esa subida es el
token de aislamiento: si el líder muerto despertara a mitad de una escritura, su época ya no
valdría (`CLAUDE.md`, «Liderazgo: la época es un token de aislamiento»).

**5. Los DataNodes siguen vivos (T1):**

```bash
dfsha cluster
```

**Mira:** los cuatro `ALIVE`. Su stream de heartbeat se cortó con el ControlNode muerto y
reconectaron a través de nginx al vivo. Alguno puede pasar un momento por `SUSPECT`; ninguno
debe llegar a `DEAD`.

**6. Las escrituras también (T1):**

```bash
dfsha put /tmp/prueba.bin /tras-el-relevo.bin --block-size 1048576
dfsha stat /tras-el-relevo.bin          # FULLY_REPLICATED
```

**Mira:** el `put` confirma. Cada bloque exige el aviso síncrono `/stored` por el 8443, que
ahora atiende el único ControlNode vivo a través de nginx.

**7. La prueba en los logs** (en la instancia del superviviente):

```bash
docker logs dfsha-control-node-b 2>&1 | grep leadership.acquired | tail -1 | jq .
```

**Mira:** `epoch` nueva, `previous_leader` con el id del muerto (con `kill`) y
`self_recovery: false`.

**8. Devolver el muerto:**

```bash
docker start dfsha-control-node-a        # o -b
```

**Mira:** vuelve `healthy` y **no** recupera el mando, porque el lease es de otro y sigue
vivo. Y si algún día lo recuperara, sería con una época nueva, nunca con la que tenía.

Si reiniciaste el **B** y luego ves peticiones fallidas contra él: nginx resuelve el nombre
`control-node` al arrancar, y si Docker le diera otra IP al volver, nginx seguiría
apuntando a la vieja. Es raro con `start` (suele conservar la IP), se recupera solo porque
nginx descarta el destino que no responde, y si no, en `dfsha-db`:
`docker compose -f deploy/docker-compose.db.yml restart lb`. El A no tiene este problema:
nginx lo alcanza por IP privada. Detalle en `CLAUDE.md`, «nginx resuelve los upstreams al
arrancar».

**Lo que esto NO demuestra**, igual que el guion local: el líder **congelado** que despierta
creyendo que manda. Un contenedor matado no despierta. Lo cubren
`test_el_lider_congelado_es_rechazado_y_no_escribe_nada` y su prueba hermana. Y la caída de
**`dfsha-db`**, que es el punto único de fallo aceptado (ver arriba).

---

## 10. Coste y limpieza

Seis `t3.micro` consumen crédito de AWS Academy mientras están encendidas. Párelas en
cuanto termines la demostración, o termínalas si ya no las necesitas:

```bash
aws ec2 stop-instances --instance-ids <id1> <id2> <id3> <id4> <id5> <id6>
```

Los volúmenes EBS siguen costando aunque la instancia esté parada. Y al volver a
arrancarlas las IP **públicas** cambian (las privadas no): el clúster sigue funcionando,
solo cambian las direcciones de SSH.
