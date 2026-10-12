/*
 * sim_addr.c - suota_sim device address selection (Task 11).
 *
 * "fixed": compile-time constant static random address (default;
 *          the address must have the two most significant bits of its
 *          first octet set to be a legal static random address).
 * "nvs":   static random address generated once with bt_addr_le_create_static()
 *          and persisted in NVS on the board's storage_partition, so the
 *          address survives reboots (bonding-friendly mode).
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/kvss/nvs.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/bluetooth/addr.h>

#include <errno.h>
#include <string.h>

#include "sim_addr.h"

LOG_MODULE_DECLARE(suota_sim);

#define SIM_ADDR_NVS_ID 1

/* nRF52840 flash page / erase unit. */
#define SIM_ADDR_SECTOR_SIZE 4096u

static bool addr_is_static_random(const bt_addr_le_t *a)
{
	/* Static random device address: the two most significant bits of the
	 * most significant octet are 1 (BT Core 5.3 Vol 6 B 1.3). */
	return (a->a.val[5] & 0xC0u) == 0xC0u;
}

static int addr_fixed(bt_addr_le_t *out)
{
	int rc = bt_addr_from_str(CONFIG_SUOTA_SIM_ADDR, &out->a);

	if (rc) {
		LOG_ERR("SUOTA_SIM_ADDR \"%s\" is not a valid address",
			CONFIG_SUOTA_SIM_ADDR);
		return -EINVAL;
	}
	out->type = BT_ADDR_LE_RANDOM;

	if (!addr_is_static_random(out)) {
		LOG_ERR("SUOTA_SIM_ADDR is not a static random address "
			"(top two bits of %02x must be 1)", out->a.val[5]);
		return -EINVAL;
	}
	return 0;
}

static int addr_nvs(bt_addr_le_t *out)
{
	static struct nvs_fs fs;
	const struct flash_area *fa;
	ssize_t rc;

	rc = flash_area_open(PARTITION_ID(storage_partition), &fa);
	if (rc) {
		LOG_ERR("storage_partition open failed: %d", (int)rc);
		return (int)rc;
	}

	fs.flash_device = flash_area_get_device(fa);
	fs.offset = fa->fa_off;
	fs.sector_size = SIM_ADDR_SECTOR_SIZE;
	fs.sector_count = fa->fa_size / fs.sector_size;

	rc = nvs_mount(&fs);
	if (rc) {
		LOG_ERR("nvs_mount failed: %d", (int)rc);
		flash_area_close(fa);
		return (int)rc;
	}

	rc = nvs_read(&fs, SIM_ADDR_NVS_ID, &out->a, sizeof(out->a));
	if (rc == sizeof(out->a) && addr_is_static_random(out)) {
		out->type = BT_ADDR_LE_RANDOM;
		LOG_INF("address restored from NVS: %02x:%02x:%02x:%02x:%02x:%02x",
			out->a.val[5], out->a.val[4], out->a.val[3],
			out->a.val[2], out->a.val[1], out->a.val[0]);
		flash_area_close(fa);
		return 0;
	}

	rc = bt_addr_le_create_static(out);
	if (rc) {
		LOG_ERR("bt_addr_le_create_static failed: %d", (int)rc);
		flash_area_close(fa);
		return (int)rc;
	}

	rc = nvs_write(&fs, SIM_ADDR_NVS_ID, &out->a, sizeof(out->a));
	if (rc != sizeof(out->a)) {
		LOG_ERR("NVS address persist failed: %d (using it anyway for this boot)",
			(int)rc);
	} else {
		LOG_INF("new static random address generated and persisted");
	}
	out->type = BT_ADDR_LE_RANDOM;
	flash_area_close(fa);
	return 0;
}

int sim_addr_get(bt_addr_le_t *out)
{
	if (strcmp(CONFIG_SUOTA_SIM_ADDR_MODE, "fixed") == 0) {
		return addr_fixed(out);
	}
	if (strcmp(CONFIG_SUOTA_SIM_ADDR_MODE, "nvs") == 0) {
		return addr_nvs(out);
	}
	LOG_ERR("SUOTA_SIM_ADDR_MODE must be \"fixed\" or \"nvs\", got \"%s\"",
		CONFIG_SUOTA_SIM_ADDR_MODE);
	return -EINVAL;
}
