// Holds objects of the generated types in Arrow tables, and reads them from such, in the layout of ARROW_FORMAT.md:
//
//   auto tables = dataclass_native::arrow::to_arrow<Trade>(trades);   // tables.root, and tables.shared
//   std::vector<Trade> read = dataclass_native::arrow::from_arrow<Trade>(schema, array, shared);
//
// A table is a struct array, as the Arrow C data interface has it: an ArrowSchema and an ArrowArray, which any Arrow
// library imports and exports without copying. Nothing here depends on such a library, or on Python.
#pragma once

#include <array>
#include <bit>
#include <chrono>
#include <concepts>
#include <cstddef>
#include <cstdint>
#include <cstring>
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

#include "dataclass_native/msgpack.hpp"
#include "dataclass_native/reflect.hpp"
#include "dataclass_native/types.hpp"

// The Arrow C data interface, as its specification gives it to be copied:
// https://arrow.apache.org/docs/format/CDataInterface.html
#ifndef ARROW_C_DATA_INTERFACE
#define ARROW_C_DATA_INTERFACE

#define ARROW_FLAG_DICTIONARY_ORDERED 1
#define ARROW_FLAG_NULLABLE 2
#define ARROW_FLAG_MAP_KEYS_SORTED 4

struct ArrowSchema
{
    const char* format;
    const char* name;
    const char* metadata;
    int64_t flags;
    int64_t n_children;
    struct ArrowSchema** children;
    struct ArrowSchema* dictionary;
    void (*release)(struct ArrowSchema*);
    void* private_data;
};

struct ArrowArray
{
    int64_t length;
    int64_t null_count;
    int64_t offset;
    int64_t n_buffers;
    int64_t n_children;
    const void** buffers;
    struct ArrowArray** children;
    struct ArrowArray* dictionary;
    void (*release)(struct ArrowArray*);
    void* private_data;
};

#endif // ARROW_C_DATA_INTERFACE

namespace dataclass_native::arrow
{
    // The tables do not hold the objects being read; or a value cannot be held in one
    class Error : public std::invalid_argument
    {
    public:
        using std::invalid_argument::invalid_argument;
    };

    // The tables are not laid out as this reads them, though they may hold the objects: e.g. with large lists
    class Unsupported : public Error
    {
    public:
        using Error::Error;
    };

    // A table: its schema and array, which are released with it unless they have been moved from
    struct Table
    {
        ArrowSchema schema{};
        ArrowArray array{};

        Table() = default;

        Table(ArrowSchema schema, ArrowArray array) : schema(schema), array(array)
        {
        }

        Table(Table&& other) noexcept
            : schema(std::exchange(other.schema, {})), array(std::exchange(other.array, {}))
        {
        }

        Table& operator=(Table&& other) noexcept
        {
            std::swap(schema, other.schema);
            std::swap(array, other.array);
            return *this;
        }

        ~Table()
        {
            if (schema.release)
                schema.release(&schema);
            if (array.release)
                array.release(&array);
        }
    };

    // The table of the objects, and that of each class which they may share: by its tag
    struct Tables
    {
        Table root;
        std::vector<std::pair<std::string_view, Table>> shared;
    };

    // A table of shared objects which is to be read
    struct SharedTable
    {
        std::string_view tag;
        const ArrowSchema* schema;
        const ArrowArray* array;
    };

    inline constexpr std::string_view tag_key = "dataclass_native:tag";
    inline constexpr std::string_view shared_key = "dataclass_native:shared";

    namespace detail
    {
        using msgpack::detail::Kind;

        using Buffer = std::vector<std::uint8_t>;

        template <class P>
        void push(Buffer& buffer, P value)
        {
            const auto* bytes = reinterpret_cast<const std::uint8_t*>(&value);
            buffer.insert(buffer.end(), bytes, bytes + sizeof(P));
        }

        // Bits, the least significant of each byte first: of which values are not null, or of bools
        struct Bits
        {
            Buffer bytes;
            std::int64_t length = 0;
            std::int64_t zeros = 0;

            void append(bool bit)
            {
                if (length % 8 == 0)
                    bytes.push_back(0);
                if (bit)
                    bytes.back() = static_cast<std::uint8_t>(bytes.back() | (1u << (length % 8)));
                else
                    ++zeros;
                ++length;
            }
        };

        inline constexpr std::int64_t day_microseconds = 86'400'000'000;

        // What a schema owns
        struct SchemaNode
        {
            std::string format;
            std::string name;
            std::string metadata;
            std::vector<ArrowSchema> children;
            std::vector<ArrowSchema*> pointers;
            ArrowSchema dictionary{};
        };

        inline void release_schema(ArrowSchema* schema)
        {
            auto* node = static_cast<SchemaNode*>(schema->private_data);
            // Not those which have been moved from, as the interface allows
            for (ArrowSchema& child : node->children)
                if (child.release)
                    child.release(&child);
            if (node->dictionary.release)
                node->dictionary.release(&node->dictionary);
            delete node;
            schema->release = nullptr;
        }

        // The one entry of a schema's metadata, as the interface encodes it
        inline std::string metadata(std::string_view key, std::string_view value)
        {
            std::string encoded;
            const auto number = [&](std::size_t n)
            {
                const auto value32 = static_cast<std::int32_t>(n);
                encoded.append(reinterpret_cast<const char*>(&value32), sizeof(value32));
            };
            number(1);
            number(key.size());
            encoded.append(key);
            number(value.size());
            encoded.append(value);
            return encoded;
        }

