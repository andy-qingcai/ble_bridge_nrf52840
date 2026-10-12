/*
 * main.c - suota_sim, SUOTA peripheral simulator.
 *
 * Boot: address selection (fixed / NVS) -> bt_enable() -> identity ->
 * advertising.  Advertising payload mirrors the brief contract:
 *   - 16-bit service UUID 0xFEF5 (AD type 0x03, on-air F5 FE) AND the same
 *     UUID inside a manufacturer-specific segment (company id 0xFFFF),
 *   - complete local name "SuotaSim".
 *
 * Advertising schedule (Kconfig):
 *   - default: continuous connectable advertising at
 *     CONFIG_SUOTA_SIM_ADV_INTERVAL_MS (100 ms).
 *   - CONFIG_SUOTA_SIM_ADV_BURST=y: burst pattern - advertise
 *     SUOTA_SIM_ADV_BURST_ON_MS, silent for the remainder of
 *     SUOTA_SIM_ADV_BURST_PERIOD_S, repeat.
 *
 * After a REBOOT command the SUOTA service module reports the restart delay
 * (~10 s restart delay, reference-device behavior) and advertising resumes then.
 *
 * The MTU quirk itself needs no code here: CONFIG_BT_GATT_AUTO_UPDATE_MTU
 * makes the stack fire ATT_EXCHANGE_MTU_REQ during connection setup (see
 * prj.conf); we only observe it via the att_mtu_updated callback.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>

#include <string.h>

#include "sim_addr.h"
#include "suota_svc.h"

LOG_MODULE_REGISTER(suota_sim);

/* 100 ms => 160 units (BLE time = N * 0.625 ms), legal ADV_IND range. */
#define ADV_UNITS(ms) ((uint16_t)((((uint32_t)(ms)) * 1000U) / 625U))

BUILD_ASSERT(ADV_UNITS(CONFIG_SUOTA_SIM_ADV_INTERVAL_MS) >= 0x0020 &&
	     ADV_UNITS(CONFIG_SUOTA_SIM_ADV_INTERVAL_MS) <= 0x3FFF,
	     "SUOTA_SIM_ADV_INTERVAL_MS outside the legal advertising range");
BUILD_ASSERT((CONFIG_SUOTA_SIM_ADV_BURST_PERIOD_S * 1000) >
	     CONFIG_SUOTA_SIM_ADV_BURST_ON_MS,
	     "advertising burst must be shorter than its period");
BUILD_ASSERT(sizeof(CONFIG_SUOTA_SIM_ADV_NAME) <= 29,
	     "SUOTA_SIM_ADV_NAME too long for one advertising packet");

#define SUOTA_ADV_UUID16 0xfef5u
#define MSD_COMPANY_TEST 0xffffu /* Bluetooth test company id */

static uint8_t ad_flags = BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR;
static uint8_t ad_uuid16[2] = { BT_UUID_16_ENCODE(SUOTA_ADV_UUID16) };       /* F5 FE */
static uint8_t ad_msd[4] = { 0xFF, 0xFF, BT_UUID_16_ENCODE(SUOTA_ADV_UUID16) }; /* 0xFFFF + F5 FE */
static char adv_name[sizeof(CONFIG_SUOTA_SIM_ADV_NAME)];

static struct bt_data adv_data[] = {
	BT_DATA(BT_DATA_FLAGS, &ad_flags, sizeof(ad_flags)),
	BT_DATA(BT_DATA_UUID16_ALL, ad_uuid16, sizeof(ad_uuid16)),
	BT_DATA(BT_DATA_MANUFACTURER_DATA, ad_msd, sizeof(ad_msd)),
	BT_DATA(BT_DATA_NAME_COMPLETE, adv_name, sizeof(adv_name) - 1),
};

static const struct bt_le_adv_param adv_param = {
	.id = 0,
	.sid = 0,
	.options = BT_LE_ADV_OPT_CONN,
	.interval_min = ADV_UNITS(CONFIG_SUOTA_SIM_ADV_INTERVAL_MS),
	.interval_max = ADV_UNITS(CONFIG_SUOTA_SIM_ADV_INTERVAL_MS),
};

static bool adv_active;
static bool conn_active;

static struct k_work_delayable burst_work;
static struct k_work_delayable adv_restart_work;

