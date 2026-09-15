"""Value object `Path`: la unica forma valida de nombrar algo en el namespace.

Toda ruta que entra por la API pasa por aqui antes de tocar un repositorio. Es el punto
donde se rechaza `..`, los nombres vacios y los caracteres de control, de modo que ningun
caso de uso tenga que acordarse de validar.

Es namespace logico, no rutas del sistema de archivos: el DataNode nunca ve una de estas.
Aun asi se rechaza `..` en vez de resolverlo, porque una ruta que sube de nivel solo llega
por error del cliente o por un intento de escape, y ninguno de los dos merece silencio.
"""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import InvalidPathError

__all__ = ["Path", "MAX_NAME_LENGTH"]

#: Limite por segmento, alineado con lo que acepta un sistema de archivos POSIX.
MAX_NAME_LENGTH = 255

_SEPARATOR = "/"


def _validate_name(name: str) -> str:
    if name == "":
        raise InvalidPathError("la ruta contiene un nombre vacio")
    if name == "..":
        raise InvalidPathError(
            "la ruta contiene '..'; las rutas de DFSha no suben de nivel", segment=name
        )
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidPathError(
            f"nombre de mas de {MAX_NAME_LENGTH} caracteres", segment=name[:32] + "..."
        )
    for char in name:
        if ord(char) < 32 or ord(char) == 127:
            raise InvalidPathError(
                "la ruta contiene caracteres de control", segment=repr(name)
            )
    return name


@dataclass(frozen=True, slots=True)
class Path:
    """Ruta absoluta y normalizada dentro del arbol de un usuario.

    `segments` vacio es la raiz. Se construye con `Path.parse`, nunca directamente con
    texto sin validar.
    """

    segments: tuple[str, ...] = ()

    # --- Construccion ------------------------------------------------------

    @classmethod
    def root(cls) -> "Path":
        return cls(())

    @classmethod
    def parse(cls, raw: str, cwd: "Path | None" = None) -> "Path":
        """Normaliza `raw` a una ruta absoluta.

        Una ruta relativa se resuelve contra `cwd`. Sin `cwd` se rechaza: adivinar la raiz
        convertiria un error del cliente en una escritura en el sitio equivocado.
        """
        if not isinstance(raw, str):
            raise InvalidPathError("la ruta debe ser una cadena")
        if raw == "":
            raise InvalidPathError("la ruta esta vacia")

        absolute = raw.startswith(_SEPARATOR)
        if not absolute:
            if cwd is None:
                raise InvalidPathError(
                    "ruta relativa sin directorio de trabajo con el que resolverla",
                    path=raw,
                )
            base = cwd.segments
        else:
            base = ()

        parts: list[str] = list(base)
        for chunk in raw.split(_SEPARATOR):
            if chunk == "" or chunk == ".":
                # Barras repetidas, barra final y '.' son ruido, no un error: se descartan.
                # '..' no cae aqui, lo rechaza _validate_name.
                continue
            parts.append(_validate_name(chunk))

        return cls(tuple(parts))

    # --- Consultas ---------------------------------------------------------

    @property
    def is_root(self) -> bool:
        return len(self.segments) == 0

    @property
    def name(self) -> str:
        """Ultimo segmento. La raiz no tiene nombre."""
        if self.is_root:
            raise InvalidPathError("la raiz no tiene nombre")
        return self.segments[-1]

    @property
    def parent(self) -> "Path":
        """Directorio contenedor. El padre de la raiz es la raiz."""
        if self.is_root:
            return self
        return Path(self.segments[:-1])

    @property
    def depth(self) -> int:
        return len(self.segments)

    def child(self, name: str) -> "Path":
        return Path((*self.segments, _validate_name(name)))

    def is_ancestor_of(self, other: "Path") -> bool:
        """True si `other` cuelga de esta ruta. Una ruta no es ancestro de si misma.

        Lo usa `mv` para rechazar mover un directorio dentro de su propio subarbol, que
        dejaria huerfano el subarbol entero.
        """
        return len(other.segments) > len(self.segments) and (
            other.segments[: len(self.segments)] == self.segments
        )

    # --- Representacion ----------------------------------------------------

    def __str__(self) -> str:
        return _SEPARATOR + _SEPARATOR.join(self.segments)

    def __repr__(self) -> str:
        return f"Path({str(self)!r})"
