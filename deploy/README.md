# Despliegue de DFSha en AWS — Etapa 2

> **Sin ejecutar.** Estos pasos están escritos y revisados, pero nadie los ha corrido
> todavía en una cuenta de AWS. Lo que sí está verificado de punta a punta es el
> despliegue local con `docker compose` y las 275 pruebas automatizadas. Esta nota se
> borra cuando alguien complete el despliegue y confirme el resultado.

Cinco instancias `t3.micro`, una por rol: un ControlNode y cuatro DataNodes, repartidos
en dos zonas de disponibilidad para que la restricción de dominios de falla sea física y
no una etiqueta.

| Instancia | Zona | Rol | Puertos que abre |
|---|---|---|---|
| `dfsha-control` | `us-east-1b` | ControlNode | 8000 (REST), 9000 (gRPC) |
| `dfsha-data-1` | `us-east-1b` | DataNode | 8001 |
| `dfsha-data-2` | `us-east-1b` | DataNode | 8001 |
| `dfsha-data-3` | `us-east-1d` | DataNode | 8001 |
| `dfsha-data-4` | `us-east-1d` | DataNode | 8001 |

**Una instancia, un servicio.** `t3.micro` son 1 GB de RAM: meter dos DataNodes en la
misma instancia acaba en OOM a mitad de una subida, y además anularía el sentido de los
dominios de falla. Por eso `docker-compose.control.yml` y `docker-compose.datanode.yml`
tienen un solo servicio cada uno.

---

## 1. Grupo de seguridad `dfsha-cluster`

Este es el punto donde es fácil hacer trampa abriendo todo a `0.0.0.0/0` y quedarse
tranquilo. No lo hagas: el tráfico interno del clúster no tiene por qué salir a Internet,
y el plano de control **no lleva autenticación mutua hasta la Etapa 3** — hoy se protege
con un secreto compartido por cabecera, que es suficiente dentro de una VPC privada y no
lo es expuesto al mundo.

Crea **un solo** grupo de seguridad, `dfsha-cluster`, y aplícalo a las cinco instancias.
Las reglas que se refieren al propio grupo (`sg-xxxxx` como origen) son las que permiten
que los miembros hablen entre sí sin abrir nada hacia fuera.

### Entrada

| Puerto | Protocolo | Origen | Por qué |
|---|---|---|---|
| 22 | TCP | **tu IP**, `x.x.x.x/32` | SSH. Nunca `0.0.0.0/0` |
| 9000 | TCP | el propio `dfsha-cluster` | gRPC del plano de control: solo los DataNodes lo usan |
| 8000 | TCP | el propio `dfsha-cluster` | REST interno: el DataNode confirma cada bloque almacenado |
| 8000 | TCP | **tu IP**, `x.x.x.x/32` | Para usar el cliente. Ver "Dónde corre el cliente" |
| 8001 | TCP | el propio `dfsha-cluster` | Reservado; hoy el DataNode no recibe tráfico de otros nodos |
| 8001 | TCP | **tu IP**, `x.x.x.x/32` | Los bytes van directos del cliente al DataNode |

### Salida

Todo permitido (`0.0.0.0/0`). Las instancias necesitan salir para `apt`, para bajarse las
imágenes base de Docker y para clonar el repositorio.

### Lo que NO se abre, y por qué importa

- **9000 desde Internet.** El plano de control acepta `Register` de cualquiera que
  conozca la URL: un atacante podría darse de alta como DataNode y recibir bloques. La
  autenticación mutua llega en la Etapa 3 con mTLS; hasta entonces, la frontera es la red.
- **8000 desde Internet sin restringir.** `/internal/v1` está detrás de
  `X-DFSha-Internal-Secret`, pero ese secreto viaja en claro sobre HTTP.
