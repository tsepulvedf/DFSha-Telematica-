"""Comprueba que las pruebas que fijan el patron CAEN al romper lo que dicen proteger.

    python scripts/verificar_pruebas.py

## Por que existe

**Una prueba que nunca has visto fallar no sabes si prueba algo.** Las protecciones del
patron de la Etapa 3 —la epoca del liderazgo, el minimo de las ACLs, el token de bloque, el
lock del RF3— tienen todas la misma propiedad incomoda: **quitarlas no rompe nada
visible**. El sistema sigue funcionando, la suite sigue en verde salvo unas pocas pruebas,
y el agujero solo se manifiesta con una pausa larga en el momento justo.

Asi que la unica forma de saber que esas pruebas sirven es **romper lo que dicen proteger y
comprobar que caen**. Esto hace exactamente eso: aplica una mutacion, corre las pruebas
correspondientes, y restaura el fichero pase lo que pase.

## Dos pruebas pasaban por el motivo equivocado, y asi se supieron

No es un ejercicio teorico: la primera pasada encontro dos.

1. `test_el_lider_congelado_es_rechazado_y_no_escribe_nada` seguia en verde al quitar la
   comparacion de epoca, porque A y B son instancias distintas y le bastaba el
   `leader_id`. La epoca la fija de verdad la prueba hermana, donde el lider congelado es
   el MISMO que recupero el lease.
2. `test_el_token_de_Beto_no_abre_el_bloque_de_Ana` seguia en verde al quitar la
   comprobacion del `block_id`, porque usaba un token de ESCRITURA donde el endpoint pide
   uno de lectura: rechazaba por la operacion y no por el bloque.

Las dos daban confianza sin respaldarla, que es peor que no existir.

## Como anadir una mutacion

Una tupla en `MUTACIONES` con el fragmento exacto a sustituir y las pruebas que deben
caer. Si el fragmento ya no esta —porque el codigo cambio— se avisa en vez de pasar en
silencio, que es lo unico que impide que esto envejezca sin que nadie se entere.

**No modifica nada de forma permanente**: cada fichero se restaura en un `finally`.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Relativas al propio script: con la ruta de una maquina concreta escrita a mano, esta
# herramienta solo corria en esa maquina.
RAIZ = Path(__file__).resolve().parents[1]
PY = sys.executable

# (nombre, fichero, texto viejo, texto nuevo, pruebas que DEBEN caer)
MUTACIONES = [
    (
        "1. Liderazgo: se deja de comparar la epoca",
        "src/dfsha/control_node/commands/leadership.py",
        """    desajuste = (
        lease.leader_id != fencing.leader_id
        or lease.epoch != fencing.epoch
        or lease.is_expired(ahora)
    )""",
        """    desajuste = lease.leader_id != fencing.leader_id""",
        ["tests/unit/test_leadership.py"],
    ),
    (
        "2. Liderazgo: recuperar el lease propio NO sube la epoca",
        "src/dfsha/control_node/domain/leadership.py",
        """    if lease.held_by(instance_id, now):
        return RENEW
    if lease.is_expired(now):
        return ACQUIRE
    return STAND_BY""",
        """    if lease.leader_id == instance_id:
        return RENEW
    if lease.is_expired(now):
        return ACQUIRE
    return STAND_BY""",
        ["tests/unit/test_leadership.py"],
    ),
    (
        "3. ACLs: el minimo exigido se ignora",
        "src/dfsha/control_node/services/permissions.py",
        """    acceso = effective_permission(uow, user_id, directory)
    if acceso.allows(minimum):
        return acceso""",
        """    acceso = effective_permission(uow, user_id, directory)
    if acceso.permission is not None or True:
        return acceso""",
        ["tests/integration/test_acl.py"],
    ),
    (
        "4. Token de bloque: basta con encadenar a la CA (sin exigir el rol)",
        "src/dfsha/common/blocktoken.py",
        """    comunes = firmante.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    if not comunes or comunes[0].value != SIGNER_COMMON_NAME:
        raise BlockTokenError("el firmante no es el ControlNode")""",
        """    pass  # mutacion: se acepta cualquier certificado de la CA""",
        ["tests/unit/test_blocktoken.py"],
    ),
    (
        "5. Token de bloque: no se comprueba de QUE bloque es",
        "src/dfsha/common/blocktoken.py",
        """    if grant.block_id != block_id:
        raise BlockTokenError("el token es para otro bloque")""",
        """    pass  # mutacion: cualquier token vale para cualquier bloque""",
        ["tests/unit/test_blocktoken.py", "tests/integration/test_acceso_directo.py"],
    ),
    (
        "5b. Token: no se comprueba la OPERACION (leer autoriza a borrar)",
        "src/dfsha/common/blocktoken.py",
        """    if grant.operation != operation:
        raise BlockTokenError("el token no autoriza esta operacion")""",
        """    pass  # mutacion""",
        ["tests/unit/test_blocktoken.py", "tests/integration/test_acceso_directo.py"],
    ),
    (
        "5c. Token: no se comprueba la EXPIRACION",
        "src/dfsha/common/blocktoken.py",
        """    if grant.expires_at <= now:
        raise BlockTokenError("el token ha caducado")""",
        """    pass  # mutacion""",
        ["tests/unit/test_blocktoken.py"],
    ),
    (
        "6. RF3: el lock se busca por epoca y no por titular",
        "src/dfsha/control_node/commands/filelock.py",
        """    mio = next((lock for lock in vivos if lock.holder_id == fencing.holder_id), None)""",
        """    mio = next((lock for lock in vivos if lock.epoch == fencing.epoch), None)""",
        ["tests/integration/test_rf3.py"],
    ),
    (
        "6b. RF3: un lock VENCIDO sigue valiendo",
        "src/dfsha/control_node/commands/filelock.py",
        """    vivos = [
        lock for lock in uow.file_locks.lock_rows(file_id) if not lock.is_expired(ahora)
    ]""",
        """    vivos = list(uow.file_locks.lock_rows(file_id))""",
        ["tests/integration/test_rf3.py"],
    ),
    (
        "7. RF3: un lock COMPARTIDO autoriza a escribir",
        "src/dfsha/control_node/commands/filelock.py",
        """    if mio.mode is not LockMode.EXCLUSIVE:""",
        """    if False:""",
        ["tests/integration/test_rf3.py"],
    ),
    (
        "11. Cifrado: el put del CLI vuelve a subir en claro si la sesion no tiene clave",
        "src/dfsha/client/cli.py",
        # Se sustituye la negativa ENTERA por la linea que habia antes. Con solo quitar el
        # `if`, `wrap_file_key` reventaria sin clave y el put fallaria igual: la prueba del
        # usuario sin sal seguiria en verde por un motivo distinto del que anuncia.
        """    if maestra is None:
        # **Nunca se sube en claro.** Esta rama subia el archivo SIN cifrar y sin decir
        # nada, y era la que tomaba cualquier usuario anterior a la migracion 0006: su sal
        # estaba vacia, el login no derivaba clave, y el criterio 11 dejaba de cumplirse en
        # silencio. «Archivo sin cifrar» sigue siendo un estado LEGIBLE —los de las Etapas
        # 1 y 2 se bajan igual—, pero ya no es algo que este cliente produzca.
        console.print(
            "[red]error[/red] esta sesion no tiene clave de cifrado, y el cliente no sube "
            "archivos en claro"
        )
        console.print(
            "[dim]vuelve a iniciar sesion con `dfsha login`. Si el error se repite, tu "
            "usuario no tiene sal de cifrado: el ControlNode necesita la migracion 0008 "
            "(`docker compose up` la aplica con el servicio migrate)[/dim]"
        )
        raise typer.Exit(code=1)
    clave_archivo = new_file_key()
""",
        """    clave_archivo = new_file_key() if maestra else None
""",
        ["tests/integration/test_cifrado.py"],
    ),
    (
        "12. C2: el pipeline vuelve a reenviar al vecino sin la CA del proyecto",
        "src/dfsha/data_node/pipeline.py",
        """            verify=verify,""",
        """            verify=True,""",
        ["tests/unit/test_pipeline.py", "tests/integration/test_tls_cliente.py"],
    ),
    (
        "13. C2: la re-replicacion vuelve a descargar del origen sin la CA del proyecto",
        "src/dfsha/data_node/orders.py",
        """headers=cabeceras, verify=self._verify""",
        """headers=cabeceras, verify=True""",
        ["tests/integration/test_tls_cliente.py"],
    ),
    (
        "14. C2: la URL guardada en la sesion vuelve a ganar a DFSHA_CONTROL_URL",
        "src/dfsha/client/session.py",
        """                os.environ.get("DFSHA_CONTROL_URL", "").strip()
                or datos.get("control_url", control_url)""",
        """                datos.get("control_url", control_url)""",
        ["tests/unit/test_c2_despliegue.py"],
    ),
]