        inline ArrowSchema make_schema(std::string format, std::string_view name, std::vector<ArrowSchema> children = {},
                                       std::int64_t flags = ARROW_FLAG_NULLABLE, std::string metadata = {},
                                       const ArrowSchema* dictionary = nullptr)
        {
            auto* node = new SchemaNode{std::move(format), std::string(name), std::move(metadata), std::move(children),
                                        {},                {}};
            for (ArrowSchema& child : node->children)
                node->pointers.push_back(&child);
            if (dictionary)
                node->dictionary = *dictionary;

            ArrowSchema schema{};
            schema.format = node->format.c_str();
            schema.name = node->name.c_str();
            schema.metadata = node->metadata.empty() ? nullptr : node->metadata.data();
            schema.flags = flags;
            schema.n_children = static_cast<std::int64_t>(node->children.size());
            schema.children = node->pointers.empty() ? nullptr : node->pointers.data();
            schema.dictionary = dictionary ? &node->dictionary : nullptr;
            schema.release = release_schema;
            schema.private_data = node;
            return schema;
        }

        // What an array owns
        struct ArrayNode
        {
            std::vector<Buffer> buffers;
            std::vector<const void*> pointers;
            std::vector<ArrowArray> children;
            std::vector<ArrowArray*> child_pointers;
            ArrowArray dictionary{};
        };

        inline void release_array(ArrowArray* array)
        {
            auto* node = static_cast<ArrayNode*>(array->private_data);
            for (ArrowArray& child : node->children)
                if (child.release)
                    child.release(&child);
            if (node->dictionary.release)
                node->dictionary.release(&node->dictionary);
            delete node;
            array->release = nullptr;
        }

        // An array, of which the first buffer is which values are not null, if it has that buffer
        inline ArrowArray make_array(std::int64_t length, std::int64_t nulls, std::vector<Buffer> buffers,
                                     std::vector<ArrowArray> children = {}, const ArrowArray* dictionary = nullptr,
                                     bool validity = true)
        {
            auto* node = new ArrayNode{std::move(buffers), {}, std::move(children), {}, {}};
            for (std::size_t i = 0; i != node->buffers.size(); ++i)
            {
                // Never null but for validity, which is when there are no nulls
                node->buffers[i].reserve(8);
                node->pointers.push_back(validity && i == 0 && !nulls ? nullptr : node->buffers[i].data());
            }
            for (ArrowArray& child : node->children)
                node->child_pointers.push_back(&child);
            if (dictionary)
                node->dictionary = *dictionary;

            ArrowArray array{};
            array.length = length;
            array.null_count = nulls;
            array.n_buffers = static_cast<std::int64_t>(node->buffers.size());
            array.n_children = static_cast<std::int64_t>(node->children.size());
            array.buffers = node->pointers.data();
            array.children = node->child_pointers.empty() ? nullptr : node->child_pointers.data();
            array.dictionary = dictionary ? &node->dictionary : nullptr;
            array.release = release_array;
            array.private_data = node;
            return array;
        }

        // What names a union's member, as a field of the struct which the union is: its tag, else what msgpack writes
        // it as
        template <class M>
        constexpr std::string_view member_name()
        {
            if (const std::string_view tag = msgpack::detail::tag_of<M>(); !tag.empty())
                return tag;
            return msgpack::detail::Codec<M>::kind == Kind::Map ? "map" : "array";
        }

        // The type of each member which a tuple of Fields is of
        template <class F>
        struct MemberOf;

        template <class S, class M>
        struct MemberOf<Field<S, M>>
        {
            using type = M;
        };

        template <class T>
        constexpr bool shares = msgpack::detail::Codec<T>::shares;

        // The format of a number
        template <class P>
        constexpr const char* format_of()
        {
            if constexpr (std::floating_point<P>)
                return sizeof(P) == 4 ? "f" : "g";
            else
            {
                constexpr const char* formats[] = {"c", "s", "i", "l", "C", "S", "I", "L"};
                return formats[(std::unsigned_integral<P> ? 4 : 0) + std::countr_zero(sizeof(P))];
            }
        }

        struct Writer;

        // The objects of a class which are shared, in the order met
        struct SharedBase
        {
            std::string_view tag;

            explicit SharedBase(std::string_view tag) : tag(tag)
            {
            }

            virtual ~SharedBase() = default;
            virtual Table build(Writer& writer) = 0;
        };

        struct Writer
        {
            std::vector<std::unique_ptr<SharedBase>> tables;

            template <class S>
            S& table(std::string_view tag)
            {
                for (auto& table : tables)
                    if (table->tag == tag)
                        return static_cast<S&>(*table);
                throw Error("A class is shared which its Reflect does not say may be");
            }
        };

        // Builds the column of a type. Each has:
        //
        //   discover   Finds the objects which a value shares, so that each has its row before any column is built
        //   append     Adds a value, and append_null a null
        //   schema     The Arrow type, as a field
        //   finish     The array of what has been added
        template <class T>
        struct Column;

        // Values of a fixed size
        template <class P>
        struct Fixed
        {
            Bits validity;
            Buffer data;

            void put(P value)
            {
                validity.append(true);
                push(data, value);
            }

            void append_null()
            {
                validity.append(false);
                push(data, P{});
            }

            ArrowArray finish()
            {
                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                buffers.push_back(std::move(data));
                return make_array(validity.length, validity.zeros, std::move(buffers));
            }
        };

        // Values of bytes: strings and binary
        struct Variable
        {
            Bits validity;
            Buffer offsets;
            Buffer data;

            Variable()
            {
                push(offsets, std::int32_t{0});
            }

            void put(const void* bytes, std::size_t size)
            {
                const auto* first = static_cast<const std::uint8_t*>(bytes);
                data.insert(data.end(), first, first + size);
                end(true);
            }

            void append_null()
            {
                end(false);
            }

            ArrowArray finish()
            {
                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                buffers.push_back(std::move(offsets));
                buffers.push_back(std::move(data));
                return make_array(validity.length, validity.zeros, std::move(buffers));
            }

