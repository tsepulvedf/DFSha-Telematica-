# Guiones de demostración

Cuatro escenarios de la Etapa 3, pensados para grabarse. Cada uno narra lo que hace,
imprime los comandos que ejecuta y **comprueba el resultado**: si algo no sale como debe,
termina en rojo y con código de salida distinto de cero.

```bash
python scripts/demo/cifrado_en_reposo.py      # criterio 11
python scripts/demo/replicacion_y_caida.py    # R=3, W=2, re-replicación
python scripts/demo/failover_del_lider.py     # relevo de liderazgo con época
python scripts/demo/permisos_y_token.py       # ACLs y token de bloque
```

## Orden recomendado, y qué deja cada uno

**Córrelos en este orden.** Los dos primeros no tocan ningún contenedor, así que si algo
sale mal en los dos últimos ya tienes grabado lo que no depende de ellos.

| # | Guion | ¿Toca contenedores? | Qué deja detrás |
|---|---|---|---|
| 1 | `cifrado_en_reposo` | no | bloques huérfanos (el `rm` es lógico) |
| 2 | `permisos_y_token` | no | usuarios `*-demo`, el grupo `equipo` y `/proyecto` |
| 3 | `replicacion_y_caida` | **para y levanta un DataNode** | puede dejar bloques con **4 copias** |
| 4 | `failover_del_lider` | **para y levanta un ControlNode** | la época sube de forma permanente |

### Entre el 3 y el 4: espera a que el nodo vuelva

`replicacion_y_caida` levanta el DataNode al terminar, pero **volver no es instantáneo**:
el nodo tiene que registrarse otra vez y mandar un block report completo. Antes de seguir:

```bash
dfsha cluster       # los cuatro DataNodes tienen que salir ALIVE
```

Suele tardar menos de diez segundos. Si sigues sin esperar, el guion siguiente puede
encontrarse un clúster con un nodo DEAD y la lectura sería más confusa de lo necesario.

### Después del 3: cuatro copias en algunos bloques

Si la espera de gracia se cumplió antes de que el nodo volviera, algunos bloques quedan
con **4 copias y R=3**. Es un estado esperable, está explicado en CLAUDE.md
(«Sobre-replicación tras una reincorporación») y **no se limpia solo**: el GC recoge
bloques de archivos borrados, y estos pertenecen a archivos vivos. No estorba para el
resto de la demostración.

### Después del 4: si el plano interno se comporta raro

nginx resuelve los nombres de sus *upstreams* **al arrancar**. Si al reiniciar el
ControlNode su IP cambia dentro de la red de compose, el balanceador puede quedarse
apuntando a la anterior, y el síntoma sería que las subidas empiezan a fallar en el
`commit` aunque los tres ControlNodes estén vivos. No siempre pasa —Docker suele conservar
la IP tras un `stop`/`start`— pero si ocurre, la salida es:

```bash
docker compose restart lb
```

**Ninguno de los cuatro obliga a `docker compose down -v`.** Si prefieres empezar de cero
para grabar, eso sí borra el metadato y los bloques.

## Antes de correrlos

```bash
python scripts/gen_certs.py          # una vez: la CA y los certificados
docker compose up --build -d
dfsha register <usuario> && dfsha login <usuario>
```

Para el de re-replicación conviene bajar la espera de gracia, que por defecto son cinco
minutos:

```bash
DFSHA_REREPLICATION_GRACE_MS=30000 docker compose up -d
```

## La regla que siguen los cuatro: control positivo junto al negativo

Todos demuestran algo por **ausencia** —el texto claro no aparece, el intruso no puede
leer, el archivo sigue disponible— y una comprobación por ausencia es fácil de pasar por
accidente:

| Guion | Lo que podria pasar por accidente | El control que lo descarta |
|---|---|---|
| `cifrado_en_reposo` | un `grep` mal escrito no encuentra nada en ningún sitio | el **mismo** `grep` contra el fichero local, donde sí tiene que encontrar |
| `replicacion_y_caida` | matar un nodo que **no tenía copias** de ese archivo | se mira dónde están las copias y se mata a uno que **sí** tiene |
| `failover_del_lider` | matar una instancia que **no era la líder** (2 de 3 no lo son) | se pregunta quién sostiene el lease y se mata a ése |
| `permisos_y_token` | un DataNode que **rechaza a todo el mundo** devuelve 403 igual | Ana **sí** lee su bloque, y Beto **sí** lee lo compartido |

Sin esa mitad no se está demostrando el sistema, se está demostrando que la comprobación
no encuentra nada, que no es lo mismo. Es la lección del Bloque C aplicada a una
demostración: *una prueba que solo comprueba que lo malo se rechaza no comprueba que lo
bueno funciona*.

## Lo que estos guiones NO demuestran

Conviene saberlo antes de que alguien lo pregunte en la sustentación:

- **El líder congelado.** `docker stop` mata; no congela. El caso peligroso es el proceso
  que se queda sin CPU y **vuelve** creyendo que sigue mandando, y ése no se monta con
  Docker. Lo cubren `test_el_lider_congelado_es_rechazado_y_no_escribe_nada` y su prueba
  hermana.
- **La exclusión mutua real del lease.** Necesita varios hilos saliendo de una barrera
  contra PostgreSQL de verdad: `tests/integration/test_leadership_postgres.py`.
- **Que las pruebas prueban algo.** Eso es `scripts/verificar_pruebas.py`.

## Dónde dejan los ficheros

En `tmp/`, que está en `.gitignore`. Los archivos remotos se borran al terminar, salvo con
`--keep` donde exista esa opción.
