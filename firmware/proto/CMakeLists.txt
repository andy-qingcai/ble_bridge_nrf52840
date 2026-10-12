# bridge_proto - UART bridge protocol v2 codec (ADR-002).
#
# Standalone build (host tools / L2 contract tests):
#
#     cmake -S . -B build                      # static lib bridge_proto
#     cmake -S . -B build -DBRIDGE_PROTO_SHARED=ON   # shared lib bridge_proto
#     cmake --build build
#
# Zephyr firmware build (both firmware/app and firmware/suota_sim):
#
#     # in the application CMakeLists.txt:
#     add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../proto proto_build)
#     target_link_libraries(app PUBLIC bridge_proto)
#
# No toolchain is hard-coded here; when pulled in via add_subdirectory() the
# parent project's compiler and flags are used.

cmake_minimum_required(VERSION 3.13)

# Declare a project only when built standalone; when included via
# add_subdirectory() (Zephyr app, host build) PROJECT_NAME is already defined.
if(NOT DEFINED PROJECT_NAME)
  project(bridge_proto C)
  set(BRIDGE_PROTO_STANDALONE ON)
endif()

option(BRIDGE_PROTO_SHARED "Build bridge_proto as a shared library (host/L2 contract tests)" OFF)

if(BRIDGE_PROTO_SHARED)
  set(BRIDGE_PROTO_LIB_TYPE SHARED)
else()
  set(BRIDGE_PROTO_LIB_TYPE STATIC)
endif()

add_library(bridge_proto ${BRIDGE_PROTO_LIB_TYPE})
target_sources(bridge_proto PRIVATE bridge_proto.c)
target_include_directories(bridge_proto PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_features(bridge_proto PUBLIC c_std_11)

# Zephyr builds: inherit the Zephyr toolchain flags (-mthumb, -mcpu=cortex-m4,
# ...).  A plain CMake static library does NOT get them implicitly: interface
# options flow into the `app` target only, never sideways into this one, so
# without this the file compiles in the compiler's default ARM (A32) state.
# The Cortex-M4 cannot execute A32 code - on real hardware the first
# bridge_proto call (bridge_parser_init from bridge_link_init) died with
# MemManage IACCV at PC=0xFFFFFE00 while native_sim (x86) never noticed.
# bring-up root cause, 2026-09-25.
if(TARGET zephyr_interface)
  target_link_libraries(bridge_proto PRIVATE zephyr_interface)
endif()

if(BRIDGE_PROTO_STANDALONE)
  # Strict hygiene for the standalone/host build; Zephyr builds keep the
  # toolchain's own warning policy.
  set_target_properties(
    bridge_proto PROPERTIES
    C_STANDARD 11
    C_STANDARD_REQUIRED ON
    C_EXTENSIONS OFF
  )
  target_compile_options(bridge_proto PRIVATE -Wall -Wextra -Werror)
endif()
