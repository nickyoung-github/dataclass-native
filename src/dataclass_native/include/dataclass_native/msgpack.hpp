// Writes and reads values of the generated types as msgpack, in the wire format of WIRE_FORMAT.md:
//
//   dataclass_native::Bytes data = dataclass_native::msgpack::to_msgpack(trade);
//   auto trade = dataclass_native::msgpack::from_msgpack<Trade>(data);
//
// of a generated struct, or of any type a field of one may have. What is written is, byte for byte, what the Python
// implementation writes. Nothing here depends on Python, or on a msgpack library.
#pragma once

#include <array>
#include <bit>
#include <chrono>
#include <concepts>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <variant>
#include <vector>

#include "dataclass_native/reflect.hpp"
#include "dataclass_native/types.hpp"

namespace dataclass_native::msgpack
{
    // What is read is not msgpack, or not the type being read as written; or a value cannot be written
    class Error : public std::invalid_argument
    {
    public:
        using std::invalid_argument::invalid_argument;
    };

    namespace detail
    {
        // Thrown where what is read is not of the type expected. Caught by what is reading the field of a struct, or
        // the whole value, which names it in an Error
        struct Mismatch
        {
        };

        // The kinds of msgpack value by which the members of a plain union are told apart
        enum class Kind
        {
            Nil,
            Bool,
            Int,
            Float,
            Str,
            Bin,
            Array,
            Map,
            Timestamp,
            Reference,
            Other,
        };

        inline constexpr std::int8_t timestamp_type = -1;
        // A reference to a shared object: the index of its table, and its row in it
        inline constexpr std::int8_t reference_type = 1;
        inline constexpr int max_depth = 256;
        inline constexpr std::int64_t day_microseconds = 86'400'000'000;

        [[noreturn]] inline void truncated()
        {
            throw Error("msgpack data is truncated");
        }

        inline bool is_utf8(std::string_view text)
        {
            const auto* at = reinterpret_cast<const unsigned char*>(text.data());
            const auto* end = at + text.size();
            while (at != end)
            {
                const unsigned first = *at++;
                if (first < 0x80)
                    continue;
                // The bytes which follow, and the least code point which needs that many
                int more = 0;
                char32_t least = 0;
                char32_t point = 0;
                if ((first & 0xE0) == 0xC0)
                    more = 1, least = 0x80, point = first & 0x1F;
                else if ((first & 0xF0) == 0xE0)
                    more = 2, least = 0x800, point = first & 0x0F;
                else if ((first & 0xF8) == 0xF0)
                    more = 3, least = 0x10000, point = first & 0x07;
                else
                    return false;
                if (end - at < more)
                    return false;
                for (; more; --more, ++at)
                {
                    if ((*at & 0xC0) != 0x80)
                        return false;
                    point = (point << 6) | (*at & 0x3F);
                }
                if (point < least || point > 0x10FFFF || (point >= 0xD800 && point < 0xE000))
                    return false;
            }
            return true;
        }

        class Writer
        {
        public:
            // The objects of a class which are shared, as written
            struct Table
            {
                std::string_view tag;
                std::vector<Bytes> rows;
                // By the address of each object, its row
                std::unordered_map<const void*, std::uint32_t> index;
            };

            Bytes out;
            // In the order first used
            std::vector<Table> tables;

            void byte(unsigned value)
            {
                out.push_back(static_cast<std::uint8_t>(value));
            }

            template <std::unsigned_integral U>
            void big_endian(U value)
            {
                std::uint8_t bytes[sizeof(U)];
                for (std::size_t i = 0; i < sizeof(U); ++i)
                    bytes[i] = static_cast<std::uint8_t>(value >> ((sizeof(U) - 1 - i) * 8));
                out.insert(out.end(), bytes, bytes + sizeof(U));
            }

            // The byte which says what follows, and that: at once, as an array is of many
            template <std::unsigned_integral U>
            void tagged(std::uint8_t tag, U value)
            {
                std::uint8_t bytes[1 + sizeof(U)]{tag};
                for (std::size_t i = 0; i < sizeof(U); ++i)
                    bytes[1 + i] = static_cast<std::uint8_t>(value >> ((sizeof(U) - 1 - i) * 8));
                out.insert(out.end(), bytes, bytes + sizeof(bytes));
            }

            void nil()
            {
                byte(0xC0);
            }

            void boolean(bool value)
            {
                byte(value ? 0xC3 : 0xC2);
            }

