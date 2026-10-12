# BLE bridge firmware (Task 9 data plane, Task 10 BLE central).
#
# Reuses the C frame library in ../proto (Task 6) verbatim via
# add_subdirectory(); only bridge_status_t was appended to bridge_proto.h.
#
# Pure-logic units (bridge_dispatch.c, bridge_info.c, bridge_ble_pure.c)
# deliberately include NO Zephyr headers - only the standard headers plus
# bridge_proto.h - so they compile as a standalone host target for
# host-side unit tests later (ADR-005 style):
#
#     cc -std=c11 -Wall -Wextra -I firmware/proto \
#        -c firmware/app/src/bridge_dispatch.c \
#        -c firmware/app/src/bridge_ble_pure.c \
#        -c firmware/app/src/bridge_info.c -c firmware/proto/bridge_proto.c

cmake_minimum_required(VERSION 3.20.0)

# ---- Bridge version, single bump point (D3, task-dfu-a report §9.5) -------
# BRIDGE_VERSION_STR is the ONE place to bump: "maj.min.rev[-suffix]".
#   - numeric maj.min.rev -> MCUboot image header version (imgtool --version,
#     "maj.min.rev+build": imgtool rejects non-numeric build counters, so a
#     pre-release suffix like "-dfu-test" rides in the ping string only);
#   - full string (suffix included) -> CONFIG_BRIDGE_VERSION (bridge ping
#     "blebridge<version>") through the APPVERSION Kconfig default.
# The injection happens at the Kconfig stage (env exported to kconfig.py by
# zephyr/cmake/modules/kconfig.cmake:156-159) because the signing script that
# finally reads CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION
# (nrf/cmake/sysbuild/image_signing.cmake:88) is included from INSIDE
# find_package(Zephyr) -> zephyr/CMakeLists.txt:2203; anything a CMakeLists
# sets after find_package is too late.  Upstream never exports the guard
# variable VERSION_MAJOR (zephyr/modules/Kconfig.mcuboot:111), which is why
# the imgtool-version default stayed "0.0.0+0" - exported here to engage it.
#
# NOTE: -DCONFIG_BRIDGE_VERSION=<s> still overrides the ping string alone
# (Kconfig CLI precedence) but then the ping string and the image header
# version decouple.  sysbuild does not forward arbitrary -D variables to the
# image builds, so the bump path is editing BRIDGE_VERSION_STR above (a
# non-sysbuild single-image `west build -DBRIDGE_VERSION_STR=...` works too).
set(BRIDGE_VERSION_STR "0.6.3" CACHE STRING
    "bridge version, maj.min.rev with optional -suffix")
string(REGEX MATCH "^([0-9]+)\\.([0-9]+)\\.([0-9]+)(-.*)?$"
       _bridge_semver "${BRIDGE_VERSION_STR}"
)
if(NOT _bridge_semver)
  message(FATAL_ERROR
    "BRIDGE_VERSION_STR (\"${BRIDGE_VERSION_STR}\") must be numeric "
    "maj.min.rev with an optional -suffix (D3: it also forms the MCUboot "
    "image header version)."
  )
endif()
set(APP_VERSION_STRING "${BRIDGE_VERSION_STR}")
set(APP_VERSION_TWEAK_STRING
    "${CMAKE_MATCH_1}.${CMAKE_MATCH_2}.${CMAKE_MATCH_3}+0")
set(ENV{VERSION_MAJOR} "${CMAKE_MATCH_1}")

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bridge_bridge LANGUAGES C)

set(BRIDGE_PROTO_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../proto)
add_subdirectory(${BRIDGE_PROTO_DIR} proto_build)

target_link_libraries(app PRIVATE bridge_proto)
target_include_directories(app PRIVATE src)

target_sources(app PRIVATE
  src/main.c
  src/bridge_phy.c
  src/bridge_link.c
  src/bridge_dispatch.c
  src/bridge_info.c
  src/bridge_ble.c
  src/bridge_ble_pure.c
  src/bridge_periph.c
)

# BRIDGE_PHY is a string Kconfig ("cdc" | "uart0"); map it onto a single
# compile definition so the C sources can select at compile time (C
# preprocessor cannot compare strings).
if(CONFIG_BRIDGE_PHY STREQUAL "uart0")
  target_compile_definitions(app PRIVATE BRIDGE_PHY_MODE_UART0=1)
else()
  if(NOT CONFIG_BRIDGE_PHY STREQUAL "cdc")
    message(FATAL_ERROR "CONFIG_BRIDGE_PHY must be \"cdc\" or \"uart0\", got \"${CONFIG_BRIDGE_PHY}\"")
  endif()
  target_compile_definitions(app PRIVATE BRIDGE_PHY_MODE_CDC=1)
endif()

# Self-imposed warning hygiene for the new sources (Zephyr already adds
# -Wall; -Wextra is our own bar for this app).
target_compile_options(app PRIVATE -Wall -Wextra)

# NOTE for anyone trying to set CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION here
# (after find_package): that CMake variable is consumed by the signing script
# while find_package(Zephyr) is still running (see the version block at the
# top of this file for the working injection point and the full story).
