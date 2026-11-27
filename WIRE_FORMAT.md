# dataclass-native: msgpack wire format

How a value of a schema type is written as [msgpack](https://github.com/msgpack/msgpack/blob/master/spec.md). Every
implementation (Python now, C++ and Rust to come) writes and reads this. The Python one, `to_msgpack` and
`from_msgpack`, is the reference: where this document and it disagree, one of them has a bug.

A reader is given the type it is to read. The data does not describe itself, beyond what a reader needs to tell
the members of a union apart and to skip a field it does not know.

## Values

| Type | Written as |
|---|---|
| `bool` | `true` or `false` |
| `int` and the sized ints | an integer |
| `float` | a 64-bit float |
| `Float32` | a 32-bit float |
| `str` | a string |
| `bytes` | binary |
| `datetime.date` | an integer: days since 1970-01-01 |
| `datetime.time` | an integer: microseconds since midnight |
| `datetime.timedelta` | an integer: microseconds |
| `datetime.datetime` | msgpack's timestamp, extension type -1 |
| an enum | a string: the member's name |
| `X \| None` | `nil`, or `X` |
| `tuple[X, ...]` | an array |
| `tuple[UInt8, ...]` | binary, as `bytes` |
| `tuple[A, B]` | an array, of as many elements as the tuple has |
| `frozenset[K]` | an array, in ascending order |
| `Mapping[K, V]` | a map, in ascending order of key |
| a `@native` class | a map: each field's name, then its value, in the order of the fields |
| `A \| B` | the member's own form |
| `DiscriminatedUnion[A, B]` | a map of two entries: `"t"`, the member's tag, then `"v"`, its value |
| `Shared[A]` | a reference, extension type 1: see [Shared objects](#shared-objects) |

Details:

- **Integers and lengths** are written in the shortest form which holds them, as msgpack recommends.
- **Floats** are written at the size of their type, whatever the value. A `float` field may be given a Python
  `int`, which is written as a float.
- **Timestamps** are written in the shortest of msgpack's three forms (4, 8 or 12 bytes) which holds the instant.
  A datetime holds microseconds, so the nanoseconds written are a multiple of 1000.
- **Order.** Numbers, dates and times ascend by value; strings by code point, which is also the order of their
  UTF-8 bytes; binary by byte; enums by the integer value of their members, not by name. This is the order in which
  C++'s `std::set` and `std::map` hold them.
- **Enums** are written by name, so that their values may be renumbered.
- **A struct's fields** are all written, including those which have their default value.
- **A plain union's members** are told apart by the kind of msgpack value each is written as, which is why no two
  may be written as the same kind. A Python `int` is written as the `int` member if there is one, and as the
  `float` member only if not.
- **A discriminated union's tags** are: for a class, its tag (by default `module.QualName`, or that given to
  `@native(tag=...)`); for an enum, `module.QualName`; and for the rest `bool`, `int`, `int8` ... `uint64`,
  `float`, `float32`, `str`, `bytes`, `date`, `time`, `datetime` and `timedelta`.

For example, `Money(1.5, Currency.EUR)` is the 30 bytes:

```
82                               a map of 2
  a6 "amount"    cb 3ff8000000000000    1.5
  a8 "currency"  a3 "EUR"
```

## Shared objects

A type from which a `Shared` field can be reached, however deeply, is written as a **bundle**: a map of two
entries, `"root"` then `"shared"`. Whether a type is one is known from the schema, and does not depend on the
value: such a type is always a bundle, even when nothing happens to be shared.

```
{
  "root":   the value, as above,
  "shared": [ [tag, [object, object, ...]],     one table for each class shared
              [tag, [object, ...]], ... ]
}
```

- Each `Shared` field is written as a reference: extension type 1, of 8 bytes, being two unsigned 32-bit
  big-endian integers. The first is the index of a table in `"shared"`, and the second the row of the object in it.
- An object is written once however many fields share it, and they all refer to that row. Objects are the same by
  identity, not by value: two equal objects which are not the same object are written twice. Reading gives back
  one object for each row, so what was shared still is.
- A shared object is written as its class is anywhere else, and may itself hold references, into any table.
- Tables are in the order in which their classes are first met, and rows in the order in which their objects are,
  in writing the root: fields in order, a shared object's own fields when it is first met.
- `Shared[A | B]` is a plain union though both members are classes, as a reference names its table, and so its
  class.

A reader must not assume that a row refers only to rows before it. It rejects a reference to a table or row which
is not there, one to an object of a class the field cannot hold, and an object which refers to itself, directly or
through others.

## Reading

- A field which is not in a struct's map takes its default. If it has none, the data is rejected.
- An entry of a struct's map which is not one of its fields is ignored. So a field may be added, given a default,
  or removed, and old and new readers each read what the other wrote.
- The entries of a struct's map, and of a bundle's, may be in any order.
- A float is read from either size. An integer is rejected where a float is expected, and the reverse.
- An integer outside the range of its type is rejected, as is a timestamp which is not a whole number of
  microseconds, and an enum name which is not a member's.
- A datetime is read as UTC, whatever timezone it was written from: it is an instant.
- Sets and maps are read whatever their order. A map with a key twice is rejected.

## Not decided

- Whether a schema's hash, or a version, should be written with the data.
- Deduplication of shared objects across writes, by a hash of their content.