            // In as few bytes as hold it
            void unsigned_integer(std::uint64_t value)
            {
                if (value < 0x80)
                    byte(static_cast<unsigned>(value));
                else if (value <= 0xFF)
                    byte(0xCC), big_endian(static_cast<std::uint8_t>(value));
                else if (value <= 0xFFFF)
                    byte(0xCD), big_endian(static_cast<std::uint16_t>(value));
                else if (value <= 0xFFFF'FFFF)
                    byte(0xCE), big_endian(static_cast<std::uint32_t>(value));
                else
                    byte(0xCF), big_endian(value);
            }

            void integer(std::int64_t value)
            {
                if (value >= 0)
                    unsigned_integer(static_cast<std::uint64_t>(value));
                else if (value >= -32)
                    byte(static_cast<unsigned>(value) & 0xFF);
                else if (value >= std::numeric_limits<std::int8_t>::min())
                    byte(0xD0), big_endian(static_cast<std::uint8_t>(value));
                else if (value >= std::numeric_limits<std::int16_t>::min())
                    byte(0xD1), big_endian(static_cast<std::uint16_t>(value));
                else if (value >= std::numeric_limits<std::int32_t>::min())
                    byte(0xD2), big_endian(static_cast<std::uint32_t>(value));
                else
                    byte(0xD3), big_endian(static_cast<std::uint64_t>(value));
            }

            void float32(float value)
            {
                tagged(0xCA, std::bit_cast<std::uint32_t>(value));
            }

            void float64(double value)
            {
                tagged(0xCB, std::bit_cast<std::uint64_t>(value));
            }

            void str(std::string_view value)
            {
                if (!is_utf8(value))
                    throw Error("A str which is not UTF-8 cannot be written");
                length(value.size(), 0xA0, 32, 0xD9);
                out.insert(out.end(), value.begin(), value.end());
            }

            void bin(std::span<const std::uint8_t> value)
            {
                length(value.size(), 0, 0, 0xC4);
                out.insert(out.end(), value.begin(), value.end());
            }

            // The start of an array, which its elements follow
            void array(std::size_t size)
            {
                length(size, 0x90, 16, 0xDC - 1);
            }

            // The start of a map, which its keys and values follow, alternately
            void map(std::size_t size)
            {
                length(size, 0x80, 16, 0xDE - 1);
            }

            // An extension value of 4, 8 or 12 bytes, which follow
            void extension(std::int8_t type, std::size_t size)
            {
                if (size == 12)
                    byte(0xC7), byte(12);
                else
                    byte(size == 4 ? 0xD6 : 0xD7);
                byte(static_cast<std::uint8_t>(type));
            }

            std::uint32_t table(std::string_view tag)
            {
                for (std::size_t i = 0; i != tables.size(); ++i)
                    if (tables[i].tag == tag)
                        return static_cast<std::uint32_t>(i);
                tables.push_back({tag, {}, {}});
                return static_cast<std::uint32_t>(tables.size() - 1);
            }

        private:
            // A length, in the first of the forms which holds it: within the type's byte (short_form, if less than
            // short_limit), or after a byte in 1 (code, unless an array's or a map's), 2 or 4 bytes
            void length(std::size_t size, unsigned short_form, std::size_t short_limit, unsigned code)
            {
                const bool one_byte = code == 0xC4 || code == 0xD9;
                if (size < short_limit)
                    byte(short_form | static_cast<unsigned>(size));
                else if (one_byte && size <= 0xFF)
                    byte(code), big_endian(static_cast<std::uint8_t>(size));
                else if (size <= 0xFFFF)
                    byte(code + 1), big_endian(static_cast<std::uint16_t>(size));
                else if (size <= 0xFFFF'FFFF)
                    byte(code + 2), big_endian(static_cast<std::uint32_t>(size));
                else
                    throw Error("A value of more than 4294967295 elements or bytes cannot be written");
            }
        };

        class Reader
        {
        public:
            // A shared object, as written, and once read
            struct Row
            {
                const std::uint8_t* at;
                std::shared_ptr<const void> object;
                bool reading = false;
            };

            struct Table
            {
                std::string_view tag;
                std::vector<Row> rows;
            };

            const std::uint8_t* at;
            const std::uint8_t* end;
            int depth = 0;
            std::vector<Table> tables;

            Reader(const std::uint8_t* at, const std::uint8_t* end) : at(at), end(end)
            {
            }

            // While one exists, what is read is within an array or a map
            class Nested
            {
            public:
                explicit Nested(Reader& reader) : reader_(reader)
                {
                    if (++reader_.depth > max_depth)
                        throw Error("msgpack data is too deeply nested");
                }

                Nested(const Nested&) = delete;
                Nested& operator=(const Nested&) = delete;

                ~Nested()
                {
                    --reader_.depth;
                }

            private:
                Reader& reader_;
            };

            std::size_t remaining() const
            {
                return static_cast<std::size_t>(end - at);
            }

            std::uint8_t peek(std::size_t offset = 0) const
            {
                if (remaining() <= offset)
                    truncated();
                return at[offset];
            }

            std::span<const std::uint8_t> take(std::size_t size)
            {
                if (remaining() < size)
                    truncated();
                at += size;
                return {at - size, size};
            }

            template <std::unsigned_integral U>
            U big_endian()
            {
                U value = 0;
                for (const std::uint8_t byte : take(sizeof(U)))
                    value = static_cast<U>((value << 8) | byte);
                return value;
            }

            // Of the value which is next
            Kind kind() const
            {
                const std::uint8_t first = peek();
                if (first < 0x80 || first >= 0xE0 || (first >= 0xCC && first <= 0xD3))
                    return Kind::Int;
                if (first < 0x90 || first == 0xDE || first == 0xDF)
                    return Kind::Map;
                if (first < 0xA0 || first == 0xDC || first == 0xDD)
                    return Kind::Array;
                if (first < 0xC0 || (first >= 0xD9 && first <= 0xDB))
                    return Kind::Str;
                if (first == 0xC0)
                    return Kind::Nil;
                if (first == 0xC1)
                    throw Error("msgpack data has the unused byte 0xc1");
                if (first <= 0xC3)
                    return Kind::Bool;
                if (first <= 0xC6)
                    return Kind::Bin;
                if (first == 0xCA || first == 0xCB)
                    return Kind::Float;
                // An extension: its type follows its length
                const std::size_t type_at = first >= 0xD4 ? 1 : first == 0xC7 ? 2 : first == 0xC8 ? 3 : 5;
                const auto type = static_cast<std::int8_t>(peek(type_at));
                return type == timestamp_type ? Kind::Timestamp : type == reference_type ? Kind::Reference : Kind::Other;
            }

            void expect(Kind expected) const
            {
                if (kind() != expected)
                    throw Mismatch{};
            }

            bool boolean()
            {
                expect(Kind::Bool);
                return take(1)[0] == 0xC3;
            }

            template <std::integral I>
            I integer()
            {
                expect(Kind::Int);
                const std::uint8_t first = take(1)[0];
                if (first < 0x80)
                    return in_range<I>(static_cast<std::uint64_t>(first));
                if (first >= 0xE0)
                    return in_range<I>(static_cast<std::int64_t>(static_cast<std::int8_t>(first)));
                switch (first)
                {
                case 0xCC:
                    return in_range<I>(static_cast<std::uint64_t>(big_endian<std::uint8_t>()));
                case 0xCD:
                    return in_range<I>(static_cast<std::uint64_t>(big_endian<std::uint16_t>()));
                case 0xCE:
                    return in_range<I>(static_cast<std::uint64_t>(big_endian<std::uint32_t>()));
                case 0xCF:
                    return in_range<I>(big_endian<std::uint64_t>());
                case 0xD0:
                    return in_range<I>(static_cast<std::int64_t>(static_cast<std::int8_t>(big_endian<std::uint8_t>())));
                case 0xD1:
                    return in_range<I>(
                        static_cast<std::int64_t>(static_cast<std::int16_t>(big_endian<std::uint16_t>())));
                case 0xD2:
                    return in_range<I>(
                        static_cast<std::int64_t>(static_cast<std::int32_t>(big_endian<std::uint32_t>())));
                default:
                    return in_range<I>(static_cast<std::int64_t>(big_endian<std::uint64_t>()));
                }
            }

            // Of either size
            double floating()
            {
                expect(Kind::Float);
                if (take(1)[0] == 0xCA)
                    return std::bit_cast<float>(big_endian<std::uint32_t>());
                return std::bit_cast<double>(big_endian<std::uint64_t>());
            }

            std::string_view str()
            {
                expect(Kind::Str);
                const std::uint8_t first = take(1)[0];
                const auto bytes = take(first < 0xC0 ? first & 0x1F : length(first - 0xD9));
                const std::string_view text{reinterpret_cast<const char*>(bytes.data()), bytes.size()};
                if (!is_utf8(text))
                    throw Error("msgpack data has a str which is not UTF-8");
                return text;
            }

            std::span<const std::uint8_t> bin()
            {
                expect(Kind::Bin);
                return take(length(take(1)[0] - 0xC4));
            }

            // The number of elements of the array which is next: they follow
            std::size_t array()
            {
                expect(Kind::Array);
                const std::uint8_t first = take(1)[0];
                return elements(first < 0xA0 ? first & 0x0F : length(first - 0xDC + 1), 1);
            }

            // The number of entries of the map which is next: their keys and values follow, alternately
            std::size_t map()
            {
                expect(Kind::Map);
                const std::uint8_t first = take(1)[0];
                return elements(first < 0x90 ? first & 0x0F : length(first - 0xDE + 1), 2);
            }

            // The bytes of the extension value which is next, of either of our two types
            std::span<const std::uint8_t> extension()
            {
                const std::uint8_t first = take(1)[0];
                const std::size_t size = first >= 0xD4 ? std::size_t{1} << (first - 0xD4) : length(first - 0xC7);
                take(1);
                return take(size);
            }

            // The table and row which the reference which is next is to
            std::array<std::uint32_t, 2> reference()
            {
                expect(Kind::Reference);
                const auto bytes = extension();
                if (bytes.size() != 8)
                    throw Mismatch{};
                Reader fields{bytes.data(), bytes.data() + 8};
                const auto table = fields.big_endian<std::uint32_t>();
                const auto row = fields.big_endian<std::uint32_t>();
                if (table >= tables.size() || row >= tables[table].rows.size())
                    throw Mismatch{};
                return {table, row};
            }

            // Passes over the value which is next
            void skip()
            {
                const std::uint8_t first = peek();
                switch (kind())
                {
                case Kind::Nil:
                case Kind::Bool:
                    take(1);
                    break;
                case Kind::Int:
                    take(first < 0xCC || first >= 0xE0 ? 1 : 1 + (std::size_t{1} << ((first - 0xCC) & 3)));
                    break;
                case Kind::Float:
                    take(first == 0xCA ? 5 : 9);
                    break;
                case Kind::Str:
                    str();
                    break;
                case Kind::Bin:
                    bin();
                    break;
                case Kind::Array:
                case Kind::Map:
                {
                    const Nested nested(*this);
                    const bool is_map = kind() == Kind::Map;
                    for (std::size_t values = is_map ? 2 * map() : array(); values; --values)
                        skip();
                    break;
                }
                default:
                    extension();
                }
            }

        private:
            template <std::integral I, std::integral V>
            static I in_range(V value)
            {
                if (!std::in_range<I>(value))
                    throw Mismatch{};
                return static_cast<I>(value);
            }

            // A length held in 1, 2 or 4 bytes
            std::size_t length(int form)
            {
                return form == 0   ? big_endian<std::uint8_t>()
                       : form == 1 ? big_endian<std::uint16_t>()
                                   : big_endian<std::uint32_t>();
            }

            // A number of elements, each of which is at least so many bytes: so not more than there is data for
            std::size_t elements(std::size_t count, std::size_t each) const
            {
                if (count > remaining() / each)
                    truncated();
                return count;
            }
        };

        // Writes and reads a type. Each has write() and read(), and those which may be a member of a union:
        //
        //   kind     The kind of msgpack value it is written as, by which the members of a plain union are told apart
        //   tag()    What names it in a discriminated union
        //
        // shares is whether a value may hold, however deeply, a Shared one: if so, it is written as a bundle
        template <class T>
        struct Codec;

        template <>
        struct Codec<bool>
        {
            static constexpr Kind kind = Kind::Bool;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "bool";
            }

            static void write(Writer& writer, bool value)
            {
                writer.boolean(value);
            }

            static bool read(Reader& reader)
            {
                return reader.boolean();
            }
        };

