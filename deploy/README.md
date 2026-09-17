# Despliegue de DFSha en AWS — Etapa 3

> **Sin ejecutar.** Estos pasos están escritos y revisados, pero nadie los ha corrido
> todavía en una cuenta de AWS. Lo que sí está verificado de punta a punta es el
> despliegue local con `docker compose` y las 520 pruebas automatizadas. Esta nota se
> borra cuando alguien complete el despliegue y confirme el resultado.

**Seis** instancias `t3.micro`, una por rol: PostgreSQL, un ControlNode y cuatro
DataNodes, repartidos en dos zonas de disponibilidad para que la restricción de dominios
de falla sea física y no una etiqueta.

| Instancia | Zona | Rol | Puertos que abre |
|---|---|---|---|
| `dfsha-db` | `us-east-1b` | PostgreSQL | 5432 |
| `dfsha-control` | `us-east-1b` | ControlNode | 8000 (REST), 8443 (interno), 9000 (gRPC) |
| `dfsha-data-1` | `us-east-1b` | DataNode | 8001 |
| `dfsha-data-2` | `us-east-1b` | DataNode | 8001 |
| `dfsha-data-3` | `us-east-1d` | DataNode | 8001 |
| `dfsha-data-4` | `us-east-1d` | DataNode | 8001 |

**La sexta instancia es la base de datos, y es nueva en la Etapa 3.** En la Etapa 2 el
metadato era un SQLite dentro de la instancia del ControlNode; ahora es PostgreSQL, y
ponerlo en la misma máquina volvería a juntar en un disco lo que el resto del diseño
separa.

**Un solo ControlNode**, aunque el código soporta tres: tres `t3.micro` más no caben en el
crédito del curso. El lease de liderazgo funciona igual con uno —lo toma y lo renueva—,
pero si muere no hay quien lo releve. **La alta disponibilidad se demuestra en local**,
donde `docker compose` levanta las tres; aquí se demuestra que el mismo código corre
repartido en máquinas de verdad.

**Una instancia, un servicio.** `t3.micro` son 1 GB de RAM: meter dos DataNodes en la
misma instancia acaba en OOM a mitad de una subida, y además anularía el sentido de los
dominios de falla. Por eso `docker-compose.control.yml` y `docker-compose.datanode.yml`
tienen un solo servicio cada uno.

---

## 1. Grupo de seguridad `dfsha-cluster`

Este es el punto donde es fácil hacer trampa abriendo todo a `0.0.0.0/0` y quedarse
tranquilo. No lo hagas.

**En la Etapa 3 el plano de control lleva TLS mutuo**, así que un puerto expuesto ya no es
el desastre que era: quien no presente un certificado firmado por nuestra CA no llega ni a
mandar la petición. Aun así el grupo sigue cerrado, por dos motivos que conviene saber
defender:

1. **Defensa en profundidad.** El mTLS protege contra quien no tiene certificado; la red
   protege contra quien encuentre un fallo en el mTLS. Quitar una capa porque la otra
   funciona es como quitar el cinturón porque el coche tiene airbag.
2. **PostgreSQL no lleva mTLS.** El 5432 se protege **solo** con la red y con la
   contraseña del rol. Ahí no hay segunda capa, y por eso ese puerto no sale nunca del
   grupo.

Crea **un solo** grupo de seguridad, `dfsha-cluster`, y aplícalo a las seis instancias.
Las reglas que se refieren al propio grupo (`sg-xxxxx` como origen) son las que permiten
que los miembros hablen entre sí sin abrir nada hacia fuera.

### Entrada

| Puerto | Protocolo | Origen | Por qué |
|---|---|---|---|
| 22 | TCP | **tu IP**, `x.x.x.x/32` | SSH. Nunca `0.0.0.0/0` |
| 9000 | TCP | el propio `dfsha-cluster` | gRPC del plano de control, con mTLS |
| 8443 | TCP | el propio `dfsha-cluster` | Plano interno con mTLS: confirmación de bloques y GC |
| 5432 | TCP | el propio `dfsha-cluster` | PostgreSQL. **Nunca hacia fuera**: aquí no hay mTLS |
| 8000 | TCP | **tu IP**, `x.x.x.x/32` | REST del cliente. Ver "Dónde corre el cliente" |
| 8001 | TCP | el propio `dfsha-cluster` | **Ya no es reservado**: por aquí va el pipeline y la re-replicación |
| 8001 | TCP | **tu IP**, `x.x.x.x/32` | Los bytes van directos del cliente al DataNode |

