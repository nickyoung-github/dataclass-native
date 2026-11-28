# dataclass-native: Arrow and Parquet layout

How objects of a `@native` class are held in [Arrow](https://arrow.apache.org/docs/format/Columnar.html) tables,
and in the Parquet files of those tables. The Python implementation, `to_arrow` and `from_arrow`, is the
reference: where this document and it disagree, one of them has a bug. It is the only implementation so far.

The objects of a class are a table: a row for each object, and a column for each field. A reader is given the class
it is to read.

## Types

| Type | Arrow type |
|---|---|
| `bool` | `bool` |
| `int`, and the sized ints | `int64`, and `int8` ... `uint64` |
| `float`, `Float32` | `float64`, `float32` |
| `str` | `string` |
| `bytes`, `tuple[UInt8, ...]` | `binary` |
| `datetime.date` | `date32` |
| `datetime.time` | `time64[us]` |
| `datetime.datetime` | `timestamp[us, UTC]` |
| `datetime.timedelta` | `duration[us]` |
| an enum | `dictionary<int32, string>`: the member's name |
| `X \| None` | as `X`, null for `None` |
| `tuple[X, ...]` | `list<X>` |
| `frozenset[K]` | `list<K>`, in ascending order |
| `tuple[A, B]` | `struct<0: A, 1: B>` |
| `Mapping[K, V]` | `map<K, V>`, in ascending order of key |
| a `@native` class | `struct`, of a field for each of its fields |
| `A \| B`, `DiscriminatedUnion[A, B]` | `struct`, of a field for each member: see [Unions](#unions) |
| `Shared[A]` | `uint32`: see [Shared objects](#shared-objects) |

Details:

- **Null.** No field is declared as not null. A value is null only where its type has `None`, and a reader rejects
  a null elsewhere.
- **Order** of a set's elements and a map's keys is as in [WIRE_FORMAT.md](WIRE_FORMAT.md): enums by the value of
  their members, not by name.
- **Enums.** Which names an enum's dictionary holds, and in what order, is not specified: only the name each value
  refers to.
- **Metadata.** A table's schema has `dataclass_native:tag`, the tag of the class its rows are of.
- **A class which holds itself**, such as a tree whose nodes hold nodes, cannot be written: an Arrow type cannot
  contain itself. A class may hold its own class through a `Shared` field.

## Unions

A union is a struct with a field for each of its members, exactly one of which is not null. Where the union's
type has `None`, the struct itself is null for `None`.

Arrow has a union type, but Parquet has not, and few tools read Arrow's. A struct is read by all of them.

A member's field is named by its tag, as in a discriminated union when written as msgpack: for a class its tag,
for an enum `module.QualName`, and for the rest `bool`, `int`, `int8` ... `uint64`, `float`, `float32`, `str`,
`bytes`, `date`, `time`, `datetime` and `timedelta`. A member which has no tag, as it is not allowed in a
discriminated union, is named `array` if it is a tuple or a frozenset, `map` if a `Mapping`, and `bin` if a
`tuple[UInt8, ...]`.

A plain union and a discriminated union are held alike: the field which is not null says which member it is.

## Shared objects

Besides the table of the objects themselves, the **root**, there is a table for each class which they may share,
however deeply. Which classes those are is known from the schema, so each has a table even when it has no rows.

- A `Shared[A]` field is the row, in the table of `A`, of the object it shares. Its field has the metadata
  `dataclass_native:shared`, which is the tag of `A`.
- An object is a row once however many fields share it, across all the rows of the root. Objects are the same by
  identity, not by value. Reading gives back one object for each row, so what was shared still is.
- A shared object's own `Shared` fields are rows too, of any of the tables, its own included.
- A root object is not a row of its class's shared table, even if that class is shared elsewhere: the root is a
  table of its own.
- `Shared[A | B]` is a union as any other: a struct of a row in the table of `A` and one in the table of `B`, of
  which one is not null.
- Rows are in the order in which their objects are first met, in writing the root: row by row, fields in order, a
  shared object's own fields when it is first met.

This is how a database holds them: a shared object is a row of its own table, and a field which shares it is a
foreign key.

In Python the tables are an `ArrowTables`: `root`, and `shared`, the other tables by tag.

## Parquet

Each table is a file, written by pyarrow, in one directory:

```
trades/
  root.parquet
  shared/
    party.v1.parquet
    schemas.common.Fund.parquet
```

A shared table's file is named by its tag, percent-encoded but for letters, digits and `-._~`.

A class with no fields cannot be written as Parquet, where it is a field of another: Parquet has no struct without
fields.

## Reading

- A field which has no column takes its default. If it has none, the table is rejected.
- A column which is not a field is ignored, and columns may be in any order. The same holds for the fields of a
  struct.
- An enum's names may be in a dictionary or not, and a string or binary may be of the large kind.
- A reference to a table or a row which is not there is rejected, as is an object which refers to itself, directly
  or through others.

## Not decided

- Holding a class which holds itself, as rows which refer to rows of its own table.
- Writing a table in batches, and appending to Parquet files.
