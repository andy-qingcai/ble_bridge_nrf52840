# suota_sim - SUOTA peripheral simulator.
#
# Standalone Zephyr peripheral application that emulates a reference SUOTA
# device: 0xFEF5 SUOTA service with all 10 characteristics,
# immediate ATT MTU exchange on connect (via CONFIG_BT_GATT_AUTO_UPDATE_MTU,
# see prj.conf) and the slave-side SUOTA state machine.
#
# Build (gate):
#   west build -p -b nrf52840dk/nrf52840 firmware/suota_sim -d ~/blebridge/ncs/build-suota_sim

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(suota_sim LANGUAGES C)

target_sources(app PRIVATE
  src/main.c
  src/suota_svc.c
  src/sim_addr.c
)

# Same warning bar as the bridge app (Zephyr already adds -Wall).
target_compile_options(app PRIVATE -Wall -Wextra)