        private:
            void end(bool valid)
            {
                if (data.size() > 0x7FFF'FFFF)
                    throw Error("A column of str or bytes cannot hold more than 2147483647 bytes");
                validity.append(valid);
                push(offsets, static_cast<std::int32_t>(data.size()));
            }
        };

        template <class T>
            requires std::integral<T> || std::floating_point<T>
        struct Column<T> : Fixed<T>
        {
            static void discover(const T&, Writer&)
            {
            }

            void append(const T& value, Writer&)
            {
                this->put(value);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return make_schema(format_of<T>(), name, {}, flags);
            }
        };

        template <>
        struct Column<bool>
        {
            Bits validity;
            Bits values;

            static void discover(bool, Writer&)
            {
            }

            void append(bool value, Writer&)
            {
                validity.append(true);
                values.append(value);
            }

            void append_null()
            {
                validity.append(false);
                values.append(false);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return make_schema("b", name, {}, flags);
            }

            ArrowArray finish()
            {
                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                buffers.push_back(std::move(values.bytes));
                return make_array(validity.length, validity.zeros, std::move(buffers));
            }
        };

        template <>
        struct Column<std::string> : Variable
        {
            static void discover(const std::string&, Writer&)
            {
            }

            void append(const std::string& value, Writer&)
            {
                if (!msgpack::detail::is_utf8(value))
                    throw Error("A str which is not UTF-8 cannot be held");
                put(value.data(), value.size());
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return make_schema("u", name, {}, flags);
            }
        };

        // bytes, and a tuple of UInt8
        template <>
        struct Column<Bytes> : Variable
        {
            static void discover(const Bytes&, Writer&)
            {
            }

            void append(const Bytes& value, Writer&)
            {
                put(value.data(), value.size());
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return make_schema("z", name, {}, flags);
            }
        };

        // A date or time, held as a number of the unit which its format names
        template <class T, class P>
        struct Temporal : Fixed<P>
        {
            static void discover(const T&, Writer&)
            {
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return make_schema(Column<T>::format, name, {}, flags);
            }
        };

        template <>
        struct Column<Date> : Temporal<Date, std::int32_t>
        {
            static constexpr const char* format = "tdD";

            void append(Date value, Writer&)
            {
                put(static_cast<std::int32_t>(value.time_since_epoch().count()));
            }
        };

        template <>
        struct Column<TimeOfDay> : Temporal<TimeOfDay, std::int64_t>
        {
            static constexpr const char* format = "ttu";

            void append(TimeOfDay value, Writer&)
            {
                const std::int64_t microseconds = value.since_midnight.count();
                if (microseconds < 0 || microseconds >= day_microseconds)
                    throw Error("A time which is not within a day cannot be held");
                put(microseconds);
            }
        };

        template <>
        struct Column<DateTime> : Temporal<DateTime, std::int64_t>
        {
            static constexpr const char* format = "tsu:UTC";

            void append(DateTime value, Writer&)
            {
                put(value.time_since_epoch().count());
            }
        };

        template <>
        struct Column<TimeDelta> : Temporal<TimeDelta, std::int64_t>
        {
            static constexpr const char* format = "tDu";

            void append(TimeDelta value, Writer&)
            {
                put(value.count());
            }
        };

        // As the position of its member, in a dictionary of their names
        template <Enum T>
        struct Column<T> : Fixed<std::int32_t>
        {
            static void discover(T, Writer&)
            {
            }

            void append(T value, Writer&)
            {
                std::int32_t index = 0;
                for (const auto& member : Reflect<T>::members)
                {
                    if (member.value == value)
                        return put(index);
                    ++index;
                }
                throw Error("A value which is not a member of " + std::string(Reflect<T>::name) + " cannot be held");
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                const ArrowSchema names = make_schema("u", "");
                return make_schema("i", name, {}, flags, {}, &names);
            }

            ArrowArray finish()
            {
                Variable names;
                for (const auto& member : Reflect<T>::members)
                    names.put(member.name.data(), member.name.size());
                const ArrowArray dictionary = names.finish();
                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                buffers.push_back(std::move(data));
                return make_array(validity.length, validity.zeros, std::move(buffers), {}, &dictionary);
            }
        };

        // As its T, or null
        template <class T>
        struct Column<std::optional<T>>
        {
            Column<T> inner;

            static void discover(const std::optional<T>& value, Writer& writer)
            {
                if (value)
                    Column<T>::discover(*value, writer);
            }

            void append(const std::optional<T>& value, Writer& writer)
            {
                if (value)
                    inner.append(*value, writer);
                else
                    inner.append_null();
            }

            void append_null()
            {
                inner.append_null();
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return Column<T>::schema(name, flags);
            }

            ArrowArray finish()
            {
                return inner.finish();
            }
        };

        // A list: of a tuple's elements, or of a set's in ascending order, which is that it is held in
        template <class C, class E>
        struct List
        {
            Bits validity;
            Buffer offsets;
            Column<E> elements;
            std::int64_t count = 0;

            List()
            {
                push(offsets, std::int32_t{0});
            }

            static void discover(const C& value, Writer& writer)
            {
                if constexpr (shares<E>)
                    for (const E& element : value)
                        Column<E>::discover(element, writer);
            }

            void append(const C& value, Writer& writer)
            {
                for (const E& element : value)
                    elements.append(element, writer);
                count += static_cast<std::int64_t>(value.size());
                end(true);
            }

            void append_null()
            {
                end(false);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                std::vector<ArrowSchema> children;
                children.push_back(Column<E>::schema("item"));
                return make_schema("+l", name, std::move(children), flags);
            }

            ArrowArray finish()
            {
                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                buffers.push_back(std::move(offsets));
                std::vector<ArrowArray> children;
                children.push_back(elements.finish());
                return make_array(validity.length, validity.zeros, std::move(buffers), std::move(children));
            }

