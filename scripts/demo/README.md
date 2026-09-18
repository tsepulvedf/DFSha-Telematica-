# Guiones de demostración

Cuatro escenarios de la Etapa 3, pensados para grabarse. Cada uno narra lo que hace,
imprime los comandos que ejecuta y **comprueba el resultado**: si algo no sale como debe,
termina en rojo y con código de salida distinto de cero.

**Funcionan en Windows**, que es donde se graban: en el host solo usan Python y `docker`
(el `grep` y el `find` corren dentro de los contenedores), y se pueden lanzar desde
cualquier directorio. **Esperan solos a que el clúster esté listo** —al menos R DataNodes
ALIVE— antes de empezar, así que lanzarlos justo después del `up` ya no da un 409 que
parezca del sistema.

En el orden recomendado, que se explica más abajo:

```bash
python scripts/demo/cifrado_en_reposo.py      # criterio 11
python scripts/demo/permisos_y_token.py       # ACLs y token de bloque
python scripts/demo/replicacion_y_caida.py    # R=3, W=2, re-replicación
python scripts/demo/failover_del_lider.py     # relevo de liderazgo con época
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

### Entre el 3 y el 4: el nodo vuelve solo

`replicacion_y_caida` levanta el DataNode al terminar y **espera** a que los cuatro vuelvan
a estar ALIVE (hasta 90 s). Si termina con un aviso de que no volvió, mira `dfsha cluster`
antes de seguir.

### Después del 3: cuatro copias en algunos bloques

Si la espera de gracia se cumplió antes de que el nodo volviera, algunos bloques quedan
con **4 copias y R=3**. Es un estado esperable, está explicado en CLAUDE.md
(«Sobre-replicación tras una reincorporación») y **no se limpia solo**: el GC recoge
bloques de archivos borrados, y estos pertenecen a archivos vivos. No estorba para el
resto de la demostración.

### Después del 4: nginx y las IP, con la medida correcta

nginx resuelve los nombres de sus *upstreams* **al arrancar**, así que si al reiniciar el
ControlNode le cambiara la IP dentro de la red de compose, el balanceador se quedaría
apuntando a la anterior.

**Pero el efecto es mucho menor de lo que parece**, y conviene decirlo con precisión
porque la versión alarmista de esta nota lleva a diagnosticar mal:

- Los tres *upstreams* llevan `max_fails=2 fail_timeout=5s` y `proxy_next_upstream`. Una
  IP obsoleta **a la que no responde nadie** se detecta al conectar, nginx pasa a la
  siguiente instancia y la saca de la rotación. **Se recupera solo.**
- El caso que sí daría problemas es que esa IP la hubiera cogido **otro contenedor que sí
  escucha**: entonces nginx conectaría con el servicio equivocado. Requiere una secuencia
  concreta y es poco probable con `stop`/`start`, donde Docker suele devolver la misma IP.

O sea: si tras el guion 4 ves fallos, **el balanceador no es el primer sospechoso**. Mira
antes los logs del ControlNode que reiniciaste. Si aun así quieres descartarlo:

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
minutos. El guion espera la reposición hasta 120 s; con la gracia por defecto termina con
un aviso (no en rojo), o pásale `--espera-reposicion 400`.

```bash
DFSHA_REREPLICATION_GRACE_MS=30000 docker compose up -d        # bash
$env:DFSHA_REREPLICATION_GRACE_MS=30000; docker compose up -d  # PowerShell
```

## La regla que siguen los cuatro: control positivo junto al negativo

Todos demuestran algo por **ausencia** —el texto claro no aparece, el intruso no puede
leer, el archivo sigue disponible— y una comprobación por ausencia es fácil de pasar por
accidente:

| Guion | Lo que podria pasar por accidente | El control que lo descarta |
|---|---|---|
| `cifrado_en_reposo` | un `grep` mal escrito no encuentra nada; o se busca en un nodo **sin** el bloque; o `grep` falla y se lee como «no encontrado» | el **mismo** `grep`, en cada contenedor, sobre la frase en claro; se busca solo en los nodos que **tienen** el `.blk`; y solo el código 1 de `grep` cuenta como «no encontrado» |
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
