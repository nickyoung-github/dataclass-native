cmake_minimum_required(VERSION 3.25)
project(shop LANGUAGES CXX)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)

execute_process(
  COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
  OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE nanobind_ROOT COMMAND_ERROR_IS_FATAL ANY)
find_package(nanobind CONFIG REQUIRED)

execute_process(
  COMMAND "${Python_EXECUTABLE}" -m dataclass_native cmake-dir
  OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE dataclass_native_ROOT COMMAND_ERROR_IS_FATAL ANY)
find_package(dataclass_native CONFIG REQUIRED)

# Generates shop/model.hpp, and builds the extension shop._model_native, installed alongside shop/model.py
dataclass_native_add_extensions(PACKAGES shop PYTHONPATH "${CMAKE_SOURCE_DIR}/src")

# The hand-written extension shop._pricing: it includes shop/model.hpp, which is generated into this directory
nanobind_add_module(_pricing cpp/bindings.cpp)
target_compile_features(_pricing PRIVATE cxx_std_20)
target_include_directories(_pricing PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/dataclass_native"
                                            "${dataclass_native_INCLUDE_DIR}")
install(TARGETS _pricing LIBRARY DESTINATION shop)
