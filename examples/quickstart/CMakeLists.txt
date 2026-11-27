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
