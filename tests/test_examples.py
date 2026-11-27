"""The projects of examples: built with CMake, as scikit-build-core builds them, and their demos run in each mode"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_bindings import BUILD, PYTHON, RELEASE, TESTS, _cmake

from dataclass_native import ENV_VAR, Mode

EXAMPLES = TESTS.parent / "examples"


def _demo(example: str, installed: Path) -> dict[Mode, list[str]]:
    """The lines which an example's demo prints in each mode, after that naming the mode"""
    pytest.importorskip("nanobind")
    source, build = EXAMPLES / example, BUILD / "examples" / example
    _cmake(
        *("-S", str(source), "-B", str(build)),
        f"-DPython_EXECUTABLE={PYTHON}",
        "-DCMAKE_BUILD_TYPE=Release",
    )
    _cmake("--build", str(build), "--parallel", *RELEASE)
    # The example's package, with its extensions: as its wheel has them
    shutil.copytree(source / "src", installed, dirs_exist_ok=True)
    _cmake("--install", str(build), "--prefix", str(installed), *RELEASE)

    lines = {}
    for mode in Mode:
        result = subprocess.run(
            [sys.executable, str(source / "demo.py")],
            env=os.environ | {ENV_VAR: mode, "PYTHONPATH": str(installed)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(f"Mode: {mode}\n")
        lines[mode] = result.stdout.splitlines()[1:]
    return lines


def test_quickstart(tmp_path):
    lines = _demo("quickstart", tmp_path)
    assert lines[Mode.STRICT] == lines[Mode.MIXED]
    assert lines[Mode.STRICT][1:] == [
        "Stored in: ['__native__']",
        "The customer is shared, not copied: True",
        "Shipped: Status.SHIPPED",
        "Rejected: Line.quantity cannot be 'two'",
    ]
    assert lines[Mode.PURE][0] == lines[Mode.STRICT][0]
    assert lines[Mode.PURE][-1] == "A plain dataclass does not check its fields"


def test_functions(tmp_path):
    lines = _demo("functions", tmp_path)
    # The C++ and the Python implementations agree
    assert lines[Mode.STRICT] == lines[Mode.MIXED] == lines[Mode.PURE]
    assert lines[Mode.STRICT][0] == "Total: 44.48"
    assert lines[Mode.STRICT][2] == "Still sharing the customer: True"
