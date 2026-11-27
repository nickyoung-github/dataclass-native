"""The Python extensions generated for schema modules: built with CMake, then test_runtime.py is run with them"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dataclass_native import ENV_VAR, Mode
from dataclass_native.backends import bindings
from dataclass_native.cli import cmake_dir, main
from dataclass_native.introspect import build_schema, describe
from dataclass_native.schema import schema_hash

TESTS = Path(__file__).parent
BUILD = TESTS.parent / "build" / "tests"


def _cmake(*arguments: str):
    path = os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]])
    cmake = shutil.which("cmake", path=path)
    if cmake is None or shutil.which("c++") is None:
        pytest.skip("No CMake or C++ compiler")

    directory = BUILD / "cmake"
    directory.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [cmake, *arguments], cwd=directory, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="session")
def built() -> Path:
    """Where to import the schemas package from, with its extensions: a copy of it, as a project would build"""
    pytest.importorskip("nanobind")
    # Kept between runs, so that only what has changed is built
    root = BUILD / "src"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(
        TESTS / "schemas", root / "schemas", dirs_exist_ok=True, ignore=ignore
    )
    _cmake(
        "-S",
        str(TESTS / "cmake"),
        f"-DPython_EXECUTABLE={sys.executable}",
        f"-DSCHEMAS_ROOT={root}",
    )
    _cmake("--build", ".", "--parallel")
    return root


def _run(root: Path, mode: Mode, script: str) -> str:
    """What a script prints, run in a mode with the built schemas: in its own process, as a process has one mode"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=os.environ | {ENV_VAR: mode, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("mode", [Mode.STRICT, Mode.MIXED])
def test_runtime_with_extensions(built, mode):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(TESTS / "test_runtime.py"),
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ]
        # Not prepending tests to the path, where the schemas have no extensions
        + ["--import-mode=importlib", "-o", f"pythonpath={built}"],
        env=os.environ | {ENV_VAR: mode},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert " skipped" not in result.stdout, result.stdout


