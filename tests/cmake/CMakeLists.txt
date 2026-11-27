# Builds the extensions of the schema package under SCHEMAS_ROOT, in place: as a project using dataclass-native would
cmake_minimum_required(VERSION 3.25)
project(dataclass_native_tests LANGUAGES CXX)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)

execute_process(
  COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
  OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE nanobind_ROOT COMMAND_ERROR_IS_FATAL ANY)
find_package(nanobind CONFIG REQUIRED)

execute_process(
  COMMAND "${Python_EXECUTABLE}" -m dataclass_native cmake-dir
  OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE dataclass_native_ROOT COMMAND_ERROR_IS_FATAL ANY)
find_package(dataclass_native CONFIG REQUIRED)

dataclass_native_add_extensions(PACKAGES schemas PYTHONPATH "${SCHEMAS_ROOT}" LIBRARY_DIRECTORY "${SCHEMAS_ROOT}")