        private:
            void end(bool valid)
            {
                if (count > 0x7FFF'FFFF)
                    throw Error("A column of tuples or frozensets cannot hold more than 2147483647 elements");
                validity.append(valid);
                push(offsets, static_cast<std::int32_t>(count));
            }
        };

        template <class E>
        struct Column<std::vector<E>> : List<std::vector<E>, E>
        {
        };

        template <class E>
        struct Column<std::set<E>> : List<std::set<E>, E>
        {
        };

        // A map, in ascending order of key
        template <class K, class V>
        struct Column<std::map<K, V>>
        {
            Bits validity;
            Buffer offsets;
            Column<K> keys;
            Column<V> values;
            std::int64_t count = 0;

            Column()
            {
                push(offsets, std::int32_t{0});
            }

            static void discover(const std::map<K, V>& value, Writer& writer)
            {
                if constexpr (shares<V>)
                    for (const auto& entry : value)
                        Column<V>::discover(entry.second, writer);
            }

            void append(const std::map<K, V>& value, Writer& writer)
            {
                for (const auto& [key, element] : value)
                {
                    keys.append(key, writer);
                    values.append(element, writer);
                }
                count += static_cast<std::int64_t>(value.size());
                end(true);
            }

            void append_null()
            {
                end(false);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                // Its entries, and their keys, are never null
                std::vector<ArrowSchema> entry;
                entry.push_back(Column<K>::schema("key", 0));
                entry.push_back(Column<V>::schema("value"));
                std::vector<ArrowSchema> children;
                children.push_back(make_schema("+s", "entries", std::move(entry), 0));
                return make_schema("+m", name, std::move(children), flags | ARROW_FLAG_MAP_KEYS_SORTED);
            }

            ArrowArray finish()
            {
                std::vector<ArrowArray> entry;
                entry.push_back(keys.finish());
                entry.push_back(values.finish());
                std::vector<Buffer> none;
                none.emplace_back();
                std::vector<ArrowArray> children;
                children.push_back(make_array(count, 0, std::move(none), std::move(entry)));

                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                buffers.push_back(std::move(offsets));
                return make_array(validity.length, validity.zeros, std::move(buffers), std::move(children));
            }

        private:
            void end(bool valid)
            {
                if (count > 0x7FFF'FFFF)
                    throw Error("A column of Mappings cannot hold more than 2147483647 entries");
                validity.append(valid);
                push(offsets, static_cast<std::int32_t>(count));
            }
        };

        // A struct: of a class's fields, of a tuple's elements, or of a union's members
        template <class... Ms>
        struct StructColumn
        {
            Bits validity;
            std::tuple<Column<Ms>...> columns;

            void append_null()
            {
                validity.append(false);
                std::apply([](Column<Ms>&... column) { (column.append_null(), ...); }, columns);
            }

            template <class Names>
            static ArrowSchema schema(std::string_view name, std::int64_t flags, const Names& names,
                                      std::string metadata = {})
            {
                std::vector<ArrowSchema> children;
                [[maybe_unused]] std::size_t index = 0;
                (children.push_back(Column<Ms>::schema(names[index++])), ...);
                return make_schema("+s", name, std::move(children), flags, std::move(metadata));
            }

            ArrowArray finish()
            {
                std::vector<ArrowArray> children;
                std::apply([&](Column<Ms>&... column) { (children.push_back(column.finish()), ...); }, columns);
                std::vector<Buffer> buffers;
                buffers.push_back(std::move(validity.bytes));
                return make_array(validity.length, validity.zeros, std::move(buffers), std::move(children));
            }
        };

        // A struct of a field for each element, named by its position
        template <class... Ts>
        struct Column<std::tuple<Ts...>> : StructColumn<Ts...>
        {
            static void discover(const std::tuple<Ts...>& value, Writer& writer)
            {
                std::apply([&](const Ts&... element) { (Column<Ts>::discover(element, writer), ...); }, value);
            }

            void append(const std::tuple<Ts...>& value, Writer& writer)
            {
                this->validity.append(true);
                std::apply(
                    [&](Column<Ts>&... column)
                    { std::apply([&](const Ts&... element) { (column.append(element, writer), ...); }, value); },
                    this->columns);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                std::array<std::string, sizeof...(Ts)> names;
                for (std::size_t i = 0; i != names.size(); ++i)
                    names[i] = std::to_string(i);
                return StructColumn<Ts...>::schema(name, flags, names);
            }
        };

        // A struct of a field for each member, of which that of the member held is not null
        template <class... Ts>
        struct Union : StructColumn<Ts...>
        {
            static constexpr std::array<std::string_view, sizeof...(Ts)> names{member_name<Ts>()...};

            static void discover(const std::variant<Ts...>& value, Writer& writer)
            {
                if constexpr ((shares<Ts> || ...))
                    std::visit(
                        [&](const auto& member)
                        { Column<std::remove_cvref_t<decltype(member)>>::discover(member, writer); },
                        value);
            }

            void append(const std::variant<Ts...>& value, Writer& writer)
            {
                if (value.valueless_by_exception())
                    throw Error("A variant without a value cannot be held");
                this->validity.append(true);
                std::size_t index = 0;
                std::apply(
                    [&](Column<Ts>&... column)
                    {
                        ((index++ == value.index() ? column.append(std::get<Ts>(value), writer) : column.append_null()),
                         ...);
                    },
                    this->columns);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return StructColumn<Ts...>::schema(name, flags, names);
            }
        };

        template <class... Ts>
        struct Column<std::variant<Ts...>> : Union<Ts...>
        {
        };

