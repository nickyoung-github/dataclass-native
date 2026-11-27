"""
dataclass-native generate cpp --package my_schemas --out generated/include
dataclass-native include-dir
"""

import pkgutil
import sys
from argparse import ArgumentParser
from importlib import import_module
from pathlib import Path

from ._mode import Mode, set_mode


def include_dir() -> Path:
    """The directory holding dataclass_native's C++ headers, which generated headers include"""
    return Path(__file__).parent / "include"


def _import_package(package: str) -> list[str]:
    module = import_module(package)
    for info in pkgutil.walk_packages(
        getattr(module, "__path__", []), prefix=f"{package}."
    ):
        import_module(info.name)

    from .introspect import loaded_modules

    return loaded_modules(package)


def _write(out: Path, files: dict[str, str]) -> list[Path]:
    """Writes files under out, leaving those unchanged untouched, so as not to trigger rebuilds"""
    written = []
    for relative, contents in files.items():
        path = out / relative
        if path.exists() and path.read_text() == contents:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="dataclass-native", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Generate code from schema modules")
    generate.add_argument("language", choices=["cpp"])
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

    args = parser.parse_args(argv)

    if args.command == "include-dir":
        print(include_dir())
        return 0

    if not args.module and not args.package:
        parser.error("generate needs --module or --package")

    # Schema modules are imported to be described, not bound
    set_mode(Mode.PURE)

    from .backends import cpp
    from .introspect import build_schema
    from .schema import SchemaError

    try:
        modules = [*args.module, *(m for p in args.package for m in _import_package(p))]
        schema = build_schema(modules)
        files = cpp.generate(schema)
    except SchemaError as e:
        print(f"dataclass-native: {e}", file=sys.stderr)
        return 1

    for path in _write(args.out, files):
        print(path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
