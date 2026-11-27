# find_package(dataclass_native CONFIG REQUIRED), given dataclass_native_ROOT from `python -m dataclass_native cmake-dir`.
# Needs Python (Interpreter, Development.Module) and nanobind to have been found first.

get_filename_component(_dataclass_native_package "${CMAKE_CURRENT_LIST_DIR}" DIRECTORY)
set(dataclass_native_INCLUDE_DIR "${_dataclass_native_package}/include")
unset(_dataclass_native_package)

# dataclass_native_add_extensions(
#     [PACKAGES <package>...]         Packages, all of whose schema modules are used
#     [MODULES <module>...]           Schema modules
#     [PYTHONPATH <dir>...]           Where to import them from, if not installed
#     [GENERATED_DIRECTORY <dir>]     Where to generate the C++ (default: dataclass_native, in the binary directory)
#     [LIBRARY_DIRECTORY <dir>]       Build each extension here, under its package's path: e.g. the source directory
#                                     of the packages, so as to use them in place
#     [TARGETS_VARIABLE <variable>])  Set to the targets added
#
# Generates the C++ of the schema modules, and of those they use, when CMake configures. Adds a nanobind module for
# each: the extension pkg._mod_native for the schema module pkg.mod, installed to pkg/.
function(dataclass_native_add_extensions)
  cmake_parse_arguments(PARSE_ARGV 0 ARG "" "GENERATED_DIRECTORY;LIBRARY_DIRECTORY;TARGETS_VARIABLE"
                        "PACKAGES;MODULES;PYTHONPATH")
  if(NOT ARG_GENERATED_DIRECTORY)
    set(ARG_GENERATED_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/dataclass_native")
  endif()

  set(command "${Python_EXECUTABLE}" -m dataclass_native generate nanobind --out "${ARG_GENERATED_DIRECTORY}")
  foreach(package IN LISTS ARG_PACKAGES)
    list(APPEND command --package "${package}")
  endforeach()
  foreach(module IN LISTS ARG_MODULES)
    list(APPEND command --module "${module}")
  endforeach()
  foreach(path IN LISTS ARG_PYTHONPATH)
    list(PREPEND command "${CMAKE_COMMAND}" -E env "--modify" "PYTHONPATH=path_list_prepend:${path}")
  endforeach()
  execute_process(COMMAND ${command} COMMAND_ERROR_IS_FATAL ANY)

  include("${ARG_GENERATED_DIRECTORY}/extensions.cmake")
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${DATACLASS_NATIVE_SCHEMAS})

  set(targets)
  foreach(extension IN LISTS DATACLASS_NATIVE_EXTENSIONS)
    string(REPLACE "." "/" path "${extension}")
    string(REPLACE "." "_" target "${extension}")
    get_filename_component(directory "${path}" DIRECTORY)
    get_filename_component(name "${path}" NAME)

    nanobind_add_module(${target} "${ARG_GENERATED_DIRECTORY}/${path}.cpp")
    target_compile_features(${target} PRIVATE cxx_std_20)
    target_include_directories(${target} PRIVATE "${ARG_GENERATED_DIRECTORY}" "${dataclass_native_INCLUDE_DIR}")
    set_target_properties(${target} PROPERTIES OUTPUT_NAME "${name}")
    if(ARG_LIBRARY_DIRECTORY)
      # As a generator expression, so that generators such as Visual Studio do not add a directory per configuration
      set_target_properties(${target} PROPERTIES
                            LIBRARY_OUTPUT_DIRECTORY "$<1:${ARG_LIBRARY_DIRECTORY}/${directory}>")
    endif()
    if(NOT directory)
      set(directory ".")
    endif()
    install(TARGETS ${target} LIBRARY DESTINATION "${directory}")
    list(APPEND targets ${target})
  endforeach()

  if(ARG_TARGETS_VARIABLE)
    set(${ARG_TARGETS_VARIABLE} "${targets}" PARENT_SCOPE)
  endif()
endfunction()
