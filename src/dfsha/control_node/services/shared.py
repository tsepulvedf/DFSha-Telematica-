"""El directorio virtual `/compartido-conmigo`.

Lo ajeno se lista **aparte**, no mezclado en tu arbol. Eso mantiene el modelo mental
limpio: tu espacio es tuyo, y lo que otros te comparten esta en un sitio del que sabes que
no es tuyo. Mezclarlo tendria dos problemas concretos, y el segundo es el peor:

1. Dos personas pueden compartirte directorios que se llamen igual.
2. Un `rm -r /` tuyo tendria que decidir que hacer con lo ajeno que colgara de tu arbol, y
   cualquier respuesta es mala.

## La forma de las rutas

    /compartido-conmigo                      -> quienes te han compartido algo
    /compartido-conmigo/ana                  -> lo que Ana te comparte
    /compartido-conmigo/ana/informes/2026    -> navegacion normal dentro de eso

Dos niveles virtuales (usuario y nombre del directorio compartido) y a partir de ahi el
recorrido es el de siempre. El usuario va primero porque es lo que desambigua: si Ana y
Beto te comparten sendos `informes`, siguen siendo rutas distintas sin que nadie tenga
que renombrar nada.

**Nada de esto existe en el metadato.** Es una vista: se traduce el prefijo a un
directorio real de otro arbol y se sigue. Por eso no hay que mantener nada sincronizado
cuando alguien mueve o deja de compartir un directorio.
"""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.acl import Permission, PrincipalType
from dfsha.control_node.domain.entities import Directory
from dfsha.control_node.domain.path import Path

__all__ = ["SHARED_ROOT", "Scope", "is_shared_path", "resolve_scope", "shared_entries"]

#: El nombre del directorio virtual. Un usuario no puede crear uno que se llame asi en su
#: raiz: lo impide `ensure_not_shadowing_shared_root`, porque si pudiera, su propio
#: directorio quedaria inalcanzable para siempre.
SHARED_ROOT = "compartido-conmigo"


@dataclass(frozen=True, slots=True)
class Scope:
    """Donde empieza de verdad un recorrido, y en el arbol de quien.

    `start` es el directorio desde el que se camina; `rest` lo que queda de ruta. Para una
    ruta normal, `start` es la raiz del propio usuario y `rest` la ruta entera, que es
    exactamente el comportamiento de las etapas anteriores.
    """

    start: Directory
    owner_id: str
    rest: Path
    #: `True` si se entro por `/compartido-conmigo`. Lo usan las consultas para saber que
    #: la ruta que devuelven al cliente lleva el prefijo virtual.
    shared: bool = False
    #: Usuario dueno del arbol, para poder reconstruir la ruta virtual.
    owner_username: str = ""
    #: Nombre del directorio compartido dentro del arbol del dueno.
    share_name: str = ""


def is_shared_path(path: Path) -> bool:
    return bool(path.segments) and path.segments[0] == SHARED_ROOT


def resolve_scope(uow, user_id: str, path: Path) -> Scope:
    """Traduce una ruta —normal o virtual— a un punto de partida real.

    Es la unica funcion que sabe que `/compartido-conmigo` no existe en el metadato. Todo
    lo que hay aguas abajo recorre directorios reales y no distingue un caso del otro.
    """
    from dfsha.control_node.services.resolver import resolve_root

    if not is_shared_path(path):
        return Scope(
            start=resolve_root(uow.directories, user_id),
            owner_id=user_id,
            rest=path,
        )

    segmentos = path.segments[1:]
    if not segmentos:
        # `/compartido-conmigo` a secas: no es un directorio real, y quien llama lo
        # resuelve listando `shared_entries`. Se devuelve un scope sin `start` valido
        # para que el error sea claro si alguien intenta caminar por el.
        raise _VirtualRoot()

    username = segmentos[0]
    dueno = uow.users.get_by_username(username)
    if dueno is None:
        raise NotFoundError("no existe ese usuario", path=str(path))

    accesibles = [e for e in shared_entries(uow, user_id) if e.owner_id == dueno.id]
    if len(segmentos) == 1:
        # `/compartido-conmigo/ana`: tampoco es un directorio real.
        raise _VirtualUser(dueno.id, username, accesibles)

    nombre = segmentos[1]
    entrada = next((e for e in accesibles if e.share_name == nombre), None)
    if entrada is None:
        raise NotFoundError("no existe la ruta", path=str(path))

    return Scope(
        start=entrada.directory,
        owner_id=dueno.id,
        rest=Path(tuple(segmentos[2:])),
        shared=True,
        owner_username=username,
        share_name=nombre,
    )


class _VirtualRoot(Exception):
    """`/compartido-conmigo` a secas. Lo captura la consulta de `ls`."""


class _VirtualUser(Exception):
    """`/compartido-conmigo/<usuario>`. Lo captura la consulta de `ls`."""

    def __init__(self, owner_id: str, username: str, entradas: list) -> None:
        super().__init__(username)
        self.owner_id = owner_id
        self.username = username
        self.entradas = entradas


@dataclass(frozen=True, slots=True)
class SharedEntry:
    """Un directorio que alguien comparte contigo, con el permiso que te dio."""

    directory: Directory
    owner_id: str
    owner_username: str
    share_name: str
    permission: Permission
    via_group: str = ""


def shared_entries(uow, user_id: str) -> list[SharedEntry]:
    """Todo lo que otros comparten con este usuario, directo o por sus grupos.

    Se listan las concesiones tal cual: si a alguien le comparten `/a` y tambien `/a/b`,
    vera las dos, porque las dos son puntos de entrada legitimos y con permisos que
    pueden diferir.
    """
    grupos = uow.acl.groups_of(user_id)
    entradas: list[SharedEntry] = []

    for fila in uow.acl.shared_with(user_id, grupos):
        directorio = uow.directories.get(fila.directory_id)
        if directorio is None or directorio.deleted_at is not None:
            continue  # el directorio se borro; la concesion es una reliquia
        if directorio.owner_id == user_id:
            continue  # es tuyo: no se lista como "compartido contigo"

        dueno = uow.users.get(directorio.owner_id)
        if dueno is None:
            continue

        entradas.append(
            SharedEntry(
                directory=directorio,
                owner_id=directorio.owner_id,
                owner_username=dueno.username,
                share_name=directorio.name or dueno.username,
                permission=Permission(fila.permission),
                via_group=(
                    fila.principal_id
                    if fila.principal_type == PrincipalType.GROUP.value
                    else ""
                ),
            )
        )

    return sorted(entradas, key=lambda e: (e.owner_username, e.share_name))
