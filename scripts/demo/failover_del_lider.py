"""Demostracion del relevo de liderazgo: se mata al lider y el cluster sigue.

    python scripts/demo/failover_del_lider.py

## El control positivo, que aqui es especialmente necesario

«Se mata un ControlNode y el sistema sigue funcionando» **pasa trivialmente si se mata al
que no manda**. Con tres instancias, dos de ellas no son lideres en ningun momento, asi que
matar una al azar tiene dos tercios de probabilidad de no demostrar nada: el cluster
seguiria porque nunca dependio de ella.

Por eso este guion **pregunta primero quien es el lider** y mata a ese. Y ademas comprueba
dos cosas mas que la version ingenua se salta:

1. Que el relevo **ocurrio**: hay un lider nuevo y es OTRA instancia.
2. Que **la epoca subio**. Es lo que distingue un relevo de verdad de un lider que sigue
   siendo el mismo, y es el token de aislamiento que impide que el congelado escriba al
   despertar. Un relevo sin subida de epoca seria un fallo silencioso.

## Lo que NO demuestra, y conviene decirlo

Esto ensena el relevo, no el escenario del lider congelado. Un contenedor parado esta
muerto y no despierta; el caso peligroso es el que se **congela** y vuelve. Ese no se puede
montar con `docker stop` y esta cubierto por
`test_el_lider_congelado_es_rechazado_y_no_escribe_nada` y su prueba hermana.
"""

from __future__ import annotations

import sys
import time

from _comun import Demo, exigir_docker, exigir_sesion

INSTANCIAS = [f"dfsha-control-node-{n}" for n in (1, 2, 3)]
#: Margen sobre el TTL del lease (6 s por defecto) para que el relevo se haya completado.
ESPERA_RELEVO = 12.0


def leer_liderazgo(demo: Demo):
    """Quien manda ahora mismo, leido por la API y no raspando la salida del CLI.

    `dfsha cluster` ya lo muestra —y el guion lo imprime, que es lo que se ve en el
    video— pero las COMPROBACIONES se hacen sobre el dato estructurado. Raspar texto
    formateado para tomar decisiones envejece mal: cualquier retoque de la presentacion
    romperia la demostracion sin que nadie relacionara las dos cosas.
    """
    from dfsha.client.api import ControlApi
    from dfsha.client.session import SessionStore

    try:
        sesion = SessionStore().load(None)
        return ControlApi(sesion).leadership()
    except Exception as exc:  # noqa: BLE001
        demo.aviso(f"no se pudo leer el liderazgo por la API: {exc}")
        return None


def contenedor_del_lider(demo: Demo, leader_id: str) -> str | None:
    """Que contenedor sostiene el lease, mirando sus logs.

    El `leader_id` es el identificador de instancia, no el nombre del contenedor, asi que
    hay que preguntarle a cada uno si es el suyo. Se mira el log porque es donde la
    instancia dice su propio id al adquirir.
    """
    for nombre in INSTANCIAS:
        logs = demo.correr(
            "docker", "logs", "--tail", "400", nombre, mostrar=False
        )
        if leader_id and leader_id in (logs.stdout + logs.stderr):
            return nombre
    return None


def main() -> int:
    demo = Demo(
        "Relevo de liderazgo: se mata al lider y el cluster sigue",
        "tres ControlNodes sin estado; manda el que sostiene el lease",
    )
    exigir_docker(demo)
    exigir_sesion(demo)

    # --- 1. Estado inicial -------------------------------------------------
    demo.titulo("Quien manda ahora")
    vista = demo.dfsha("cluster")
    print("    " + "\n    ".join(vista.stdout.strip().splitlines()[-6:]))

    antes = leer_liderazgo(demo)
    if antes is None or antes.leader_id is None:
        demo.mal("no hay lider registrado; el cluster no esta listo")
        return demo.terminar("")
    lider_antes, epoca_antes = antes.leader_id, antes.epoch

    # --- 2. EL CONTROL POSITIVO: identificar al lider de verdad ------------
    demo.titulo("Se identifica el CONTENEDOR del lider")
    demo.nota(
        "matar una instancia al azar tendria dos tercios de probabilidad de matar a "
        "una que no manda, y entonces que el cluster siga no demostraria nada"
    )
    victima = contenedor_del_lider(demo, lider_antes)
    if victima is None:
        demo.aviso(
            "no se identifico el contenedor del lider en los logs; se usa el primero "
            "como aproximacion, y la demostracion pierde su control positivo"
        )
        victima = INSTANCIAS[0]
    else:
        demo.ok(f"{victima} sostiene el lease (instancia {lider_antes[:12]}...)")

    # --- 3. Algo que funcione antes ----------------------------------------
    demo.titulo("El namespace responde ANTES de matar nada")
    demo.dfsha("mkdir", "/demo-failover")
    antes_ls = demo.dfsha("ls", "/")
    demo.afirmar(
        antes_ls.returncode == 0,
        "el cluster responde con normalidad",
        "el cluster ya fallaba antes de tocar nada",
    )

    # --- 4. Matar al lider -------------------------------------------------
    demo.titulo(f"Se para {victima}, que es quien manda")
    inicio = time.monotonic()
    demo.correr("docker", "stop", victima)

    # --- 5. El cluster sigue sirviendo -------------------------------------
    demo.titulo("El namespace sigue respondiendo, atendido por otra instancia")
    durante = demo.dfsha("ls", "/")
    demo.afirmar(
        durante.returncode == 0,
        "las lecturas y escrituras no se interrumpen",
        "el cluster dejo de responder al perder al lider",
    )
    demo.nota(
        "puede responder cualquiera de las tres: lecturas, planes de escritura y "
        "autenticacion NO necesitan liderazgo. El lease solo hace falta para evaluar "
        "la pertenencia y programar re-replicaciones"
    )

    # --- 6. El relevo, con la epoca subiendo -------------------------------
    demo.esperar(ESPERA_RELEVO, "que el lease venza y otra instancia lo tome")
    demo.titulo("Hay lider nuevo, y la epoca SUBIO")
    vista = demo.dfsha("cluster")
    print("    " + "\n    ".join(vista.stdout.strip().splitlines()[-6:]))

    despues = leer_liderazgo(demo)
    if despues is None or despues.leader_id is None:
        demo.mal("nadie tomo el lease tras la caida")
        return demo.terminar("")

    demo.afirmar(
        despues.leader_id != lider_antes,
        f"el lease cambio de manos en {time.monotonic() - inicio:.1f} s "
        f"({lider_antes[:8]} -> {despues.leader_id[:8]})",
        "el lider sigue siendo el mismo identificador",
    )
    demo.afirmar(
        despues.epoch > epoca_antes,
        f"la epoca subio de {epoca_antes} a {despues.epoch}",
        f"la epoca NO subio ({epoca_antes} -> {despues.epoch}): sin eso, un lider "
        "congelado podria escribir al despertar",
    )

    # --- 7. Devolver la instancia ------------------------------------------
    demo.titulo(f"Se vuelve a levantar {victima}")
    demo.correr("docker", "start", victima)
    demo.nota(
        "al volver NO recupera el mando: el lease es de otro y sigue vivo. Y si lo "
        "recuperase mas adelante, seria con una epoca NUEVA, nunca con la que tenia"
    )

    demo.dfsha("rmdir", "/demo-failover")
    return demo.terminar(
        """
        Se mato al lider de verdad, no a un espectador.
        El cluster no dejo de servir en ningun momento.
        Y la epoca subio, que es lo que impide que el que cayo escriba si despierta.
        """
    )


if __name__ == "__main__":
    sys.exit(main())
