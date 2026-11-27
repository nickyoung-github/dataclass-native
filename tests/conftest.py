import os
import sys
import textwrap
from collections.abc import Callable, Iterator
from itertools import count
from types import ModuleType

import pytest

# Before dataclass_native is imported: the tests describe the schemas, rather than binding them. Unless the mode is
# set, as it is to run test_runtime.py again with the schemas bound: see test_bindings.py
os.environ.setdefault("DATACLASS_NATIVE_MODE", "pure")

_ids = count()


@pytest.fixture
def make_module() -> Iterator[Callable[..., ModuleType]]:
    """Makes importable modules from source, removed after the test"""
    names: list[str] = []

    def make(source: str, name: str | None = None) -> ModuleType:
        name = name or f"dynamic_schema_{next(_ids)}"
        module = ModuleType(name)
        sys.modules[name] = module
        names.append(name)
        exec(compile(textwrap.dedent(source), f"<{name}>", "exec"), module.__dict__)  # noqa: S102
        return module

    yield make

    for name in names:
        sys.modules.pop(name, None)
