"""
dataclass-native generate cpp --package my_schemas --out generated/include
dataclass-native generate nanobind --package my_schemas --out generated
dataclass-native include-dir
dataclass-native cmake-dir
"""

import pkgutil
import sys
from argparse import ArgumentParser
from importlib import import_module
from pathlib import Path

from ._mode import Mode, set_mode
from ._native import extension_name


def include_dir() -> Path:
    """The directory holding dataclass_native's C++ headers, which generated headers include"""
    return Path(__file__).parent / "include"


def cmake_dir() -> Path:
    """The directory of dataclass_native's CMake package, which adds the extensions of schema modules to a build"""
    return Path(__file__).parent / "cmake"


def _import_package(package: str) -> list[str]:
    module = import_module(package)
    extensions: set[str] = set()
    for info in pkgutil.walk_packages(
        getattr(module, "__path__", []), prefix=f"{package}."
    ):
        # Not the extensions of schema modules, built by an earlier run
        if info.name not in extensions:
            import_module(info.name)
            extensions.add(extension_name(info.name))

    from .introspect import loaded_modules

    return loaded_modules(package)


def _write(out: Path, files: dict[str, str]) -> list[Path]:
    """Writes files under out, leaving those unchanged untouched, so as not to trigger rebuilds"""
    written = []
    for relative, contents in files.items():
        path = out / relative
        if path.exists() and path.read_text(encoding="utf-8") == contents:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="dataclass-native", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Generate code from schema modules")
    generate.add_argument(
        "language",
        choices=["cpp", "nanobind"],
        help="cpp: headers. nanobind: also the sources of the Python extension modules binding them",
    )
    generate.add_argument(
        "-m",
        "--module",
        action="append",
        default=[],
        help="A schema module (repeatable)",
    )
    generate.add_argument(
        "-p",
        "--package",
        action="append",
        default=[],
        help="A package, all of whose schema modules are used",
    )
    generate.add_argument(
        "-o", "--out", type=Path, required=True, help="The output directory"
    )

    commands.add_parser(
        "include-dir", help="Print the directory of dataclass_native's C++ headers"
    )
    commands.add_parser(
        "cmake-dir", help="Print the directory of dataclass_native's CMake package"
    )

    args = parser.parse_args(argv)

    if args.command != "generate":
        print(include_dir() if args.command == "include-dir" else cmake_dir())
        return 0

    if not args.module and not args.package:
        parser.error("generate needs --module or --package")

    # Schema modules are imported to be described, not bound
    set_mode(Mode.PURE)

    from .backends import bindings, cpp
    from .introspect import build_schema
    from .schema import SchemaError

    try:
        modules = [*args.module, *(m for p in args.package for m in _import_package(p))]
        schema = build_schema(modules)
        files = cpp.generate(schema)
        if args.language == "nanobind":
            files |= bindings.generate(schema)
    except SchemaError as e:
        print(f"dataclass-native: {e}", file=sys.stderr)
        return 1

    for path in _write(args.out, files):
        print(path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
