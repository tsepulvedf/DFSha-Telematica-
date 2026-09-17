"""Token de bloque: lo que autoriza y, sobre todo, lo que NO.

El camino feliz aqui dice poco. Lo que hace util a este modulo es que cada forma de
saltarselo falle, y hay mas formas de las que parecen: firmar con otro certificado del
mismo emisor, reusar un token de otro bloque, ascender una lectura a un borrado.

La leccion del Bloque C aplicada: **una prueba que solo comprueba que lo malo se rechaza
no comprueba que lo bueno funciona**, asi que el primero de este fichero es el caso bueno.
Si ese falla, los demas no prueban nada: rechazarian todo, incluido lo correcto.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dfsha.common.blocktoken import (
    BlockGrant,
    BlockTokenError,
    TokenSigner,
    verify_token,
)

from tests.certs import material

BLOQUE = "3f51a20b-d707-4016-a88f-3af85429fd49"
OTRO_BLOQUE = "9c2f1e4a-0000-4444-8888-aaaabbbbcccc"


@pytest.fixture(scope="module")
def ca() -> bytes:
    return material("control").ca_cert.read_bytes()


@pytest.fixture(scope="module")
def control() -> TokenSigner:
    m = material("control")
    return TokenSigner.from_paths(str(m.key), str(m.cert))


@pytest.fixture(scope="module")
def ahora() -> dt.datetime:
    return dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)


def _verificar(token, ca, *, block_id=BLOQUE, operation="read", now):
    return verify_token(
        token, ca_cert_pem=ca, block_id=block_id, operation=operation, now=now
    )


# --- El caso bueno, primero -------------------------------------------------


def test_un_token_del_controlnode_autoriza_su_bloque(control, ca, ahora) -> None:
    token = control.issue(BLOQUE, "read", now=ahora, subject="ana")

    grant = _verificar(token, ca, now=ahora)

    assert grant == BlockGrant(
        block_id=BLOQUE,
        operation="read",
        expires_at=grant.expires_at,
        subject="ana",
    )


@pytest.mark.parametrize("operacion", ["read", "write", "delete"])
def test_las_tres_operaciones_se_emiten_y_se_verifican(
    control, ca, ahora, operacion
) -> None:
    token = control.issue(BLOQUE, operacion, now=ahora)

    assert _verificar(token, ca, operation=operacion, now=ahora).operation == operacion


# --- LA trampa: firmado por la CA no basta ---------------------------------


def test_un_datanode_no_puede_firmar_sus_propios_tokens(ca, ahora) -> None:
    """La comprobacion que sostiene todo el mecanismo.

    La MISMA CA firmo `dfsha-control`, `dfsha-data` y `dfsha-client`. Si la verificacion
    se quedara en «encadena a nuestra CA», un DataNode podria emitirse tokens para
    cualquier bloque con su propio certificado, y el de al lado se los aceptaria: seria un
    sistema de autorizacion que autoriza justo a quien debia limitar.

    Y no es hipotetico: el DataNode YA tiene ese certificado y su clave, porque los
    necesita para el mTLS del plano interno. No habria que robar nada.
    """
    m = material("data")
    impostor = TokenSigner.from_paths(str(m.key), str(m.cert))

    token = impostor.issue(BLOQUE, "read", now=ahora)

    with pytest.raises(BlockTokenError, match="no es el ControlNode"):
        _verificar(token, ca, now=ahora)


def test_el_cliente_tampoco_puede_firmar(ca, ahora) -> None:
    """El GC corre con el certificado de cliente. Que no pueda emitir tokens de borrado
    es lo que hace que el ControlNode siga siendo quien decide que es un huerfano."""
    m = material("client")
    impostor = TokenSigner.from_paths(str(m.key), str(m.cert))

    with pytest.raises(BlockTokenError, match="no es el ControlNode"):
        _verificar(impostor.issue(BLOQUE, "delete", now=ahora), ca, operation="delete", now=ahora)


# --- Atado a SU bloque y a SU operacion ------------------------------------


def test_el_token_de_un_bloque_no_abre_otro(control, ca, ahora) -> None:
    """Sin esto, tener acceso a UN bloque propio daria acceso a todos: basta cambiar el
    id de la URL y reusar el mismo token."""
    token = control.issue(BLOQUE, "read", now=ahora)

    with pytest.raises(BlockTokenError, match="otro bloque"):
        _verificar(token, ca, block_id=OTRO_BLOQUE, now=ahora)


def test_leer_no_autoriza_a_BORRAR(control, ca, ahora) -> None:
    """La escalada mas obvia y la mas grave: cualquiera con permiso de lectura sobre un
    archivo compartido podria destruirlo."""
    token = control.issue(BLOQUE, "read", now=ahora)

    with pytest.raises(BlockTokenError, match="no autoriza esta operacion"):
        _verificar(token, ca, operation="delete", now=ahora)


def test_leer_no_autoriza_a_ESCRIBIR(control, ca, ahora) -> None:
    token = control.issue(BLOQUE, "read", now=ahora)

    with pytest.raises(BlockTokenError, match="no autoriza esta operacion"):
        _verificar(token, ca, operation="write", now=ahora)


# --- El reloj ---------------------------------------------------------------


def test_un_token_caducado_no_vale(control, ca, ahora) -> None:
    """No hay revocacion, asi que la expiracion es lo unico que acota la ventana en la
    que un token filtrado sirve. Por eso es corta."""
    token = control.issue(BLOQUE, "read", now=ahora, ttl_seconds=60)

    with pytest.raises(BlockTokenError, match="caducado"):
        _verificar(token, ca, now=ahora + dt.timedelta(seconds=61))


def test_dentro_de_su_ventana_sigue_valiendo(control, ca, ahora) -> None:
    token = control.issue(BLOQUE, "read", now=ahora, ttl_seconds=60)

    assert _verificar(token, ca, now=ahora + dt.timedelta(seconds=59))


# --- Manipulacion -----------------------------------------------------------


def test_cambiar_el_payload_invalida_la_firma(control, ca, ahora) -> None:
    """Lo que hace que el resto de comprobaciones signifiquen algo: el contenido no se
    puede editar sin romper la firma, asi que nadie se asciende a `delete` a mano."""
    payload, firma, cert = control.issue(BLOQUE, "read", now=ahora).split(".")
    falso = control.issue(BLOQUE, "delete", now=ahora).split(".")[0]

    with pytest.raises(BlockTokenError, match="firma"):
        _verificar(f"{falso}.{firma}.{cert}", ca, operation="delete", now=ahora)


def test_cambiar_la_firma_no_cuela(control, ca, ahora) -> None:
    payload, firma, cert = control.issue(BLOQUE, "read", now=ahora).split(".")
    rota = firma[:-4] + ("AAAA" if not firma.endswith("AAAA") else "BBBB")

    with pytest.raises(BlockTokenError):
        _verificar(f"{payload}.{rota}.{cert}", ca, now=ahora)


@pytest.mark.parametrize(
    "basura",
    ["", "   ", "sin-puntos", "solo.dos", "a.b.c", "a.b.c.d", "....", "null"],
    ids=["vacio", "espacios", "sin-puntos", "dos-partes", "no-b64", "cuatro", "puntos", "null"],
)
def test_la_basura_se_rechaza_sin_reventar(ca, ahora, basura) -> None:
    """Un token es entrada de red: tiene que fallar con `BlockTokenError` y no con una
    excepcion suelta que se convierta en un 500. Un 500 aqui seria un canal lateral."""
    with pytest.raises(BlockTokenError):
        _verificar(basura, ca, now=ahora)


def test_sin_token_tampoco(ca, ahora) -> None:
    with pytest.raises(BlockTokenError, match="falta el token"):
        _verificar(None, ca, now=ahora)


# --- Lo que separa esto de un secreto compartido ---------------------------


def test_el_token_NO_lleva_la_clave_que_lo_firma(control, ca, ahora) -> None:
    """La diferencia con un HMAC, comprobada por su efecto.

    Con clave compartida, quien verifica tambien puede emitir: comprometer un DataNode
    daria la capacidad de firmar. Aqui el token lleva el certificado —la parte PUBLICA—
    y la privada no sale del ControlNode.
    """
    token = control.issue(BLOQUE, "read", now=ahora)
    privada = material("control").key.read_bytes()

    # Ni el PEM entero ni su cuerpo en base64 aparecen en el token.
    cuerpo = b"".join(
        l for l in privada.splitlines() if not l.startswith(b"-----")
    ).decode()
    assert cuerpo not in token
    assert cuerpo[:64] not in token
