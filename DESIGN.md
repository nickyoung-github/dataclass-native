# dataclass-native: design

Python dataclasses are used as a data description language. We generate native structs, C++ currently, Rust to follow.
The storage of a Python instance lives in its native struct: the dataclass's fields become descriptors which get
and set the native struct's members. A class can also stay a plain dataclass: see [Modes](#modes).

This is a rewrite of [pydantic-bind](https://github.com/nickyoung-github/pydantic-bind), which this document refers
to as "the old implementation".

## Decisions

| | Decision | Why |
|---|---|---|
| Schema | Plain stdlib `@dataclass(frozen=True)`, plus `@native` | Type checkers and IDEs see ordinary dataclasses |
| Mutability | Frozen classes only, for now | A copy and a view of an immutable value cannot be told apart, so Python's reference semantics and native value semantics agree |
| Opt-in | The `@native` decorator, applied above `@dataclass` | It runs once the dataclass's fields are final, needs no metaclass, and so never conflicts with other metaclasses |
| Polymorphism | None: use unions | Inheritance only reuses fields, which are copied into each native struct, so C++ and Rust match |
| C++ bindings | nanobind | It is faster and smaller than pybind11, stores C++ objects inside the Python object, and makes Python enums |
| pydantic | Not supported | `BaseModel` and pydantic's dataclass validation set `__dict__` directly, which bypasses the descriptors. It is possible we'll address this later but pydantic have not made it easy |
| Serialisation | Our own drivers, using reflection generated from the schema | We know the schema at generation time, which opens up cross-language serialisation. |
| msgpack structs | Maps keyed by field name | Readers can tolerate added or removed fields |

## Architecture

```
schema modules (.py) ──introspect──▶ schema model ──backends──▶ C++ headers ──▶ nanobind bindings (phase 2)
                                         │                   ──▶ Rust crate (phase 5)
                                         └──▶ runtime: @native descriptors, pure-Python codecs
```

- `dataclass_native.schema` is the language-neutral schema model. Introspection does everything that must agree
  across languages: union rules, where recursion is broken, ordering, defaults, tags.
- Each backend only renders the model. Language-specific concerns stay in the backend: keywords, how
  defaults are written.

## Modes

`DATACLASS_NATIVE_MODE` (read once, at import):

- `strict` (default): instances store their fields in the native struct. A missing binding raises
  `NativeBindingError` rather than falling back silently: the binding must have been generated, compiled and be
  importable.
- `mixed`: as `strict` for a class whose binding is available. A class without one behaves as a plain dataclass.
  The rules which do not depend on the binding still apply, e.g. that subclasses are decorated, so that code which
  runs without a binding also runs with one.
- `pure`: `@native` returns the class unchanged, as a plain dataclass, whether or not its binding is available.
  This is for testing, and code generation always runs in this mode.

`set_mode()` may be called before any `@native` class is defined. Each mode needs its own process, e.g. one CI job
per mode.

With frozen classes, the remaining difference between a bound class and a plain dataclass is validation. Native
setters reject values of the wrong type or out of range, while a plain dataclass accepts anything. So `mixed` mode
validates only the classes which are bound. A `pure-checked` mode, validating against the schema, may be added.

A native struct cannot hold a plain dataclass, so in `mixed` mode a class is bound only if every class its fields
use is bound (phase 2).

## Types

| Python | Schema model | C++ | Rust (planned) |
|---|---|---|---|
| `bool` | `Bool` | `bool` | `bool` |
| `int`, `Int8`…`Int64`, `UInt8`…`UInt64` | `Int(bits, signed)` | `std::int64_t` etc. | `i64` etc. |
| `float`, `Float32` | `Float(bits)` | `double`, `float` | `f64`, `f32` |
| `str` | `Str` | `std::string` | `String` |
| `bytes` | `Bytes` | `dataclass_native::Bytes` (`std::vector<std::uint8_t>`) | `Vec<u8>` |
| `datetime.date` | `Date` | `dataclass_native::Date` (`sys_days`) | |
| `datetime.time` (no tz) | `Time` | `dataclass_native::TimeOfDay` | |
| `datetime.datetime` (aware) | `DateTime` | `dataclass_native::DateTime` (µs since the epoch, UTC) | |
| `datetime.timedelta` | `TimeDelta` | `dataclass_native::TimeDelta` (µs) | |
| `Enum` | `EnumRef` | `enum class` | `enum` |
| `@native` class | `StructRef` | `struct` | `struct` |
| `X \| None` | `Optional` | `std::optional<X>` | `Option<X>` |
| `tuple[X, ...]` | `Sequence` | `std::vector<X>` | `Vec<X>` |
| `tuple[A, B]` | `Tuple` | `std::tuple<A, B>` | `(A, B)` |
| `frozenset[K]` | `Set` | `std::set<K>` | `BTreeSet<K>` |
| `Mapping[K, V]` | `Map` | `std::map<K, V>` | `BTreeMap<K, V>` |
| `A \| B`, `DiscriminatedUnion[A, B]` | `Union` | `std::variant<A, B>` | `enum` |
| `Shared[A]` | `Shared` | `dataclass_native::Shared<A>` | `Arc<A>` |
| (recursion) | `Indirect` | `dataclass_native::Indirect<A>` | `Box<A>` |

- `list`, `dict` and `set` are rejected, with a suggested immutable alternative.
- Set elements and Mapping keys must be scalars or enums.
- `NewType` and `type X = ...` aliases resolve to their underlying type. `Any`, `object`, `Literal`, `Decimal`,
  `Flag` enums and generics are rejected.
- Python ints are unbounded: native setters (and serialisers) raise on overflow.
- Enums keep int values; other enums are numbered by position. Enums are serialised by name, so reordering them
  doesn't break data.
- C++ keywords get a trailing `_` (`default` → `default_`). Names that C++ reserves (containing `__`, or starting
  with `_` and a capital) are rejected.
- Python module `pkg.sub.mod` → header `pkg/sub/mod.hpp`, namespace `pkg::sub::mod`.
- Structs are C++20 aggregates with default member initialisers and a defaulted `operator==`, so designated
  initialisers work, with fields in Python's order.

### Unions

- Members are chosen by exact type. A field typed `Foo` never holds a subclass, so values are never sliced.
- A plain `A | B` must be distinguishable when serialised: no two members may share a wire kind (map, array, str,
  int, ...). Two structs, `str | SomeEnum` or `date | int` need `DiscriminatedUnion`, which is serialised as
  `{"t": tag, "v": value}`.
- Tags are language-neutral: by default `module.QualName`, set with `@native(tag="trade.v1")`, so that moving or
  renaming a class keeps the wire format. Primitives' tags are their type name (`int`, `str`, ...).

### Shared

`Shared[T]` says the value is shared, not copied.

- In memory it is a reference-counted pointer to an immutable value, compared by value, like Rust's `Arc<T>`.
- `Shared` applies to each member of a union: `Shared[A | B]` is `Shared[A] | Shared[B]`. What is shared is a
  particular `A`, which another `Shared[A]` field may also hold.
- `Shared` applies only to `@native` classes (and unions or optionals of them).
- Because only frozen classes are supported, a graph of shared objects can have no cycles: an immutable object
  can't be changed after it is built to point back at its parent. Reference-counted cycles would leak, so mutable
  classes will need this rule enforced.
- When serialised, `Shared` means "write separately, and refer to it" (see below).

### Recursion

- A struct may hold itself, or a type in a cycle with it, through `Optional`, `Union`, `Tuple` or `Map`.
  Introspection finds these cycles (as strongly connected components of by-value references) and makes exactly
  those references `Indirect`, so every backend breaks cycles at the same fields.
- Sequences already store their elements out of line, so need no `Indirect`.
- Types in a cycle must be in the same module, and modules may not use each other's types.
- `dataclass_native::Indirect` is empty when default-constructed, unlike C++26's `std::indirect`, so that
  default-constructing a recursive type terminates.

## Serialisation (phases 3 and 4)

All three implementations (C++, Rust and a pure-Python one, used by plain dataclasses) follow one wire format. The
pure-Python codec is the reference implementation, and writes golden files that the C++ and Rust tests must read.

| Kind | msgpack | Arrow |
|---|---|---|
| struct | map keyed by field name. Readers ignore unknown fields; missing fields take their default | struct |
| `X \| None` | nil | nullable |
| plain union | the member's own encoding | dense union |
| discriminated union | `{"t": tag, "v": value}` | dense union. Parquet has no union type, so it is written as a struct of nullable fields |
| enum | name | dictionary-encoded string |
| datetime | msgpack timestamp extension (-1) | `timestamp[us, UTC]` |
| date / time / timedelta | int days / µs / µs | `date32` / `time64[us]` / `duration[us]` |
| `Shared[T]` | extension type `(tag index, row)` | `Shared[A]`: `uint32` row. `Shared[A \| B]`: `struct<t: dictionary<string>, i: uint32>` |

**Shared and bundles.** A type reaching any `Shared` field is serialised as a bundle: the roots, plus a table of
objects per type. Each `Shared` field is a reference to a row of one of those tables.

- msgpack: `{"root": ..., "shared": {tag: [...]}}`.
- Arrow: a table per tag, i.e. a `dict[tag, Table]` in Python, and a file per tag for Parquet.

This means:

1. `Shared[A | B]` needs no union column: the reference carries the tag.
2. Objects are deduplicated by identity within a write, so sharing survives a round trip.
3. Recursive types become representable in Arrow, as rows referring to rows.
4. Reading uses a cached recursive lookup of `(tag, row)`, which builds each shared object once.

Identities are only valid within one write. Content-hash ids, for deduplication across writes, may come later.

**C++ drivers.** For each struct, the generator emits `dataclass_native::reflect<T>`: its tag, and a tuple of
`(name, member pointer)` pairs. The drivers are written against that:

- msgpack: a small hand-written encoder and decoder, with no msgpack-c or Boost dependency.
- Arrow: nanoarrow, exported through the Arrow C Data Interface (`__arrow_c_stream__`). pyarrow, polars and duckdb
  can read it without copying, and our extension needn't link Arrow C++, which avoids version clashes with pyarrow.
- Parquet: written with pyarrow from Python. For C++ users, an optional component linking Arrow C++.

**Rust.** serde, with `rmp_serde::to_vec_named` (structs as maps). Unions need a custom serialiser for the
`{"t", "v"}` layout. Arrow is via arrow-rs; the result must be checked against the C++ output.

## Python runtime (phase 2)

- `@native` installs a `__new__` that creates the native object and stores its handle on the instance. It also
  checks that every class in the hierarchy is decorated, since a subclass's fields would otherwise have no storage.
- Each field becomes a data descriptor forwarding to the native member. The stock dataclass `__init__`, `__eq__`,
  `__repr__`, `replace`, `fields` and `asdict` keep working. The frozen `__init__` uses `object.__setattr__`, which
  still goes through data descriptors.
- `__copy__`, `__deepcopy__` and `__reduce__` are defined, as the default copy would share the native handle.
  Pickles use `(cls, (), {field: value})` in every mode, so they load in any.
- `cls._from_native(handle)` wraps a native object without copying, e.g. one returned by C++.
- `Shared` fields keep a weak cache from native address to Python wrapper, so that `a.foo is f` holds.
- The extension is looked up lazily, at first instantiation, so schema modules import without it.
- Each native type carries a hash of its schema, checked when binding, to detect stale builds.

## Limitations

- A field read goes through a Python descriptor, the native getter and a conversion (about 100–200 ns, against
  about 20 ns for a plain attribute). That suits messages, not hot loops. Generating the descriptors in C, or making
  the Python class subclass the native type, could come later.
- `vars(obj)` doesn't show the fields of a bound class.
- Free-threaded Python: native members have no locking.
- Using these types from other extension modules requires the same nanobind ABI.
- Generation imports the schema modules, so runs their code.
- Mutable classes (later) bring back the problem of references versus copies: path-based proxies for nested values
  and containers, and `Shared` for real aliasing.

## Phases

1. **Done:** schema model, introspection, C++ header generation, `@native` with its modes (no class has a
   binding yet), CLI.
2. nanobind bindings, the descriptor runtime, the schema-hash check, and a CMake/scikit-build-core helper.
3. The written wire format specification, the pure-Python msgpack codec, `reflect<T>` and the C++ msgpack driver.
4. Arrow via nanoarrow, Parquet via pyarrow, optional Arrow C++ Parquet.
5. Rust: structs with serde, PyO3 bindings, Arrow.
6. Mutable classes.

## Open questions

- Should timezone-naive datetimes be rejected (current), or treated as UTC?
- Should the namespace include the module (`pkg::mod`, current) or only the package?
- When generating from explicit modules, all their dependency modules are generated too. Is a `--no-deps` option
  needed for dependencies generated by another project?
