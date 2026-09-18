"""Resolucion de permisos: los cinco casos del enunciado y los que faltaban.

Funciones puras, sin base de datos. `resolve` es el **unico** sitio donde se decide si
alguien puede hacer algo, asi que estas pruebas son el contrato entero del modelo de
permisos: si algo no esta aqui, no esta comprobado en ninguna parte.
"""

from __future__ import annotations

import pytest

from dfsha.control_node.domain.acl import (
    AclEntry,
    Permission,
    PrincipalType,
    effective,
    resolve,
)

ANA = "ana"
BETO = "beto"
CARLA = "carla"
EQUIPO = "grupo-equipo"

# /compartido/informes, con la raiz de Ana arriba.
RAIZ, COMPARTIDO, INFORMES = "dir-raiz", "dir-compartido", "dir-informes"
CADENA = [RAIZ, COMPARTIDO, INFORMES]


def concesion(
    directory_id: str,
    principal_id: str,
    permiso: Permission,
    tipo: PrincipalType = PrincipalType.USER,
) -> AclEntry:
    return AclEntry(
        directory_id=directory_id,
        principal_type=tipo,
        principal_id=principal_id,
        permission=permiso,
    )


def permiso_de(
    usuario: str,
    entradas: dict[str, list[AclEntry]] | None = None,
    grupos: set[str] | None = None,
    cadena: list[str] | None = None,
) -> Permission | None:
    return effective(
        resolve(
            user_id=usuario,
            owner_id=ANA,
            chain=cadena if cadena is not None else CADENA,
            entries_by_directory=entradas or {},
            user_groups=grupos or set(),
        )
    )


# --- El orden de los permisos ----------------------------------------------


def test_los_permisos_estan_ordenados_por_potencia() -> None:
    """Que sean comparables es lo que permite preguntar "al menos WRITE" sin una tabla
    de equivalencias que se pueda desincronizar de la jerarquia."""
    assert Permission.READ < Permission.WRITE < Permission.ADMIN
    assert Permission.ADMIN >= Permission.WRITE
    assert not (Permission.READ >= Permission.WRITE)


def test_un_permiso_desconocido_se_rechaza_nombrando_los_validos() -> None:
    with pytest.raises(ValueError, match="read, write, admin"):
        Permission.parse("ejecutar")


@pytest.mark.parametrize("texto", ["read", "READ", " Write ", "admin"])
def test_se_aceptan_mayusculas_y_espacios(texto: str) -> None:
    assert Permission.parse(texto) in Permission


# --- Los cinco casos del enunciado -----------------------------------------


def test_sin_concesion_no_hay_permiso() -> None:
    """**Ausencia es denegacion.** Es la regla que sostiene todo el modelo: sin ella
    harian falta reglas de «denegar», y con ellas hay que definir precedencias entre
    permitir y denegar a distintos niveles del arbol."""
    assert permiso_de(BETO) is None


def test_concesion_directa_al_usuario() -> None:
    entradas = {INFORMES: [concesion(INFORMES, BETO, Permission.WRITE)]}

    assert permiso_de(BETO, entradas) is Permission.WRITE


def test_concesion_por_grupo() -> None:
    entradas = {
        INFORMES: [concesion(INFORMES, EQUIPO, Permission.READ, PrincipalType.GROUP)]
    }

    assert permiso_de(BETO, entradas, grupos={EQUIPO}) is Permission.READ


def test_un_grupo_al_que_no_pertenece_no_da_nada() -> None:
    entradas = {
        INFORMES: [concesion(INFORMES, EQUIPO, Permission.ADMIN, PrincipalType.GROUP)]
    }

    assert permiso_de(BETO, entradas, grupos=set()) is None


def test_se_hereda_de_un_ancestro() -> None:
    """La concesion esta en /compartido y la pregunta es sobre /compartido/informes."""
    entradas = {COMPARTIDO: [concesion(COMPARTIDO, BETO, Permission.WRITE)]}

    assert permiso_de(BETO, entradas) is Permission.WRITE


def test_el_maximo_entre_lo_directo_y_lo_del_grupo() -> None:
    """Los permisos solo pueden sumar, que es la consecuencia de no tener denegaciones."""
    entradas = {
        INFORMES: [
            concesion(INFORMES, BETO, Permission.READ),
            concesion(INFORMES, EQUIPO, Permission.WRITE, PrincipalType.GROUP),
        ]
    }

    assert permiso_de(BETO, entradas, grupos={EQUIPO}) is Permission.WRITE


