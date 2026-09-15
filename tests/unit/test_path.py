"""Normalizacion y validacion de rutas, incluidos los intentos de escape con '..'."""

from __future__ import annotations

import pytest

from dfsha.common.errors import InvalidPathError
from dfsha.control_node.domain.path import MAX_NAME_LENGTH, Path


class TestNormalizacion:
    @pytest.mark.parametrize(
        ("raw", "esperado"),
        [
            ("/", "/"),
            ("//", "/"),
            ("/a", "/a"),
            ("/a/b/c", "/a/b/c"),
            ("/a//b", "/a/b"),
            ("/a/b/", "/a/b"),
            ("/a/./b", "/a/b"),
            ("/./a", "/a"),
            ("/a/b///c//", "/a/b/c"),
        ],
    )
    def test_rutas_absolutas(self, raw: str, esperado: str) -> None:
        assert str(Path.parse(raw)) == esperado

    @pytest.mark.parametrize(
        ("cwd", "raw", "esperado"),
        [
            ("/", "a", "/a"),
            ("/a", "b", "/a/b"),
            ("/a/b", "c/d", "/a/b/c/d"),
            ("/a/b", "./c", "/a/b/c"),
            ("/a", "/b", "/b"),
        ],
    )
    def test_rutas_relativas_se_resuelven_contra_el_cwd(
        self, cwd: str, raw: str, esperado: str
    ) -> None:
        assert str(Path.parse(raw, cwd=Path.parse(cwd))) == esperado

    def test_ruta_relativa_sin_cwd_se_rechaza(self) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse("a/b")


class TestRechazos:
    @pytest.mark.parametrize(
        "raw",
        [
            "/..",
            "/a/..",
            "/a/../b",
            "/../etc/passwd",
            "..",
            "/a/b/../../..",
        ],
    )
    def test_dos_puntos_siempre_se_rechaza(self, raw: str) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse(raw, cwd=Path.parse("/base"))

    def test_cadena_vacia(self) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse("")

    @pytest.mark.parametrize("raw", ["/a\x00b", "/a\nb", "/a\tb", "/\x7f"])
    def test_caracteres_de_control(self, raw: str) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse(raw)

    def test_nombre_demasiado_largo(self) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse("/" + "x" * (MAX_NAME_LENGTH + 1))

    def test_nombre_en_el_limite_se_acepta(self) -> None:
        assert Path.parse("/" + "x" * MAX_NAME_LENGTH).name == "x" * MAX_NAME_LENGTH

    def test_no_es_cadena(self) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse(None)  # type: ignore[arg-type]


class TestConsultas:
    def test_raiz(self) -> None:
        root = Path.root()
        assert root.is_root
        assert str(root) == "/"
        assert root.depth == 0
        assert root.parent == root

    def test_la_raiz_no_tiene_nombre(self) -> None:
        with pytest.raises(InvalidPathError):
            Path.root().name

    def test_nombre_y_padre(self) -> None:
        p = Path.parse("/a/b/c")
        assert p.name == "c"
        assert str(p.parent) == "/a/b"
        assert p.depth == 3

    def test_child(self) -> None:
        assert str(Path.parse("/a").child("b")) == "/a/b"

    def test_child_valida_el_nombre(self) -> None:
        with pytest.raises(InvalidPathError):
            Path.parse("/a").child("..")

    @pytest.mark.parametrize(
        ("ancestro", "otro", "esperado"),
        [
            ("/a", "/a/b", True),
            ("/a", "/a/b/c", True),
            ("/", "/a", True),
            ("/a", "/a", False),
            ("/a/b", "/a", False),
            ("/a", "/ab", False),
        ],
    )
    def test_is_ancestor_of(self, ancestro: str, otro: str, esperado: bool) -> None:
        assert Path.parse(ancestro).is_ancestor_of(Path.parse(otro)) is esperado

    def test_igualdad_y_hash(self) -> None:
        assert Path.parse("/a//b/") == Path.parse("/a/b")
        assert len({Path.parse("/a/b"), Path.parse("/a/b/")}) == 1
