"""Cifrado extremo a extremo: AES-256-GCM, con la clave siempre en el cliente.

**El DataNode almacena texto cifrado y nunca ve la clave.** Es mas fuerte que cifrar en
el servidor: alli quien controla el servidor puede leerlo todo, y aqui no puede leer
nada, ni siquiera con acceso al disco y al metadato completos.

## El esquema de claves, en tres niveles

    contrasena del usuario
        |  PBKDF2-HMAC-SHA256, 600 000 iteraciones, sal por usuario
        v
    clave maestra del usuario  (32 bytes, NUNCA sale del cliente)
        |  AES-256-GCM (envoltura)
        v
    clave de archivo           (32 bytes, aleatoria y distinta por archivo)
        |  AES-256-GCM
        v
    cada bloque, con su propio nonce

Lo que viaja al ControlNode es **la clave de archivo envuelta**, que sin la clave maestra
no es mas que 60 bytes de ruido. El servidor guarda esa envoltura y la sal del usuario;
con las dos cosas y todos los bloques del disco, sigue sin poder descifrar nada.

**Una clave por archivo y no una sola por usuario**: asi compartir un archivo puede
llegar a ser entregar su clave, sin dar acceso a todo lo demas. Y si una clave de archivo
se compromete, se lleva un archivo, no la cuenta.

## El nonce: derivado, nunca aleatorio

Esto es lo mas facil de hacer mal de todo el modulo. En GCM, **repetir un nonce con la
misma clave rompe el cifrado por completo**: revela el XOR de los dos textos claros y
permite falsificar mensajes. No es una degradacion gradual, es un fallo total.

Un nonce aleatorio de 96 bits repite con probabilidad no despreciable a partir de unos
2^32 bloques por clave, y —lo que es peor— **una repeticion no se puede detectar**: no hay
nada en el sistema que avise. Aqui se deriva de `(file_id, index)`:

    nonce = HMAC-SHA256(clave_de_archivo, file_id || index)[:12]

El `(file_id, index)` es **unico por construccion**: lo garantiza el indice
`UNIQUE(file_id, index)` de la tabla `blocks`, que existe desde la Etapa 1 por motivos que
no tenian nada que ver con esto. Y como la clave de archivo es nueva en cada archivo, dos
archivos distintos no comparten ni clave ni espacio de nonces.

La consecuencia practica que lo hace seguro: **un `block_id` no se reescribe nunca** (WORM,
decision 1), asi que el par `(file_id, index)` no se reutiliza jamas con la misma clave.
Si alguien anadiera alguna vez la posibilidad de reescribir un bloque, esta derivacion
dejaria de ser segura y habria que cambiarla — por eso queda dicho aqui y no solo en
CLAUDE.md.

## El checksum va sobre el TEXTO CIFRADO

El DataNode verifica la integridad de lo que recibe sin descifrar nada, y el pipeline
sigue pudiendo comprobar antes de reenviar. Si el checksum fuera del texto claro, el
DataNode no podria comprobarlo y habria que confiar en el cliente.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "KDF_ITERATIONS",
    "KEY_BYTES",
    "NONCE_BYTES",
    "SALT_BYTES",
    "DecryptionError",
    "new_salt",
    "derive_master_key",
    "new_file_key",
    "wrap_file_key",
    "unwrap_file_key",
    "TAG_BYTES",
    "block_nonce",
    "encrypt_block",
    "decrypt_block",
]

#: 600 000, el orden de magnitud que OWASP recomienda hoy para PBKDF2-HMAC-SHA256.
#:
#: **Argon2id seria preferible** por su resistencia a hardware especializado (GPU y ASIC):
#: PBKDF2 es barato de paralelizar y ahi esta su debilidad. Se eligio PBKDF2 por estar en
#: la biblioteca estandar y no anadir una dependencia mas. Es un limite reconocido y
#: escrito, no una omision, y cambiarlo seria cambiar esta funcion y nada mas.
KDF_ITERATIONS = 600_000
KEY_BYTES = 32   # AES-256
NONCE_BYTES = 12  # 96 bits, el tamano nativo de GCM
SALT_BYTES = 16
#: Lo que GCM anade a cada bloque: su etiqueta de autenticacion de 128 bits.
#:
#: No es un detalle interno. Es la diferencia entre el tamano CLARO de un bloque y el que
#: ocupa en disco, y por tanto entre `files.size` y la suma de `blocks.size`. Quien
#: convierta entre esos dos mundos usa esta constante y nunca un 16 escrito a mano.
TAG_BYTES = 16


class DecryptionError(Exception):
    """El descifrado fallo: clave equivocada, o los datos fueron alterados.

    GCM **autentica** ademas de cifrar, asi que esto cubre los dos casos y no se pueden
    distinguir — ni conviene: decirle a quien prueba claves que «la clave es correcta pero
    los datos estan mal» le confirmaria la clave.
    """


# --- Claves -----------------------------------------------------------------


def new_salt() -> bytes:
    """Sal nueva para un usuario. Una por usuario, guardada junto a su fila.

    La sal no es secreta: su trabajo es que dos usuarios con la misma contrasena tengan
    claves distintas, y que no se puedan precalcular tablas contra todo el sistema a la
    vez.
    """
    return secrets.token_bytes(SALT_BYTES)


def derive_master_key(password: str, salt: bytes) -> bytes:
    """La clave maestra del usuario. **Nunca sale del cliente.**

    Es cara a proposito: 600 000 iteraciones son unas decimas de segundo en el cliente y
    el precio que paga quien intente adivinar contrasenas por fuerza bruta.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, KDF_ITERATIONS, dklen=KEY_BYTES
    )