**El 8001 dejó de ser reservado en la Etapa 3.** En la Etapa 2 solo el cliente hablaba con
los DataNodes; ahora **los DataNodes hablan entre sí** —la cadena del pipeline y la
re-replicación— y ese tráfico entra por el mismo puerto desde el propio grupo. Si esa
regla falta, las subidas fallan con «no alcanzan el quórum de escritura», que es un
síntoma que apunta a capacidad y no a red.

### Salida

Todo permitido (`0.0.0.0/0`). Las instancias necesitan salir para `apt`, para bajarse las
imágenes base de Docker y para clonar el repositorio.

### Lo que NO se abre, y por qué importa

- **5432 desde cualquier sitio que no sea el grupo.** Es el único servicio sin mTLS. Con
  el metadato se lee el árbol entero de todos los usuarios, y aunque los bloques estén
  cifrados, las claves envueltas y las sales están ahí.
- **8443 y 9000 desde Internet.** El mTLS los defendería, pero no hay ninguna razón para
  ponerlo a prueba: nadie fuera del clúster tiene nada que hacer en esos puertos.
- **8000 a `0.0.0.0/0`.** El REST del cliente **todavía va en HTTP plano** —el TLS de
  cliente es lo último que queda del hito—, así que el token JWT viaja sin cifrar.
  Restringirlo a tu IP es lo que lo hace aceptable para una demostración.

**Lo que ya NO es un problema, y en la Etapa 2 sí lo era**: el DataNode servía bloques sin
ninguna autenticación, de modo que quien conociera un `block_id` se lo descargaba. Eso era
oscuridad y no control de acceso. Desde el Bloque C cada petición exige un **token de
bloque firmado** por el ControlNode.

```bash
# Con la CLI de AWS, si la tienes configurada.
SG=$(aws ec2 create-security-group --group-name dfsha-cluster \
  --description "DFSha cluster" --vpc-id <VPC_ID> --query GroupId --output text)
MI_IP=$(curl -s https://checkip.amazonaws.com)/32

aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22   --cidr "$MI_IP"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8000 --cidr "$MI_IP"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8001 --cidr "$MI_IP"

# Dentro del clúster: el origen es el propio grupo.
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 9000 --source-group "$SG"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8443 --source-group "$SG"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8001 --source-group "$SG"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 5432 --source-group "$SG"
```

---

## 2. Lanzar las instancias

Seis `t3.micro` con Ubuntu Server 22.04 LTS (o Amazon Linux 2023), el grupo de
seguridad `dfsha-cluster` y tu par de claves. Reparte las zonas según la tabla de arriba:
la zona se elige en la subred, así que necesitas una subred en `us-east-1b` y otra en
`us-east-1d`.

Disco: 8 GB por defecto llega para la demostración. Si vas a subir archivos grandes, sube
los DataNodes a 20 GB y ajusta `DFSHA_DATANODE_CAPACITY_BYTES`.

Apunta la **IP privada** de cada instancia: es lo que van a anunciar.

---

## 3. Instalar Docker (en las seis)

```bash
ssh -i <tu-clave.pem> ubuntu@<IP_PUBLICA>

sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Para no tener que usar sudo en cada comando. Requiere volver a entrar por SSH.
sudo usermod -aG docker ubuntu
exit
```

```bash
ssh -i <tu-clave.pem> ubuntu@<IP_PUBLICA>
git clone https://github.com/tsepulvedf/DFSha-Telematica-.git
cd DFSha-Telematica-
```

---

## 4. La CA y los certificados

**Esto es nuevo en la Etapa 3 y es el paso que más fácil es hacer mal.**
`DFSHA_INTERNAL_SECRET` ya no existe: lo sustituyó el TLS mutuo, así que ya no hay ningún
secreto que copiar idéntico en las seis máquinas. Lo que hay es una CA y un certificado
por rol.

### Generar, EN TU MÁQUINA, con las IP privadas dentro

```bash
python scripts/gen_certs.py --hosts \
  <IP_PRIV_CONTROL>,<IP_PRIV_DATA_1>,<IP_PRIV_DATA_2>,<IP_PRIV_DATA_3>,<IP_PRIV_DATA_4>
```

**Las IP privadas en `--hosts` no son opcionales.** El nodo que llama verifica el nombre
del certificado del otro contra su SAN, y en AWS los nodos se buscan por IP privada, no
por un nombre de servicio de Docker. Sin ellas el pipeline falla en el **handshake** en
vez de en el `connect`: un fallo distinto con el mismo efecto, y bastante más confuso.

