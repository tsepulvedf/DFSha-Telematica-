"""Permisos: el modelo, y **una sola** funcion que los resuelve.

Toda pregunta sobre "¿puede este usuario hacer esto aqui?" pasa por `resolve`. No hay
comprobaciones repartidas, y eso no es limpieza: es lo unico que hace revisable un sistema
de permisos. Diez sitios que comprueban permisos son diez sitios donde olvidarse de uno.

## El modelo, en cuatro reglas

1. **Tres permisos, ordenados**: `READ` < `WRITE` < `ADMIN`. Quien puede escribir puede
   leer; quien administra puede todo. Estan ordenados a proposito, para que "tener al
   menos X" sea una comparacion y no una tabla.
2. **Solo concesiones.** La ausencia de concesion es denegacion. **No hay reglas de
   «denegar»**, y esa es la decision que mantiene el sistema razonable: en cuanto existe
   un «denegar», hay que definir que gana cuando un ancestro permite y un descendiente
   niega, o al reves, y en que orden se evaluan los grupos. Ahi es donde estos modelos
   dejan de poder explicarse.
3. **Herencia hacia arriba.** Para saber el permiso sobre una ruta se sube por el arbol
   hasta encontrar la primera regla aplicable. **No se propagan permisos a los hijos al
   conceder**, porque eso se corrompe en cuanto se mueve un directorio: el hijo llevaria
   permisos heredados de un padre que ya no es el suyo. Resolviendo hacia arriba, mover
   un directorio cambia sus permisos efectivos automaticamente, que es lo correcto.
4. **El maximo entre usuario y grupos.** Si a Beto le dan READ directamente y su grupo
   tiene WRITE, Beto tiene WRITE. Es la consecuencia de no tener denegaciones: los
   permisos solo pueden sumar.

## El propietario

Tiene `ADMIN` implicito sobre su arbol, y no como un caso especial cosido aparte: entra en
la misma funcion, como la ultima regla que se aplica al llegar a la raiz. Eso es lo que
hace que las 275 pruebas de las etapas anteriores —todas sobre usuarios en su propio
arbol— sigan describiendo exactamente el mismo comportamiento.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Permission",
    "PrincipalType",
    "AclEntry",
    "Grant",
    "resolve",
    "effective",
]


class Permission(IntEnum):
    """Los tres permisos, ordenados por potencia.

    `IntEnum` y no `StrEnum` para que `>=` signifique "al menos": comparar es la
    operacion que se hace todo el rato, y una tabla de equivalencias seria una copia mas
    de la jerarquia que se puede desincronizar.
    """

    READ = 1
    WRITE = 2
    ADMIN = 3

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def parse(cls, texto: str) -> "Permission":
        try:
            return cls[texto.strip().upper()]
        except KeyError:
            validos = ", ".join(p.name.lower() for p in cls)
            raise ValueError(
                f"permiso desconocido '{texto}'; los validos son: {validos}"
            ) from None


class PrincipalType(IntEnum):
    USER = 1
    GROUP = 2


@dataclass(frozen=True, slots=True)
class AclEntry:
    """Una concesion sobre un directorio."""

    directory_id: str
    principal_type: PrincipalType
    principal_id: str
    permission: Permission


@dataclass(frozen=True, slots=True)
class Grant:
    """De donde sale el permiso que tiene alguien. Sirve para explicarlo.

    Que `resolve` devuelva tambien el ORIGEN no es un adorno: un permiso que no se puede
    explicar no se puede auditar, y "no entiendo por que Beto ve esto" es la pregunta mas
    frecuente sobre cualquier sistema de permisos. `dfsha acl` lo muestra.
    """

    permission: Permission
    #: "owner", "user" o "group".
    source: str
    #: Directorio donde estaba la regla que gano. Igual a la ruta consultada si la regla
    #: era suya; un ancestro si se heredo.
    directory_id: str
    #: Id del grupo, cuando el permiso vino por uno.
    principal_id: str = ""

    @property
    def inherited_from(self) -> str:
        return self.directory_id


def resolve(
    *,
    user_id: str,
    owner_id: str,
    chain: list[str],
    entries_by_directory: dict[str, list[AclEntry]],
    user_groups: set[str],
) -> Grant | None:
    """El permiso efectivo de `user_id` sobre el ultimo directorio de `chain`.

    `chain` va **de la raiz hacia abajo**: `[raiz, a, b]` para `/a/b`. Se recorre al reves
    —del mas especifico al mas general— y **gana la primera regla que aplique**, que es la
    mas cercana a la ruta consultada.

    Devuelve `None` cuando no hay ninguna concesion: **ausencia es denegacion**.

    Parametros por nombre a proposito. Cinco argumentos del mismo tipo (`user_id`,
    `owner_id`, ids de directorio) invitan a un fallo silencioso si se pasan en el orden
    equivocado, y aqui ese fallo seria darle a alguien permisos que no tiene.
    """
    if user_id == owner_id:
        # El propietario manda sobre su arbol. No es un caso especial cosido aparte:
        # es la regla que aplica cuando el arbol es suyo, y va primero porque ninguna
        # concesion puede rebajarla (no hay denegaciones).
        return Grant(
            permission=Permission.ADMIN,
            source="owner",
            directory_id=chain[-1] if chain else "",
        )

    for directory_id in reversed(chain):
        entradas = entries_by_directory.get(directory_id, ())
        if not entradas:
            continue

        mejor: Grant | None = None
        for entrada in entradas:
            aplica = (
                entrada.principal_type is PrincipalType.USER
                and entrada.principal_id == user_id
            ) or (
                entrada.principal_type is PrincipalType.GROUP
                and entrada.principal_id in user_groups
            )
            if not aplica:
                continue

            # El MAXIMO entre lo concedido al usuario y a sus grupos. Es la consecuencia
            # directa de no tener denegaciones: los permisos solo pueden sumar.
            if mejor is None or entrada.permission > mejor.permission:
                mejor = Grant(
                    permission=entrada.permission,
                    source=(
                        "user"
                        if entrada.principal_type is PrincipalType.USER
                        else "group"
                    ),
                    directory_id=directory_id,
                    principal_id=(
                        entrada.principal_id
                        if entrada.principal_type is PrincipalType.GROUP
                        else ""
                    ),
                )

        if mejor is not None:
            # La primera regla aplicable subiendo gana: no se sigue mirando hacia arriba.
            # Si se siguiera y se tomara el maximo de todo el camino, una concesion en la
            # raiz haria imposible dar menos permiso en un subdirectorio, y el modelo
            # perderia la unica forma que tiene de acotar el alcance.
            return mejor

    return None


def effective(grant: Grant | None) -> Permission | None:
    return grant.permission if grant is not None else None
