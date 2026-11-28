"""Fields which are numpy arrays. Run in pure mode, and by test_bindings in strict and mixed modes"""

import copy
import gc
import pickle
from collections.abc import Mapping
from dataclasses import asdict, replace
from typing import Any

import msgpack
import numpy as np
import pyarrow as pa
import pytest
from numpy.typing import NDArray
from schemas import functions
from schemas.arrays import Book, Curve, Surface

from dataclass_native import (
    ArrowTables,
    Mode,
    Shared,
    from_arrow,
    from_msgpack,
    mode,
    to_arrow,
    to_msgpack,
)
from dataclass_native import schema as s
from dataclass_native._arrow import read as read_arrow
from dataclass_native._arrow import write as write_arrow
from dataclass_native._msgpack import unpack
from dataclass_native._wire import read, write
from dataclass_native.arrays import Matrix, Vector
from dataclass_native.introspect import describe, resolve
from dataclass_native.schema import SchemaError

BOUND = mode() is not Mode.PURE
bound_only = pytest.mark.skipif(not BOUND, reason="Plain dataclasses in pure mode")


def _curve(**fields: Any) -> Curve:
    given = {
        "name": "usd",
        "points": np.arange(3.0),
        "weights": np.array([0.5], np.float32),
        "ids": np.array([1, -2], np.int32),
        "octets": np.array([0, 255], np.uint8),
    }
    return Curve(**given | fields)


def _packed(plain: Any) -> bytes:
    packed = msgpack.packb(plain, use_bin_type=True)
    assert packed is not None
    return packed


def _empty() -> Curve:
    return Curve("empty", np.array([], np.float64))


def _surface(**fields: Any) -> Surface:
    given = {
        "values": np.arange(6.0).reshape(2, 3),
        "counts": np.array([[1], [-2]], np.int16),
        "image": np.arange(4, dtype=np.uint8).reshape(2, 1, 2),
    }
    return Surface(**given | fields)


def test_schema():
    struct = describe(Curve)
    assert isinstance(struct, s.Struct)
    assert [f.type for f in struct.fields[1:]] == [
        s.Array(s.Float()),
        s.Optional(s.Array(s.Float(32))),
        s.Optional(s.Array(s.Int(32))),
        s.Optional(s.Array(s.Int(8, signed=False))),
    ]
    unsigned = s.Array(s.Int(64, signed=False))
    assert resolve(Vector[np.uint64], "x") == unsigned
    assert unsigned.dtype == "uint64"

    surface = describe(Surface)
    assert isinstance(surface, s.Struct)
    assert [f.type for f in surface.fields] == [
        s.Array(s.Float(), 2),
        s.Optional(s.Array(s.Int(16), 2)),
        s.Optional(s.Array(s.Int(8, signed=False), 3)),
    ]


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (tuple[Vector[np.float64], ...], "x: an array may be a field, or None, but"),
        (tuple[int, Matrix[np.float64]], "an array may be a field"),
        (Mapping[str, Vector[np.float64]], "an array may be a field"),
        (tuple[Vector[np.float64] | None, ...], "an array may be a field"),
        (Vector[np.float64] | int, "an array may be a field"),
        (frozenset[Vector[np.float64]], "must be scalars or enums"),
        (Shared[Vector[np.float64]], "Shared applies only to @native classes"),
        (Vector[np.bool_], "unsupported array .*: use one of a sized int or float"),
        (Matrix[np.complex128], "unsupported array"),
        (Vector[Any], "unsupported array"),
        # Of any number of dimensions
        (NDArray[np.float64], "x: unsupported array .*: its number of dimensions must"),
        (np.ndarray[Any, np.dtype[np.float64]], "its number of dimensions must"),
        (
            np.ndarray[tuple[int, ...], np.dtype[np.float64]],
            "must be given. Use Vector",
        ),
        (np.ndarray[tuple[()], np.dtype[np.float64]], "its number of dimensions"),
        (np.ndarray[tuple[int, Any], np.dtype[np.float64]], "its number of dimensions"),
        (np.ndarray, "unsupported type ndarray"),
    ],
)
def test_unsupported(annotation, message):
    with pytest.raises(SchemaError, match=message):
        resolve(annotation, "x")