Si ya generaste certificados antes de conocer las IP, hay que volver a hacerlo con
`--force` y **redistribuir los seis**. `gen_certs.py` se niega a regenerar una CA sin ese
argumento a propósito: refirmarla invalida todo lo emitido, y un clúster a medio rotar
deja de hablar consigo mismo.

### Repartir: cada instancia recibe SOLO lo suyo

```bash
# ControlNode
scp -i <clave.pem> certs/ca.crt certs/control.crt certs/control.key \
    ubuntu@<IP_PUB_CONTROL>:/tmp/
ssh -i <clave.pem> ubuntu@<IP_PUB_CONTROL> \
    'sudo mkdir -p /etc/dfsha/certs && sudo mv /tmp/{ca.crt,control.crt,control.key} /etc/dfsha/certs/ && sudo chmod 600 /etc/dfsha/certs/*.key'

# Cada DataNode (repetir para los cuatro)
scp -i <clave.pem> certs/ca.crt certs/data.crt certs/data.key ubuntu@<IP_PUB_DATA_N>:/tmp/
ssh -i <clave.pem> ubuntu@<IP_PUB_DATA_N> \
    'sudo mkdir -p /etc/dfsha/certs && sudo mv /tmp/{ca.crt,data.crt,data.key} /etc/dfsha/certs/ && sudo chmod 600 /etc/dfsha/certs/*.key'
```

**`ca.key` no se copia a ninguna instancia.** Solo firma, y firmar se hace en tu máquina.
Aquí se diferencia del `docker compose` local, que monta el directorio entero por
comodidad: ahí da igual porque todo corre en el mismo host, y en AWS no.

---

## 5. Configurar y arrancar

### Los secretos que quedan

Solo dos, y ninguno se comparte entre roles: `DFSHA_JWT_SECRET` (lo usa el ControlNode) y
las dos contraseñas de PostgreSQL.

```bash
python -c "import secrets; print('DFSHA_JWT_SECRET=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('DFSHA_PG_PASSWORD=' + secrets.token_urlsafe(24))"
python -c "import secrets; print('DFSHA_PG_REPLICATION_PASSWORD=' + secrets.token_urlsafe(24))"
```

No los pases por el historial de comandos de una instancia compartida ni los subas al
repositorio.

### PostgreSQL (`dfsha-db`), primero

Los demás no arrancan sin él.

```bash
docker run -d --name dfsha-postgres --restart unless-stopped \
  -e POSTGRES_USER=dfsha -e POSTGRES_DB=dfsha \
  -e POSTGRES_PASSWORD='<DFSHA_PG_PASSWORD>' \
  -p 5432:5432 -v dfsha-pg:/var/lib/postgresql/data \
  postgres:16 -c wal_level=replica -c max_wal_senders=10

docker exec dfsha-postgres pg_isready -U dfsha    # tiene que decir "accepting connections"
```

**Sin réplica de lectura en AWS**, a diferencia del compose local: sería una séptima
instancia. `DFSHA_DB_REPLICA_URL` vacía es un **modo soportado** y no una degradación —el
lado de consulta de CQRS se sirve del primario— y es exactamente lo que hace que
prescindir de la réplica sea borrar una línea del `.env` en vez de deshacer código.

### Migrar el esquema, UNA vez

Desde la instancia del ControlNode, antes de levantarlo:

```bash
docker compose -f deploy/docker-compose.control.yml run --rm --entrypoint "" control-node \
  alembic upgrade head
```

El ControlNode **no migra**: solo comprueba al arrancar que la base está en la última
revisión, y si no lo está falla pronto y con el comando exacto.

### ControlNode (`dfsha-control`)

```bash
cp deploy/.env.control.example .env
nano .env        # el secreto de JWT y la URL de la base
docker compose -f deploy/docker-compose.control.yml up --build -d
curl localhost:8000/health
```

`DFSHA_DB_URL` apunta a la instancia de la base:

```
DFSHA_DB_URL=postgresql+psycopg://dfsha:<DFSHA_PG_PASSWORD>@<IP_PRIV_DB>:5432/dfsha
```

### Cada DataNode (`dfsha-data-1` … `dfsha-data-4`)

```bash
cp deploy/.env.datanode.example .env
nano .env        # ver los tres valores que cambian por instancia
docker compose -f deploy/docker-compose.datanode.yml up --build -d
curl localhost:8001/health
```

Los valores que **cambian en cada DataNode**:

