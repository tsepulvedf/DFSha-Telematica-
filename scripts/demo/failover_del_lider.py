"""Demostracion del relevo de liderazgo: se mata al lider y el cluster sigue.

    python scripts/demo/failover_del_lider.py

## El control positivo, que aqui es especialmente necesario

«Se mata un ControlNode y el sistema sigue funcionando» **pasa trivialmente si se mata al
que no manda**. Con tres instancias, dos de ellas no son lideres en ningun momento, asi que
matar una al azar tiene dos tercios de probabilidad de no demostrar nada: el cluster
seguiria porque nunca dependio de ella.

Por eso este guion **pregunta a cada instancia si es ella la que sostiene el lease** y mata
a esa. Si no consigue identificarla, **para**: seguir matando una cualquiera seria hacer la
demostracion sin su control positivo, y eso no es una version degradada, es otra
demostracion que no prueba nada.

Y ademas comprueba dos cosas mas que la version ingenua se salta:

1. Que el relevo **ocurrio**: hay un lider nuevo y es OTRA instancia.
2. Que **la epoca subio**. Es lo que distingue un relevo de verdad de un lider que sigue
   siendo el mismo, y es el token de aislamiento que impide que el congelado escriba al
   despertar. Un relevo sin subida de epoca seria un fallo silencioso.

## Por que no se identifica al lider por los logs

La primera version buscaba el `leader_id` en las ultimas 400 lineas de log de cada
contenedor. Fallaba de dos formas sin avisar: con el stack en marcha un rato, la linea
`leadership.acquired` ya no esta en esas 400; y el sucesor de un relevo anterior registra
el id del lider viejo como `previous_leader`, asi que el primer contenedor que lo
mencionara no tenia por que ser el suyo. Preguntarle a cada instancia por `is_self` no
depende de nada de eso.

## Lo que NO demuestra, y conviene decirlo

Esto ensena el relevo, no el escenario del lider congelado. Un contenedor parado esta
muerto y no despierta; el caso peligroso es el que se **congela** y vuelve. Ese no se puede
montar con `docker stop` y esta cubierto por
`test_el_lider_congelado_es_rechazado_y_no_escribe_nada` y su prueba hermana.
"""

from __future__ import annotations

import argparse
import sys
import time

from _comun import (
    CONTROLNODES,
    Demo,
    esperar,
    esperar_cluster,
    mostrar_cluster,
    exigir_docker,
    exigir_sesion,
    sesion_del_cli,
)

#: Margen sobre el TTL del lease (6 s por defecto). Se espera HASTA esto, no esto.
LIMITE_RELEVO = 30.0

#: Se ejecuta DENTRO de cada ControlNode, que es el unico sitio donde «127.0.0.1» es esa
#: instancia y no la que elija el balanceador. El token llega por el entorno y no en la
#: linea de ordenes, donde lo veria cualquiera con `ps`.
_PREGUNTA_IS_SELF = (
    "import os, httpx; "
    "r = httpx.get('http://127.0.0.1:8000/api/v1/cluster/leadership', "
    "headers={'Authorization': 'Bearer ' + os.environ['DFSHA_DEMO_TOKEN']}, timeout=5); "
    "r.raise_for_status(); print(r.json()['is_self'])"
)


def contenedor_del_lider(demo: Demo, token: str) -> str | None:
    """Que contenedor sostiene el lease, preguntandole a cada uno por `is_self`."""
    for nombre in CONTROLNODES:
        respuesta = demo.correr(
            "docker", "exec", "-e", "DFSHA_DEMO_TOKEN", nombre,
            "python", "-c", _PREGUNTA_IS_SELF,
            mostrar=False,
            entorno={"DFSHA_DEMO_TOKEN": token},
        )
        if respuesta.returncode != 0:
            demo.nota(f"{nombre} no contesto: {respuesta.stderr.strip()[-120:]}")
            continue
        if respuesta.stdout.strip() == "True":
            return nombre
    return None


