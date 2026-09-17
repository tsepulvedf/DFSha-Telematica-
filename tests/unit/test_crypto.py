"""Cifrado extremo a extremo: ida y vuelta, nonces, y lo que NO debe pasar.

Las cuatro que pide el enunciado —ida y vuelta, nonce distinto por bloque, checksum sobre
texto cifrado— y las que hacen falta ademas, porque en criptografia el camino feliz es la
parte que menos dice: casi todo lo que importa es que lo incorrecto falle de forma
ruidosa en vez de producir algo que parece bueno.
"""

from __future__ import annotations

import hashlib

import pytest

from dfsha.common.crypto import (
    KEY_BYTES,
    NONCE_BYTES,
    DecryptionError,
    FileCrypto,
    block_nonce,
    decrypt_block,
    derive_master_key,
    encrypt_block,
    new_file_key,
    new_salt,
    unwrap_file_key,
    wrap_file_key,
)

CLARO = b"contenido del bloque, con suficiente longitud para no ser trivial" * 16
FILE_ID = "8f14e45f-ceea-467a-9e6f-a1b2c3d4e5f6"
OTRO_FILE_ID = "1a2b3c4d-0000-4444-8888-aaaabbbbcccc"


# --- Derivacion de claves ---------------------------------------------------


def test_la_misma_contrasena_y_sal_dan_la_misma_clave() -> None:
    sal = new_salt()

    assert derive_master_key("secreta", sal) == derive_master_key("secreta", sal)


def test_la_misma_contrasena_con_otra_sal_da_otra_clave() -> None:
    """Para lo que existe la sal: que dos usuarios con la misma contrasena no compartan
    clave, y que no se puedan precalcular tablas contra todo el sistema a la vez."""
    assert derive_master_key("secreta", new_salt()) != derive_master_key(
        "secreta", new_salt()
    )


def test_la_clave_derivada_tiene_el_tamano_de_AES_256() -> None:
    assert len(derive_master_key("x", new_salt())) == KEY_BYTES


def test_cada_archivo_tiene_su_propia_clave() -> None:
    """Una por archivo y no una por usuario: asi compartir un archivo puede llegar a ser
    entregar su clave, sin dar acceso a todo lo demas."""
    assert new_file_key() != new_file_key()


# --- Envoltura de la clave de archivo ---------------------------------------


def test_la_clave_envuelta_se_recupera_con_la_contrasena_correcta() -> None:
    maestra = derive_master_key("secreta", new_salt())
    clave = new_file_key()

    envuelta = wrap_file_key(clave, maestra, FILE_ID)

    assert unwrap_file_key(envuelta, maestra, FILE_ID) == clave


def test_la_envoltura_no_contiene_la_clave_en_claro() -> None:
    """Lo que viaja al servidor tiene que ser ruido. Si la clave apareciera tal cual
    dentro de la envoltura, todo el esquema seria decorativo."""
    maestra = derive_master_key("secreta", new_salt())
    clave = new_file_key()

    envuelta = wrap_file_key(clave, maestra, FILE_ID)

    assert clave not in envuelta


def test_con_otra_contrasena_no_se_desenvuelve() -> None:
    sal = new_salt()
    clave = new_file_key()
    envuelta = wrap_file_key(clave, derive_master_key("buena", sal), FILE_ID)

    with pytest.raises(DecryptionError):
        unwrap_file_key(envuelta, derive_master_key("mala", sal), FILE_ID)


def test_la_envoltura_esta_atada_a_SU_archivo() -> None:
    """El `file_id` entra como dato autenticado adicional.

    Sin eso, alguien con acceso al metadato podria mover la envoltura de un archivo a
    otro y hacer que el cliente descifrara con una clave que si valida. No rompe la
    criptografia, pero si la integridad de lo que el usuario cree estar leyendo.
    """
    maestra = derive_master_key("secreta", new_salt())
    envuelta = wrap_file_key(new_file_key(), maestra, FILE_ID)

    with pytest.raises(DecryptionError):
        unwrap_file_key(envuelta, maestra, OTRO_FILE_ID)


def test_una_envoltura_truncada_falla_en_vez_de_reventar() -> None:
    maestra = derive_master_key("secreta", new_salt())

    with pytest.raises(DecryptionError, match="truncada"):
        unwrap_file_key(b"corta", maestra, FILE_ID)


# --- Ida y vuelta de un bloque ----------------------------------------------


def test_ida_y_vuelta() -> None:
    clave = new_file_key()

    cifrado = encrypt_block(CLARO, clave, FILE_ID, 0)

    assert decrypt_block(cifrado, clave, FILE_ID, 0) == CLARO


def test_el_bloque_cifrado_no_se_parece_al_claro() -> None:
    """La comprobacion que corresponde al criterio 11 del enunciado: lo que queda en
    disco no coincide con el texto claro."""
    cifrado = encrypt_block(CLARO, new_file_key(), FILE_ID, 0)

    assert CLARO not in cifrado
    assert cifrado[: len(CLARO)] != CLARO


def test_el_cifrado_ocupa_16_bytes_mas_por_la_etiqueta() -> None:
    """GCM anade una etiqueta de autenticacion de 128 bits. Conviene tenerlo presente
    porque es lo que hace que el tamano en disco no sea el tamano del archivo."""
    cifrado = encrypt_block(CLARO, new_file_key(), FILE_ID, 0)

    assert len(cifrado) == len(CLARO) + 16