| Variable | `dfsha-data-1` | `dfsha-data-2` | `dfsha-data-3` | `dfsha-data-4` |
|---|---|---|---|---|
| `DFSHA_DATANODE_ADVERTISE_URL` | `http://<IP_PRIV_1>:8001` | `http://<IP_PRIV_2>:8001` | `http://<IP_PRIV_3>:8001` | `http://<IP_PRIV_4>:8001` |
| `DFSHA_DATANODE_FAULT_DOMAIN` | `us-east-1b` | `us-east-1b` | `us-east-1d` | `us-east-1d` |

Y los que son **iguales en los cuatro**:

```
DFSHA_CONTROL_GRPC_URL=<IP_PRIV_CONTROL>:9000
DFSHA_CONTROL_PRIVATE_IP=<IP_PRIV_CONTROL>
```

**`DFSHA_DATANODE_PEER_URL` se deja VACÍA aquí, y conviene entender por qué.** Vacía
significa «la misma que la de cliente», que es el despliegue donde el cliente y los nodos
comparten red — y en AWS es el caso, porque la opción soportada es el cliente dentro de la
VPC. En `docker compose` sí hacen falta las dos porque el cliente está fuera de la red de
contenedores. No es un caso degradado: es el caso simple.

**IP privada, no pública.** En AWS Academy las públicas cambian cada vez que se reinicia
la instancia, y una URL anunciada que deja de resolver convierte cada `put` en un fallo de
conexión sin explicación aparente. La privada es estable dentro de la VPC. Y es además la
que tiene que estar en el SAN del certificado: ver el paso 4.

### Comprobar

Desde el ControlNode, con un token de un usuario cualquiera:

```bash
curl -s localhost:8000/api/v1/cluster/status -H "Authorization: Bearer <token>" | jq
```

Los cuatro tienen que salir `ALIVE`, dos en `us-east-1b` y dos en `us-east-1d`.

Y una comprobación que **solo tiene sentido desde la Etapa 3**: sube un archivo y mira
que `dfsha stat` diga `FULLY_REPLICATED (3 de 3)`. Si dice menos, o si el `put` falla con
«no alcanzan el quórum de escritura», el sospechoso número uno es el **8001 cerrado al
propio grupo**: los DataNodes no pueden reenviarse entre sí. El número dos es un
certificado sin las IP privadas en el SAN, y ese se distingue porque el error de los logs
habla del *handshake* y no de `ConnectError`.

---

## 6. Dónde corre el cliente

Como el DataNode anuncia su **IP privada**, el cliente tiene que estar dentro de la VPC o
llegar a ella por un túnel. Dos opciones:

**a) El cliente en la instancia del ControlNode** (lo más simple):

```bash
sudo apt-get install -y python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DFSHA_CONTROL_URL=http://localhost:8000
dfsha register ana && dfsha login ana
dfsha cluster
```

**b) Túnel SSH desde tu máquina**, si quieres usar la CLI en local. Necesitas un túnel
por cada IP privada que el cliente vaya a tocar, porque los bytes van directos:

```bash
ssh -i <clave.pem> -L 8000:localhost:8000 \
    -L 8001:<IP_PRIV_1>:8001 -L 8002:<IP_PRIV_2>:8001 \
    -L 8003:<IP_PRIV_3>:8001 -L 8004:<IP_PRIV_4>:8001 \
    ubuntu@<IP_PUBLICA_CONTROL>
```

Con la opción (b) los DataNodes tendrían que anunciar `http://localhost:800N` para que el
cliente resolviera bien, lo que rompe la comunicación entre nodos. Es la misma tensión de
una sola dirección anunciada que está documentada en `CLAUDE.md`: **la opción (a) es la
soportada**.

---

## 7. Demostrar la caída de un nodo

```bash
# En dfsha-data-3
docker compose -f deploy/docker-compose.datanode.yml stop

# En el ControlNode, repetido cada pocos segundos
dfsha cluster
```

A los ~10 s pasa a `SUSPECT` y a los ~30 s a `DEAD`. Un `put` posterior no le asigna
bloques. Al volver a arrancarlo, se reincorpora con el mismo `boot_id` y sus réplicas
vuelven a servirse tras el primer report completo.

---

## 8. Coste y limpieza

Seis `t3.micro` consumen crédito de AWS Academy mientras están encendidas. Párelas en
cuanto termines la demostración, o termínalas si ya no las necesitas:

```bash
aws ec2 stop-instances --instance-ids <id1> <id2> <id3> <id4> <id5> <id6>
```

Los volúmenes EBS siguen costando aunque la instancia esté parada.
