"""
The msgpack format, as much of it as the wire format uses: see WIRE_FORMAT.md. Written here, rather than taken from
the msgpack package, as it is small, and what is written must not vary with that package's version or options.
"""

import struct
from typing import Any, NamedTuple


class Ext(NamedTuple):
    """An extension value"""

    code: int
    data: bytes


def pack_nil(out: bytearray):
    out.append(0xC0)


def pack_bool(out: bytearray, value: bool):
    out.append(0xC3 if value else 0xC2)


def pack_int(out: bytearray, value: int):
    """In as few bytes as hold it. Raises struct.error if it is not within 64 bits"""
    if -32 <= value < 128:
        out.append(value & 0xFF)
    elif value >= 0:
        if value < 1 << 8:
            out += struct.pack(">BB", 0xCC, value)
        elif value < 1 << 16:
            out += struct.pack(">BH", 0xCD, value)
        elif value < 1 << 32:
            out += struct.pack(">BI", 0xCE, value)
        else:
            out += struct.pack(">BQ", 0xCF, value)
    elif value >= -(1 << 7):
        out += struct.pack(">Bb", 0xD0, value)
    elif value >= -(1 << 15):
        out += struct.pack(">Bh", 0xD1, value)
    elif value >= -(1 << 31):
        out += struct.pack(">Bi", 0xD2, value)
    else:
        out += struct.pack(">Bq", 0xD3, value)


def pack_float(out: bytearray, value: float, bits: int = 64):
    """Raises OverflowError if it is too large for 32 bits"""
    out += (
        struct.pack(">Bf", 0xCA, value)
        if bits == 32
        else struct.pack(">Bd", 0xCB, value)
    )


def _pack_length(
    out: bytearray,
    length: int,
    inline: tuple[int, int] | None,
    codes: tuple[int | None, int, int],
):
    """
    A length, in the first of the forms which holds it: within the type's byte (inline: that byte, and the lengths
    it has room for), or after a byte (codes) in 1, 2 or 4 bytes
    """
    if inline and length < inline[1]:
        out.append(inline[0] | length)
    elif codes[0] is not None and length < 1 << 8:
        out += struct.pack(">BB", codes[0], length)
    elif length < 1 << 16:
        out += struct.pack(">BH", codes[1], length)
    else:
        out += struct.pack(">BI", codes[2], length)


def pack_str(out: bytearray, value: str):
    data = value.encode()
    _pack_length(out, len(data), (0xA0, 32), (0xD9, 0xDA, 0xDB))
    out += data


def pack_bin(out: bytearray, value: bytes):
    _pack_length(out, len(value), None, (0xC4, 0xC5, 0xC6))
    out += value


def pack_array(out: bytearray, length: int):
    """The start of an array, which its elements follow"""
    _pack_length(out, length, (0x90, 16), (None, 0xDC, 0xDD))


def pack_map(out: bytearray, length: int):
    """The start of a map, which its keys and values follow, alternately"""
    _pack_length(out, length, (0x80, 16), (None, 0xDE, 0xDF))


_FIXED_EXT = {1: 0xD4, 2: 0xD5, 4: 0xD6, 8: 0xD7, 16: 0xD8}


def pack_ext(out: bytearray, code: int, data: bytes):
    if fixed := _FIXED_EXT.get(len(data)):
        out.append(fixed)
    else:
        _pack_length(out, len(data), None, (0xC7, 0xC8, 0xC9))
    out += struct.pack(">b", code)
    out += data


# By first byte: the format of the length, or of the number, which follows it
_LENGTHS = {
    **{0xC4 + i: f for i, f in enumerate([">B", ">H", ">I"])},
    **{0xC7 + i: f for i, f in enumerate([">B", ">H", ">I"])},
    **{0xD9 + i: f for i, f in enumerate([">B", ">H", ">I"])},
    **{0xDC + i: f for i, f in enumerate([">H", ">I", ">H", ">I"])},
}
_NUMBERS = {
    0xCA: ">f",
    0xCB: ">d",
    **{
        0xCC + i: f
        for i, f in enumerate([">B", ">H", ">I", ">Q", ">b", ">h", ">i", ">q"])
    },
}


def unpack(data: bytes) -> Any:
    """
    The one value which data holds: None, a bool, int, float, str or bytes, a list, a dict or an Ext. Raises
    ValueError if it is not one msgpack value, or has a map with a key twice
    """
    position = 0

    def take(length: int) -> bytes:
        nonlocal position
        if position + length > len(data):
            raise ValueError("msgpack data is truncated")
        position += length
        return data[position - length : position]

    def number(fmt: str) -> Any:
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0]

    def text(length: int) -> str:
        try:
            return take(length).decode()
        except UnicodeDecodeError:
            raise ValueError("msgpack data has a str which is not UTF-8") from None

    def mapping(length: int) -> dict[Any, Any]:
        items = {}
        for _ in range(length):
            key = value()
            try:
                duplicate = key in items
            except TypeError:
                raise ValueError(
                    "msgpack data has a map keyed by an array or a map"
                ) from None
            if duplicate:
                raise ValueError(f"msgpack data has a map with the key {key!r} twice")
            items[key] = value()
        return items

    def value() -> Any:
        first = take(1)[0]
        if first < 0x80:
            return first
        if first >= 0xE0:
            return first - 0x100
        if first < 0x90:
            return mapping(first & 0x0F)
        if first < 0xA0:
            return [value() for _ in range(first & 0x0F)]
        if first < 0xC0:
            return text(first & 0x1F)
        if first == 0xC0:
            return None
        if first in (0xC2, 0xC3):
            return first == 0xC3
        if first in _NUMBERS:
            return number(_NUMBERS[first])
        if first in _FIXED_EXT.values():
            code = number(">b")
            return Ext(code, take(1 << (first - 0xD4)))
        if first not in _LENGTHS:
            raise ValueError(f"msgpack data has the unused byte {first:#x}")

        length = number(_LENGTHS[first])
        if first <= 0xC6:
            return take(length)
        if first <= 0xC9:
            code = number(">b")
            return Ext(code, take(length))
        if first <= 0xDB:
            return text(length)
        if first <= 0xDD:
            return [value() for _ in range(length)]
        return mapping(length)

    try:
        result = value()
    except RecursionError:
        raise ValueError("msgpack data is too deeply nested") from None
    if position != len(data):
        raise ValueError("msgpack data has more than one value")
    return result