- **Todo a `0.0.0.0/0`.** Además de lo anterior, el DataNode sirve bloques sin
  autenticación: quien conozca un `block_id` se lo puede descargar. El `block_id` es un
  UUID v4, así que no se adivina, pero eso es oscuridad, no control de acceso.

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
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8000 --source-group "$SG"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8001 --source-group "$SG"
```

---

## 2. Lanzar las instancias

Cinco `t3.micro` con Ubuntu Server 22.04 LTS (o Amazon Linux 2023), el grupo de
seguridad `dfsha-cluster` y tu par de claves. Reparte las zonas según la tabla de arriba:
la zona se elige en la subred, así que necesitas una subred en `us-east-1b` y otra en
`us-east-1d`.

Disco: 8 GB por defecto llega para la demostración. Si vas a subir archivos grandes, sube
los DataNodes a 20 GB y ajusta `DFSHA_DATANODE_CAPACITY_BYTES`.

Apunta la **IP privada** de cada instancia: es lo que van a anunciar.

---

## 3. Instalar Docker (en las cinco)

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

## 4. Configurar y arrancar

### Los secretos: los mismos en las cinco

`DFSHA_INTERNAL_SECRET` tiene que ser **idéntico** en el ControlNode y en los cuatro
DataNodes, o el plano de control los rechaza. `DFSHA_JWT_SECRET` solo lo usa el
ControlNode. Genéralos **una vez**, en tu máquina, y cópialos a cada `.env`:

```bash
python -c "import secrets; print('DFSHA_JWT_SECRET=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('DFSHA_INTERNAL_SECRET=' + secrets.token_urlsafe(48))"
```

No los pases por el historial de comandos de una instancia compartida ni los subas al
repositorio.

### ControlNode (`dfsha-control`)

```bash
cp deploy/.env.control.example .env
nano .env        # rellena los dos secretos
docker compose -f deploy/docker-compose.control.yml up --build -d
curl localhost:8000/health
```

### Cada DataNode (`dfsha-data-1` … `dfsha-data-4`)

```bash
cp deploy/.env.datanode.example .env
nano .env        # ver los tres valores que cambian por instancia
docker compose -f deploy/docker-compose.datanode.yml up --build -d
curl localhost:8001/health
```

Los tres valores que **cambian en cada DataNode**:

| Variable | `dfsha-data-1` | `dfsha-data-2` | `dfsha-data-3` | `dfsha-data-4` |
|---|---|---|---|---|
| `DFSHA_DATANODE_ADVERTISE_URL` | `http://<IP_PRIV_1>:8001` | `http://<IP_PRIV_2>:8001` | `http://<IP_PRIV_3>:8001` | `http://<IP_PRIV_4>:8001` |
| `DFSHA_DATANODE_FAULT_DOMAIN` | `us-east-1b` | `us-east-1b` | `us-east-1d` | `us-east-1d` |
| `DFSHA_CONTROL_GRPC_URL` | `<IP_PRIV_CONTROL>:9000` | idem | idem | idem |

**IP privada, no pública.** En AWS Academy las públicas cambian cada vez que se reinicia
la instancia, y una URL anunciada que deja de resolver convierte cada `put` en un fallo
de conexión sin explicación aparente. La privada es estable dentro de la VPC.

### Comprobar

Desde el ControlNode, con un token de un usuario cualquiera:

```bash
curl -s localhost:8000/api/v1/cluster/status -H "Authorization: Bearer <token>" | jq
```

Los cuatro tienen que salir `ALIVE`, dos en `us-east-1b` y dos en `us-east-1d`.

---

## 5. Dónde corre el cliente

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

## 6. Demostrar la caída de un nodo

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

## 7. Coste y limpieza

Cinco `t3.micro` consumen crédito de AWS Academy mientras están encendidas. Párelas en
cuanto termines la demostración, o termínalas si ya no las necesitas:

```bash
aws ec2 stop-instances --instance-ids <id1> <id2> <id3> <id4> <id5>
```

Los volúmenes EBS siguen costando aunque la instancia esté parada.
