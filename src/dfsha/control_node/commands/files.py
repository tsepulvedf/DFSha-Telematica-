"""Lado escritura de la transferencia: create, commit, abort.

La escritura tiene tres fases porque los bloques son inmutables (WORM). El ControlNode
reserva el plan, el cliente sube los bytes directamente a los DataNodes, y solo entonces
se confirma. Mientras tanto el archivo esta en WRITING: invisible para todos y con fecha
de caducidad, para que un cliente que se cae no deje el nombre bloqueado para siempre.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta

from datetime import datetime

from dfsha.common.errors import (
    BlocksNotStoredError,
    InvalidPathError,
    InvalidStateError,
    NotFoundError,
)
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    File,
    FileState,
    ReplicaState,
    utcnow,
)
from dfsha.control_node.domain.partition import plan_blocks
from dfsha.control_node.domain.path import Path
from dfsha.control_node.domain.rules import (
    ensure_can_abort,
    ensure_can_commit,
    ensure_no_live_reservation,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id
from dfsha.control_node.services.placement import BlockPlacementPolicy
from dfsha.control_node.domain.acl import Permission
from dfsha.control_node.services.access import directory_for
from dfsha.control_node.services.permissions import require
from dfsha.control_node.domain.filelock import LockFencing
from dfsha.control_node.commands.filelock import require_lock
from dfsha.control_node.queries.files import ReadBlock
from dfsha.control_node.services.resolver import absolute_path
from dfsha.control_node.tracing import command

__all__ = ["CreatedFile", "PlannedBlock", "CommittedFile", "create_file", "commit_file", "abort_file"]


@dataclass(frozen=True, slots=True)
class PlannedBlock:
    block_id: str
    index: int
    size: int
    #: (data_node_id, base_url) con la direccion alcanzable por el CLIENTE.
    replicas: list[tuple[str, str]]
    #: Resto de la cadena del pipeline, con las direcciones alcanzables por OTROS
    #: DATANODES. El cliente la copia tal cual a la cabecera y no construye nada: asi no
    #: necesita saber nada de la topologia interna del cluster, que es lo mismo que decir
    #: que no puede equivocarse al deducirla.
    pipeline: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CreatedFile:
    file_id: str
    block_size: int
    expires_at: object
    blocks: list[PlannedBlock]


@dataclass(frozen=True, slots=True)
class CommittedFile:
    path: str
    size: int
    block_count: int


@command("files.create")
def create_file(
    uow: SqlUnitOfWork,
    placement: BlockPlacementPolicy,
    owner_id: str,
    raw_path: str,
    size: int,
    default_block_size: int,
    write_ttl_seconds: int,
    block_size: int | None = None,
    replication_factor: int = 1,
    cipher_overhead: int = 0,
) -> CreatedFile:
    path = Path.parse(raw_path)
    if path.is_root:
        raise InvalidPathError("la raiz es un directorio, no un archivo")

    efectivo = block_size or default_block_size

    with uow:
        # Subir es escribir: hace falta WRITE sobre el directorio destino. Es lo
        # que separa a quien puede leer un directorio compartido de quien puede
        # meter cosas en el.
        padre = directory_for(uow, owner_id, path.parent, Permission.WRITE).directory
        ahora = utcnow()

        existente = uow.files.get_live_by_name(padre.id, path.name)
        ensure_no_live_reservation(path, existente, ahora)
        if existente is not None and existente.state is FileState.WRITING:
            # Reserva vencida: se cierra aqui mismo para que no vuelva a aparecer, y sus
            # bloques quedan a la vista del GC.
            uow.files.mark_deleted(existente.id, ahora)

        if uow.directories.get_child(padre.id, path.name) is not None:
            raise InvalidPathError("ya existe un directorio con ese nombre", path=str(path))

        archivo = File(
            id=new_id(),
            directory_id=padre.id,
            name=path.name,
            owner_id=owner_id,
            size=size,
            block_size=efectivo,
            state=FileState.WRITING,
            created_at=ahora,
            # `size` son bytes CLAROS: es el tamano del archivo tal y como el usuario
            # lo ve. La clave envuelta no se fija aqui sino en el commit, porque se
            # envuelve con el `file_id`, que en este punto todavia no existe.
            expires_at=ahora + timedelta(seconds=write_ttl_seconds),
        )
        uow.files.add(archivo)

        bloques: list[Block] = []
        replicas: list[BlockReplica] = []
        planificados: list[PlannedBlock] = []

        for spec in plan_blocks(size, efectivo):
            block_id = new_id()
            # `blocks.size` es lo que habra EN DISCO. Con cifrado son los bytes claros
            # mas la etiqueta de GCM. `files.size` (arriba) sigue siendo el tamano claro:
            # uno es lo que ocupa y el otro lo que el usuario ve.
            tamano_almacenado = spec.size + cipher_overhead
            # La colocacion se decide aqui y se registra: no se recalcula nunca por hash.
            destinos = placement.select(tamano_almacenado, replication_factor)
            bloques.append(
                Block(
                    block_id=block_id,
                    file_id=archivo.id,
                    index=spec.index,
                    size=tamano_almacenado,
                )
            )
            replicas.extend(
                BlockReplica(
                    block_id=block_id,
                    data_node_id=nodo.id,
                    state=ReplicaState.PENDING,
                    created_at=ahora,
                )
                for nodo in destinos
            )
            planificados.append(
                PlannedBlock(
                    block_id=block_id,
                    index=spec.index,
                    size=tamano_almacenado,
                    # Al cliente, las direcciones que el puede alcanzar...
                    replicas=[(nodo.id, nodo.advertise_url) for nodo in destinos],
                    # ...y para la cadena, las que se alcanzan entre nodos. Las dos son
                    # estaticas: el ControlNode no elige cual mandar segun quien pregunte,
                    # manda las dos y cada una va en su sitio del mensaje.
                    pipeline=[nodo.peer_base_url for nodo in destinos[1:]],
                )
            )

        uow.blocks.add_plan(bloques, replicas)
        uow.commit()

        return CreatedFile(
            file_id=archivo.id,
            block_size=efectivo,
            expires_at=archivo.expires_at,
            blocks=planificados,
        )


@command("files.commit")
def commit_file(
    uow: SqlUnitOfWork,
    owner_id: str,
    file_id: str,
    write_quorum: int = 1,
    replication_factor: int = 1,
    wrapped_key: str = "",
    key_algo: str = "",
) -> CommittedFile:
    """Confirma la reserva y, si habia un archivo en esa ruta, lo retira.

    Las dos cosas ocurren en la misma transaccion. Si se hicieran en dos pasos, una caida
    entre ellos dejaria la ruta sin ningun archivo visible: el viejo ya retirado y el
    nuevo todavia sin confirmar.

    **El quorum se decide aqui, y solo aqui.** El cliente ve un `X-DFSha-Replicas-Acked`
    que le devuelve la cadena del pipeline, pero eso es informativo: la cuenta buena es
    la de `block_replicas`, que cada DataNode actualiza por su cuenta al almacenar su
    copia. Que esto no tenga carreras depende de una decision de la Etapa 2: el aviso
    `/internal/v1/blocks/{id}/stored` es sincrono y va ANTES del 201 al cliente, asi que
    cuando el cliente puede pedir el commit, el ControlNode ya sabe de esas copias.

    Confirmar con W < R deja el archivo **sub-replicado, no roto**: con 2 de 3 todavia
    tolera perder un nodo, y la copia que falta la completa la re-replicacion. Se registra
    en el log para que no sea invisible.
    """
    log = get_logger("control_node")
    inicio = time.perf_counter()

    with uow:
        archivo = uow.files.get(file_id)
        if archivo is None or archivo.owner_id != owner_id:
            raise NotFoundError("no existe la reserva", file_id=file_id)

        ahora = utcnow()
        sin_quorum = uow.blocks.blocks_below_quorum(file_id, write_quorum)
        if sin_quorum:
            log.warning(
                "replication.quorum_failed",
                file_id=file_id,
                quorum=write_quorum,
                blocks_below_quorum=len(sin_quorum),
                sample=sin_quorum[:5],
                duration_ms=round((time.perf_counter() - inicio) * 1000, 3),
                detail="el cliente debe reintentar la subida",
            )
        ensure_can_commit(archivo, sin_quorum, ahora, quorum=write_quorum)

        anterior = uow.files.get_live_by_name(archivo.directory_id, archivo.name)
        if anterior is not None and anterior.id != archivo.id:
            if anterior.state is FileState.COMMITTED:
                # Copy-on-write: la version anterior sale de escena y sus bloques pasan a
                # ser huerfanos para el GC. Los bytes nuevos ya estan en disco.
                uow.files.mark_deleted(anterior.id, ahora)

        if wrapped_key:
            # La envoltura de la clave llega ahora y se guarda en la MISMA transaccion
            # que el commit. Si se guardara antes y el commit fallara, quedaria una clave
            # apuntando a un archivo que nunca existio; si se guardara despues, un fallo
            # entre medias dejaria un archivo visible que nadie puede descifrar.
            uow.files.set_wrapped_key(file_id, wrapped_key, key_algo)

        uow.files.mark_committed(file_id, ahora)
        uow.commit()

        copias = uow.blocks.stored_replica_counts(file_id)
        incompletos = [b for b, c in copias.items() if c < replication_factor]
        log.info(
            "replication.quorum_met",
            file_id=file_id,
            block_count=len(copias),
            quorum=write_quorum,
            replication_factor=replication_factor,
            min_replicas=min(copias.values()) if copias else 0,
            under_replicated_blocks=len(incompletos),
            duration_ms=round((time.perf_counter() - inicio) * 1000, 3),
        )

        ruta = absolute_path(uow.directories, archivo.directory_id).child(archivo.name)
        return CommittedFile(
            path=str(ruta),
            size=archivo.size,
            block_count=len(uow.blocks.list_for_file(file_id)),
        )


@command("files.abort")
def abort_file(uow: SqlUnitOfWork, owner_id: str, file_id: str) -> None:
    """Cancela una reserva. Idempotente: abortar dos veces no es un error.

    Los bloques ya subidos se quedan en disco hasta que se corra el GC; abortar no habla
    con los DataNodes.
    """
    with uow:
        archivo = uow.files.get(file_id)
        if archivo is None or archivo.owner_id != owner_id:
            raise NotFoundError("no existe la reserva", file_id=file_id)

        ensure_can_abort(archivo)
        if archivo.state is FileState.WRITING:
            uow.files.mark_deleted(file_id, utcnow())
            uow.commit()


@dataclass(frozen=True, slots=True)
class AppendPlan:
    """Lo que hace falta para anadir datos al final de un archivo.

    ## Por que anadir a un archivo WORM no es trivial

    Los bloques son inmutables (decision 1 de la seccion 1). Si el ultimo bloque de un
    archivo esta **a medias** —lo normal, salvo que el tamano sea multiplo exacto— anadir
    datos tiene que llenarlo, y llenarlo es reescribirlo. Las dos salidas posibles:

    (a) **Empezar siempre un bloque nuevo.** Barato de escribir y caro para siempre: un
        archivo al que se anade mil veces acaba con mil bloques diminutos, cada uno con su
        colocacion, sus R=3 filas de replica y su sitio en la cola de re-replicacion. Mil
        bloques de 12 bytes son 3000 filas para 12 KB de datos.

    (b) **Reescribir el bloque de cola** como copy-on-write: nace con OTRO `block_id`,
        ocupa el mismo indice, y el viejo se desliga y queda para el GC.

    Se eligio **(b)**, y el precio hay que decirlo entero: **anadir un byte puede reescribir
    hasta un bloque completo** —64 MB con el default—. Es amplificacion de escritura
    acotada por el tamano de bloque, no por el del archivo. A cambio se conserva el
    invariante «todos los bloques llenos salvo el ultimo», que es lo que mantiene la
    colocacion, la contabilidad de tamanos y las lecturas por rango razonando sobre bloques
    homogeneos.

    ## Y la cola la reescribe el CLIENTE, no el servidor

    No es una comodidad: con cifrado extremo a extremo **el servidor no podria hacerlo
    aunque quisiera**. Para concatenar los bytes nuevos al bloque de cola hay que
    descifrarlo, y la clave no sale del cliente. La consecuencia es que el append encaja
    solo en la decision 4 —los bytes no pasan por el ControlNode— sin que haya habido que
    forzar nada.

    El cliente recibe `tail` (el bloque parcial, como plan de LECTURA), se lo baja, lo
    descifra, le pega los datos nuevos, y sube el resultado como el primer bloque de
    `blocks`.
    """

    file_id: str
    block_size: int
    #: El bloque de cola a reescribir, o `None` si el archivo acaba en bloque lleno (o
    #: esta vacio). Es un plan de LECTURA porque el cliente tiene que bajarselo.
    tail: ReadBlock | None
    #: Bytes CLAROS que ese bloque de cola tiene hoy. El cliente los conserva y escribe
    #: detras. Va aparte de `tail.size` porque ese es el tamano ALMACENADO.
    tail_plain_size: int
    #: `block_id` del bloque de cola viejo, a desligar en el commit. Vacio si no hay cola.
    replaces: str
    blocks: list[PlannedBlock]
    expires_at: object


def append_to_file(
    uow: SqlUnitOfWork,
    placement,
    owner_id: str,
    file_id: str,
    added_size: int,
    *,
    write_ttl_seconds: int,
    replication_factor: int = 1,
    cipher_overhead: int = 0,
    fencing: LockFencing | None = None,
    now: datetime | None = None,
) -> AppendPlan:
    """Planifica anadir `added_size` bytes claros al final de un archivo COMMITTED.

    **`require_lock` se llama DENTRO de este `with uow:`**, no antes. Es la regla del
    Bloque A trasladada al RF3: comprobar el lock en una transaccion y planificar en otra
    deja una ventana por la que se cuela el cliente congelado. Ver `commands/filelock.py`.
    """
    if added_size <= 0:
        raise InvalidStateError("no hay nada que anadir", added=added_size)

    ahora = now or utcnow()

    with uow:
        archivo = uow.files.get(file_id)
        if archivo is None or archivo.state is not FileState.COMMITTED:
            raise NotFoundError("no hay un archivo confirmado con ese id", file_id=file_id)

        # Anadir es escribir: WRITE sobre el directorio que lo contiene. Se resuelve por
        # el mismo camino que todo lo demas, con el minimo en la firma.
        directorio = uow.directories.get(archivo.directory_id)
        if directorio is None:
            raise NotFoundError("el directorio del archivo no existe", file_id=file_id)
        require(uow, owner_id, directorio, Permission.WRITE)

        # EL LOCK, dentro de la misma transaccion que lo que se va a escribir.
        require_lock(uow, file_id, fencing, now=ahora)

        bloques = uow.blocks.list_for_file(file_id)
        efectivo = archivo.block_size

        # ¿Acaba en bloque a medias? Se mira el tamano CLARO, que es el que el usuario ve.
        cola = bloques[-1] if bloques else None
        cola_clara = (cola.size - cipher_overhead) if cola is not None else 0
        reescribe_cola = cola is not None and cola_clara < efectivo

        nuevo_tamano = archivo.size + added_size

        # Lo que hay que escribir: los bytes de la cola que se conservan mas los nuevos.
        por_escribir = (cola_clara if reescribe_cola else 0) + added_size
        indice_inicial = (cola.index if reescribe_cola else len(bloques))

        bloques_nuevos: list[Block] = []
        replicas: list[BlockReplica] = []
        planificados: list[PlannedBlock] = []

        for spec in plan_blocks(por_escribir, efectivo):
            block_id = new_id()
            indice = indice_inicial + spec.index
            almacenado = spec.size + cipher_overhead
            destinos = placement.select(almacenado, replication_factor)
            # file_id=None: el bloque nace SUELTO. Se engancha en el commit del append,
            # cuando sus bytes ya estan en disco. Si se enganchara aqui, entre la
            # planificacion y la subida el archivo tendria bloques que nadie ha escrito y
            # cualquier lectura fallaria.
            bloques_nuevos.append(
                Block(block_id=block_id, file_id=None, index=indice, size=almacenado)
            )
            replicas.extend(
                BlockReplica(
                    block_id=block_id,
                    data_node_id=nodo.id,
                    state=ReplicaState.PENDING,
                    created_at=ahora,
                )
                for nodo in destinos
            )
            planificados.append(
                PlannedBlock(
                    block_id=block_id,
                    index=indice,
                    size=almacenado,
                    replicas=[(n.id, n.advertise_url) for n in destinos],
                    pipeline=[n.peer_base_url for n in destinos[1:]],
                )
            )

        uow.blocks.add_plan(bloques_nuevos, replicas)
        uow.commit()

        lectura_cola = None
        if reescribe_cola:
            copias = uow.blocks.list_replicas([cola.block_id]).get(cola.block_id, [])
            almacenadas = [r for r in copias if r.state is ReplicaState.STORED]
            if not almacenadas:
                raise NotFoundError(
                    "no se puede anadir: el bloque de cola no tiene ninguna copia legible",
                    file_id=file_id,
                    block_id=cola.block_id,
                )
            lectura_cola = ReadBlock(
                block_id=cola.block_id,
                index=cola.index,
                size=cola.size,
                checksum_sha256=cola.checksum_sha256 or "",
                replicas=[
                    (r.data_node_id, nodo.advertise_url)
                    for r in almacenadas
                    if (nodo := uow.data_nodes.get(r.data_node_id)) is not None
                ],
            )

    get_logger("control_node").info(
        "file.append_planned",
        file_id=file_id,
        added_bytes=added_size,
        new_size=nuevo_tamano,
        rewrites_tail=reescribe_cola,
        rewritten_bytes=cola_clara if reescribe_cola else 0,
        new_blocks=len(planificados),
    )

    return AppendPlan(
        file_id=file_id,
        block_size=efectivo,
        tail=lectura_cola,
        tail_plain_size=cola_clara if reescribe_cola else 0,
        replaces=cola.block_id if reescribe_cola else "",
        blocks=planificados,
        expires_at=ahora + timedelta(seconds=write_ttl_seconds),
    )


def commit_append(
    uow: SqlUnitOfWork,
    owner_id: str,
    file_id: str,
    *,
    block_ids: list[str],
    new_size: int,
    replaces: str = "",
    write_quorum: int = 1,
    fencing: LockFencing | None = None,
    now: datetime | None = None,
) -> CommittedFile:
    """Confirma un append. **Todo el cambio de metadato ocurre aqui, de una vez.**

    Hasta esta llamada el archivo no ha cambiado: sigue con su tamano viejo, su bloque de
    cola viejo, y los bloques nuevos existen sueltos sin pertenecerle. Un lector que pase
    por en medio ve el archivo de antes, entero y legible. Eso es lo que distingue este
    diseno de uno que fuera actualizando el metadato sobre la marcha, que dejaria ventanas
    en las que el archivo dice medir mas de lo que se puede leer.

    Las tres escrituras —desligar la cola vieja, enganchar los bloques nuevos, actualizar
    el tamano— van en **una sola transaccion**, por el mismo motivo que el copy-on-write
    del `commit` normal: una caida en medio dejaria el archivo describiendose a si mismo
    de forma incoherente.

    El orden importa: **primero desligar y luego enganchar**. Al reves, el bloque nuevo y
    el viejo compartirian `(file_id, index)` un instante y saltaria `uq_blocks_file_index`.
    """
    ahora = now or utcnow()

    with uow:
        archivo = uow.files.get(file_id)
        if archivo is None or archivo.state is not FileState.COMMITTED:
            raise NotFoundError("no hay un archivo confirmado con ese id", file_id=file_id)

        directorio = uow.directories.get(archivo.directory_id)
        if directorio is None:
            raise NotFoundError("el directorio del archivo no existe", file_id=file_id)
        require(uow, owner_id, directorio, Permission.WRITE)

        # El lock se vuelve a exigir AQUI, no solo al planificar. Entre el plan y el
        # commit el cliente subio bytes, lo que puede tardar minutos con bloques de 64 MB:
        # tiempo de sobra para que su lease venciera y otro tomara el archivo.
        require_lock(uow, file_id, fencing, now=ahora)

        sin_quorum = uow.blocks.ids_below_quorum(block_ids, write_quorum)
        if sin_quorum:
            raise BlocksNotStoredError(
                "no alcanzan el quorum de escritura",
                file_id=file_id,
                quorum=write_quorum,
                blocks=sin_quorum[:5],
            )

        if replaces:
            uow.blocks.detach(replaces)
        uow.blocks.attach(block_ids, file_id)
        uow.files.set_size(file_id, new_size)
        uow.commit()

        bloques = uow.blocks.list_for_file(file_id)

    get_logger("control_node").info(
        "file.appended",
        file_id=file_id,
        new_size=new_size,
        new_blocks=len(block_ids),
        rewrote_tail=bool(replaces),
        block_count=len(bloques),
    )
    return CommittedFile(path=archivo.name, size=new_size, block_count=len(bloques))
