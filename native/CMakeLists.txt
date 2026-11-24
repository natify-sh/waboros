# waboros native code (S0016): sljit as a library, built with CMake alone, standalone or as a
# subproject: add_subdirectory(native), or FetchContent_MakeAvailable with SOURCE_SUBDIR native.
cmake_minimum_required(VERSION 3.21)
project(waboros_native LANGUAGES C)

option(WABOROS_SLJIT_SHARED "build a shared library (ctypes, tests); static otherwise" ${PROJECT_IS_TOP_LEVEL})
option(WABOROS_SLJIT_DEBUG "sljit's SLJIT_DEBUG and SLJIT_VERBOSE" OFF)
option(WABOROS_BUILD_TESTS "build and register the ctest self-tests" ${PROJECT_IS_TOP_LEVEL})

set(sljit_dir "${CMAKE_CURRENT_SOURCE_DIR}/third_party/sljit/sljit_src")
if(NOT EXISTS "${sljit_dir}/sljitLir.c")
  message(FATAL_ERROR "sljit is missing: git submodule update --init native/third_party/sljit")
endif()

# One translation unit (it includes sljitLir.c); sljit's own CMakeLists.txt is not used.
if(WABOROS_SLJIT_SHARED)
  set(_kind SHARED)
else()
  set(_kind STATIC)
endif()
add_library(waboros_sljit ${_kind} "${CMAKE_CURRENT_SOURCE_DIR}/sljit/waboros_sljit.c")
add_library(waboros::sljit ALIAS waboros_sljit)
set_target_properties(waboros_sljit PROPERTIES
  C_STANDARD 99 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF POSITION_INDEPENDENT_CODE ON)
target_include_directories(waboros_sljit PUBLIC
  "$<BUILD_INTERFACE:${sljit_dir}>" "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/sljit>")
target_compile_definitions(waboros_sljit PRIVATE _DEFAULT_SOURCE)
target_compile_definitions(waboros_sljit PUBLIC SLJIT_CONFIG_AUTO=1)
# DEBUG and VERBOSE change the layout of struct sljit_compiler, so they are PUBLIC (users see the same)
if(WABOROS_SLJIT_DEBUG)
  target_compile_definitions(waboros_sljit PUBLIC SLJIT_VERBOSE=1 SLJIT_DEBUG=1)
else()
  target_compile_definitions(waboros_sljit PUBLIC SLJIT_VERBOSE=0 SLJIT_DEBUG=0)
endif()
if(NOT WABOROS_SLJIT_SHARED)  # sljit's symbols stay internal, only waboros_sljit_* is visible
  target_compile_definitions(waboros_sljit PUBLIC SLJIT_CONFIG_STATIC=1)
endif()

if(WABOROS_BUILD_TESTS)
  if(NOT WABOROS_SLJIT_SHARED)
    message(FATAL_ERROR "the tests call sljit's API: WABOROS_BUILD_TESTS needs WABOROS_SLJIT_SHARED")
  endif()
  enable_testing()
  add_executable(waboros_test_native tests/test_native.c)
  target_link_libraries(waboros_test_native PRIVATE waboros_sljit)
  set_target_properties(waboros_test_native PROPERTIES C_STANDARD 99 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
  target_compile_definitions(waboros_test_native PRIVATE _DEFAULT_SOURCE)
  add_test(NAME native COMMAND waboros_test_native)
  if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|ARM64)$")
    add_test(NAME bti_pingpong COMMAND waboros_test_native bti 1)
    if(CMAKE_CROSSCOMPILING_EMULATOR)  # the control: QEMU enforces BTI, so without the pad it is SIGILL
      add_test(NAME bti_control COMMAND waboros_test_native bti 0)
    endif()
  endif()
  # the embedding check: a tiny project that takes native/ as a subproject, both ways
  set(_tc "")
  if(CMAKE_CROSSCOMPILING)
    string(REPLACE ";" "\;" _emu "${CMAKE_CROSSCOMPILING_EMULATOR}")
    set(_tc -DCMAKE_SYSTEM_NAME=${CMAKE_SYSTEM_NAME} -DCMAKE_SYSTEM_PROCESSOR=${CMAKE_SYSTEM_PROCESSOR}
      -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER} "-DCMAKE_CROSSCOMPILING_EMULATOR=${_emu}")
  endif()
  foreach(_how add_subdirectory FetchContent)
    add_test(NAME consumer_${_how} COMMAND ${CMAKE_CTEST_COMMAND} --build-and-test
      "${CMAKE_CURRENT_SOURCE_DIR}/tests/consumer" "${CMAKE_CURRENT_BINARY_DIR}/consumer_${_how}"
      --build-generator "${CMAKE_GENERATOR}"
      --build-options --warn-uninitialized -Werror=dev "-DWABOROS_ROOT=${CMAKE_CURRENT_SOURCE_DIR}/.." -DEMBED=${_how} ${_tc}
      --test-command ${CMAKE_CTEST_COMMAND} --output-on-failure)
  endforeach()
endif()
