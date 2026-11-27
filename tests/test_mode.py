import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

import dataclass_native._mode as mode_module
from dataclass_native import ENV_VAR, Mode, mode, native, set_mode


def test_pure_mode_leaves_class_unchanged():
    @dataclass(frozen=True)
    class Point:
        x: int
        y: int = 0

    before = dict(vars(Point))
    assert native(Point) is Point
    assert dict(vars(Point)) == before
    assert Point(1) == Point(1, 0)
    assert mode() is Mode.PURE


def test_native_with_tag():
    @native(tag="point.v2")
    @dataclass(frozen=True)
    class Point:
        x: int

    from dataclass_native._native import native_info

    assert native_info(Point).tag == "point.v2"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: type("NotADataclass", (), {}), "must be applied to a dataclass"),
        (
            lambda: dataclass(type("Mutable", (), {"__annotations__": {"x": int}})),
            "only frozen dataclasses",
        ),
        (
            lambda: dataclass(frozen=True, slots=True)(type("Slotted", (), {})),
            "dataclasses with slots are not supported",
        ),
    ],
)
def test_native_rejects(make, message):
    with pytest.raises(TypeError, match=message):
        native(make())


def test_native_rejects_generic():
    @dataclass(frozen=True)
    class Box[T]:
        value: T

    with pytest.raises(TypeError, match="generic"):
        native(Box)


def test_set_mode_after_decoration():
    @native
    @dataclass(frozen=True)
    class Point:
        x: int

    set_mode(Mode.PURE)  # Unchanged, so allowed
    with pytest.raises(RuntimeError, match="already been defined"):
        set_mode(Mode.STRICT)


def test_mode_from_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, " Pure ")
    assert mode_module._from_env() is Mode.PURE
    monkeypatch.setenv(ENV_VAR, "mixed")
    assert mode_module._from_env() is Mode.MIXED
    monkeypatch.setenv(ENV_VAR, "")
    assert mode_module._from_env() is Mode.STRICT
    monkeypatch.setenv(ENV_VAR, "fast")
    with pytest.raises(ValueError, match="invalid"):
        mode_module._from_env()


def _run(mode: Mode, script: str) -> str:
    """What script prints, run in mode: in its own process, as a process has one mode"""
    env = os.environ | {ENV_VAR: mode, "PYTHONPATH": str(Path(__file__).parent)}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


_UNDECORATED = """
@dataclass(frozen=True)
class Undecorated(Party):
    extra: int = 0

try:
    Undecorated("x")
except TypeError as e:
    print("subclass:", e)
"""
_UNDECORATED_MESSAGE = "subclass: Undecorated derives from @native Party, so must be decorated with @native"


def test_strict_mode_without_binding():
    script = """
from dataclasses import dataclass
from dataclass_native import NativeBindingError, mode
from schemas.common import Money, Party

assert mode().value == "strict"
try:
    Money(1.0)
except NativeBindingError as e:
    print("binding:", e)
"""
    out = _run(Mode.STRICT, script + _UNDECORATED)
    assert "binding: No native binding is available for schemas.common.Money" in out
    assert _UNDECORATED_MESSAGE in out


def test_mixed_mode_without_binding():
    script = """
import copy
import pickle
from dataclasses import dataclass, replace
from dataclass_native import mode
from schemas.common import Fund, Money, Party

assert mode().value == "mixed"
money = Money(1.0)
assert money == replace(money) == copy.deepcopy(money) == pickle.loads(pickle.dumps(money))
print("plain:", vars(money), Fund("f", manager="m"))
"""
    out = _run(Mode.MIXED, script + _UNDECORATED)
    assert (
        "plain: {'amount': 1.0, 'currency': <Currency.USD: 1>} "
        "Fund(name='f', lei=None, rating=0, manager='m')"
    ) in out
    # As in strict mode, so that code which runs without a binding runs with one
    assert _UNDECORATED_MESSAGE in out