        template <std::integral T>
        struct Codec<T>
        {
            static constexpr Kind kind = Kind::Int;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                constexpr std::string_view names[] = {"int8",  "int16",  "int32",  "int",
                                                      "uint8", "uint16", "uint32", "uint64"};
                return names[(std::unsigned_integral<T> ? 4 : 0) + std::countr_zero(sizeof(T))];
            }

            static void write(Writer& writer, T value)
            {
                if constexpr (std::unsigned_integral<T>)
                    writer.unsigned_integer(value);
                else
                    writer.integer(value);
            }

            static T read(Reader& reader)
            {
                return reader.integer<T>();
            }
        };

        template <std::floating_point T>
        struct Codec<T>
        {
            static constexpr Kind kind = Kind::Float;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return sizeof(T) == 4 ? "float32" : "float";
            }

            static void write(Writer& writer, T value)
            {
                if constexpr (sizeof(T) == 4)
                    writer.float32(value);
                else
                    writer.float64(value);
            }

            static T read(Reader& reader)
            {
                return static_cast<T>(reader.floating());
            }
        };

        template <>
        struct Codec<std::string>
        {
            static constexpr Kind kind = Kind::Str;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "str";
            }

            static void write(Writer& writer, const std::string& value)
            {
                writer.str(value);
            }

