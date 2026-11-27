// Plain C++, of the generated structs: nothing here knows about Python
#pragma once

#include <optional>
#include <utility>

#include "shop/model.hpp"

namespace shop
{
    inline double total(const model::Order& order)
    {
        double sum = 0;
        for (const auto& line : order.lines)
            sum += line.quantity * line.unit_price;
        return sum;
    }

    // The order, shipped to the address given, or else to its customer's
    inline model::Order ship(model::Order order, std::optional<model::Address> to)
    {
        order.status = model::Status::SHIPPED;
        order.ship_to = to ? std::move(*to) : order.customer->address;
        return order;
    }
} // namespace shop
