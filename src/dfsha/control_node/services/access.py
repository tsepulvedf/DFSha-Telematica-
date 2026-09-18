"""Resolver una ruta **y** autorizarla, en un solo paso.

Estas dos funciones sustituyen a `resolve_directory` y `resolve_entry` en todo lo que
atiende una peticion de usuario. La sustitucion es deliberada y es media defensa del
Bloque C: al obligar a pasar el permiso minimo como argumento, **no se puede resolver una
ruta sin decir para que**. Un sitio que se olvidara de comprobar el permiso no compilaria
como un olvido silencioso, sino que tendria que escribir explicitamente que permiso pide.

Antes del Bloque C el aislamiento era estructural: todo recorrido arrancaba en
`get_root(owner_id)`, asi que un usuario no podia ni nombrar el arbol de otro. Eso dejo
de bastar en cuanto existe algo que compartir, y lo que lo sustituye no puede ser "una
comprobacion en cada sitio": tiene que ser *el mismo* paso que resuelve la ruta.
"""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.acl import Permission
from dfsha.control_node.domain.entities import Directory, File
from dfsha.control_node.domain.path import Path
from dfsha.control_node.services.permissions import Access, require
from dfsha.control_node.services.shared import Scope, resolve_scope

__all__ = ["ResolvedAccess", "directory_for", "entry_for", "walk"]


@dataclass(frozen=True, slots=True)
class ResolvedAccess:
    """Lo que hay en una ruta, con el permiso ya comprobado."""

    path: Path
    scope: Scope
    access: Access
    directory: Directory | None = None
    file: File | None = None

    @property
    def is_shared(self) -> bool:
        return self.scope.shared

    # Las tres propiedades del `Resolved` de las etapas anteriores, con el mismo
    # significado. `ResolvedAccess` lo sustituye anadiendole el permiso comprobado, asi
    # que quien las usaba no tiene que cambiar de vocabulario.
    @property
    def exists(self) -> bool:
        return self.directory is not None or self.file is not None

    @property
    def is_directory(self) -> bool:
        return self.directory is not None

    @property
    def is_file(self) -> bool:
        return self.file is not None


def walk(directories, start: Directory, rest: Path) -> Directory | None:
    """Recorre `rest` desde `start`. `None` si algun tramo no existe."""
    actual = start
    for nombre in rest.segments:
        siguiente = directories.get_child(actual.id, nombre)
        if siguiente is None:
            return None
        actual = siguiente
    return actual


def directory_for(
    uow, user_id: str, path: Path, minimum: Permission
) -> ResolvedAccess:
    """El directorio de `path`, si existe y si `user_id` tiene al menos `minimum`."""
    scope = resolve_scope(uow, user_id, path)
    directorio = walk(uow.directories, scope.start, scope.rest)
    if directorio is None:
        raise NotFoundError("no existe el directorio", path=str(path))

    acceso = require(uow, user_id, directorio, minimum, path=str(path))
    return ResolvedAccess(path=path, scope=scope, access=acceso, directory=directorio)


def entry_for(
    uow, user_id: str, path: Path, minimum: Permission
) -> ResolvedAccess:
    """Lo que haya en `path` —directorio, archivo o nada— con el permiso comprobado.

    El permiso de un archivo es el de **su directorio**: las concesiones se cuelgan de
    directorios, no de archivos sueltos. Colgarlas tambien de archivos obligaria a mirar
    dos sitios en cada comprobacion sin ganar expresividad, porque un archivo siempre esta
    en un directorio al que se le puede conceder.

    Como `resolve_entry`, no lanza por no encontrar nada: devuelve `directory=None` y
    `file=None`, y quien llama decide si eso es un 404 o "el nombre esta libre". Lo que si
    lanza es la falta de permiso sobre el directorio donde se busca, porque ahi no hay
    ambiguedad: si no puedes mirar dentro, no puedes saber si el nombre esta libre.
    """
    scope = resolve_scope(uow, user_id, path)

    if not scope.rest.segments:
        # La ruta ES el punto de entrada: la raiz propia, o el directorio compartido.
        acceso = require(uow, user_id, scope.start, minimum, path=str(path))
        return ResolvedAccess(
            path=path, scope=scope, access=acceso, directory=scope.start
        )

    padre = walk(uow.directories, scope.start, scope.rest.parent)
    if padre is None:
        raise NotFoundError("no existe la ruta", path=str(path))

    acceso = require(uow, user_id, padre, minimum, path=str(path))

    nombre = scope.rest.name
    hijo = uow.directories.get_child(padre.id, nombre)
    if hijo is not None:
        return ResolvedAccess(path=path, scope=scope, access=acceso, directory=hijo)

    archivo = uow.files.get_live_by_name(padre.id, nombre)
    return ResolvedAccess(path=path, scope=scope, access=acceso, file=archivo)