def test_arrays_are_read():
    curve = _curve()
    points, ids = curve.points, curve.ids
    assert type(points) is np.ndarray and points.dtype == np.float64
    assert points.tolist() == [0.0, 1.0, 2.0]
    assert ids is not None and ids.dtype == np.int32 and ids.tolist() == [1, -2]
    assert _empty().points.shape == (0,) and _empty().weights is None

    given = np.arange(3.0)
    held = _curve(points=given)
    if not BOUND:
        # A plain dataclass holds the array it was given
        assert held.points is given
        return

    assert list(vars(curve)) == ["__native__"]
    # Not copied when read, though it was when given
    assert np.shares_memory(points, curve.points)
    given[0] = 9.0
    assert held.points[0] == 0.0
    # Nor written to
    assert not points.flags.writeable
    with pytest.raises(ValueError, match="read-only"):
        points[0] = 9.0
    with pytest.raises(ValueError, match="cannot set WRITEABLE"):
        points.flags.writeable = True

    # It keeps what it reads alive, and is not changed by what holds it being given another
    object.__setattr__(curve, "points", np.array([7.0]))
    assert curve.points.tolist() == [7.0]
    del curve
    gc.collect()
    assert points.tolist() == [0.0, 1.0, 2.0]

    # Of every other spacing, so not as it is in memory
    assert _curve(points=np.arange(6.0)[::2]).points.tolist() == [0.0, 2.0, 4.0]


def test_more_dimensions_are_read():
    surface = _surface()
    values, image = surface.values, surface.image
    assert type(values) is np.ndarray and values.dtype == np.float64
    assert values.shape == (2, 3) and values[1].tolist() == [3.0, 4.0, 5.0]
    assert image is not None and image.shape == (2, 1, 2) and image[1, 0, 1] == 3
    assert surface.counts is not None and surface.counts.tolist() == [[1], [-2]]
    # A dimension of none keeps the others
    assert _surface(values=np.zeros((0, 3))).values.shape == (0, 3)

    # Whatever the order it is in memory
    turned = np.asfortranarray(np.arange(6.0).reshape(2, 3))
    assert turned.flags.f_contiguous and _surface(values=turned) == surface
    if BOUND:
        assert np.shares_memory(values, surface.values)
        assert values.flags.c_contiguous and not values.flags.writeable
        assert _surface(values=turned).values.flags.c_contiguous
        del surface
        gc.collect()
        assert values[1, 2] == 5.0


def test_equality_and_hash():
    assert _curve() == _curve() and hash(_curve()) == hash(_curve())
    assert len({_curve(), _curve(), _empty()}) == 2
    assert _curve() != _curve(points=np.arange(4.0))
    assert _curve() != _curve(ids=None)
    assert _curve() != _curve(name="eur")
    assert _curve() != "usd"
    # By the bits they hold: so one of NaN equals itself, and 0.0 is not -0.0
    assert _curve(points=np.array([np.nan])) == _curve(points=np.array([np.nan]))
    assert _curve(points=np.array([0.0])) != _curve(points=np.array([-0.0]))
    # And so are what hold them
    assert Book((_curve(),), _curve()) == Book((_curve(),), _curve())
    assert Book((_curve(),)) != Book((_empty(),))
    # Nor are those of the same numbers, in another shape
    assert _surface() == _surface() and hash(_surface()) == hash(_surface())
    assert _surface() != _surface(values=np.arange(6.0).reshape(3, 2))
    assert _surface() != _surface(image=None)
    assert Book(surface=_surface()) == Book(surface=_surface()) != Book()


def test_annotations_as_strings(make_module):
    module = make_module(
        """
        from __future__ import annotations
        from dataclasses import dataclass
        import numpy as np
        from dataclass_native import native
        from dataclass_native.arrays import Matrix

        @native
        @dataclass(frozen=True)
        class Path:
            points: Matrix[np.float64] | None
            # Not yet defined, when Path is
            then: Later | None = None

        @native
        @dataclass(frozen=True)
        class Later:
            name: str
        """
    )
    # Only a class with arrays is compared by them
    assert "_compare_arrays" in module.Path.__eq__.__qualname__
    assert "_compare_arrays" not in module.Later.__eq__.__qualname__
    if mode() is not Mode.STRICT:
        # Which has no extension, so is a plain dataclass
        path = module.Path(np.zeros((1, 2)), module.Later("x"))
        assert path == module.Path(np.zeros((1, 2)), module.Later("x"))
        assert path != module.Path(None) and hash(path) == hash(replace(path))


def test_dataclass_functions():
    curve = _curve()
    assert replace(curve, name="eur") == _curve(name="eur")
    assert asdict(curve)["points"].tolist() == [0.0, 1.0, 2.0]
    assert copy.copy(curve) == curve == copy.deepcopy(curve)
    assert pickle.loads(pickle.dumps(curve)) == curve
    assert repr(curve).startswith("Curve(name='usd', points=array([0., 1., 2.]), ")


@bound_only
@pytest.mark.parametrize(
    "points",
    [
        [0.0, 1.0],
        (0.0,),
        None,
        np.arange(3),
        np.zeros(2, np.float32),
        np.zeros(2, ">f8"),
        np.zeros((2, 2)),
        np.float64(1.0),
        np.ma.masked_array([1.0]),
    ],
)
def test_invalid_values(points):
    with pytest.raises(TypeError, match="Curve.points cannot be "):
        _curve(points=points)


