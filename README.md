# dataclass-native

Use Python dataclasses as a data description language. From them, generate matching C++ structs (and later Rust),
whose storage the Python classes use directly. Native serialisation (msgpack, Arrow/Parquet) is planned.

**Status:** early. Schema introspection and C++ header generation work. The native Python binding, serialisation
and Rust are to come: see [DESIGN.md](DESIGN.md).

## Defining a schema

```python
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

from dataclass_native import DiscriminatedUnion, Int32, Shared, native


class Currency(Enum):
    USD = auto()
    EUR = auto()


@native
@dataclass(frozen=True)
class Money:
    amount: float
    currency: Currency = Currency.USD


@native(tag="party.v1")  # Names the type when serialised
@dataclass(frozen=True)
class Party:
    name: str
    rating: Int32 = 0


@native
@dataclass(frozen=True)
class Cash:
    money: Money


@native
@dataclass(frozen=True)
class Bond:
    isin: str


@native
@dataclass(frozen=True)
class Trade:
    id: int
    instrument: DiscriminatedUnion[Cash, Bond]   # Two structs: needs a tag when serialised
    counterparty: Shared[Party]                  # Shared, not copied; serialised by reference
    tags: tuple[str, ...] = ()
    limits: Mapping[Currency, float] | None = None
    executed: dt.datetime | None = None
```

Classes must be frozen. Containers are `tuple[T, ...]`, `tuple[A, B]`, `frozenset[T]` and `Mapping[K, V]`.

## Generating C++

```shell
dataclass-native generate cpp --package my_schemas --out generated/include
dataclass-native include-dir   # dataclass_native's own headers, which the generated headers include
```

This writes a header per module (`my_schemas/trades.hpp`, namespace `my_schemas::trades`) of plain C++20 structs,
with no dependency on Python:

```cpp
struct Trade
{
    std::int64_t id{};
    std::variant<Cash, Bond> instrument{};
    dataclass_native::Shared<Party> counterparty{};
    std::vector<std::string> tags = std::vector<std::string>{};
    std::optional<std::map<Currency, double>> limits = std::nullopt;
    std::optional<dataclass_native::DateTime> executed = std::nullopt;

    bool operator==(const Trade&) const = default;
};
```

Unchanged files are left alone, so builds don't rerun needlessly.

## Modes

`DATACLASS_NATIVE_MODE` is one of:

- `strict` (the default): fields are stored in the native structs, and a class whose binding is missing raises.
- `mixed`: as `strict`, but a class whose binding is missing behaves as a plain dataclass.
- `pure`: `@native` returns every class unchanged, as a plain dataclass, for testing.

## Development

```shell
uv sync
uv run pytest
uv run ruff check . && uv run pyright
```
