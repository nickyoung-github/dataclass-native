// The extension shop._pricing: the functions of pricing.hpp, for those of shop/pricing.py
#include "dataclass_native/python.hpp"

#include <nanobind/stl/optional.h>

#include "pricing.hpp"

NB_MODULE(_pricing, m)
{
    m.def("total", &shop::total);
    m.def("ship", &shop::ship);
}