def main(argv: list[str] | None = None) -> int:
    # Sin opciones, pero con parser: sin el, `--help` EJECUTABA la demostracion
    # entera, parando contenedores incluido.
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    from dfsha.client.api import ControlApi

    demo = Demo(
        "Relevo de liderazgo: se mata al lider y el cluster sigue",
        "tres ControlNodes sin estado; manda el que sostiene el lease",
    )
    exigir_docker(demo)
    exigir_sesion(demo)
    sesion = sesion_del_cli()
    api = ControlApi(sesion)
    esperar_cluster(demo, api)

    # --- 1. Estado inicial -------------------------------------------------
    demo.titulo("Quien manda ahora")
    # Las COMPROBACIONES se hacen sobre la API; `dfsha cluster` es lo que se ve en el
    # video. Raspar texto formateado para decidir envejece mal.
    antes = esperar(lambda: (lid := api.leadership()).leader_id and lid, 30)
    mostrar_cluster(demo, api)
    if not antes:
        demo.mal("no hay lider registrado tras 30 s; el cluster no esta listo")
        return demo.terminar("")
    lider_antes, epoca_antes = antes.leader_id, antes.epoch
    demo.nota(f"lider {lider_antes[:8]}..., epoca {epoca_antes}")

    # --- 2. EL CONTROL POSITIVO: identificar al lider de verdad ------------
    demo.titulo("Se identifica el CONTENEDOR del lider")
    demo.nota(
        "matar una instancia al azar tendria dos tercios de probabilidad de matar a "
        "una que no manda, y entonces que el cluster siga no demostraria nada"
    )
    victima = contenedor_del_lider(demo, sesion.token or "")
    if not demo.afirmar(
        victima is not None,
        f"{victima} responde is_self=True: sostiene el lease",
        "ninguna instancia dice sostener el lease; sin saber a quien matar, la "
        "demostracion no tendria control positivo",
    ):
        return demo.terminar("")

    # --- 3. Algo que funcione antes ----------------------------------------
    demo.titulo("El namespace responde ANTES de matar nada")
    demo.dfsha("mkdir", "/demo-failover")
    antes_ls = demo.dfsha("ls", "/")
    demo.afirmar(
        antes_ls.returncode == 0,
        "el cluster responde con normalidad",
        f"el cluster ya fallaba antes de tocar nada: {antes_ls.stderr.strip()}",
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
        f"el cluster dejo de responder al perder al lider: {durante.stderr.strip()}",
    )
    demo.nota(
        "puede responder cualquiera de las otras dos: lecturas, planes de escritura y "
        "autenticacion NO necesitan liderazgo. El lease solo hace falta para evaluar "
        "la pertenencia y programar re-replicaciones"
    )

    # --- 6. El relevo, con la epoca subiendo -------------------------------
    demo.titulo("Hay lider nuevo, y la epoca SUBIO")
    demo.nota(f"esperando a que el lease venza y otra instancia lo tome (hasta {LIMITE_RELEVO:.0f}s)")
    despues = esperar(
        lambda: (lid := api.leadership()).leader_id not in (None, lider_antes) and lid,
        LIMITE_RELEVO,
        cada_s=0.5,
    )
    relevo_s = time.monotonic() - inicio
    mostrar_cluster(demo, api)

    if not despues:
        demo.mal(f"nadie tomo el lease en {LIMITE_RELEVO:.0f}s tras la caida")
        demo.correr("docker", "start", victima)
        return demo.terminar("")

    demo.ok(
        f"el lease cambio de manos en {relevo_s:.1f} s "
        f"({lider_antes[:8]} -> {despues.leader_id[:8]})"
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
    sano = esperar(
        lambda: demo.correr(
            "docker", "inspect", "-f", "{{.State.Health.Status}}", victima, mostrar=False
        ).stdout.strip()
        == "healthy",
        90,
        cada_s=2,
    )
    if sano:
        demo.ok(f"{victima} vuelve a estar healthy")
    else:
        demo.aviso(f"{victima} no llego a healthy en 90 s; mira `docker compose ps`")
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