def main() -> int:
    # `--solo 11` corre solo las mutaciones cuyo nombre empieza por ese texto.
    solo = sys.argv[sys.argv.index("--solo") + 1] if "--solo" in sys.argv else None
    respaldo = Path(tempfile.mkdtemp(prefix="mutaciones-"))
    resultados = []

    for nombre, rel, viejo, nuevo, pruebas in MUTACIONES:
        if solo and not nombre.startswith(solo):
            continue
        destino = RAIZ / rel
        copia = respaldo / rel.replace("/", "__")
        shutil.copy2(destino, copia)

        texto = destino.read_text(encoding="utf-8")
        if viejo not in texto:
            resultados.append((nombre, "NO SE PUDO MUTAR", "el codigo cambio"))
            print(f"[!] {nombre}: no se encontro el fragmento a mutar")
            continue

        destino.write_text(texto.replace(viejo, nuevo, 1), encoding="utf-8")
        print(f"\n=== {nombre} ===")
        try:
            proceso = subprocess.run(
                [PY, "-m", "pytest", *pruebas, "-q", "--no-header", "-p", "no:cacheprovider"],
                cwd=RAIZ,
                capture_output=True,
                text=True,
                timeout=900,
            )
            salida = proceso.stdout
            caidas = [
                l.split("::")[-1].split(" ")[0]
                for l in salida.splitlines()
                if l.startswith("FAILED")
            ]
            if caidas:
                resultados.append((nombre, "CAE", f"{len(caidas)}: " + ", ".join(caidas[:4])))
                print(f"    CAE ({len(caidas)} pruebas)")
                for c in caidas[:6]:
                    print(f"      - {c}")
            else:
                resultados.append((nombre, "*** NO CAE ***", salida.strip().splitlines()[-1]))
                print("    *** NO CAE: la prueba no cubre lo que dice cubrir ***")
        finally:
            shutil.copy2(copia, destino)

    print("\n" + "=" * 78)
    print("  RESUMEN")
    print("=" * 78)
    for nombre, estado, detalle in resultados:
        print(f"  {estado:18} {nombre}")
        if estado != "CAE":
            print(f"                     {detalle}")

    shutil.rmtree(respaldo, ignore_errors=True)
    return 0 if all(r[1] == "CAE" for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