def new_file_key() -> bytes:
    """Una clave nueva, aleatoria, para un archivo."""
    return secrets.token_bytes(KEY_BYTES)


def wrap_file_key(file_key: bytes, master_key: bytes, file_id: str) -> bytes:
    """Envuelve la clave del archivo con la maestra. Es lo unico que viaja al servidor.

    `file_id` entra como **datos autenticados adicionales**: la envoltura queda atada a su
    archivo. Sin eso, alguien con acceso al metadato podria mover la envoltura de un
    archivo a otro y hacer que el cliente descifrara el bloque equivocado con una clave
    que si valida — un ataque que no rompe la criptografia pero si la integridad de lo que
    el usuario cree estar leyendo.
    """
    nonce = os.urandom(NONCE_BYTES)
    envuelta = AESGCM(master_key).encrypt(
        nonce, file_key, file_id.encode("utf-8")
    )
    return nonce + envuelta


def unwrap_file_key(wrapped: bytes, master_key: bytes, file_id: str) -> bytes:
    """Desenvuelve la clave del archivo. Lanza si la contrasena no es la correcta."""
    if len(wrapped) <= NONCE_BYTES:
        raise DecryptionError("la clave envuelta esta truncada")
    nonce, cuerpo = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
    try:
        return AESGCM(master_key).decrypt(nonce, cuerpo, file_id.encode("utf-8"))
    except InvalidTag as exc:
        raise DecryptionError(
            "no se pudo desenvolver la clave del archivo: contrasena incorrecta o "
            "metadato alterado"
        ) from exc


# --- Bloques ----------------------------------------------------------------


def block_nonce(file_key: bytes, file_id: str, index: int) -> bytes:
    """Nonce DETERMINISTA de un bloque. Ver la nota larga de la cabecera del modulo.

    No es aleatorio, y eso es lo correcto aqui: `(file_id, index)` es unico por
    construccion —lo garantiza `UNIQUE(file_id, index)` en `blocks`— y los bloques no se
    reescriben nunca (WORM), asi que el par no se reutiliza jamas con la misma clave. Un
    nonce aleatorio, en cambio, puede repetirse **sin que nada lo detecte**, y en GCM una
    repeticion no degrada la seguridad: la elimina.

    Se pasa por HMAC en vez de concatenar los valores en claro para que el nonce no
    revele la posicion del bloque a quien mire el disco, y para que dos archivos con el
    mismo indice no compartan nonce ni siquiera visualmente.
    """
    material = f"{file_id}:{index}".encode("utf-8")
    return hmac.new(file_key, material, hashlib.sha256).digest()[:NONCE_BYTES]


def encrypt_block(plaintext: bytes, file_key: bytes, file_id: str, index: int) -> bytes:
    """Cifra un bloque. El resultado es lo que se sube y lo que se guarda en disco.

    El nonce **no se guarda**: se vuelve a derivar al descifrar, de `(file_id, index)`,
    que el cliente ya conoce porque viene en el plan. Guardarlo seria 12 bytes por bloque
    de informacion redundante y un sitio mas donde algo puede no cuadrar.
    """
    nonce = block_nonce(file_key, file_id, index)
    return AESGCM(file_key).encrypt(nonce, plaintext, None)


def decrypt_block(
    ciphertext: bytes, file_key: bytes, file_id: str, index: int
) -> bytes:
    """Descifra y **verifica**. GCM autentica: un byte cambiado hace fallar esto.

    Es una segunda red bajo el SHA-256 del bloque, y no es redundante: el checksum
    protege contra corrupcion accidental y lo verifica el DataNode, que no tiene la
    clave; esto protege contra alteracion deliberada y solo puede verificarlo quien la
    tiene.
    """
    nonce = block_nonce(file_key, file_id, index)
    try:
        return AESGCM(file_key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError(
            "el bloque no se pudo descifrar: clave incorrecta o contenido alterado"
        ) from exc


@dataclass(frozen=True, slots=True)
class FileCrypto:
    """La clave de un archivo ya desenvuelta, lista para cifrar o descifrar sus bloques.

    Existe para que quien sube o baja no tenga que ir pasando `(file_key, file_id)` por
    todas partes, que es como se acaba cifrando un bloque con el `file_id` de otro.
    """

    file_id: str
    file_key: bytes

    @property
    def overhead(self) -> int:
        """Bytes que este cifrado anade a CADA bloque.

        El cliente lo declara al crear el archivo para que el ControlNode planifique los
        bloques con el tamano que de verdad ocuparan en disco.
        """
        return TAG_BYTES

    def encrypt(self, plaintext: bytes, index: int) -> bytes:
        return encrypt_block(plaintext, self.file_key, self.file_id, index)

    def decrypt(self, ciphertext: bytes, index: int) -> bytes:
        return decrypt_block(ciphertext, self.file_key, self.file_id, index)
