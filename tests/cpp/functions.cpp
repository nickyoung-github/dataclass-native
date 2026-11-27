// The extension schemas._functions: the native functions of tests/schemas/functions.py, written by hand as a
// project's would be
#include "dataclass_native/python.hpp"

#include <chrono>
#include <optional>
#include <string>
#include <vector>

#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include "schemas/common.hpp"

using namespace schemas::common;

NB_MODULE(_functions, m)
{
    m.def("implementation", [] { return "native"; });

    m.def("total", [](const std::vector<Money>& amounts, Currency currency) {
        Money total{0, currency};
        for (const auto& money : amounts)
            if (money.currency == currency)
                total.amount += money.amount;
        return total;
    });

    m.def("renamed", [](Party party, std::optional<std::string> name) {
        party.name = name.value_or("?");
        return party;
    });

    m.def("same", [](dataclass_native::Shared<Party> party) { return party; });

    m.def("later", [](dataclass_native::Date day, int days) { return day + std::chrono::days{days}; });

    m.def("nothing", [](int) {});

    m.def("unannotated", [](int value) { return value; });
}
