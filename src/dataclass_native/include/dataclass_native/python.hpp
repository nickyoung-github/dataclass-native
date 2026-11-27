// Conversions between Python objects and the types of dataclass_native/types.hpp, for nanobind bindings. Include it
// before any use of those types in a binding, as it converts Bytes (a std::vector) to bytes, not a list.
#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <utility>

#include <nanobind/nanobind.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/variant.h>

#include <datetime.h>

#include "dataclass_native/msgpack.hpp"
#include "dataclass_native/types.hpp"

namespace dataclass_native::python
{
    // Python's datetime C API. Not datetime.h's PyDateTimeAPI, which is a variable of each translation unit
    inline PyDateTime_CAPI* datetime_api()
    {
        static PyDateTime_CAPI* api = static_cast<PyDateTime_CAPI*>(PyCapsule_Import(PyDateTime_CAPSULE_NAME, 0));
        return api;
    }

    inline bool is_exactly(PyObject* o, PyTypeObject* type)
    {
        return Py_TYPE(o) == type;
    }

    // The microseconds of a timedelta, if they fit
    inline bool microseconds(PyObject* delta, std::chrono::microseconds& value)
    {
        // Beyond this many days, the microseconds overflow 64 bits
        constexpr std::int64_t max_days = 106'751'990;
        const std::int64_t days = PyDateTime_DELTA_GET_DAYS(delta);
        if (days < -max_days || days > max_days)
            return false;
        value = std::chrono::microseconds{(days * 86'400 + PyDateTime_DELTA_GET_SECONDS(delta)) * 1'000'000 +
                                          PyDateTime_DELTA_GET_MICROSECONDS(delta)};
        return true;
    }

    // A native object as msgpack, and one read from it: see msgpack.hpp
    template <class T>
    nanobind::bytes to_msgpack(const T& value)
    {
        const Bytes data = msgpack::to_msgpack(value);
        return nanobind::bytes(reinterpret_cast<const char*>(data.data()), data.size());
    }

    template <class T>
    T from_msgpack(nanobind::bytes data)
    {
        return msgpack::from_msgpack<T>({reinterpret_cast<const std::uint8_t*>(data.c_str()), data.size()});
    }
} // namespace dataclass_native::python

NAMESPACE_BEGIN(NB_NAMESPACE)
NAMESPACE_BEGIN(detail)

template <>
struct type_caster<dataclass_native::Bytes>
{
    NB_TYPE_CASTER(dataclass_native::Bytes, const_name("bytes"))

    bool from_python(handle src, uint32_t, cleanup_list*) noexcept
    {
        if (!PyBytes_Check(src.ptr()))
            return false;
        const auto* data = reinterpret_cast<const std::uint8_t*>(PyBytes_AsString(src.ptr()));
        value.assign(data, data + PyBytes_Size(src.ptr()));
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy, cleanup_list*) noexcept
    {
        return PyBytes_FromStringAndSize(reinterpret_cast<const char*>(value.data()),
                                         static_cast<Py_ssize_t>(value.size()));
    }
};

template <>
struct type_caster<dataclass_native::Date>
{
    NB_TYPE_CASTER(dataclass_native::Date, const_name("datetime.date"))

    bool from_python(handle src, uint32_t, cleanup_list*) noexcept
    {
        PyObject* o = src.ptr();
        // Exactly, as a datetime is a date
        if (!dataclass_native::python::is_exactly(o, dataclass_native::python::datetime_api()->DateType))
            return false;
        value = std::chrono::year{PyDateTime_GET_YEAR(o)} /
                std::chrono::month{static_cast<unsigned>(PyDateTime_GET_MONTH(o))} /
                std::chrono::day{static_cast<unsigned>(PyDateTime_GET_DAY(o))};
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy, cleanup_list*) noexcept
    {
        auto* api = dataclass_native::python::datetime_api();
        const std::chrono::year_month_day ymd{value};
        return api->Date_FromDate(static_cast<int>(ymd.year()), static_cast<int>(static_cast<unsigned>(ymd.month())),
                                  static_cast<int>(static_cast<unsigned>(ymd.day())), api->DateType);
    }
};

template <>
struct type_caster<dataclass_native::TimeDelta>
{
    NB_TYPE_CASTER(dataclass_native::TimeDelta, const_name("datetime.timedelta"))

    bool from_python(handle src, uint32_t, cleanup_list*) noexcept
    {
        return dataclass_native::python::is_exactly(src.ptr(), dataclass_native::python::datetime_api()->DeltaType) &&
               dataclass_native::python::microseconds(src.ptr(), value);
    }

    static handle from_cpp(const Value& value, rv_policy, cleanup_list*) noexcept
    {
        auto* api = dataclass_native::python::datetime_api();
        const auto days = std::chrono::floor<std::chrono::days>(value);
        const auto seconds = std::chrono::floor<std::chrono::seconds>(value - days);
        return api->Delta_FromDelta(static_cast<int>(days.count()), static_cast<int>(seconds.count()),
                                    static_cast<int>((value - days - seconds).count()), 0, api->DeltaType);
    }
};

template <>
struct type_caster<dataclass_native::TimeOfDay>
{
    NB_TYPE_CASTER(dataclass_native::TimeOfDay, const_name("datetime.time"))

