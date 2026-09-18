"""RF1 sobre la app real: autenticacion, arbol de directorios y aislamiento."""

from __future__ import annotations

from .conftest import Sesion

MB = 1024 * 1024


class TestAutenticacion:
    def test_registro_y_login(self, control) -> None:
        assert control.post(
            "/api/v1/auth/register", json={"username": "ana", "password": "contrasena-larga"}
        ).status_code == 201

        respuesta = control.post(
            "/api/v1/auth/login", json={"username": "ana", "password": "contrasena-larga"}
        )
        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert cuerpo["access_token"] and cuerpo["expires_in"] > 0

    def test_usuario_duplicado(self, control) -> None:
        Sesion(control, "ana")
        assert control.post(
            "/api/v1/auth/register", json={"username": "ana", "password": "contrasena-larga"}
        ).status_code == 409

    def test_contrasena_incorrecta(self, control) -> None:
        Sesion(control, "ana")
        assert control.post(
            "/api/v1/auth/login", json={"username": "ana", "password": "otra-contrasena"}
        ).status_code == 401

    def test_usuario_inexistente_responde_igual_que_contrasena_mala(self, control) -> None:
        # Distinguirlos convertiria el login en un oraculo de que cuentas existen.
        Sesion(control, "ana")
        mala = control.post(
            "/api/v1/auth/login", json={"username": "ana", "password": "otra-contrasena"}
        )
        inexistente = control.post(
            "/api/v1/auth/login", json={"username": "nadie", "password": "otra-contrasena"}
        )
        assert mala.status_code == inexistente.status_code == 401
        assert mala.json()["message"] == inexistente.json()["message"]

    def test_el_token_protege_las_rutas(self, control) -> None:
        assert control.get("/api/v1/fs/ls", params={"path": "/"}).status_code == 401
        assert control.get(
            "/api/v1/fs/ls",
            params={"path": "/"},
            headers={"Authorization": "Bearer inventado"},
        ).status_code == 401
        assert control.get(
            "/api/v1/fs/ls", params={"path": "/"}, headers={"Authorization": "Basic x"}
        ).status_code == 401

    def test_el_token_de_un_usuario_solo_ve_su_arbol(self, ana: Sesion, beto: Sesion) -> None:
        ana.mkdir("/solo-de-ana")
        assert ana.nombres("/") == ["solo-de-ana"]
        assert beto.nombres("/") == []
        assert beto.get("/api/v1/fs/ls", params={"path": "/solo-de-ana"}).status_code == 404