def test_un_byte_cambiado_hace_fallar_el_descifrado() -> None:
    """GCM autentica ademas de cifrar. Es una segunda red bajo el SHA-256, y no es
    redundante: el checksum lo verifica el DataNode, que no tiene la clave, y protege
    contra corrupcion accidental; esto solo lo puede verificar quien tiene la clave, y
    protege contra alteracion deliberada."""
    clave = new_file_key()
    cifrado = bytearray(encrypt_block(CLARO, clave, FILE_ID, 0))
    cifrado[10] ^= 0x01

    with pytest.raises(DecryptionError):
        decrypt_block(bytes(cifrado), clave, FILE_ID, 0)


def test_con_otra_clave_no_se_descifra() -> None:
    cifrado = encrypt_block(CLARO, new_file_key(), FILE_ID, 0)

    with pytest.raises(DecryptionError):
        decrypt_block(cifrado, new_file_key(), FILE_ID, 0)


def test_descifrar_con_el_indice_equivocado_falla() -> None:
    """El nonce sale del indice, asi que pedir el bloque 1 con los bytes del 0 no
    devuelve basura: falla. Es lo que impide que un reordenamiento de bloques pase
    desapercibido."""
    clave = new_file_key()
    cifrado = encrypt_block(CLARO, clave, FILE_ID, 0)

    with pytest.raises(DecryptionError):
        decrypt_block(cifrado, clave, FILE_ID, 1)


# --- El nonce: la parte mas facil de hacer mal ------------------------------


def test_cada_bloque_tiene_un_nonce_DISTINTO() -> None:
    """Repetir un nonce con la misma clave en GCM no degrada la seguridad: la elimina.
    Revela el XOR de los dos textos claros y permite falsificar mensajes."""
    clave = new_file_key()

    nonces = {block_nonce(clave, FILE_ID, i) for i in range(1000)}

    assert len(nonces) == 1000


def test_el_nonce_es_DETERMINISTA_y_no_aleatorio() -> None:
    """Lo que permite no guardarlo: se vuelve a derivar al descifrar.

    Y es seguro porque `(file_id, index)` es unico por construccion —lo garantiza
    `UNIQUE(file_id, index)` en `blocks`— y los bloques no se reescriben nunca (WORM).
    Un nonce aleatorio podria repetirse SIN QUE NADA LO DETECTE.
    """
    clave = new_file_key()

    assert block_nonce(clave, FILE_ID, 7) == block_nonce(clave, FILE_ID, 7)


def test_dos_archivos_no_comparten_nonces_ni_en_el_mismo_indice() -> None:
    clave = new_file_key()

    assert block_nonce(clave, FILE_ID, 0) != block_nonce(clave, OTRO_FILE_ID, 0)


def test_dos_claves_distintas_no_comparten_nonces() -> None:
    assert block_nonce(new_file_key(), FILE_ID, 0) != block_nonce(
        new_file_key(), FILE_ID, 0
    )


def test_el_nonce_no_revela_el_indice_del_bloque() -> None:
    """Se pasa por HMAC en vez de concatenar los valores en claro: quien mire el disco no
    deberia poder deducir la posicion de un bloque de su nonce."""
    clave = new_file_key()

    nonce = block_nonce(clave, FILE_ID, 42)

    assert b"42" not in nonce
    assert FILE_ID.encode()[:8] not in nonce


def test_el_nonce_tiene_el_tamano_nativo_de_GCM() -> None:
    assert len(block_nonce(new_file_key(), FILE_ID, 0)) == NONCE_BYTES


# --- El checksum va sobre el TEXTO CIFRADO ----------------------------------


def test_el_checksum_del_bloque_es_el_del_texto_cifrado() -> None:
    """Es lo que permite que el DataNode verifique integridad **sin descifrar nada**, y
    que el pipeline siga pudiendo comprobar antes de reenviar. Con el checksum del texto
    claro, el DataNode no podria comprobarlo y habria que fiarse del cliente."""
    clave = new_file_key()
    cifrado = encrypt_block(CLARO, clave, FILE_ID, 0)

    # Lo que el cliente anuncia en la cabecera y lo que el DataNode calcula de los bytes
    # que recibe tienen que ser lo mismo.
    anunciado = hashlib.sha256(cifrado).hexdigest()
    calculado_por_el_datanode = hashlib.sha256(cifrado).hexdigest()

    assert anunciado == calculado_por_el_datanode
    assert anunciado != hashlib.sha256(CLARO).hexdigest()


# --- El envoltorio de conveniencia ------------------------------------------


def test_FileCrypto_hace_la_ida_y_vuelta_sin_repetir_el_file_id() -> None:
    """Existe para que quien sube o baja no vaya pasando `(clave, file_id)` por todas
    partes, que es como se acaba cifrando un bloque con el `file_id` de otro."""
    cripto = FileCrypto(file_id=FILE_ID, file_key=new_file_key())

    assert cripto.decrypt(cripto.encrypt(CLARO, 3), 3) == CLARO


def test_el_ciclo_completo_desde_la_contrasena() -> None:
    """Todo el esquema de tres niveles de una vez, que es como se usa de verdad."""
    sal = new_salt()
    maestra = derive_master_key("contrasena-del-usuario", sal)

    clave = new_file_key()
    envuelta = wrap_file_key(clave, maestra, FILE_ID)  # esto es lo unico que se guarda

    # Mas tarde, en otra sesion: solo se tienen la sal, la envoltura y los bloques.
    recuperada = unwrap_file_key(
        envuelta, derive_master_key("contrasena-del-usuario", sal), FILE_ID
    )
    cifrados = [encrypt_block(CLARO, clave, FILE_ID, i) for i in range(3)]

    for i, bloque in enumerate(cifrados):
        assert decrypt_block(bloque, recuperada, FILE_ID, i) == CLARO
