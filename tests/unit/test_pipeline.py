"""Pipeline de escritura en cadena.

Lo que se prueba aqui es sobre todo **que un fallo aguas abajo no tumba la subida**. Es
la decision que hace que W=2 signifique algo: si un fallo en la tercera replica devolviera
error al cliente, W=3 seria el minimo de hecho y el quorum sobraria.
"""

from __future__ import annotations

import httpx
import pytest

from dfsha.data_node.pipeline import (
    ACKED_HEADER,
    CHECKSUM_HEADER,
    MAX_HOPS,
    PIPELINE_HEADER,
    forward,
    parse_pipeline,
)

BLOQUE = b"contenido del bloque"
CHECKSUM = "a" * 64


# --- Lectura de la cabecera ------------------------------------------------


def test_cadena_vacia_cuando_no_hay_cabecera() -> None:
    assert parse_pipeline(None) == []
    assert parse_pipeline("") == []


def test_se_respeta_el_orden_del_plan() -> None:
    """El orden lo decidio la politica de colocacion, que es quien conoce la carga y los
    dominios de falla. El DataNode no reordena."""
    cabecera = "http://dn2:8001,http://dn3:8001"
    assert parse_pipeline(cabecera) == ["http://dn2:8001", "http://dn3:8001"]


def test_se_limpian_espacios_y_barras_finales() -> None:
    assert parse_pipeline(" http://dn2:8001/ , http://dn3:8001 ") == [
        "http://dn2:8001",
        "http://dn3:8001",
    ]


def test_una_cadena_absurda_se_recorta() -> None:
    """El DataNode todavia no autentica al cliente: el token de bloque llega en el
    Bloque C. Hasta entonces, nadie puede convertir un PUT en una tormenta mandando mil
    saltos."""
    cabecera = ",".join(f"http://dn{i}:8001" for i in range(50))
    assert len(parse_pipeline(cabecera)) == MAX_HOPS


# --- Reenvio ---------------------------------------------------------------


def _transporte(manejador):
    """Sustituye la red por una funcion. Devuelve tambien lo que se envio."""
    enviados: list[httpx.Request] = []

    def capturar(request: httpx.Request) -> httpx.Response:
        enviados.append(request)
        return manejador(request)

    return httpx.MockTransport(capturar), enviados


@pytest.fixture()
def red(monkeypatch):
    """Parchea httpx.put para que `forward` hable con un transporte simulado."""

    def instalar(manejador):
        transporte, enviados = _transporte(manejador)

        def put_falso(url, *, content, headers, timeout):
            with httpx.Client(transport=transporte) as cliente:
                return cliente.put(url, content=content, headers=headers)

        monkeypatch.setattr("dfsha.data_node.pipeline.httpx.put", put_falso)
        return enviados

    return instalar


def test_sin_cadena_no_se_reenvia_nada(red) -> None:
    enviados = red(lambda req: httpx.Response(201))

    resultado = forward("b1", BLOQUE, CHECKSUM, [])

    assert resultado.downstream_acked == 0
    assert resultado.hops == 0
    assert enviados == []


def test_se_reenvia_al_primero_con_el_resto_de_la_cadena_detras(red) -> None:
    """El nucleo del encadenado: cada nodo se quita de la lista y pasa el resto."""
    enviados = red(lambda req: httpx.Response(201, headers={ACKED_HEADER: "2"}))

    resultado = forward("b1", BLOQUE, CHECKSUM, ["http://dn2:8001", "http://dn3:8001"])

    assert len(enviados) == 1
    peticion = enviados[0]
    assert str(peticion.url) == "http://dn2:8001/api/v1/blocks/b1"
    assert peticion.headers[PIPELINE_HEADER] == "http://dn3:8001"
    assert peticion.headers[CHECKSUM_HEADER] == CHECKSUM
    assert peticion.content == BLOQUE
    assert resultado.downstream_acked == 2


def test_el_ultimo_salto_no_lleva_cabecera_de_pipeline(red) -> None:
    enviados = red(lambda req: httpx.Response(201, headers={ACKED_HEADER: "1"}))

    forward("b1", BLOQUE, CHECKSUM, ["http://dn3:8001"])

    assert PIPELINE_HEADER not in enviados[0].headers


def test_un_fallo_de_red_aguas_abajo_no_lanza(red) -> None:
    """LA prueba de este fichero. El nodo de arriba ya escribio bien; convertir esto en
    un error dejaria W=3 como minimo de hecho."""

    def cae(req):
        raise httpx.ConnectError("no hay nadie ahi", request=req)

    red(cae)
    resultado = forward("b1", BLOQUE, CHECKSUM, ["http://dn2:8001", "http://dn3:8001"])

    assert not resultado.ok
    assert resultado.downstream_acked == 0
    assert resultado.hops == 2


def test_un_rechazo_http_aguas_abajo_tampoco_lanza(red) -> None:
    red(lambda req: httpx.Response(500, text="boom"))

    resultado = forward("b1", BLOQUE, CHECKSUM, ["http://dn2:8001"])

    assert not resultado.ok
    assert resultado.downstream_acked == 0
    assert resultado.error == "http_500"


def test_un_409_aguas_abajo_cuenta_como_replica(red) -> None:
    """Los bloques son inmutables: si el siguiente ya lo tiene, el que tiene es ESTE
    mismo. Pasa en un reintento del cliente tras un fallo parcial, y tratarlo como error
    haria que el reintento nunca alcanzara el quorum."""
    red(lambda req: httpx.Response(409, text="ya existe"))

    resultado = forward("b1", BLOQUE, CHECKSUM, ["http://dn2:8001"])

    assert resultado.ok
    assert resultado.downstream_acked == 1


@pytest.mark.parametrize("cabecera", [None, "", "no-es-un-numero", "0", "-3"])
def test_un_acked_ausente_o_raro_se_cuenta_como_uno(red, cabecera) -> None:
    """1 es lo unico que se sabe con certeza cuando el siguiente responde 201. Contar de
    mas haria creer al cliente que alcanzo un quorum que no existe."""
    cabeceras = {} if cabecera is None else {ACKED_HEADER: cabecera}
    red(lambda req: httpx.Response(201, headers=cabeceras))

    assert forward("b1", BLOQUE, CHECKSUM, ["http://dn2:8001"]).downstream_acked == 1