        template <class... Ts>
        struct Column<DiscriminatedUnion<Ts...>> : Union<Ts...>
        {
            static void discover(const DiscriminatedUnion<Ts...>& value, Writer& writer)
            {
                Union<Ts...>::discover(value.as_variant(), writer);
            }

            void append(const DiscriminatedUnion<Ts...>& value, Writer& writer)
            {
                Union<Ts...>::append(value.as_variant(), writer);
            }
        };

        // A struct of a field for each of the class's fields. Defined for each number of fields by Fields
        template <class T, class Fields>
        struct ClassColumn;

        template <class T, class... Ms>
        struct ClassColumn<T, std::tuple<Field<T, Ms>...>> : StructColumn<Ms...>
        {
            static void discover(const T& value, Writer& writer)
            {
                if constexpr (shares<T>)
                    std::apply(
                        [&](const Field<T, Ms>&... field)
                        { (Column<Ms>::discover(value.*field.member, writer), ...); },
                        Reflect<T>::fields);
            }

            void append(const T& value, Writer& writer)
            {
                this->validity.append(true);
                std::apply(
                    [&](Column<Ms>&... column)
                    {
                        std::apply([&](const Field<T, Ms>&... field)
                                   { (column.append(value.*field.member, writer), ...); },
                                   Reflect<T>::fields);
                    },
                    this->columns);
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE,
                                      std::string metadata = {})
            {
                const auto names = std::apply(
                    [](const Field<T, Ms>&... field) { return std::array<std::string_view, sizeof...(Ms)>{field.name...}; },
                    Reflect<T>::fields);
                return StructColumn<Ms...>::schema(name, flags, names, std::move(metadata));
            }
        };

        template <dataclass_native::Struct T>
        struct Column<T> : ClassColumn<T, std::remove_const_t<decltype(Reflect<T>::fields)>>
        {
        };

        template <class T>
        struct SharedObjects : SharedBase
        {
            std::vector<const T*> objects;
            std::unordered_map<const T*, std::uint32_t> rows;

            SharedObjects() : SharedBase(Reflect<T>::tag)
            {
            }

            Table build(Writer& writer) override
            {
                Column<T> column;
                for (const T* object : objects)
                    column.append(*object, writer);
                return {Column<T>::schema("", ARROW_FLAG_NULLABLE, metadata(tag_key, tag)), column.finish()};
            }
        };

        // As the row of its T, in the table of T's class
        template <class T>
        struct Column<Shared<T>> : Fixed<std::uint32_t>
        {
            SharedObjects<T>* table = nullptr;

            static void discover(const Shared<T>& value, Writer& writer)
            {
                if (!value)
                    throw Error("An empty Shared cannot be held");
                auto& table = writer.table<SharedObjects<T>>(Reflect<T>::tag);
                const auto row = static_cast<std::uint32_t>(table.objects.size());
                if (table.rows.emplace(value.get(), row).second)
                {
                    // Its row is taken before what it shares is found, as that may be of its own class
                    table.objects.push_back(value.get());
                    Column<T>::discover(*value, writer);
                }
            }

            void append(const Shared<T>& value, Writer& writer)
            {
                if (!table)
                    table = &writer.table<SharedObjects<T>>(Reflect<T>::tag);
                put(table->rows.at(value.get()));
            }

            static ArrowSchema schema(std::string_view name, std::int64_t flags = ARROW_FLAG_NULLABLE)
            {
                return make_schema("I", name, {}, flags, metadata(shared_key, Reflect<T>::tag));
            }
        };

        template <class... Ss>
        void add_tables(Writer& writer, Types<Ss...>)
        {
            (writer.tables.push_back(std::make_unique<SharedObjects<Ss>>()), ...);
        }

        // --- Reading

        [[noreturn]] inline void unsupported()
        {
            throw Unsupported("The table is not laid out as it is read");
        }

        [[noreturn]] inline void invalid()
        {
            throw Error("The table does not hold the objects read");
        }

        inline bool has_format(const ArrowSchema& schema, std::string_view format)
        {
            return std::string_view(schema.format) == format;
        }

        // An array which a view reads, having checked that it has the buffers and children of its format
        inline const ArrowArray& checked(const ArrowSchema& schema, const ArrowArray& array, std::int64_t buffers,
                                         std::int64_t children)
        {
            if (array.n_buffers != buffers || array.n_children != children || schema.n_children != children)
                unsupported();
            return array;
        }

        template <class P>
        P value_at(const ArrowArray& array, int buffer, std::int64_t index)
        {
            P value;
            std::memcpy(&value, static_cast<const std::uint8_t*>(array.buffers[buffer]) + index * sizeof(P), sizeof(P));
            return value;
        }

        inline bool bit_at(const void* bits, std::int64_t index)
        {
            return (static_cast<const std::uint8_t*>(bits)[index / 8] >> (index % 8)) & 1;
        }

        // Whether the value at an index of an array, which is from the array's offset, is null
        inline bool null_at(const ArrowArray& array, std::int64_t index)
        {
            return array.null_count != 0 && array.buffers[0] && !bit_at(array.buffers[0], array.offset + index);
        }

        // Strings, or binary, of either size of offset
        struct Text
        {
            const ArrowArray* array = nullptr;
            bool large = false;

            Text() = default;

            Text(const ArrowSchema& schema, const ArrowArray& array, std::string_view small, std::string_view big)
                : array(&checked(schema, array, 3, 0)), large(has_format(schema, big))
            {
                if (!large && !has_format(schema, small))
                    unsupported();
            }

            std::string_view get(std::int64_t index) const
            {
                index += array->offset;
                const std::int64_t start =
                    large ? value_at<std::int64_t>(*array, 1, index) : value_at<std::int32_t>(*array, 1, index);
                const std::int64_t end = large ? value_at<std::int64_t>(*array, 1, index + 1)
                                               : value_at<std::int32_t>(*array, 1, index + 1);
                return {static_cast<const char*>(array->buffers[2]) + start, static_cast<std::size_t>(end - start)};
            }
        };

