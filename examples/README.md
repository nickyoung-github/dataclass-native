# Examples

Each is a project of its own, built by [scikit-build-core](https://scikit-build-core.readthedocs.io) from its
`CMakeLists.txt`, and using the `dataclass-native` of this repository.

- [quickstart](quickstart): a schema of customers and orders, stored in the C++ structs generated from it.
- [functions](functions): the same schema, with C++ functions which take and return its structs, called from
  Python as functions of its dataclasses.

To build and run one, with a C++20 compiler installed:

```shell
cd examples/quickstart
uv sync             # Generates the C++, compiles it and installs the package
uv run demo.py
DATACLASS_NATIVE_MODE=pure uv run demo.py   # As plain dataclasses
```

After changing a schema module or the C++, build again with `uv sync --reinstall-package shop`.