@bound_only
@pytest.mark.parametrize(
    "values",
    [
        [[0.0, 1.0]],
        np.zeros(4),
        np.zeros((2, 2, 1)),
        np.zeros((2, 2), np.float32),
        np.ma.masked_array([[1.0]]),
    ],
)
def test_invalid_values_of_more_dimensions(values):
    with pytest.raises(TypeError, match="Surface.values cannot be "):
        _surface(values=values)


def test_msgpack():
    curve = _curve()
    data = to_msgpack(curve)
    # As the tuple of its numbers is
    assert unpack(data) == {
        "name": "usd",
        "points": [0.0, 1.0, 2.0],
        "weights": [0.5],
        "ids": [1, -2],
        "octets": b"\x00\xff",
    }
    assert write(curve) == data
    assert data[data.index(b"weights") + 7 :][:6] == b"\x91\xca\x3f\x00\x00\x00"
    for reading in (from_msgpack, read):
        result = reading(Curve, data)
        assert result == curve and not result.points.flags.writeable

    book = Book((curve, _empty()), curve)
    assert from_msgpack(Book, to_msgpack(book)) == book
    assert write(book) == to_msgpack(book)
    big = _curve(
        points=np.linspace(-1.0, 1.0, 5000), ids=np.arange(70000, dtype=np.int32)
    )
    assert from_msgpack(Curve, to_msgpack(big)) == big and write(big) == to_msgpack(big)

    for invalid in ({"points": [1.0, "a"]}, {"points": None}, {"points": [1]}):
        with pytest.raises(ValueError, match="Curve.points cannot be read"):
            from_msgpack(Curve, _packed({"name": "x"} | invalid))
    with pytest.raises(ValueError, match="Curve.ids cannot be read"):
        from_msgpack(Curve, _packed({"name": "x", "points": [], "ids": [2**31]}))


def test_msgpack_of_more_dimensions():
    surface = _surface()
    data = to_msgpack(surface)
    # Its shape, then its numbers as those of one dimension are
    assert unpack(data) == {
        "values": [[2, 3], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]],
        "counts": [[2, 1], [1, -2]],
        "image": [[2, 1, 2], b"\x00\x01\x02\x03"],
    }
    assert write(surface) == data
    for reading in (from_msgpack, read):
        result = reading(Surface, data)
        assert result == surface and not result.values.flags.writeable
        assert result.values.shape == (2, 3)

    # In the order of its rows, whatever the order it is in memory
    turned = _surface(values=np.asfortranarray(surface.values), counts=None)
    assert unpack(to_msgpack(turned))["values"] == unpack(data)["values"]
    for other in (
        _surface(values=np.zeros((0, 3)), image=np.zeros((2, 0, 5), np.uint8)),
        _surface(values=np.linspace(0.0, 1.0, 6000).reshape(60, 100), image=None),
    ):
        result = from_msgpack(Surface, to_msgpack(other))
        assert result == other and result.values.shape == other.values.shape
        assert write(other) == to_msgpack(other)
    book = Book((_curve(),), surface=surface)
    assert from_msgpack(Book, to_msgpack(book)) == book

    for invalid in (
        [1.0, 2.0],
        [[2], [1.0, 2.0]],
        [[2, 2], [1.0, 2.0]],
        [[-1, -2], [1.0, 2.0]],
        [[2**32, 2**32], []],
        [[1, 2.0], [1.0, 2.0]],
        [[1, 2], [1.0, 2.0], []],
        [[1, 2], None],
        None,
    ):
        with pytest.raises(ValueError, match="Surface.values cannot be read"):
            from_msgpack(Surface, _packed({"values": invalid}))


def test_arrow():
    curves = (_curve(), _empty(), _curve(points=np.linspace(0.0, 1.0, 100)))
    tables = to_arrow(curves)
    tables.root.validate(full=True)
    schema = tables.root.schema
    assert schema.field("points").type.equals(pa.list_(pa.float64()))
    assert schema.field("weights").type.equals(pa.list_(pa.float32()))
    assert schema.field("ids").type.equals(pa.list_(pa.int32()))
    assert schema.field("octets").type.equals(pa.binary())
    assert tables.root.slice(0, 2).to_pylist() == [
        {
            "name": "usd",
            "points": [0.0, 1.0, 2.0],
            "weights": [0.5],
            "ids": [1, -2],
            "octets": b"\x00\xff",
        },
        {"name": "empty", "points": [], "weights": None, "ids": None, "octets": None},
    ]
    assert write_arrow(curves).root.equals(tables.root)

    for reading in (from_arrow, read_arrow):
        result = reading(Curve, tables)
        assert result == curves and not result[0].points.flags.writeable
    # From within a table
    assert from_arrow(Curve, ArrowTables(tables.root.slice(1), {})) == curves[1:]

    books = (Book(curves, curves[0]), Book())
    assert from_arrow(Book, to_arrow(books)) == books
    assert write_arrow(books).root.equals(to_arrow(books).root)

    # A number which is null is not one
    points = pa.array([[1.0, None]], pa.list_(pa.float64()))
    table = pa.table({"name": ["x"], "points": points})
    with pytest.raises(ValueError, match="Curve.points cannot be read"):
        from_arrow(Curve, ArrowTables(table, {}))