        struct Reader;

        struct ReadBase
        {
            std::string_view tag;
            virtual ~ReadBase() = default;
        };

        struct Reader
        {
            std::span<const SharedTable> shared;
            std::vector<std::unique_ptr<ReadBase>> tables;

            template <class S>
            S& table(std::string_view tag)
            {
                for (auto& table : tables)
                    if (table->tag == tag)
                        return static_cast<S&>(*table);
                for (const SharedTable& given : shared)
                    if (given.tag == tag)
                    {
                        tables.push_back(std::make_unique<S>(*given.schema, *given.array));
                        return static_cast<S&>(*tables.back());
                    }
                invalid();
            }
        };

        // Reads the column of a type: null() is whether a value is null, and get() the value, at an index which is
        // from the array's offset. Constructing one throws Unsupported if the column is not laid out as it reads
        template <class T>
        struct View;

        // The value at an index, which may not be null: but for an optional, of which null() is never true
        template <class T>
        T element(const View<T>& view, std::int64_t index, Reader& reader)
        {
            if (view.null(index))
                invalid();
            return view.get(index, reader);
        }

        struct Slots
        {
            const ArrowArray* array = nullptr;

            bool null(std::int64_t index) const
            {
                return null_at(*array, index);
            }
        };

        template <class T>
            requires std::integral<T> || std::floating_point<T>
        struct View<T> : Slots
        {
            View(const ArrowSchema& schema, const ArrowArray& array) : Slots{&checked(schema, array, 2, 0)}
            {
                if (!has_format(schema, format_of<T>()))
                    unsupported();
            }

            T get(std::int64_t index, Reader&) const
            {
                return value_at<T>(*array, 1, array->offset + index);
            }
        };

        template <>
        struct View<bool> : Slots
        {
            View(const ArrowSchema& schema, const ArrowArray& array) : Slots{&checked(schema, array, 2, 0)}
            {
                if (!has_format(schema, "b"))
                    unsupported();
            }

            bool get(std::int64_t index, Reader&) const
            {
                return bit_at(array->buffers[1], array->offset + index);
            }
        };

        template <>
        struct View<std::string> : Slots
        {
            Text text;

            View(const ArrowSchema& schema, const ArrowArray& array) : Slots{&array}, text(schema, array, "u", "U")
            {
            }

            std::string get(std::int64_t index, Reader&) const
            {
                return std::string(text.get(index));
            }
        };

        template <>
        struct View<Bytes> : Slots
        {
            Text text;

            View(const ArrowSchema& schema, const ArrowArray& array) : Slots{&array}, text(schema, array, "z", "Z")
            {
            }

            Bytes get(std::int64_t index, Reader&) const
            {
                const std::string_view bytes = text.get(index);
                return {bytes.begin(), bytes.end()};
            }
        };

        template <class T, class P>
        struct TemporalView : Slots
        {
            TemporalView(const ArrowSchema& schema, const ArrowArray& array) : Slots{&checked(schema, array, 2, 0)}
            {
                if (!has_format(schema, Column<T>::format))
                    unsupported();
            }

            P number(std::int64_t index) const
            {
                return value_at<P>(*array, 1, array->offset + index);
            }
        };

        template <>
        struct View<Date> : TemporalView<Date, std::int32_t>
        {
            using TemporalView::TemporalView;

            Date get(std::int64_t index, Reader&) const
            {
                return Date{std::chrono::days{number(index)}};
            }
        };

        template <>
        struct View<TimeOfDay> : TemporalView<TimeOfDay, std::int64_t>
        {
            using TemporalView::TemporalView;

            TimeOfDay get(std::int64_t index, Reader&) const
            {
                const std::int64_t microseconds = number(index);
                if (microseconds < 0 || microseconds >= day_microseconds)
                    invalid();
                return {std::chrono::microseconds{microseconds}};
            }
        };

        template <>
        struct View<DateTime> : TemporalView<DateTime, std::int64_t>
        {
            using TemporalView::TemporalView;

            DateTime get(std::int64_t index, Reader&) const
            {
                return DateTime{std::chrono::microseconds{number(index)}};
            }
        };

        template <>
        struct View<TimeDelta> : TemporalView<TimeDelta, std::int64_t>
        {
            using TemporalView::TemporalView;

            TimeDelta get(std::int64_t index, Reader&) const
            {
                return TimeDelta{number(index)};
            }
        };

        // Names, in a dictionary or not
        template <Enum T>
        struct View<T> : Slots
        {
            Text names;
            // Of each name in the dictionary, the member it is: if it is one
            std::vector<std::optional<T>> members;
            bool dictionary;

            View(const ArrowSchema& schema, const ArrowArray& array)
                : Slots{&array}, dictionary(schema.dictionary != nullptr)
            {
                if (!dictionary)
                {
                    names = Text(schema, array, "u", "U");
                    return;
                }
                if (!has_format(schema, "i") || !array.dictionary)
                    unsupported();
                checked(schema, array, 2, 0);
                names = Text(*schema.dictionary, *array.dictionary, "u", "U");
                for (std::int64_t i = 0; i != array.dictionary->length; ++i)
                    members.push_back(null_at(*array.dictionary, i) ? std::nullopt : member(names.get(i)));
            }

            T get(std::int64_t index, Reader&) const
            {
                if (!dictionary)
                    return required(member(names.get(index)));
                const auto position = value_at<std::int32_t>(*array, 1, array->offset + index);
                if (position < 0 || static_cast<std::size_t>(position) >= members.size())
                    invalid();
                return required(members[static_cast<std::size_t>(position)]);
            }