static void adv_start(void)
{
	int err;

	if (conn_active) {
		return;
	}

	err = bt_le_adv_start(&adv_param, adv_data, ARRAY_SIZE(adv_data), NULL, 0);
	if (err) {
		LOG_ERR("advertising failed: %d", err);
		return;
	}
	adv_active = true;
	LOG_INF("advertising: \"%s\" 0xFEF5 @ %d ms%s",
		adv_name, CONFIG_SUOTA_SIM_ADV_INTERVAL_MS,
		IS_ENABLED(CONFIG_SUOTA_SIM_ADV_BURST) ? " (burst mode)" : "");

	if (IS_ENABLED(CONFIG_SUOTA_SIM_ADV_BURST)) {
		k_work_schedule(&burst_work,
				K_MSEC(CONFIG_SUOTA_SIM_ADV_BURST_ON_MS));
	}
}

static void burst_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);
	int err;

	if (adv_active) {
		err = bt_le_adv_stop();
		if (err) {
			LOG_ERR("burst stop failed: %d", err);
		}
		adv_active = false;
		LOG_INF("burst end, silent for %d s",
			CONFIG_SUOTA_SIM_ADV_BURST_PERIOD_S * 1000 -
			CONFIG_SUOTA_SIM_ADV_BURST_ON_MS);
		k_work_schedule(&burst_work,
				K_MSEC(CONFIG_SUOTA_SIM_ADV_BURST_PERIOD_S * 1000 -
				       CONFIG_SUOTA_SIM_ADV_BURST_ON_MS));
	} else {
		adv_start();
	}
}

static void adv_restart_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);
	LOG_INF("restart delay elapsed, re-advertising (REBOOT emulation)");
	adv_start();
}

/* Observability for the G1 gate: logs the negotiated ATT MTU the moment it
 * settles - i.e. the peripheral-initiated MTU exchange answered. */
static void mtu_updated(struct bt_conn *conn, uint16_t tx, uint16_t rx)
{
	ARG_UNUSED(conn);
	LOG_INF("ATT MTU updated: tx=%u rx=%u (peer MTU_REQ answered)", tx, rx);
}

static struct bt_gatt_cb gatt_cbs = {
	.att_mtu_updated = mtu_updated,
};

static void connected(struct bt_conn *conn, uint8_t err)
{
	char addr_str[BT_ADDR_LE_STR_LEN];

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr_str, sizeof(addr_str));

	if (err) {
		LOG_ERR("connection failed (%u) from %s", err, addr_str);
		return;
	}

	conn_active = true;
	adv_active = false; /* connectable adv is torn down by the stack */
	k_work_cancel_delayable(&burst_work);
	k_work_cancel_delayable(&adv_restart_work);

	LOG_INF("connected: %s", addr_str);
	suota_connected(conn);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	char addr_str[BT_ADDR_LE_STR_LEN];
	uint32_t delay_ms;

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr_str, sizeof(addr_str));
	LOG_INF("disconnected (reason 0x%02x): %s", reason, addr_str);

	conn_active = false;

	delay_ms = suota_disconnected(conn);
	if (delay_ms == 0) {
		adv_start();
	} else {
		/* REBOOT emulation: stay quiet for the configured delay. */
		k_work_schedule(&adv_restart_work, K_MSEC(delay_ms));
	}
}

BT_CONN_CB_DEFINE(conn_cbs) = {
	.connected = connected,
	.disconnected = disconnected,
};

int main(void)
{
	bt_addr_le_t addr;
	bool have_addr;
	int err;

	suota_init();

	have_addr = sim_addr_get(&addr) == 0;

	err = bt_enable(NULL);
	if (err) {
		LOG_ERR("bt_enable failed: %d", err);
		return err;
	}
	LOG_INF("Bluetooth ready");

	err = bt_set_name(CONFIG_SUOTA_SIM_ADV_NAME);
	if (err) {
		LOG_WRN("bt_set_name failed: %d", err);
	}
	strcpy(adv_name, CONFIG_SUOTA_SIM_ADV_NAME);

	if (have_addr) {
		char addr_str[BT_ADDR_LE_STR_LEN];

		err = bt_id_create(&addr, NULL);
		if (err < 0) {
			LOG_ERR("bt_id_create(fixed) failed: %d, falling back", err);
		}
		bt_addr_le_to_str(&addr, addr_str, sizeof(addr_str));
		LOG_INF("identity address: %s", addr_str);
	}
	if (!have_addr || err < 0) {
		/* Stack-generated random static address (changes per boot). */
		err = bt_id_create(NULL, NULL);
		if (err < 0) {
			LOG_ERR("bt_id_create failed: %d", err);
			return err;
		}
	}

	bt_gatt_cb_register(&gatt_cbs);

	k_work_init_delayable(&burst_work, burst_work_handler);
	k_work_init_delayable(&adv_restart_work, adv_restart_work_handler);

	adv_start();

	for (;;) {
		k_sleep(K_FOREVER);
	}
	return 0;
}