def test_arrow_of_more_dimensions():
    surfaces = (
        _surface(),
        _surface(values=np.zeros((0, 3)), counts=None, image=None),
        _surface(values=np.linspace(0.0, 1.0, 600).reshape(20, 30)),
    )
    tables = to_arrow(surfaces)
    tables.root.validate(full=True)
    schema = tables.root.schema

    def shaped(numbers: pa.DataType, dimensions: int) -> pa.DataType:
        shape = pa.list_(pa.int32(), dimensions)
        return pa.struct([("data", numbers), ("shape", shape)])

    # As arrow.variable_shape_tensor is stored
    values = shaped(pa.list_(pa.float64()), 2)
    assert schema.field("values").type.equals(values)
    assert schema.field("counts").type.equals(shaped(pa.list_(pa.int16()), 2))
    assert schema.field("image").type.equals(shaped(pa.binary(), 3))
    assert tables.root.slice(0, 2).to_pylist() == [
        {
            "values": {"data": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0], "shape": [2, 3]},
            "counts": {"data": [1, -2], "shape": [2, 1]},
            "image": {"data": b"\x00\x01\x02\x03", "shape": [2, 1, 2]},
        },
        {"values": {"data": [], "shape": [0, 3]}, "counts": None, "image": None},
    ]
    assert write_arrow(surfaces).root.equals(tables.root)

    for reading in (from_arrow, read_arrow):
        result = reading(Surface, tables)
        assert result == surfaces and not result[0].values.flags.writeable
        assert [r.values.shape for r in result] == [(2, 3), (0, 3), (20, 30)]
    assert from_arrow(Surface, ArrowTables(tables.root.slice(1), {})) == surfaces[1:]
    books = (Book(surface=surfaces[0]), Book((_curve(),)))
    assert from_arrow(Book, to_arrow(books)) == books

    for invalid in (
        {"data": [1.0, 2.0], "shape": [2, 2]},
        {"data": [1.0, 2.0], "shape": [-1, -2]},
        {"data": [1.0, 2.0], "shape": [1, None]},
        {"data": [1.0, 2.0], "shape": None},
        {"data": None, "shape": [0, 0]},
        {"data": [1.0, None], "shape": [1, 2]},
        None,
    ):
        table = pa.table({"values": pa.array([invalid], values)})
        with pytest.raises(ValueError, match="Surface.values cannot be read"):
            from_arrow(Surface, ArrowTables(table, {}))
    # Of a shape which is not of as many dimensions
    cube = pa.array(
        [{"data": [1.0, 2.0], "shape": [1, 2, 1]}], shaped(pa.list_(pa.float64()), 3)
    )
    with pytest.raises(ValueError, match="Surface.values cannot be read"):
        from_arrow(Surface, ArrowTables(pa.table({"values": cube}), {}))


def test_invalid_field():
    # In a bound mode, it is making the object which raises: a bound class holds only what is of its fields' types
    invalid: Any = [1.0]
    with pytest.raises(TypeError, match=r"Curve.points cannot be \[1.0\]"):
        to_msgpack(_curve(points=invalid))
    with pytest.raises(TypeError, match=r"Curve.points cannot be \[1.0\]"):
        to_arrow((_curve(points=invalid),))


def test_native_function():
    scaled = functions.scaled(np.arange(3.0), 2.0)
    assert type(scaled) is np.ndarray and scaled.tolist() == [0.0, 2.0, 4.0]
    # Of what the native function returned, not copied
    assert scaled.flags.writeable is not BOUND
    assert functions.scaled(_curve().points, 0.5).tolist() == [0.0, 0.5, 1.0]
    if BOUND:
        invalid: Any = [1.0]
        with pytest.raises(TypeError, match="scaled: points cannot be"):
            functions.scaled(invalid, 2.0)

    turned = functions.transposed(np.arange(6.0).reshape(2, 3))
    assert turned.shape == (3, 2) and turned.tolist() == [[0, 3], [1, 4], [2, 5]]
    if BOUND:
        assert turned.flags.c_contiguous
        # Which a type checker rejects too
        flat: Any = np.arange(6.0)
        with pytest.raises(TypeError, match="transposed: values cannot be"):
            functions.transposed(flat)