        private:
            static std::optional<T> member(std::string_view name)
            {
                for (const auto& member : Reflect<T>::members)
                    if (member.name == name)
                        return member.value;
                return std::nullopt;
            }

            static T required(std::optional<T> member)
            {
                if (!member)
                    invalid();
                return *member;
            }
        };

        template <class T>
        struct View<std::optional<T>>
        {
            View<T> inner;

            View(const ArrowSchema& schema, const ArrowArray& array) : inner(schema, array)
            {
            }

            bool null(std::int64_t) const
            {
                return false;
            }

            std::optional<T> get(std::int64_t index, Reader& reader) const
            {
                if (inner.null(index))
                    return std::nullopt;
                return inner.get(index, reader);
            }
        };

        // The elements of a list, or the entries of a map, at an index: from first to before last, of their array
        inline std::pair<std::int64_t, std::int64_t> range_at(const ArrowArray& array, std::int64_t index)
        {
            index += array.offset;
            return {value_at<std::int32_t>(array, 1, index), value_at<std::int32_t>(array, 1, index + 1)};
        }

        template <class C, class E>
        struct ListView : Slots
        {
            View<E> elements;

            ListView(const ArrowSchema& schema, const ArrowArray& array)
                : Slots{&checked(schema, array, 2, 1)}, elements(child(schema), *array.children[0])
            {
            }

            C get(std::int64_t index, Reader& reader) const
            {
                C value;
                for (auto [at, end] = range_at(*array, index); at != end; ++at)
                    value.insert(value.end(), element(elements, at, reader));
                return value;
            }

        private:
            static const ArrowSchema& child(const ArrowSchema& schema)
            {
                if (!has_format(schema, "+l") || schema.n_children != 1)
                    unsupported();
                return *schema.children[0];
            }
        };

        template <class E>
        struct View<std::vector<E>> : ListView<std::vector<E>, E>
        {
            using ListView<std::vector<E>, E>::ListView;
        };

        template <class E>
        struct View<std::set<E>> : ListView<std::set<E>, E>
        {
            using ListView<std::set<E>, E>::ListView;
        };

        template <class K, class V>
        struct View<std::map<K, V>> : Slots
        {
            const ArrowArray* entries;
            View<K> keys;
            View<V> values;

            View(const ArrowSchema& schema, const ArrowArray& array)
                : Slots{&checked(schema, array, 2, 1)}, entries(array.children[0]),
                  keys(*entry(schema).children[0], *checked(entry(schema), *entries, 1, 2).children[0]),
                  values(*entry(schema).children[1], *entries->children[1])
            {
            }

            std::map<K, V> get(std::int64_t index, Reader& reader) const
            {
                std::map<K, V> value;
                for (auto [at, end] = range_at(*array, index); at != end; ++at)
                {
                    const std::int64_t entry = entries->offset + at;
                    K key = element(keys, entry, reader);
                    if (!value.emplace(std::move(key), element(values, entry, reader)).second)
                        invalid();
                }
                return value;
            }

        private:
            static const ArrowSchema& entry(const ArrowSchema& schema)
            {
                if (!has_format(schema, "+m") || schema.n_children != 1 || !has_format(*schema.children[0], "+s"))
                    unsupported();
                return *schema.children[0];
            }
        };

        // The views of a struct's fields, each of which is the field of a name: if the struct has it
        template <class... Ms>
        struct StructView : Slots
        {
            std::tuple<std::optional<View<Ms>>...> views;

            template <class Names>
            StructView(const ArrowSchema& schema, const ArrowArray& array, const Names& names) : Slots{&array}
            {
                if (!has_format(schema, "+s") || array.n_buffers != 1 || array.n_children != schema.n_children)
                    unsupported();
                [[maybe_unused]] std::size_t index = 0;
                std::apply(
                    [&](std::optional<View<Ms>>&... view)
                    {
                        ((find(schema, array, names[index++], view)), ...);
                    },
                    views);
            }

        private:
            template <class M>
            static void find(const ArrowSchema& schema, const ArrowArray& array, std::string_view name,
                             std::optional<View<M>>& view)
            {
                for (std::int64_t child = 0; child != schema.n_children; ++child)
                    if (schema.children[child]->name && name == schema.children[child]->name)
                    {
                        view.emplace(*schema.children[child], *array.children[child]);
                        return;
                    }
            }
        };

        template <class... Ts>
        struct View<std::tuple<Ts...>> : StructView<Ts...>
        {
            View(const ArrowSchema& schema, const ArrowArray& array) : StructView<Ts...>(schema, array, names())
            {
                if (!std::apply([](const auto&... view) { return (view.has_value() && ...); }, this->views))
                    invalid();
            }

            std::tuple<Ts...> get(std::int64_t index, Reader& reader) const
            {
                index += this->array->offset;
                return std::apply(
                    [&](const std::optional<View<Ts>>&... view)
                    {
                        // In braces, so in order
                        return std::tuple<Ts...>{element(*view, index, reader)...};
                    },
                    this->views);
            }

        private:
            static std::array<std::string, sizeof...(Ts)> names()
            {
                std::array<std::string, sizeof...(Ts)> names;
                for (std::size_t i = 0; i != names.size(); ++i)
                    names[i] = std::to_string(i);
                return names;
            }
        };

        template <class... Ts>
        struct UnionView : StructView<Ts...>
        {
            UnionView(const ArrowSchema& schema, const ArrowArray& array)
                : StructView<Ts...>(schema, array, Union<Ts...>::names)
            {
            }

            std::variant<Ts...> get(std::int64_t index, Reader& reader) const
            {
                index += this->array->offset;
                // The one member which is not null
                std::optional<std::variant<Ts...>> value;
                std::apply(
                    [&](const std::optional<View<Ts>>&... view)
                    {
                        ((view && !view->null(index)
                              ? (value ? invalid() : void(value.emplace(std::in_place_type<Ts>, view->get(index, reader))))
                              : void()),
                         ...);
                    },
                    this->views);
                if (!value)
                    invalid();
                return std::move(*value);
            }
        };

