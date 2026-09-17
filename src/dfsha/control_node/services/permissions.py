"""El unico sitio por el que se autoriza algo.

Todas las operaciones del namespace pasan por `require`. Antes del Bloque C la
comprobacion era implicita —cada consulta arrancaba en `get_root(owner_id)`, asi que un
usuario no podia ni nombrar el arbol de otro— y eso funcionaba precisamente porque no
habia nada que compartir. Al existir las ACLs, esa comprobacion implicita deja de bastar
y hay que hacerla explicita **en un solo sitio**.

Que sea uno solo no es limpieza, es lo unico que hace revisable un sistema de permisos:
diez comprobaciones repartidas son diez sitios donde olvidarse de una, y la que se olvide
no se va a notar hasta que alguien lea lo que no debia.

## La division con `domain/acl`

- `domain/acl.resolve` **decide**, y es puro: recibe la cadena de directorios, las
  concesiones y los grupos, y devuelve el permiso. Se prueba sin base de datos.
- Este modulo **lee lo que hace falta** para poder preguntarle, y traduce la respuesta a
  un error o a un `None`.

Esa frontera es la que permite que las 22 pruebas del modelo de permisos no necesiten
levantar nada.
"""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import AuthorizationError, NotFoundError
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.acl import Grant, Permission, resolve
from dfsha.control_node.domain.entities import Directory
from dfsha.control_node.repositories.base import DirectoryRepository

__all__ = ["Access", "ancestor_chain", "effective_permission", "require", "owner_of"]


@dataclass(frozen=True, slots=True)
class Access:
    """El permiso de alguien sobre un directorio, con su origen y su dueno."""

    directory: Directory
    owner_id: str
    grant: Grant | None

    @property
    def permission(self) -> Permission | None:
        return self.grant.permission if self.grant else None

    def allows(self, minimum: Permission) -> bool:
        return self.grant is not None and self.grant.permission >= minimum


def ancestor_chain(
    directories: DirectoryRepository, directory: Directory
) -> list[Directory]:
    """La cadena desde la RAIZ hasta `directory`, ambos incluidos.

    De la raiz hacia abajo para que `resolve` la recorra al reves y pare en la primera
    regla aplicable, que es la mas cercana a la ruta consultada.

    El coste es la profundidad del arbol, no su tamano — la misma propiedad que hace que
    `mv` sea O(1): el metadato guarda enlaces padre-hijo, no rutas.
    """
    cadena: list[Directory] = [directory]
    actual = directory
    while not actual.is_root and actual.parent_id:
        padre = directories.get(actual.parent_id)
        if padre is None:
            break  # arbol roto; se resuelve con lo que hay en vez de reventar
        cadena.append(padre)
        actual = padre
    cadena.reverse()
    return cadena


def owner_of(directories: DirectoryRepository, directory: Directory) -> str:
    """El dueno del ARBOL al que pertenece el directorio.

    Se toma del propio directorio: `owner_id` se propaga al crearlo, asi que todo el
    subarbol comparte dueno. Subir hasta la raiz para leerlo de alli seria el mismo
    resultado con mas viajes.
    """
    return directory.owner_id


def effective_permission(
    uow, user_id: str, directory: Directory
) -> Access:
    """Que puede hacer `user_id` en `directory`. `None` si nada.

    Tres lecturas: la cadena de ancestros, las concesiones de toda la cadena en una sola
    consulta, y los grupos del usuario. Ni una mas.
    """
    cadena = ancestor_chain(uow.directories, directory)
    ids = [d.id for d in cadena]

    concesiones = uow.acl.entries_for(ids)
    grupos = uow.acl.groups_of(user_id) if concesiones else set()

    grant = resolve(
        user_id=user_id,
        owner_id=owner_of(uow.directories, directory),
        chain=ids,
        entries_by_directory=concesiones,
        user_groups=grupos,
    )
    return Access(directory=directory, owner_id=directory.owner_id, grant=grant)


def require(
    uow,
    user_id: str,
    directory: Directory,
    minimum: Permission,
    path: str = "",
) -> Access:
    """Exige al menos `minimum`, o lanza.

    **Lanza `NotFoundError`, no `AuthorizationError`, cuando el usuario no tiene ningun
    permiso.** Es deliberado: responder 403 sobre una ruta ajena confirma que existe, y
    eso convierte el arbol de otro usuario en algo enumerable a base de probar nombres.
    Un 404 no dice nada. Se distingue el caso de tener ALGO de permiso pero no bastante,
    que si es 403: ahi el usuario ya sabe que la ruta existe, y decirle "no te alcanza"
    es mas util que mentirle.

    El evento `acl.denied` se emite en los dos casos con el permiso pedido y el efectivo,
    que es lo que hace diagnosticable un permiso mal puesto.
    """
    acceso = effective_permission(uow, user_id, directory)
    if acceso.allows(minimum):
        return acceso

    get_logger("control_node").warning(
        "acl.denied",
        user=user_id,
        path=path or directory.id,
        required=minimum.name,
        effective=acceso.permission.name if acceso.permission else None,
        owner=acceso.owner_id,
        duration_ms=0.0,
    )

    if acceso.grant is None:
        raise NotFoundError("no existe la ruta", path=path)

    raise AuthorizationError(
        f"hace falta permiso {minimum.name} y tienes {acceso.permission.name}",
        path=path,
        required=minimum.name,
        effective=acceso.permission.name,
    )