def test_el_maximo_tambien_cuando_el_grupo_da_menos() -> None:
    entradas = {
        INFORMES: [
            concesion(INFORMES, BETO, Permission.ADMIN),
            concesion(INFORMES, EQUIPO, Permission.READ, PrincipalType.GROUP),
        ]
    }

    assert permiso_de(BETO, entradas, grupos={EQUIPO}) is Permission.ADMIN


# --- El propietario --------------------------------------------------------


def test_el_propietario_es_admin_de_su_arbol() -> None:
    assert permiso_de(ANA) is Permission.ADMIN


def test_el_propietario_lo_es_aunque_no_haya_ninguna_concesion() -> None:
    """Es la regla que hace que las pruebas de las etapas anteriores —todas sobre un
    usuario en su propio arbol— sigan describiendo el mismo comportamiento."""
    assert permiso_de(ANA, entradas={}, cadena=[RAIZ]) is Permission.ADMIN


def test_una_concesion_no_puede_rebajar_al_propietario() -> None:
    """No hay denegaciones, asi que darle READ a Ana sobre su propio arbol no le quita
    nada: el maximo sigue siendo ADMIN."""
    entradas = {INFORMES: [concesion(INFORMES, ANA, Permission.READ)]}

    assert permiso_de(ANA, entradas) is Permission.ADMIN


# --- La regla mas fina: gana la mas cercana --------------------------------


def test_gana_la_regla_mas_cercana_aunque_conceda_MENOS() -> None:
    """La decision que da al modelo su unica forma de acotar el alcance.

    Ana comparte todo `/compartido` con WRITE, pero en `/compartido/informes` deja solo
    READ. Beto tiene READ ahi, no WRITE.

    Si en vez de parar en la primera regla se tomara el maximo de todo el camino, una
    concesion amplia arriba haria **imposible** dar menos permiso mas abajo, y la unica
    forma de acotar seria anadir denegaciones, que es justo lo que este modelo no tiene.
    """
    entradas = {
        COMPARTIDO: [concesion(COMPARTIDO, BETO, Permission.WRITE)],
        INFORMES: [concesion(INFORMES, BETO, Permission.READ)],
    }

    assert permiso_de(BETO, entradas) is Permission.READ


def test_la_regla_mas_cercana_tambien_puede_conceder_MAS() -> None:
    entradas = {
        COMPARTIDO: [concesion(COMPARTIDO, BETO, Permission.READ)],
        INFORMES: [concesion(INFORMES, BETO, Permission.ADMIN)],
    }

    assert permiso_de(BETO, entradas) is Permission.ADMIN


def test_una_regla_que_no_aplica_no_detiene_la_busqueda_hacia_arriba() -> None:
    """En `/compartido/informes` hay una concesion, pero para CARLA. Beto tiene que
    seguir heredando la de `/compartido`: lo que detiene la busqueda es una regla
    APLICABLE, no una regla cualquiera."""
    entradas = {
        COMPARTIDO: [concesion(COMPARTIDO, BETO, Permission.WRITE)],
        INFORMES: [concesion(INFORMES, CARLA, Permission.ADMIN)],
    }

    assert permiso_de(BETO, entradas) is Permission.WRITE


def test_la_concesion_en_un_hermano_no_alcanza() -> None:
    """Un directorio que no esta en la cadena no influye. Parece obvio, pero es lo que
    separa "heredar hacia arriba" de "tener permiso en algun sitio del arbol"."""
    entradas = {"dir-otro": [concesion("dir-otro", BETO, Permission.ADMIN)]}

    assert permiso_de(BETO, entradas) is None


# --- El origen del permiso, que es lo que lo hace auditable ----------------


def test_resolve_dice_de_donde_sale_el_permiso() -> None:
    """Un permiso que no se puede explicar no se puede auditar.

    «No entiendo por que Beto ve esto» es la pregunta mas frecuente sobre cualquier
    sistema de permisos, y `dfsha acl` la responde con estos campos.
    """
    entradas = {COMPARTIDO: [concesion(COMPARTIDO, EQUIPO, Permission.WRITE, PrincipalType.GROUP)]}

    grant = resolve(
        user_id=BETO,
        owner_id=ANA,
        chain=CADENA,
        entries_by_directory=entradas,
        user_groups={EQUIPO},
    )

    assert grant is not None
    assert grant.permission is Permission.WRITE
    assert grant.source == "group"
    assert grant.principal_id == EQUIPO
    # Heredado de /compartido, no de la ruta consultada.
    assert grant.inherited_from == COMPARTIDO


def test_el_origen_del_propietario_se_distingue_de_una_concesion() -> None:
    grant = resolve(
        user_id=ANA, owner_id=ANA, chain=CADENA, entries_by_directory={}, user_groups=set()
    )

    assert grant is not None and grant.source == "owner"
