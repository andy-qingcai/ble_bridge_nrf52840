/*
 * sim_addr.h - suota_sim device address selection (Task 11).
 *
 * SUOTA_SIM_ADDR_MODE = "fixed": compile-time constant static random
 *   address (CONFIG_SUOTA_SIM_ADDR), deterministic across reboots.
 * SUOTA_SIM_ADDR_MODE = "nvs": static random address generated once and
 *   persisted in NVS on the board's storage_partition.
 */

#ifndef SUOTA_SIM_SIM_ADDR_H_
#define SUOTA_SIM_SIM_ADDR_H_

#include <zephyr/bluetooth/addr.h>

/* Returns 0 and fills *out (BT_ADDR_LE_RANDOM) on success, -errno otherwise. */
int sim_addr_get(bt_addr_le_t *out);

#endif /* SUOTA_SIM_SIM_ADDR_H_ */