        template <class... Ts>
        struct View<std::variant<Ts...>> : UnionView<Ts...>
        {
            using UnionView<Ts...>::UnionView;
        };

        template <class... Ts>
        struct View<DiscriminatedUnion<Ts...>> : UnionView<Ts...>
        {
            using UnionView<Ts...>::UnionView;

            DiscriminatedUnion<Ts...> get(std::int64_t index, Reader& reader) const
            {
                return DiscriminatedUnion<Ts...>(UnionView<Ts...>::get(index, reader));
            }
        };

        template <class T, class Fields>
        struct ClassView;

        template <class T, class... Ms>
        struct ClassView<T, std::tuple<Field<T, Ms>...>> : StructView<Ms...>
        {
            ClassView(const ArrowSchema& schema, const ArrowArray& array) : StructView<Ms...>(schema, array, names())
            {
                // A field which has no column takes its default, if it has one
                std::apply(
                    [&](const std::optional<View<Ms>>&... view)
                    {
                        std::apply([&](const Field<T, Ms>&... field)
                                   { ((view || field.has_default ? void() : invalid()), ...); },
                                   Reflect<T>::fields);
                    },
                    this->views);
            }

            T get(std::int64_t index, Reader& reader) const
            {
                index += this->array->offset;
                T value{};
                std::apply(
                    [&](const std::optional<View<Ms>>&... view)
                    {
                        std::apply(
                            [&](const Field<T, Ms>&... field)
                            { ((view ? void(value.*field.member = element(*view, index, reader)) : void()), ...); },
                            Reflect<T>::fields);
                    },
                    this->views);
                return value;
            }

        private:
            static std::array<std::string_view, sizeof...(Ms)> names()
            {
                return std::apply(
                    [](const Field<T, Ms>&... field) { return std::array<std::string_view, sizeof...(Ms)>{field.name...}; },
                    Reflect<T>::fields);
            }
        };

        template <dataclass_native::Struct T>
        struct View<T> : ClassView<T, std::remove_const_t<decltype(Reflect<T>::fields)>>
        {
            using ClassView<T, std::remove_const_t<decltype(Reflect<T>::fields)>>::ClassView;
        };

        // The table of a class's shared objects, and those of them which have been read
        template <class T>
        struct SharedRows : ReadBase
        {
            View<T> view;
            std::vector<std::shared_ptr<const T>> objects;
            std::vector<bool> reading;

            SharedRows(const ArrowSchema& schema, const ArrowArray& array)
                : view(schema, array), objects(static_cast<std::size_t>(array.length)),
                  reading(static_cast<std::size_t>(array.length))
            {
                tag = Reflect<T>::tag;
            }
        };

        template <class T>
        struct View<Shared<T>> : Slots
        {
            View(const ArrowSchema& schema, const ArrowArray& array) : Slots{&checked(schema, array, 2, 0)}
            {
                if (!has_format(schema, "I"))
                    unsupported();
            }

            Shared<T> get(std::int64_t index, Reader& reader) const
            {
                const auto row = value_at<std::uint32_t>(*array, 1, array->offset + index);
                auto& table = reader.table<SharedRows<T>>(Reflect<T>::tag);
                if (row >= table.objects.size())
                    invalid();
                if (!table.objects[row])
                {
                    // An object which shares itself, which no object held could
                    if (table.reading[row])
                        invalid();
                    table.reading[row] = true;
                    table.objects[row] = std::make_shared<const T>(element(table.view, row, reader));
                }
                return Shared<T>(table.objects[row]);
            }
        };
    } // namespace detail

    // The objects as tables: a row for each, and the tables of the objects which they share
    template <class T>
    Tables to_arrow(std::span<const T* const> objects)
    {
        static_assert(Reflect<T>::arrow, "A class which holds itself cannot be held in an Arrow table");
        detail::Writer writer;
        detail::add_tables(writer, typename Reflect<T>::shared{});
        // First, so that each shared object has its row
        for (const T* object : objects)
            detail::Column<T>::discover(*object, writer);

        detail::Column<T> column;
        for (const T* object : objects)
            column.append(*object, writer);

        Tables tables;
        const std::string_view tag = Reflect<T>::tag;
        tables.root = {detail::Column<T>::schema("", ARROW_FLAG_NULLABLE, detail::metadata(tag_key, tag)),
                       column.finish()};
        for (auto& table : writer.tables)
            tables.shared.emplace_back(table->tag, table->build(writer));
        return tables;
    }

    template <class T>
    Tables to_arrow(std::span<const T> objects)
    {
        std::vector<const T*> pointers;
        pointers.reserve(objects.size());
        for (const T& object : objects)
            pointers.push_back(&object);
        return to_arrow<T>(std::span<const T* const>(pointers));
    }

    // The objects which a table holds, given the tables of the objects which they share. Throws Unsupported if a
    // table is not laid out as this reads it, and otherwise Error if they do not hold objects of the class.
    // The tables must be valid Arrow: that is not checked
    template <class T>
    std::vector<T> from_arrow(const ArrowSchema& schema, const ArrowArray& array,
                              std::span<const SharedTable> shared = {})
    {
        static_assert(Reflect<T>::arrow, "A class which holds itself cannot be held in an Arrow table");
        detail::Reader reader{shared, {}};
        const detail::View<T> view(schema, array);
        std::vector<T> objects;
        objects.reserve(static_cast<std::size_t>(array.length));
        for (std::int64_t row = 0; row != array.length; ++row)
            objects.push_back(detail::element(view, row, reader));
        return objects;
    }
} // namespace dataclass_native::arrow