    bool from_python(handle src, uint32_t, cleanup_list*) noexcept
    {
        PyObject* o = src.ptr();
        if (!dataclass_native::python::is_exactly(o, dataclass_native::python::datetime_api()->TimeType) ||
            PyDateTime_TIME_GET_TZINFO(o) != Py_None)
            return false;
        value.since_midnight =
            std::chrono::hours{PyDateTime_TIME_GET_HOUR(o)} + std::chrono::minutes{PyDateTime_TIME_GET_MINUTE(o)} +
            std::chrono::seconds{PyDateTime_TIME_GET_SECOND(o)} +
            std::chrono::microseconds{PyDateTime_TIME_GET_MICROSECOND(o)};
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy, cleanup_list*) noexcept
    {
        auto* api = dataclass_native::python::datetime_api();
        const std::chrono::hh_mm_ss time{value.since_midnight};
        return api->Time_FromTime(static_cast<int>(time.hours().count()), static_cast<int>(time.minutes().count()),
                                  static_cast<int>(time.seconds().count()),
                                  static_cast<int>(time.subseconds().count()), Py_None, api->TimeType);
    }
};

// An aware datetime, of any timezone: read back as UTC
template <>
struct type_caster<dataclass_native::DateTime>
{
    NB_TYPE_CASTER(dataclass_native::DateTime, const_name("datetime.datetime"))

    bool from_python(handle src, uint32_t, cleanup_list*) noexcept
    {
        auto* api = dataclass_native::python::datetime_api();
        PyObject* o = src.ptr();
        if (!dataclass_native::python::is_exactly(o, api->DateTimeType))
            return false;

        std::chrono::microseconds offset{};
        if (PyObject* tzinfo = PyDateTime_DATE_GET_TZINFO(o); tzinfo != api->TimeZone_UTC)
        {
            if (tzinfo == Py_None)
                return false;
            PyObject* delta = PyObject_CallMethod(o, "utcoffset", nullptr);
            const bool valid = delta && dataclass_native::python::is_exactly(delta, api->DeltaType) &&
                               dataclass_native::python::microseconds(delta, offset);
            Py_XDECREF(delta);
            if (!valid)
            {
                PyErr_Clear();
                return false;
            }
        }

        const std::chrono::sys_days day = std::chrono::year{PyDateTime_GET_YEAR(o)} /
                                          std::chrono::month{static_cast<unsigned>(PyDateTime_GET_MONTH(o))} /
                                          std::chrono::day{static_cast<unsigned>(PyDateTime_GET_DAY(o))};
        value = day + std::chrono::hours{PyDateTime_DATE_GET_HOUR(o)} +
                std::chrono::minutes{PyDateTime_DATE_GET_MINUTE(o)} +
                std::chrono::seconds{PyDateTime_DATE_GET_SECOND(o)} +
                std::chrono::microseconds{PyDateTime_DATE_GET_MICROSECOND(o)} - offset;
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy, cleanup_list*) noexcept
    {
        auto* api = dataclass_native::python::datetime_api();
        const auto day = std::chrono::floor<std::chrono::days>(value);
        const std::chrono::year_month_day ymd{day};
        const std::chrono::hh_mm_ss time{value - day};
        return api->DateTime_FromDateAndTime(
            static_cast<int>(ymd.year()), static_cast<int>(static_cast<unsigned>(ymd.month())),
            static_cast<int>(static_cast<unsigned>(ymd.day())), static_cast<int>(time.hours().count()),
            static_cast<int>(time.minutes().count()), static_cast<int>(time.seconds().count()),
            static_cast<int>(time.subseconds().count()), api->TimeZone_UTC, api->DateTimeType);
    }
};

// The Python object of a Shared<T> is that of its T, and keeps it alive: sharing it shares the object
template <typename T>
struct type_caster<dataclass_native::Shared<T>>
{
    using Caster = make_caster<std::shared_ptr<const T>>;
    NB_TYPE_CASTER(dataclass_native::Shared<T>, Caster::Name)

    bool from_python(handle src, uint32_t flags, cleanup_list* cleanup) noexcept
    {
        Caster caster;
        if (src.is_none() || !caster.from_python(src, flags, cleanup))
            return false;
        value = Value(std::move(caster.value));
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy policy, cleanup_list* cleanup) noexcept
    {
        return Caster::from_cpp(value.ptr(), policy, cleanup);
    }
};

// As the std::variant it is
template <typename... Ts>
struct type_caster<dataclass_native::DiscriminatedUnion<Ts...>>
{
    using Caster = make_caster<std::variant<Ts...>>;
    NB_TYPE_CASTER(dataclass_native::DiscriminatedUnion<Ts...>, Caster::Name)

    bool from_python(handle src, uint32_t flags, cleanup_list* cleanup) noexcept
    {
        Caster caster;
        if (!caster.from_python(src, flags, cleanup))
            return false;
        value = Value(caster.operator cast_t<std::variant<Ts...>>());
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy policy, cleanup_list* cleanup) noexcept
    {
        return Caster::from_cpp(value.as_variant(), policy, cleanup);
    }
};

// As its T. An empty Indirect, which Python cannot make, is None
template <typename T>
struct type_caster<dataclass_native::Indirect<T>>
{
    using Caster = make_caster<T>;
    NB_TYPE_CASTER(dataclass_native::Indirect<T>, Caster::Name)

    bool from_python(handle src, uint32_t flags, cleanup_list* cleanup) noexcept
    {
        Caster caster;
        if (!caster.from_python(src, flags_for_local_caster<T>(flags), cleanup) || !caster.template can_cast<T>())
            return false;
        value = Value(caster.operator cast_t<T>());
        return true;
    }

    static handle from_cpp(const Value& value, rv_policy policy, cleanup_list* cleanup) noexcept
    {
        if (!value)
            return none().release();
        return Caster::from_cpp(*value, policy, cleanup);
    }
};

NAMESPACE_END(detail)
NAMESPACE_END(NB_NAMESPACE)
