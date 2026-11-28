// How the generated structs and enums are described to code which is written for any of them, such as msgpack.hpp
#pragma once

#include <array>
#include <string_view>
#include <tuple>

namespace dataclass_native
{
    // Specialised, in a generated header, for each of its structs and enums:
    //
    //   of a struct   name      The class's name
    //                 tag       What names it when serialised
    //                 shares    Whether one may hold, however deeply, a Shared value
    //                 shared    The classes of those values, as Types
    //                 arrow     Whether it may be held in an Arrow table: not if it, or a class it may share, holds
    //                           itself
    //                 fields    A std::tuple of Field
    //
    //   of an enum    name, tag
    //                 members   A std::array of Member
    template <class T>
    struct Reflect;

    template <class S, class T>
    struct Field
    {
        // As in Python: not the member's name, where that is a C++ keyword
        std::string_view name;
        T S::* member;
        // Else the field must be given, to make one in Python
        bool has_default;
    };

    template <class S, class T>
    Field(std::string_view, T S::*, bool) -> Field<S, T>;

    // Some types
    template <class... Ts>
    struct Types
    {
    };

    template <class E>
    struct Member
    {
        E value;
        std::string_view name;
    };

    template <class E>
    Member(E, std::string_view) -> Member<E>;

    template <class T>
    concept Struct = requires { Reflect<T>::fields; };

    template <class T>
    concept Enum = requires { Reflect<T>::members; };
} // namespace dataclass_native
