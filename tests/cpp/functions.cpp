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

    m.def("scaled", [](const dataclass_native::Array<double>& points, double factor) {
        std::vector<double> scaled = *points;
        for (double& point : scaled)
            point *= factor;
        return dataclass_native::Array<double>(std::move(scaled));
    });

    m.def("transposed", [](const dataclass_native::Array<double, 2>& values) {
        const auto [rows, columns] = values.shape();
        std::vector<double> transposed;
        for (std::size_t column = 0; column != columns; ++column)
            for (std::size_t row = 0; row != rows; ++row)
                transposed.push_back(values(row, column));
        return dataclass_native::Array<double, 2>(std::move(transposed), {columns, rows});
    });

    m.def("nothing", [](int) {});

    m.def("unannotated", [](int value) { return value; });
}