            static std::string read(Reader& reader)
            {
                return std::string(reader.str());
            }
        };

        // bytes, and a tuple of UInt8
        template <>
        struct Codec<Bytes>
        {
            static constexpr Kind kind = Kind::Bin;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "bytes";
            }

            static void write(Writer& writer, const Bytes& value)
            {
                writer.bin(value);
            }

            static Bytes read(Reader& reader)
            {
                const auto bytes = reader.bin();
                return {bytes.begin(), bytes.end()};
            }
        };

        // As days since 1970-01-01
        template <>
        struct Codec<Date>
        {
            static constexpr Kind kind = Kind::Int;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "date";
            }

            static void write(Writer& writer, Date value)
            {
                writer.integer(value.time_since_epoch().count());
            }

            static Date read(Reader& reader)
            {
                return Date{std::chrono::days{reader.integer<std::chrono::days::rep>()}};
            }
        };

        // As microseconds since midnight
        template <>
        struct Codec<TimeOfDay>
        {
            static constexpr Kind kind = Kind::Int;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "time";
            }

            static void write(Writer& writer, TimeOfDay value)
            {
                const std::int64_t microseconds = value.since_midnight.count();
                if (microseconds < 0 || microseconds >= day_microseconds)
                    throw Error("A time which is not within a day cannot be written");
                writer.integer(microseconds);
            }

            static TimeOfDay read(Reader& reader)
            {
                const auto microseconds = reader.integer<std::int64_t>();
                if (microseconds < 0 || microseconds >= day_microseconds)
                    throw Mismatch{};
                return {std::chrono::microseconds{microseconds}};
            }
        };

        // As microseconds
        template <>
        struct Codec<TimeDelta>
        {
            static constexpr Kind kind = Kind::Int;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "timedelta";
            }

            static void write(Writer& writer, TimeDelta value)
            {
                writer.integer(value.count());
            }

            static TimeDelta read(Reader& reader)
            {
                return TimeDelta{reader.integer<std::int64_t>()};
            }
        };

        // As msgpack's timestamp, in the shortest of its three forms which holds it
        template <>
        struct Codec<DateTime>
        {
            static constexpr Kind kind = Kind::Timestamp;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return "datetime";
            }

            static void write(Writer& writer, DateTime value)
            {
                const std::int64_t microseconds = value.time_since_epoch().count();
                // Rounding down, so that the nanoseconds are not negative
                const std::int64_t seconds = microseconds / 1'000'000 - (microseconds % 1'000'000 < 0);
                const auto nanoseconds = static_cast<std::uint32_t>((microseconds - seconds * 1'000'000) * 1000);
                if (seconds < 0 || seconds >= std::int64_t{1} << 34)
                {
                    writer.extension(timestamp_type, 12);
                    writer.big_endian(nanoseconds);
                    writer.big_endian(static_cast<std::uint64_t>(seconds));
                }
                else if (nanoseconds || seconds >= std::int64_t{1} << 32)
                {
                    writer.extension(timestamp_type, 8);
                    writer.big_endian(std::uint64_t{nanoseconds} << 34 | static_cast<std::uint64_t>(seconds));
                }
                else
                {
                    writer.extension(timestamp_type, 4);
                    writer.big_endian(static_cast<std::uint32_t>(seconds));
                }
            }

            static DateTime read(Reader& reader)
            {
                reader.expect(Kind::Timestamp);
                const auto bytes = reader.extension();
                Reader fields{bytes.data(), bytes.data() + bytes.size()};
                std::uint32_t nanoseconds = 0;
                std::int64_t seconds = 0;
                if (bytes.size() == 4)
                    seconds = fields.big_endian<std::uint32_t>();
                else if (bytes.size() == 8)
                {
                    const auto both = fields.big_endian<std::uint64_t>();
                    nanoseconds = static_cast<std::uint32_t>(both >> 34);
                    seconds = static_cast<std::int64_t>(both & ((std::uint64_t{1} << 34) - 1));
                }
                else if (bytes.size() == 12)
                {
                    nanoseconds = fields.big_endian<std::uint32_t>();
                    seconds = static_cast<std::int64_t>(fields.big_endian<std::uint64_t>());
                }
                else
                    throw Mismatch{};

                // A whole number of microseconds, which are within 64 bits
                constexpr std::int64_t max_seconds = std::numeric_limits<std::int64_t>::max() / 1'000'000 - 1;
                if (nanoseconds >= 1'000'000'000 || nanoseconds % 1000 || seconds < -max_seconds ||
                    seconds > max_seconds)
                    throw Mismatch{};
                return DateTime{std::chrono::microseconds{seconds * 1'000'000 + nanoseconds / 1000}};
            }
        };

        // By name
        template <Enum T>
        struct Codec<T>
        {
            static constexpr Kind kind = Kind::Str;
            static constexpr bool shares = false;

            static constexpr std::string_view tag()
            {
                return Reflect<T>::tag;
            }

            static void write(Writer& writer, T value)
            {
                for (const auto& member : Reflect<T>::members)
                    if (member.value == value)
                        return writer.str(member.name);
                throw Error("A value which is not a member of " + std::string(Reflect<T>::name) +
                            " cannot be written");
            }

            static T read(Reader& reader)
            {
                const std::string_view name = reader.str();
                for (const auto& member : Reflect<T>::members)
                    if (member.name == name)
                        return member.value;
                throw Mismatch{};
            }
        };

        template <class T>
        struct Codec<std::optional<T>>
        {
            static constexpr bool shares = Codec<T>::shares;

            static void write(Writer& writer, const std::optional<T>& value)
            {
                if (value)
                    Codec<T>::write(writer, *value);
                else
                    writer.nil();
            }

            static std::optional<T> read(Reader& reader)
            {
                if (reader.kind() != Kind::Nil)
                    return Codec<T>::read(reader);
                reader.take(1);
                return std::nullopt;
            }
        };

        template <class T>
        struct Codec<std::vector<T>>
        {
            static constexpr Kind kind = Kind::Array;
            static constexpr bool shares = Codec<T>::shares;

            static void write(Writer& writer, const std::vector<T>& value)
            {
                writer.array(value.size());
                for (const auto& element : value)
                    Codec<T>::write(writer, element);
            }

            static std::vector<T> read(Reader& reader)
            {
                const Reader::Nested nested(reader);
                std::vector<T> value;
                std::size_t size = reader.array();
                value.reserve(size);
                for (; size; --size)
                    value.push_back(Codec<T>::read(reader));
                return value;
            }
        };

        // An array of numbers: as the tuple of them is, after its shape if it is of more than one dimension
        template <class T, std::size_t N>
        struct Codec<Array<T, N>>
        {
            static constexpr bool shares = false;

            static void write(Writer& writer, const Array<T, N>& value)
            {
                writer.out.reserve(writer.out.size() + value->size() * (sizeof(T) + 1));
                if constexpr (N > 1)
                {
                    writer.array(2);
                    writer.array(N);
                    for (const std::size_t size : value.shape())
                        writer.unsigned_integer(size);
                }
                Codec<std::vector<T>>::write(writer, *value);
            }

            static Array<T, N> read(Reader& reader)
            {
                if constexpr (N == 1)
                    return Codec<std::vector<T>>::read(reader);
                else
                {
                    const Reader::Nested nested(reader);
                    if (reader.array() != 2 || reader.array() != N)
                        throw Mismatch{};
                    typename Array<T, N>::Shape shape;
                    for (std::size_t& size : shape)
                        size = reader.integer<std::size_t>();
                    std::vector<T> values = Codec<std::vector<T>>::read(reader);
                    if (!Array<T, N>::fits(shape, values.size()))
                        throw Mismatch{};
                    return {std::move(values), shape};
                }
            }
        };

        // In ascending order, which is that it is held in
        template <class T>
        struct Codec<std::set<T>>
        {
            static constexpr Kind kind = Kind::Array;
            static constexpr bool shares = false;

            static void write(Writer& writer, const std::set<T>& value)
            {
                writer.array(value.size());
                for (const auto& element : value)
                    Codec<T>::write(writer, element);
            }

            static std::set<T> read(Reader& reader)
            {
                const Reader::Nested nested(reader);
                std::set<T> value;
                for (std::size_t size = reader.array(); size; --size)
                    value.insert(Codec<T>::read(reader));
                return value;
            }
        };

        // In ascending order of key
        template <class K, class V>
        struct Codec<std::map<K, V>>
        {
            static constexpr Kind kind = Kind::Map;
            static constexpr bool shares = Codec<V>::shares;

            static void write(Writer& writer, const std::map<K, V>& value)
            {
                writer.map(value.size());
                for (const auto& [key, element] : value)
                {
                    Codec<K>::write(writer, key);
                    Codec<V>::write(writer, element);
                }
            }

            static std::map<K, V> read(Reader& reader)
            {
                const Reader::Nested nested(reader);
                std::map<K, V> value;
                for (std::size_t size = reader.map(); size; --size)
                {
                    K key = Codec<K>::read(reader);
                    if (!value.emplace(std::move(key), Codec<V>::read(reader)).second)
                        throw Error("msgpack data has a map with a key twice");
                }
                return value;
            }
        };

        template <class... Ts>
        struct Codec<std::tuple<Ts...>>
        {
            static constexpr Kind kind = Kind::Array;
            static constexpr bool shares = (Codec<Ts>::shares || ...);

            static void write(Writer& writer, const std::tuple<Ts...>& value)
            {
                writer.array(sizeof...(Ts));
                std::apply([&](const Ts&... elements) { (Codec<Ts>::write(writer, elements), ...); }, value);
            }

            static std::tuple<Ts...> read(Reader& reader)
            {
                const Reader::Nested nested(reader);
                if (reader.array() != sizeof...(Ts))
                    throw Mismatch{};
                // In braces, so in order
                return std::tuple<Ts...>{Codec<Ts>::read(reader)...};
            }
        };

        // As its T
        template <class T>
        struct Codec<Indirect<T>>
        {
            static constexpr Kind kind = Codec<T>::kind;
            static constexpr bool shares = Codec<T>::shares;

            static constexpr std::string_view tag()
            {
                return Codec<T>::tag();
            }

            static void write(Writer& writer, const Indirect<T>& value)
            {
                if (!value)
                    throw Error("An empty Indirect cannot be written");
                Codec<T>::write(writer, *value);
            }

            static Indirect<T> read(Reader& reader)
            {
                return Indirect<T>(Codec<T>::read(reader));
            }
        };

        // As a reference to its T, which is written in the table of its class
        template <class T>
        struct Codec<Shared<T>>
        {
            static constexpr Kind kind = Kind::Reference;
            static constexpr bool shares = true;

            static constexpr std::string_view tag()
            {
                return Reflect<T>::tag;
            }

            static void write(Writer& writer, const Shared<T>& value)
            {
                if (!value)
                    throw Error("An empty Shared cannot be written");

                const std::uint32_t table = writer.table(Reflect<T>::tag);
                const auto next = static_cast<std::uint32_t>(writer.tables[table].rows.size());
                const auto [found, added] = writer.tables[table].index.emplace(value.get(), next);
                const std::uint32_t row = found->second;
                if (added)
                {
                    // Its row is taken before it is written, as it may share objects of its own class
                    writer.tables[table].rows.emplace_back();
                    Bytes rest = std::exchange(writer.out, {});
                    Codec<T>::write(writer, *value);
                    writer.tables[table].rows[row] = std::exchange(writer.out, std::move(rest));
                }

                writer.extension(reference_type, 8);
                writer.big_endian(table);
                writer.big_endian(row);
            }

            static Shared<T> read(Reader& reader)
            {
                const auto [table, row] = reader.reference();
                if (reader.tables[table].tag != Reflect<T>::tag)
                    throw Mismatch{};

                if (!reader.tables[table].rows[row].object)
                {
                    // An object which shares itself, which no object written could
                    if (std::exchange(reader.tables[table].rows[row].reading, true))
                        throw Mismatch{};
                    const std::uint8_t* rest = std::exchange(reader.at, reader.tables[table].rows[row].at);
                    auto object = std::make_shared<const T>(Codec<T>::read(reader));
                    reader.at = rest;
                    reader.tables[table].rows[row].object = std::move(object);
                }
                return Shared<T>(std::static_pointer_cast<const T>(reader.tables[table].rows[row].object));
            }
        };

        // As a map of each field's name and value
        template <Struct T>
        struct Codec<T>
        {
            static constexpr Kind kind = Kind::Map;
            static constexpr bool shares = Reflect<T>::shares;

            static constexpr std::string_view tag()
            {
                return Reflect<T>::tag;
            }

            static void write(Writer& writer, const T& value)
            {
                std::apply(
                    [&](const auto&... field)
                    {
                        writer.map(sizeof...(field));
                        (write_field(writer, value, field), ...);
                    },
                    Reflect<T>::fields);
            }

            static T read(Reader& reader)
            {
                const Reader::Nested nested(reader);
                T value{};
                std::array<bool, std::tuple_size_v<decltype(Reflect<T>::fields)>> written{};
                for (std::size_t entries = reader.map(); entries; --entries)
                {
                    // An entry which is not a field's is ignored
                    const bool named = reader.kind() == Kind::Str;
                    const std::string_view name = named ? reader.str() : std::string_view{};
                    if (!named)
                        reader.skip();
                    std::size_t index = 0;
                    const bool is_field = named && std::apply(
                                                       [&](const auto&... field)
                                                       {
                                                           return ((field.name == name
                                                                        ? (read_field(reader, value, field,
                                                                                      written[index]),
                                                                           true)
                                                                        : (++index, false)) ||
                                                                   ...);
                                                       },
                                                       Reflect<T>::fields);
                    if (!is_field)
                        reader.skip();
                }

                // A field which is not written takes its default, if it has one
                std::size_t index = 0;
                std::apply(
                    [&](const auto&... field)
                    {
                        ((written[index++] || field.has_default
                              ? void()
                              : throw Error(std::string(Reflect<T>::name) + " cannot be read: no " +
                                            std::string(field.name))),
                         ...);
                    },
                    Reflect<T>::fields);
                return value;
            }

        private:
            static std::string named(std::string_view field)
            {
                return std::string(Reflect<T>::name) + "." + std::string(field);
            }

            template <class M>
            static void write_field(Writer& writer, const T& value, const Field<T, M>& field)
            {
                writer.str(field.name);
                Codec<M>::write(writer, value.*field.member);
            }

            template <class M>
            static void read_field(Reader& reader, T& value, const Field<T, M>& field, bool& written)
            {
                if (std::exchange(written, true))
                    throw Error(named(field.name) + " is written twice");
                try
                {
                    value.*field.member = Codec<M>::read(reader);
                }
                catch (const Mismatch&)
                {
                    throw Error(named(field.name) + " cannot be read: it is not of its type, as written");
                }
            }
        };

        // What names a type in a discriminated union, or as the class of a shared object: if anything does
        template <class T>
        constexpr std::string_view tag_of()
        {
            if constexpr (requires { Codec<T>::tag(); })
                return Codec<T>::tag();
            else
                return {};
        }

        // Reads the first member of a union for which matches is true, into value
        template <class Matches, class... Ts>
        void read_member(Reader& reader, std::variant<Ts...>& value, const Matches& matches)
        {
            const bool read =
                ((matches(std::type_identity<Ts>{}) ? (value.template emplace<Ts>(Codec<Ts>::read(reader)), true)
                                                    : false) ||
                 ...);
            if (!read)
                throw Mismatch{};
        }

        // A plain union: as its member, each being written as a kind of msgpack value of its own
        template <class... Ts>
        struct Codec<std::variant<Ts...>>
        {
            static constexpr bool shares = (Codec<Ts>::shares || ...);

            static void write(Writer& writer, const std::variant<Ts...>& value)
            {
                if (value.valueless_by_exception())
                    throw Error("A variant without a value cannot be written");
                std::visit(
                    [&](const auto& member) { Codec<std::remove_cvref_t<decltype(member)>>::write(writer, member); },
                    value);
            }

            static std::variant<Ts...> read(Reader& reader)
            {
                const Kind kind = reader.kind();
                // A reference names the class of what it is to, by its table
                std::string_view tag;
                if (kind == Kind::Reference)
                {
                    const std::uint8_t* reference = reader.at;
                    tag = reader.tables[reader.reference()[0]].tag;
                    reader.at = reference;
                }

                std::variant<Ts...> value;
                read_member(reader, value,
                            [&]<class M>(std::type_identity<M>)
                            { return Codec<M>::kind == kind && (kind != Kind::Reference || tag_of<M>() == tag); });
                return value;
            }
        };

        // As a map of "t", its member's tag, and "v", its member
        template <class... Ts>
        struct Codec<DiscriminatedUnion<Ts...>>
        {
            static constexpr Kind kind = Kind::Map;
            static constexpr bool shares = (Codec<Ts>::shares || ...);

            static void write(Writer& writer, const DiscriminatedUnion<Ts...>& value)
            {
                if (value.valueless_by_exception())
                    throw Error("A variant without a value cannot be written");
                writer.map(2);
                writer.str("t");
                std::visit(
                    [&](const auto& member)
                    {
                        using M = Codec<std::remove_cvref_t<decltype(member)>>;
                        writer.str(M::tag());
                        writer.str("v");
                        M::write(writer, member);
                    },
                    value.as_variant());
            }

            static DiscriminatedUnion<Ts...> read(Reader& reader)
            {
                const Reader::Nested nested(reader);
                if (reader.map() != 2)
                    throw Mismatch{};

                // In either order: so the member may be before the tag which says what it is
                std::optional<std::string_view> tag;
                const std::uint8_t* member = nullptr;
                for (int entry = 0; entry != 2; ++entry)
                {
                    const std::string_view key = reader.str();
                    if (key == "t" && !tag)
                        tag = reader.str();
                    else if (key == "v" && !member)
                    {
                        member = reader.at;
                        reader.skip();
                    }
                    else
                        throw Mismatch{};
                }

                const std::uint8_t* rest = std::exchange(reader.at, member);
                std::variant<Ts...> value;
                read_member(reader, value, [&]<class M>(std::type_identity<M>) { return Codec<M>::tag() == *tag; });
                reader.at = rest;
                return DiscriminatedUnion<Ts...>(std::move(value));
            }
        };

        template <class T>
        std::string described()
        {
            if constexpr (Struct<T>)
                return "a " + std::string(Reflect<T>::name);
            else
                return "of the type read";
        }
    } // namespace detail

    template <class T>
    Bytes to_msgpack(const T& value)
    {
        detail::Writer writer;
        if (!detail::Codec<T>::shares)
        {
            detail::Codec<T>::write(writer, value);
            return std::move(writer.out);
        }

        // A bundle: the value, then the objects which it shares
        writer.map(2);
        writer.str("root");
        detail::Codec<T>::write(writer, value);
        writer.str("shared");
        writer.array(writer.tables.size());
        for (std::size_t table = 0; table != writer.tables.size(); ++table)
        {
            writer.array(2);
            writer.str(writer.tables[table].tag);
            writer.array(writer.tables[table].rows.size());
            for (const Bytes& row : writer.tables[table].rows)
                writer.out.insert(writer.out.end(), row.begin(), row.end());
        }
        return std::move(writer.out);
    }

    // Throws Error if data is not a T, as written
    template <class T>
    T from_msgpack(std::span<const std::uint8_t> data)
    {
        using detail::Kind;
        detail::Reader reader{data.data(), data.data() + data.size()};
        try
        {
            if (!detail::Codec<T>::shares)
            {
                T value = detail::Codec<T>::read(reader);
                if (reader.remaining())
                    throw Error("msgpack data has more than one value");
                return value;
            }

            // A bundle. Its tables are found before the value is read, as that refers to them
            const std::uint8_t* root = nullptr;
            bool shared = false;
            for (std::size_t entries = reader.map(); entries; --entries)
            {
                const bool named = reader.kind() == Kind::Str;
                const std::string_view name = named ? reader.str() : std::string_view{};
                if (name == "shared" && !shared)
                {
                    shared = true;
                    for (std::size_t tables = reader.array(); tables; --tables)
                    {
                        if (reader.array() != 2)
                            throw detail::Mismatch{};
                        detail::Reader::Table table{reader.str(), {}};
                        for (std::size_t rows = reader.array(); rows; --rows)
                        {
                            table.rows.push_back({reader.at, {}});
                            reader.skip();
                        }
                        reader.tables.push_back(std::move(table));
                    }
                    continue;
                }
                if (name == "root" && !root)
                    root = reader.at;
                else if (!named)
                    reader.skip();
                reader.skip();
            }
            if (reader.remaining())
                throw Error("msgpack data has more than one value");
            if (!root || !shared)
                throw detail::Mismatch{};

            reader.at = root;
            return detail::Codec<T>::read(reader);
        }
        catch (const detail::Mismatch&)
        {
            throw Error("The data is not " + detail::described<T>() + ", as written");
        }
    }
} // namespace dataclass_native::msgpack