def test_extensions_install_to_their_packages(built, tmp_path):
    # As scikit-build-core does, to make a wheel
    _cmake("--install", ".", "--prefix", str(tmp_path))
    installed = sorted(
        str(p.relative_to(tmp_path)).partition(".")[0]
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
    assert installed == [
        f"schemas/{name}_native"
        for name in (
            "_common",
            "_kinds",
            "_trades",
            "_tree",
            "_validated",
            "sub/_inner",
        )
    ]


def test_extension_built_from_another_version(built):
    out = _run(
        built,
        Mode.MIXED,
        """
        from dataclass_native import NativeBindingError
        from schemas import _common_native
        from schemas.common import Currency, Money, Party

        _common_native.__schema__["Currency"] = "0" * 16
        del _common_native.__schema__["Party"]

        # Even in mixed mode, as the extension is not missing but wrong
        try:
            Money(1.0)
        except NativeBindingError as e:
            print(e)
        print(vars(Party("p")))
        """,
    )
    assert out.splitlines() == [
        "schemas.common.Currency has changed since schemas._common_native was built: build it again",
        "{'name': 'p', 'lei': None, 'rating': 0}",
    ]


def test_class_missing_from_extension(built):
    out = _run(
        built,
        Mode.STRICT,
        """
        from dataclass_native import NativeBindingError
        from schemas import _common_native
        from schemas.common import Money, Party

        del _common_native.__schema__["Money"]
        try:
            # It has a Money as a default
            import schemas.trades
        except NativeBindingError as e:
            print(e)
        print(Party("p"))
        """,
    )
    missing = "No native binding is available for schemas.common.Money: schemas._common_native was built without Money"
    assert out.splitlines() == [
        f"{missing}. Build its extension, or set DATACLASS_NATIVE_MODE to mixed or pure",
        "Party(name='p', lei=None, rating=0)",
    ]


def test_class_is_bound_only_if_those_it_uses_are(built):
    out = _run(
        built,
        Mode.MIXED,
        """
        from schemas import _common_native

        del _common_native.__schema__["Money"]
        del _common_native.__schema__["Fund"]

        from schemas.common import Fund, Money, Party
        from schemas.trades import Bond, Cash

        cash, fund = Cash(Money(1.0)), Fund("f", rating=1, manager="m")
        # Plain, as is the Money it holds. Bond uses nothing unbound. Fund is plain, though its base is bound
        print(vars(cash), list(vars(Bond("XS1"))), list(vars(Party("p"))), vars(fund))
        print(fund, fund == Fund("f", rating=1, manager="m"))
        """,
    )
    plain_fund = "{'name': 'f', 'lei': None, 'rating': 1, 'manager': 'm'}"
    assert out.splitlines() == [
        f"{{'money': Money(amount=1.0, currency=<Currency.USD: 1>)}} ['__native__'] ['__native__'] {plain_fund}",
        "Fund(name='f', lei=None, rating=1, manager='m') True",
    ]


def test_function_is_native_only_if_the_classes_it_uses_are_bound(built):
    out = _run(
        built,
        Mode.MIXED,
        """
        from schemas import _common_native

        del _common_native.__schema__["Money"]

        from schemas.common import Money, Party
        from schemas.functions import implementation, renamed, total

        # Its Python implementation, as Money is plain
        print(implementation(), vars(total((Money(1.0), Money(2.0)))), list(vars(renamed(Party("p")))))
        """,
    )
    assert out.splitlines() == [
        "native {'amount': 3.0, 'currency': <Currency.USD: 1>} ['__native__']"
    ]


def test_pickles_load_in_any_mode(built, tmp_path):
    script = """
        import pickle
        from pathlib import Path
        from schemas.common import Currency, Money, Party, Side
        from schemas.trades import Cash, Trade

        trade = Trade(1, Side.BUY, Cash(Money(2.0, Currency.GBP)), Party("p"))
        if "{action}" == "dump":
            Path("{path}").write_bytes(pickle.dumps(trade))
        else:
            print(pickle.loads(Path("{path}").read_bytes()) == trade)
    """
    for dump, load in ((Mode.PURE, Mode.STRICT), (Mode.STRICT, Mode.PURE)):
        path = tmp_path / f"{dump}.pickle"
        _run(built, dump, script.format(action="dump", path=path))
        assert _run(built, load, script.format(action="load", path=path)) == "True\n"


def test_generated_source():
    source = bindings.generate(build_schema(["schemas.sub.inner"]))
    assert sorted(source) == [
        "extensions.cmake",
        "schemas/_common_native.cpp",
        "schemas/sub/_inner_native.cpp",
    ]

    from schemas.sub.inner import Order

    assert source["schemas/sub/_inner_native.cpp"] == textwrap.dedent(
        f"""\
        // Generated by dataclass-native from schemas.sub.inner: do not edit
        #include "dataclass_native/python.hpp"

        #include <nanobind/stl/map.h>
        #include <nanobind/stl/optional.h>
        #include <nanobind/stl/set.h>
        #include <nanobind/stl/string.h>
        #include <nanobind/stl/tuple.h>
        #include <nanobind/stl/variant.h>
        #include <nanobind/stl/vector.h>

        #include "schemas/sub/inner.hpp"

        NB_MODULE(_inner_native, m)
        {{
            nanobind::module_::import_("schemas._common_native");

            // By name, the hash of the schema each type was generated from
            nanobind::dict schema;

            nanobind::class_<::schemas::sub::inner::Order>(m, "Order")
                .def(nanobind::init<>())
                .def_rw("default", &::schemas::sub::inner::Order::default_)
                .def_rw("new", &::schemas::sub::inner::Order::new_)
                .def_rw("total", &::schemas::sub::inner::Order::total);
            schema["Order"] = "{schema_hash(describe(Order))}";

            m.attr("__schema__") = schema;
        }}
        """
    )
    assert source["extensions.cmake"] == textwrap.dedent(
        f"""\
        # Generated by dataclass-native: do not edit
        set(DATACLASS_NATIVE_EXTENSIONS
            "schemas.sub._inner_native"
            "schemas._common_native"
        )
        # The schema modules, on changing which this must be generated again
        set(DATACLASS_NATIVE_SCHEMAS
            [==[{TESTS / "schemas" / "sub" / "inner.py"}]==]
            [==[{TESTS / "schemas" / "common.py"}]==]
        )
        """
    )


def test_hash_is_of_fields_tag_and_members(make_module):
    from schemas.common import Currency, Money
    from schemas.tree import Node

    def hash_of(declaration: str) -> str:
        preamble = "from dataclasses import dataclass\nfrom enum import Enum\nfrom dataclass_native import native\n"
        module = make_module(preamble + declaration, name="hashed")
        return schema_hash(describe(module.Hashed))

    struct = "@native{tag}\n@dataclass(frozen=True)\nclass Hashed:\n    x: {type} = {default}"
    hashes = [
        hash_of(struct.format(tag="", type="int", default="0")),
        hash_of(struct.format(tag="", type="float", default="0")),
        hash_of(struct.format(tag="(tag='t')", type="int", default="0")),
        hash_of("class Hashed(Enum):\n    A = 1"),
        hash_of("class Hashed(Enum):\n    A = 2"),
        hash_of("class Hashed(Enum):\n    B = 1"),
    ]
    assert len(set(hashes)) == len(hashes)
    # Not of defaults, which Python does not leave to the native class
    assert hash_of(struct.format(tag="", type="int", default="1")) == hashes[0]

    # The same of a class alone as of it in its module's schema, where its recursive references are Indirect
    schema = build_schema(["schemas.tree", "schemas.common"])
    in_schema = {
        item.name: schema_hash(item)
        for m in schema.modules.values()
        for item in (*m.structs, *m.enums)
    }
    for cls in (Node, Money, Currency):
        assert schema_hash(describe(cls)) == in_schema[cls.__name__]


def test_cli(tmp_path, capsys):
    assert main(["cmake-dir"]) == 0
    assert capsys.readouterr().out.strip() == str(cmake_dir())
    assert (cmake_dir() / "dataclass_native-config.cmake").exists()

    assert (
        main(["generate", "nanobind", "--package", "schemas", "--out", str(tmp_path)])
        == 0
    )
    written = {
        str(Path(p).relative_to(tmp_path)) for p in capsys.readouterr().out.split()
    }
    assert {
        "schemas/common.hpp",
        "schemas/_common_native.cpp",
        "extensions.cmake",
    } <= written
    assert len(written) == 13