class TestDirectorios:
    def test_mkdir_p_y_ls_en_cada_nivel(self, ana: Sesion) -> None:
        assert ana.mkdir("/a/b/c", parents=True).status_code == 201

        assert ana.nombres("/") == ["a"]
        assert ana.nombres("/a") == ["b"]
        assert ana.nombres("/a/b") == ["c"]
        assert ana.nombres("/a/b/c") == []

    def test_mkdir_sin_parents_exige_el_padre(self, ana: Sesion) -> None:
        assert ana.mkdir("/a/b").status_code == 404
        assert ana.mkdir("/a").status_code == 201
        assert ana.mkdir("/a/b").status_code == 201

    def test_mkdir_duplicado(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        assert ana.mkdir("/a").status_code == 409
        # Con parents=true es idempotente, como `mkdir -p`.
        assert ana.mkdir("/a", parents=True).status_code == 201

    def test_rmdir_no_vacio(self, ana: Sesion) -> None:
        ana.mkdir("/a/b", parents=True)
        respuesta = ana.delete("/api/v1/fs/rmdir", params={"path": "/a"})
        assert respuesta.status_code == 409
        assert respuesta.json()["code"] == "directory_not_empty"

    def test_rmdir_vacio(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        assert ana.delete("/api/v1/fs/rmdir", params={"path": "/a"}).status_code == 204
        assert ana.nombres("/") == []

    def test_rmdir_recursivo(self, ana: Sesion) -> None:
        ana.mkdir("/a/b/c", parents=True)
        assert ana.delete(
            "/api/v1/fs/rmdir", params={"path": "/a", "recursive": True}
        ).status_code == 204
        assert ana.nombres("/") == []
        assert ana.ls("/a").status_code == 404

    def test_se_puede_recrear_un_directorio_borrado(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        ana.delete("/api/v1/fs/rmdir", params={"path": "/a"})
        assert ana.mkdir("/a").status_code == 201

    def test_no_se_borra_la_raiz(self, ana: Sesion) -> None:
        assert ana.delete("/api/v1/fs/rmdir", params={"path": "/"}).status_code == 400

    def test_rmdir_inexistente(self, ana: Sesion) -> None:
        assert ana.delete("/api/v1/fs/rmdir", params={"path": "/no-existe"}).status_code == 404


class TestRutas:
    def test_se_rechazan_los_intentos_de_escape(self, ana: Sesion) -> None:
        for ruta in ("/a/../../etc", "/..", "/a/b/../.."):
            respuesta = ana.ls(ruta)
            assert respuesta.status_code == 400, ruta
            assert respuesta.json()["code"] == "invalid_path"

    def test_rutas_equivalentes(self, ana: Sesion) -> None:
        ana.mkdir("/a/b", parents=True)
        for ruta in ("/a/b", "/a/b/", "//a//b", "/a/./b"):
            assert ana.ls(ruta).status_code == 200, ruta


class TestStat:
    def test_stat_de_directorio(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        cuerpo = ana.get("/api/v1/fs/stat", params={"path": "/a"}).json()
        assert cuerpo["type"] == "directory"
        assert cuerpo["path"] == "/a"
        assert cuerpo["size"] == 0

    def test_stat_de_la_raiz(self, ana: Sesion) -> None:
        assert ana.get("/api/v1/fs/stat", params={"path": "/"}).json()["type"] == "directory"

    def test_stat_inexistente(self, ana: Sesion) -> None:
        assert ana.get("/api/v1/fs/stat", params={"path": "/no-existe"}).status_code == 404


class TestMv:
    def test_renombrar_un_directorio(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        assert ana.post("/api/v1/fs/mv", json={"src": "/a", "dst": "/b"}).status_code == 204
        assert ana.nombres("/") == ["b"]

    def test_mover_dentro_de_un_directorio_existente(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        ana.mkdir("/destino")
        assert ana.post(
            "/api/v1/fs/mv", json={"src": "/a", "dst": "/destino"}
        ).status_code == 204
        assert ana.nombres("/") == ["destino"]
        assert ana.nombres("/destino") == ["a"]

    def test_no_se_mueve_un_directorio_dentro_de_si_mismo(self, ana: Sesion) -> None:
        ana.mkdir("/a/b", parents=True)
        respuesta = ana.post("/api/v1/fs/mv", json={"src": "/a", "dst": "/a/b"})
        assert respuesta.status_code == 400
        assert respuesta.json()["code"] == "invalid_path"

    def test_destino_ocupado(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        ana.mkdir("/b")
        # /b existe y es directorio: mueve dentro. La segunda vez /b/a ya existe.
        assert ana.post("/api/v1/fs/mv", json={"src": "/a", "dst": "/b"}).status_code == 204
        ana.mkdir("/a")
        assert ana.post("/api/v1/fs/mv", json={"src": "/a", "dst": "/b"}).status_code == 409

    def test_origen_inexistente(self, ana: Sesion) -> None:
        assert ana.post(
            "/api/v1/fs/mv", json={"src": "/no-existe", "dst": "/b"}
        ).status_code == 404

    def test_el_padre_del_destino_debe_existir(self, ana: Sesion) -> None:
        ana.mkdir("/a")
        assert ana.post(
            "/api/v1/fs/mv", json={"src": "/a", "dst": "/no-existe/b"}
        ).status_code == 404


class TestPlanoInterno:
    """El plano interno ya no vive en el puerto de cliente.

    Estas pruebas comprobaban en las Etapas 1 y 2 que `/internal/v1` respondia 401 sin el
    secreto compartido. Desde el Bloque C ese secreto no existe: el plano interno se
    sirve en OTRO puerto, con TLS mutuo. Asi que lo que se afirma aqui cambia, y a algo
    mas fuerte: desde el puerto de cliente esas rutas **no existen**, con secreto, sin el
    o con el token de un usuario. No hay nada que adivinar.

    Que ese otro puerto exija certificado se prueba donde se puede probar de verdad,
    abriendo un socket: `test_mtls.py`.
    """

    def test_el_plano_interno_no_se_alcanza_desde_el_puerto_de_cliente(
        self, control
    ) -> None:
        assert control.get("/internal/v1/gc/orphan-blocks").status_code == 404
        # Tampoco con el secreto de las etapas anteriores: ya no significa nada.
        assert control.get(
            "/internal/v1/gc/orphan-blocks",
            headers={"X-DFSha-Internal-Secret": "el-de-la-etapa-2"},
        ).status_code == 404

    def test_un_token_de_usuario_tampoco_abre_el_plano_interno(self, ana: Sesion) -> None:
        """Un usuario autenticado sigue sin poder tocar el plano interno, y ahora ni
        siquiera puede intentarlo desde donde el habla."""
        assert ana.get("/internal/v1/gc/orphan-blocks").status_code == 404

    def test_el_registro_ya_no_esta_en_rest(self, internal) -> None:
        # Se fue a gRPC (ControlPlane.Register) con el resto del plano de control. La
        # idempotencia del registro se comprueba en test_control_plane.py, contra el
        # servidor gRPC de verdad. Se mira en la app INTERNA, que es donde estaria si
        # siguiera existiendo.
        respuesta = internal.post(
            "/internal/v1/datanodes/register",
            json={"base_url": "http://data-node-1:8001", "capacity_bytes": 10 * MB},
        )
        assert respuesta.status_code == 404

    def test_el_plano_interno_sirve_sus_rutas_en_su_propia_app(self, internal) -> None:
        """Y no se rompio al mudarse: las rutas que si existen siguen respondiendo."""
        assert internal.get("/internal/v1/gc/orphan-blocks").status_code == 200
