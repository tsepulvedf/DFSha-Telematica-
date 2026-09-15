"""Resolucion de rutas logicas a filas del metadato.

Todo recorrido empieza en la raiz del usuario que hace la peticion. Eso es lo que hace
que el aislamiento entre usuarios sea estructural y no una comprobacion que alguien pueda
olvidarse de escribir: no hay forma de nombrar el arbol de otro, porque el punto de
partida es siempre el propio.
"""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.entities import Directory, File
from dfsha.control_node.domain.path import Path
from dfsha.control_node.repositories.base import DirectoryRepository, FileRepository

__all__ = [
    "Resolved",
    "resolve_root",
    "resolve_directory",
    "find_directory",
    "resolve_entry",
    "absolute_path",
]


def resolve_root(directories: DirectoryRepository, owner_id: str) -> Directory:
    raiz = directories.get_root(owner_id)
    if raiz is None:
        # Solo puede pasar si el alta de usuario dejo el arbol a medias.
        raise NotFoundError("el usuario no tiene arbol de archivos", owner_id=owner_id)
    return raiz


def find_directory(
    directories: DirectoryRepository, owner_id: str, path: Path
) -> Directory | None:
    """Recorre la ruta segmento a segmento. `None` si algun tramo no existe."""
    actual = resolve_root(directories, owner_id)
    for nombre in path.segments:
        siguiente = directories.get_child(actual.id, nombre)
        if siguiente is None:
            return None
        actual = siguiente
    return actual


def resolve_directory(
    directories: DirectoryRepository, owner_id: str, path: Path
) -> Directory:
    directorio = find_directory(directories, owner_id, path)
    if directorio is None:
        raise NotFoundError("no existe el directorio", path=str(path))
    return directorio


@dataclass(frozen=True, slots=True)
class Resolved:
    """Lo que hay en una ruta: un directorio, un archivo, o nada.

    `file` puede venir en cualquier estado; quien llame decide si le sirve. `ls` y `open`
    exigen COMMITTED (`ensure_visible`), mientras que `create` necesita ver la reserva
    vencida para poder pisarla.
    """

    path: Path
    directory: Directory | None = None
    file: File | None = None

    @property
    def exists(self) -> bool:
        return self.directory is not None or self.file is not None

    @property
    def is_directory(self) -> bool:
        return self.directory is not None


def resolve_entry(
    directories: DirectoryRepository,
    files: FileRepository,
    owner_id: str,
    path: Path,
) -> Resolved:
    """Resuelve una ruta que puede ser directorio o archivo.

    Nunca lanza por no encontrar nada: devuelve un `Resolved` vacio. Quien llama traduce
    eso a 404 o lo usa como "el nombre esta libre", que son cosas distintas.
    """
    if path.is_root:
        return Resolved(path=path, directory=resolve_root(directories, owner_id))

    padre = find_directory(directories, owner_id, path.parent)
    if padre is None:
        return Resolved(path=path)

    directorio = directories.get_child(padre.id, path.name)
    if directorio is not None:
        return Resolved(path=path, directory=directorio)

    return Resolved(path=path, file=files.get_live_by_name(padre.id, path.name))


def absolute_path(directories: DirectoryRepository, directory_id: str) -> Path:
    """Reconstruye la ruta de un directorio subiendo por sus padres.

    Hace falta porque el metadato guarda el arbol por enlaces padre-hijo y no por rutas:
    eso es lo que hace que `mv` sea O(1), pero obliga a recomponer la ruta cuando hay que
    devolversela al cliente. El coste es la profundidad del arbol, no su tamano.
    """
    segmentos: list[str] = []
    actual = directories.get(directory_id)
    while actual is not None and not actual.is_root:
        segmentos.append(actual.name)
        actual = directories.get(actual.parent_id) if actual.parent_id else None
    return Path(tuple(reversed(segmentos)))
