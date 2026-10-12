/*
 * suota_svc.c - SUOTA (0xFEF5) slave emulation for suota_sim (Task 11).
 *
 * Everything in here is a 1:1 re-creation of the observable behavior of a
 * reference SUOTA (0xFEF5) device, per Dialog/Renesas AN-B-022 and live
 * captures:
 *
 *   - 10 characteristics with the canonical SUOTA UUIDs/properties.
 *   - Slave-side state machine: MEM_DEV 0x12/0x13 starts
 *     the session and notifies 0x10, PATCH_LEN arms a block, PATCH_DATA
 *     (write-without-response) accumulates it, block end => delayed 0x02
 *     ACK, END/REBOOT/ABORT commands.
 *   - Accounting mode: the image is not stored - only a running byte total
 *     and a running XOR over every received byte are kept (the XOR of the
 *     whole upload is 0 iff the sender appended the correct whole-file
 *     XOR checksum byte).
 *   - Status codes: 0x02 OK, 0x04 CRC_MISMATCH, 0x05 PATCH_LENGTH_ERROR,
 *     0x08 INVALID_MEMORY_TYPE, 0x10 IMAGE_STARTED, 0x13 INVALID_IMAGE_SIZE,
 *     0x15 SAME_IMAGE_ERROR.  Behavior note (live measurement): the
 *     reference device refuses the MEM_DEV start write *in the ATT write
 *     response* (error 0x15) and sends no notification; AN-B-022 lists
 *     0x15 as a SERV_STATUS notify value -- the observed behavior wins,
 *     and host suota.py maps both paths to SameImageError.
 *
 * Timing rules: notify values are static; the PATCH_DATA write callback only
 * does accounting (no heavy work) and every notification timing goes through
 * a single k_work_delayable.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/__assert.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>

#include <errno.h>
#include <string.h>

#include "suota_svc.h"

LOG_MODULE_DECLARE(suota_sim);

/* ------------------------------------------------------------------ */
/* SERV_STATUS codes (AN-B-022, S1 SuotaProfile). */
/* ------------------------------------------------------------------ */
#define SUOTA_STATUS_OK                 0x02u /* block ACK / END ACK          */
#define SUOTA_STATUS_CRC_MISMATCH       0x04u /* XOR checksum failed          */
#define SUOTA_STATUS_PATCH_LENGTH_ERROR 0x05u /* block bytes != PATCH_LEN     */
#define SUOTA_STATUS_INVALID_MEM_TYPE   0x08u /* unknown MEM_DEV command      */
#define SUOTA_STATUS_IMAGE_STARTED      0x10u /* SET_MEMORY_DEVICE ack        */
#define SUOTA_STATUS_INVALID_IMAGE_SIZE 0x13u /* END: total size mismatch     */
#define SUOTA_STATUS_SAME_IMAGE_ERROR   0x15u /* MEM_DEV bank == running bank */

/* SAME_IMAGE as an ATT-level write-response error: the reference device
 * carries the 0x15 refusal in the MEM_DEV write response itself instead
 * of notifying SERV_STATUS (AN-B-022 wording).
 * Not a standard ATT error code: Dialog's SUOTA profile reuses the slot. */
#define SUOTA_ATT_ERR_SAME_IMAGE 0x15u

/* MEM_DEV command bytes (AN-B-022 SuotaProfile). */
#define SUOTA_MEM_DEV_TYPE_I2C   0x12u
#define SUOTA_MEM_DEV_TYPE_SPI   0x13u
#define SUOTA_MEM_DEV_CMD_END    0xFEu
#define SUOTA_MEM_DEV_CMD_REBOOT 0xFDu
#define SUOTA_MEM_DEV_CMD_ABORT  0xFFu

/* Advertising-facing 16-bit service UUID: 0xFEF5, on-air LE bytes F5 FE. */
#define SUOTA_SERVICE_UUID16 0xfef5u

/* ------------------------------------------------------------------ */
/* GATT table layout (declared early: suota_notify_status() below needs
 * the SERV_STATUS value attribute).                                     */
/* ------------------------------------------------------------------ */
enum {
	IDX_SVC = 0,
	IDX_MEM_DEV,
	IDX_MEM_DEV_VAL,
	IDX_GPIO_MAP,
	IDX_GPIO_MAP_VAL,
	IDX_MEM_INFO,
	IDX_MEM_INFO_VAL,
	IDX_PATCH_LEN,
	IDX_PATCH_LEN_VAL,
	IDX_PATCH_DATA,
	IDX_PATCH_DATA_VAL,
	IDX_SERV_STATUS,
	IDX_SERV_STATUS_VAL,
	IDX_SERV_STATUS_CCC,
	IDX_VERSION,
	IDX_VERSION_VAL,
	IDX_CHUNK_SIZE,
	IDX_CHUNK_SIZE_VAL,
	IDX_MTU,
	IDX_MTU_VAL,
	IDX_PSM,
	IDX_PSM_VAL,
	IDX_COUNT,
};

/* Created by BT_GATT_SERVICE_DEFINE() further down. */
extern const struct bt_gatt_attr attr_suota_svc[];

/* ------------------------------------------------------------------ */
/* UUIDs - derived from the canonical form via BT_UUID_128_ENCODE so a
 * byte-order typo is impossible: the five arguments are exactly the five
 * dash-separated groups of the canonical string (report section 1).
 * suota_init() additionally renders every UUID back into canonical form
 * at boot and compares against the report strings.                       */
/* ------------------------------------------------------------------ */
#define UUID_MEM_DEV       BT_UUID_128_ENCODE(0x8082caa8, 0x41a6, 0x4021, 0x91c6, 0x56f9b954cc34) /* 8082caa8-41a6-4021-91c6-56f9b954cc34 */
#define UUID_GPIO_MAP      BT_UUID_128_ENCODE(0x724249f0, 0x5ec3, 0x4b5f, 0x8804, 0x42345af08651) /* 724249f0-5ec3-4b5f-8804-42345af08651 */
#define UUID_MEM_INFO      BT_UUID_128_ENCODE(0x6c53db25, 0x47a1, 0x45fe, 0xa022, 0x7c92fb334fd4) /* 6c53db25-47a1-45fe-a022-7c92fb334fd4 */
#define UUID_PATCH_LEN     BT_UUID_128_ENCODE(0x9d84b9a3, 0x000c, 0x49d8, 0x9183, 0x855b673fda31) /* 9d84b9a3-000c-49d8-9183-855b673fda31 */
#define UUID_PATCH_DATA    BT_UUID_128_ENCODE(0x457871e8, 0xd516, 0x4ca1, 0x9116, 0x57d0b17b9cb2) /* 457871e8-d516-4ca1-9116-57d0b17b9cb2 */
#define UUID_SERV_STATUS   BT_UUID_128_ENCODE(0x5f78df94, 0x798c, 0x46f5, 0x990a, 0xb3eb6a065c88) /* 5f78df94-798c-46f5-990a-b3eb6a065c88 */
#define UUID_SUOTA_VERSION BT_UUID_128_ENCODE(0x64b4e8b5, 0x0de5, 0x401b, 0xa21d, 0xacc8db3b913a) /* 64b4e8b5-0de5-401b-a21d-acc8db3b913a */
#define UUID_CHUNK_SIZE    BT_UUID_128_ENCODE(0x42c3dfdd, 0x77be, 0x4d9c, 0x8454, 0x8f875267fb3b) /* 42c3dfdd-77be-4d9c-8454-8f875267fb3b */
#define UUID_SUOTA_MTU     BT_UUID_128_ENCODE(0xb7de1eea, 0x823d, 0x43bb, 0xa3af, 0xc4903dfce23c) /* b7de1eea-823d-43bb-a3af-c4903dfce23c */
#define UUID_L2CAP_PSM     BT_UUID_128_ENCODE(0x61c8849c, 0xf639, 0x4765, 0x946e, 0x5c3419bebb2a) /* 61c8849c-f639-4765-946e-5c3419bebb2a */

static const struct bt_uuid_16 suota_uuid_svc = BT_UUID_INIT_16(SUOTA_SERVICE_UUID16);
static const struct bt_uuid_128 uuid_mem_dev = BT_UUID_INIT_128(UUID_MEM_DEV);
static const struct bt_uuid_128 uuid_gpio_map = BT_UUID_INIT_128(UUID_GPIO_MAP);
static const struct bt_uuid_128 uuid_mem_info = BT_UUID_INIT_128(UUID_MEM_INFO);
static const struct bt_uuid_128 uuid_patch_len = BT_UUID_INIT_128(UUID_PATCH_LEN);
static const struct bt_uuid_128 uuid_patch_data = BT_UUID_INIT_128(UUID_PATCH_DATA);
static const struct bt_uuid_128 uuid_serv_status = BT_UUID_INIT_128(UUID_SERV_STATUS);
static const struct bt_uuid_128 uuid_version = BT_UUID_INIT_128(UUID_SUOTA_VERSION);
static const struct bt_uuid_128 uuid_chunk_size = BT_UUID_INIT_128(UUID_CHUNK_SIZE);
static const struct bt_uuid_128 uuid_mtu = BT_UUID_INIT_128(UUID_SUOTA_MTU);
static const struct bt_uuid_128 uuid_psm = BT_UUID_INIT_128(UUID_L2CAP_PSM);

/* Configuration sanity, checked at compile time. */
BUILD_ASSERT(CONFIG_SUOTA_SIM_MAX_CHUNK >= 20 && CONFIG_SUOTA_SIM_MAX_CHUNK <= 244,
	     "SUOTA_SIM_MAX_CHUNK outside 20..244 (spec reference value 244)");
BUILD_ASSERT(CONFIG_SUOTA_SIM_BLOCK_MAX >= 20 && CONFIG_SUOTA_SIM_BLOCK_MAX <= 65535,
	     "SUOTA_SIM_BLOCK_MAX must fit PATCH_LEN (u16)");

/* ------------------------------------------------------------------ */
/* Characteristic value storage (all static: notify/read/write data must
 * never be stack allocated).                                             */
/* ------------------------------------------------------------------ */
static uint8_t mem_dev_val[4];                                /* last MEM_DEV write   */
static uint8_t gpio_map_val[4];                               /* last GPIO_MAP write  */
static uint8_t mem_info_val[4];                               /* constant capacity    */
static uint16_t patch_len_val;                                /* current PATCH_LEN    */
static uint8_t patch_data_last[CONFIG_SUOTA_SIM_MAX_CHUNK];  /* last chunk (read)    */
static uint16_t patch_data_last_len;
static uint8_t serv_status_val = SUOTA_STATUS_OK;             /* read/notify value    */
static uint8_t version_val = (uint8_t)CONFIG_SUOTA_SIM_VERSION;
static uint8_t chunk_size_val[2];                             /* u16 LE, from Kconfig */
static uint8_t mtu_val[2];                                    /* u16 LE, from Kconfig */
static uint8_t psm_val[2];                                    /* u16 LE, from Kconfig */
static uint8_t notify_val;                                    /* 1-byte notify payload */

#if CONFIG_SUOTA_SIM_KEEP_HEADER
#define IMAGE_HEADER_MAX 4096u
static uint8_t image_header[IMAGE_HEADER_MAX];
static bool image_header_logged;
#endif

/* ------------------------------------------------------------------ */
/* Session state.  Written from the BT RX thread (GATT write callbacks)
 * and from the system workqueue (ack/reboot work) => spinlock.           */
/* ------------------------------------------------------------------ */
struct suota_session {
	bool active;            /* MEM_DEV start accepted, not ended        */
	bool image_done;        /* END accepted (0x02 acked)                */
	bool block_error;       /* block received more bytes than PATCH_LEN */
	bool reboot_pending;    /* REBOOT seen: disconnect + re-adv delay   */
	bool subscribed;        /* peer wrote CCCD 0x0001                   */
	uint8_t mem_type;       /* 0x12 I2C / 0x13 SPI                      */
	uint8_t bank;           /* bank from the last MEM_DEV start         */
	uint8_t running_bank;   /* bank the device "runs" (0x15 comparison) */
	uint16_t patch_len;
	uint32_t block_received;
	uint32_t total_bytes;
	uint8_t running_xor;
};

static struct suota_session sess = {
	.running_bank = (uint8_t)CONFIG_SUOTA_SIM_SAME_IMAGE_BANK,
};
static struct bt_conn *cur_conn;
static struct k_spinlock sess_lock;

/* Delayed work driving every notification (block ACK delay). */
enum ack_kind { ACK_NONE = 0, ACK_STARTED, ACK_BLOCK, ACK_END, ACK_STATUS };

static struct k_work_delayable ack_work;
static enum ack_kind ack_kind = ACK_NONE;
static uint8_t ack_code;
static struct k_work_delayable reboot_work;

static void suota_notify_status(uint8_t code)
{
	int err;

	serv_status_val = code;

	if (cur_conn == NULL) {
		return;
	}
	if (!bt_gatt_is_subscribed(cur_conn, &attr_suota_svc[IDX_SERV_STATUS_VAL],
				   BT_GATT_CCC_NOTIFY)) {
		LOG_WRN("status 0x%02x dropped: peer not subscribed (CCCD)", code);
		return;
	}

	notify_val = code; /* static storage: notify data must outlive the call */
	err = bt_gatt_notify(NULL, &attr_suota_svc[IDX_SERV_STATUS_VAL], &notify_val, 1);
	if (err) {
		LOG_WRN("notify 0x%02x failed: %d", code, err);
	} else {
		LOG_INF("notify SERV_STATUS 0x%02x", code);
	}
}

static void schedule_ack(enum ack_kind kind, uint8_t code, k_timeout_t delay)
{
	k_spinlock_key_t key = k_spin_lock(&sess_lock);

	if (ack_kind != ACK_NONE) {
		/* Pending ACK (only reachable from out-of-protocol central
		 * behaviour, e.g. END before the last block ACK); reference
		 * device behavior here is unspecified, we keep the newest. */
		LOG_WRN("ACK work busy (kind %d), replacing", (int)ack_kind);
	}
	ack_kind = kind;
	ack_code = code;
	k_spin_unlock(&sess_lock, key);

	k_work_schedule(&ack_work, delay);
}

static void ack_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);
	enum ack_kind kind;
	uint8_t code;

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	kind = ack_kind;
	code = ack_code;
	ack_kind = ACK_NONE;
	if (kind == ACK_BLOCK) {
		sess.block_received = 0;
		sess.block_error = false;
	}
	k_spin_unlock(&sess_lock, key);

	if (kind == ACK_NONE) {
		return;
	}
	suota_notify_status(code);
}

static void reboot_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);
	struct bt_conn *conn;

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	conn = cur_conn;
	k_spin_unlock(&sess_lock, key);

	if (conn == NULL) {
		return;
	}
	LOG_INF("REBOOT: dropping connection (reference device drops the link ~0.1 s after the command)");
	bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
}

/* ------------------------------------------------------------------ */
/* GATT read callbacks                                                    */
/* ------------------------------------------------------------------ */
static ssize_t read_mem_dev(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			    void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, mem_dev_val,
				 sizeof(mem_dev_val));
}

static ssize_t read_gpio_map(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, gpio_map_val,
				 sizeof(gpio_map_val));
}

static ssize_t read_mem_info(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, mem_info_val,
				 sizeof(mem_info_val));
}

static ssize_t read_patch_len(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			      void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, &patch_len_val,
				 sizeof(patch_len_val));
}

static ssize_t read_patch_data(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			       void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, patch_data_last,
				 patch_data_last_len);
}

static ssize_t read_serv_status(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				void *buf, uint16_t len, uint16_t offset)
{
	/* Reading SERV_STATUS is the documented IMAGE_STARTED fallback
	 * (AN-B-022 SuotaProfile): it returns the last status we would
	 * have notified. */
	return bt_gatt_attr_read(conn, attr, buf, len, offset, &serv_status_val,
				 sizeof(serv_status_val));
}

static ssize_t read_version(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			    void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, &version_val,
				 sizeof(version_val));
}

static ssize_t read_chunk_size(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			       void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, chunk_size_val,
				 sizeof(chunk_size_val));
}

static ssize_t read_mtu(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, mtu_val, sizeof(mtu_val));
}

static ssize_t read_psm(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			void *buf, uint16_t len, uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset, psm_val, sizeof(psm_val));
}

/* ------------------------------------------------------------------ */
/* CCCD for SERV_STATUS                                                   */
/* ------------------------------------------------------------------ */
static void serv_status_ccc(const struct bt_gatt_attr *attr, uint16_t value)
{
	ARG_UNUSED(attr);
	bool on = (value & BT_GATT_CCC_NOTIFY) != 0;

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	sess.subscribed = on;
	k_spin_unlock(&sess_lock, key);

	LOG_INF("SERV_STATUS notifications %s (CCCD 0x%04x)",
		on ? "enabled" : "disabled", value);
}

/* ------------------------------------------------------------------ */
/* MEM_DEV command handling                                        */
/* ------------------------------------------------------------------ */
static bool mem_dev_start(uint8_t type, uint8_t bank)
{
	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	bool same_image = IS_ENABLED(CONFIG_SUOTA_SIM_SAME_IMAGE) &&
			  (bank == sess.running_bank);
	k_spin_unlock(&sess_lock, key);

	if (same_image) {
		/* Reference-device fidelity: the refusal is returned by
		 * write_mem_dev in the ATT write response (error 0x15) -- no
		 * SERV_STATUS notification is sent. */
		LOG_WRN("MEM_DEV bank %u == running bank: SAME_IMAGE (ATT err 0x15)",
			bank);
		return false;
	}

	key = k_spin_lock(&sess_lock);
	sess.active = true;
	sess.image_done = false;
	sess.block_error = false;
	sess.mem_type = type;
	sess.bank = bank;
	sess.patch_len = 0;
	sess.block_received = 0;
	sess.total_bytes = 0;
	sess.running_xor = 0;
#if CONFIG_SUOTA_SIM_KEEP_HEADER
	image_header_logged = false;
#endif
	k_spin_unlock(&sess_lock, key);

	LOG_INF("SUOTA session started: mem_type 0x%02x bank %u", type, bank);
	/* Notify 0x10 immediately after the MEM_DEV write. */
	schedule_ack(ACK_STARTED, SUOTA_STATUS_IMAGE_STARTED, K_NO_WAIT);
	return true;
}

static void mem_dev_end(void)
{
	uint8_t code;
	uint32_t total;
	uint8_t xored;
	bool incomplete_block;

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	if (!sess.active) {
		k_spin_unlock(&sess_lock, key);
		LOG_WRN("END without an active session: ignored");
		return;
	}

	code = SUOTA_STATUS_OK;
	if (CONFIG_SUOTA_SIM_EXPECTED_SIZE != 0 &&
	    sess.total_bytes != (uint32_t)CONFIG_SUOTA_SIM_EXPECTED_SIZE) {
		code = SUOTA_STATUS_INVALID_IMAGE_SIZE;
	} else if (IS_ENABLED(CONFIG_SUOTA_SIM_CHECK_XOR) && sess.running_xor != 0) {
		code = SUOTA_STATUS_CRC_MISMATCH;
	}

	if (code == SUOTA_STATUS_OK) {
		sess.image_done = true;
		sess.running_bank = sess.bank; /* "flashed": SAME_IMAGE trigger */
	}
	total = sess.total_bytes;
	xored = sess.running_xor;
	/* Flag END arriving while the armed block is still incomplete.
	 * AN-B-022 maps "received bytes != PATCH_LEN" to 0x05
	 * PATCH_LENGTH_ERROR; whether a device also synthesizes 0x05 when
	 * END lands mid-block is unspecified, so we keep the documented END
	 * semantics (0x02/0x13/0x04) and only surface the anomaly in the
	 * log. */
	incomplete_block = (sess.patch_len != 0) &&
			   (sess.block_received < (uint32_t)sess.patch_len);
	k_spin_unlock(&sess_lock, key);

	if (incomplete_block) {
		LOG_WRN("END with incomplete block (%u/%u B): 0x05 "
			"PATCH_LENGTH_ERROR territory, not synthesized",
			sess.block_received, sess.patch_len);
	}

	LOG_INF("END: total=%u B xor=0x%02x expected=%u => status 0x%02x",
		total, xored, (uint32_t)CONFIG_SUOTA_SIM_EXPECTED_SIZE, code);

	/* END ack is delayed by the configured ACK delay. */
	schedule_ack(ACK_END, code, K_MSEC(CONFIG_SUOTA_SIM_BLOCK_ACK_DELAY_MS));
}

static void mem_dev_reboot(void)
{
	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	sess.active = false;
	sess.reboot_pending = true;
	k_spin_unlock(&sess_lock, key);

	LOG_INF("REBOOT command accepted");
	/* Reference device drops the link ~0.1 s after the write. */
	k_work_schedule(&reboot_work, K_MSEC(100));
}

static void mem_dev_abort(void)
{
	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	sess.active = false;
	sess.image_done = false;
	sess.block_error = false;
	sess.patch_len = 0;
	sess.block_received = 0;
	k_spin_unlock(&sess_lock, key);

	LOG_WRN("ABORT: session state machine reset");
	schedule_ack(ACK_STATUS, (uint8_t)CONFIG_SUOTA_SIM_ABORT_STATUS, K_NO_WAIT);
}

static ssize_t write_mem_dev(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	ARG_UNUSED(conn);
	ARG_UNUSED(attr);
	ARG_UNUSED(flags);

	if (offset != 0 || len != 4) {
		LOG_WRN("MEM_DEV write: bad length %u (must be 4)", len);
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	uint32_t cmd = sys_get_le32(buf);
	uint8_t top = (uint8_t)(cmd >> 24);
	uint8_t bank = (uint8_t)(cmd & 0xFF);

	switch (top) {
	case SUOTA_MEM_DEV_TYPE_SPI:
	case SUOTA_MEM_DEV_TYPE_I2C:
		if (!mem_dev_start(top, bank)) {
			/* SAME_IMAGE refusal rides the ATT write response
			 * (reference device), NOT a
			 * SERV_STATUS notification; the errored write never
			 * lands, so mem_dev_val keeps its previous value.
			 * Host suota.py maps both this and the legacy notify
			 * path (older suota_sim) to SameImageError. */
			return BT_GATT_ERR(SUOTA_ATT_ERR_SAME_IMAGE);
		}
		break;
	case SUOTA_MEM_DEV_CMD_END:
		mem_dev_end();
		break;
	case SUOTA_MEM_DEV_CMD_REBOOT:
		mem_dev_reboot();
		break;
	case SUOTA_MEM_DEV_CMD_ABORT:
		mem_dev_abort();
		break;
	default:
		LOG_WRN("MEM_DEV unknown command type 0x%02x", top);
		schedule_ack(ACK_STATUS, SUOTA_STATUS_INVALID_MEM_TYPE, K_NO_WAIT);
		break;
	}

	memcpy(mem_dev_val, buf, sizeof(mem_dev_val)); /* readable back */
	LOG_INF("MEM_DEV write 0x%08x", cmd);
	return len;
}

static ssize_t write_gpio_map(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			      const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	ARG_UNUSED(conn);
	ARG_UNUSED(attr);
	ARG_UNUSED(flags);

	if (offset != 0 || len != 4) {
		LOG_WRN("GPIO_MAP write: bad length %u (must be 4)", len);
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	memcpy(gpio_map_val, buf, sizeof(gpio_map_val));
	LOG_INF("GPIO_MAP write 0x%08x", sys_get_le32(gpio_map_val));
	/* Only acks via the write response - no notification
	 * (AN-B-022 reference behavior).  Whether the value is validated is
	 * unspecified: we accept anything. */
	return len;
}

static ssize_t write_patch_len(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			       const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	ARG_UNUSED(conn);
	ARG_UNUSED(attr);
	ARG_UNUSED(flags);

	if (offset != 0 || len != 2) {
		LOG_WRN("PATCH_LEN write: bad length %u (must be 2)", len);
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	uint16_t v = sys_get_le16(buf);

	/* Semantic stretch: AN-B-022 defines 0x05 PATCH_LENGTH_ERROR as
	 * "block bytes != PATCH_LEN".  Reusing it for an illegal PATCH_LEN
	 * *value* is suota_sim's own choice - the reaction to a bad
	 * PATCH_LEN write is unspecified (most likely just the ATT-level
	 * error we additionally return here). */
	if (v == 0 || v > CONFIG_SUOTA_SIM_BLOCK_MAX) {
		LOG_WRN("PATCH_LEN %u rejected (0 < len <= %d)", v,
			CONFIG_SUOTA_SIM_BLOCK_MAX);
		schedule_ack(ACK_STATUS, SUOTA_STATUS_PATCH_LENGTH_ERROR, K_NO_WAIT);
		return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
	}

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	sess.patch_len = v;
	sess.block_received = 0;
	sess.block_error = false;
	k_spin_unlock(&sess_lock, key);

	patch_len_val = v;
	LOG_INF("PATCH_LEN %u (block armed)", v);
	return len;
}

static ssize_t write_patch_data(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	ARG_UNUSED(attr);
	ARG_UNUSED(offset);
	ARG_UNUSED(flags);

	bool schedule_now = false;
	uint8_t ack = SUOTA_STATUS_OK;

	/* Value-length behavior: a reference device silently ignores
	 * oversized PATCH_DATA writes - no ATT error, no response
	 * (max 244 = min(char_size, MTU-3)). */
	uint16_t cap = MIN((uint16_t)CONFIG_SUOTA_SIM_MAX_CHUNK,
			   (uint16_t)(bt_gatt_get_mtu(conn) - 3));
	if (len > cap) {
		LOG_WRN("PATCH_DATA %u B > cap %u: silently dropped (reference-device behaviour)",
			len, cap);
		return len;
	}

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	if (!sess.active || sess.image_done) {
		k_spin_unlock(&sess_lock, key);
		LOG_WRN("PATCH_DATA %u B outside an active session: dropped", len);
		return len; /* silently, like the reference device */
	}
	if (sess.patch_len == 0) {
		k_spin_unlock(&sess_lock, key);
		LOG_WRN("PATCH_DATA before PATCH_LEN: dropped");
		return len;
	}

	if (sess.block_received + (uint32_t)len > (uint32_t)sess.patch_len) {
		sess.block_error = true;
	}

	const uint8_t *p = buf;
	for (uint16_t i = 0; i < len; i++) {
		sess.running_xor ^= p[i];
	}
	sess.total_bytes += len;
	sess.block_received += len;

#if CONFIG_SUOTA_SIM_KEEP_HEADER
	{
		size_t before = sess.total_bytes - len;

		if (before < IMAGE_HEADER_MAX) {
			size_t n = MIN((size_t)len, IMAGE_HEADER_MAX - before);

			memcpy(&image_header[before], buf, n);
			if (before + n == IMAGE_HEADER_MAX && !image_header_logged) {
				image_header_logged = true;
				LOG_INF("image header (%u B) captured at %p (dump via debugger)",
					IMAGE_HEADER_MAX, image_header);
			}
		}
	}
#endif

	if (sess.block_received >= sess.patch_len && ack_kind == ACK_NONE) {
		ack = sess.block_error ? SUOTA_STATUS_PATCH_LENGTH_ERROR
				       : SUOTA_STATUS_OK;
		schedule_now = true;
	}
	k_spin_unlock(&sess_lock, key);

	if (schedule_now) {
		/* Block complete: ACK after the configured delay. */
		schedule_ack(ACK_BLOCK, ack, K_MSEC(CONFIG_SUOTA_SIM_BLOCK_ACK_DELAY_MS));
	}

	/* Keep the last chunk readable (PATCH_DATA has the read property). */
	memcpy(patch_data_last, buf, len);
	patch_data_last_len = len;

	return len; /* write-without-response: no error path in the air */
}

/* ------------------------------------------------------------------ */
/* GATT database - 0xFEF5 with all 10 characteristics, property bits
 * per AN-B-022.                                                          */
/* ------------------------------------------------------------------ */

/* Plain permissions everywhere: SUOTA runs without pairing/bonding
 * (AN-B-022 reference behavior). */
#define PERM_R (BT_GATT_PERM_READ)
#define PERM_RW (BT_GATT_PERM_READ | BT_GATT_PERM_WRITE)

BT_GATT_SERVICE_DEFINE(suota_svc,
	BT_GATT_PRIMARY_SERVICE(&suota_uuid_svc.uuid),

	/* 1 MEM_DEV: read, write (with response) */
	BT_GATT_CHARACTERISTIC(&uuid_mem_dev.uuid,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE,
			       PERM_RW, read_mem_dev, write_mem_dev, NULL),
	/* 2 GPIO_MAP: read, write (with response) */
	BT_GATT_CHARACTERISTIC(&uuid_gpio_map.uuid,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE,
			       PERM_RW, read_gpio_map, write_gpio_map, NULL),
	/* 3 MEM_INFO: read */
	BT_GATT_CHARACTERISTIC(&uuid_mem_info.uuid,
			       BT_GATT_CHRC_READ,
			       PERM_R, read_mem_info, NULL, NULL),
	/* 4 PATCH_LEN: read, write (with response) */
	BT_GATT_CHARACTERISTIC(&uuid_patch_len.uuid,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE,
			       PERM_RW, read_patch_len, write_patch_len, NULL),
	/* 5 PATCH_DATA: write-without-response, read, write (AN-B-022) */
	BT_GATT_CHARACTERISTIC(&uuid_patch_data.uuid,
			       BT_GATT_CHRC_WRITE_WITHOUT_RESP | BT_GATT_CHRC_READ |
			       BT_GATT_CHRC_WRITE,
			       PERM_RW, read_patch_data, write_patch_data, NULL),
	/* 6 SERV_STATUS: notify, read + CCCD */
	BT_GATT_CHARACTERISTIC(&uuid_serv_status.uuid,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
			       PERM_R, read_serv_status, NULL, NULL),
	BT_GATT_CCC(serv_status_ccc, PERM_RW),
	/* 7 SUOTA_VERSION: read (= 13 = 0x0D reference value) */
	BT_GATT_CHARACTERISTIC(&uuid_version.uuid,
			       BT_GATT_CHRC_READ,
			       PERM_R, read_version, NULL, NULL),
	/* 8 PATCH_DATA_CHAR_SIZE: read (244) */
	BT_GATT_CHARACTERISTIC(&uuid_chunk_size.uuid,
			       BT_GATT_CHRC_READ,
			       PERM_R, read_chunk_size, NULL, NULL),
	/* 9 SUOTA MTU: read (251) */
	BT_GATT_CHARACTERISTIC(&uuid_mtu.uuid,
			       BT_GATT_CHRC_READ,
			       PERM_R, read_mtu, NULL, NULL),
	/* 10 L2CAP_PSM: read (129) */
	BT_GATT_CHARACTERISTIC(&uuid_psm.uuid,
			       BT_GATT_CHRC_READ,
			       PERM_R, read_psm, NULL, NULL),
);

BUILD_ASSERT(ARRAY_SIZE(attr_suota_svc) == IDX_COUNT, "GATT table index drift");

/* ------------------------------------------------------------------ */
/* Boot-time UUID self-check: render each UUID in canonical string form
 * and compare against the canonical strings above.  (C11 cannot
 * decode string literals inside _Static_assert, so the compile-time part
 * of the anti-typo story is the BT_UUID_128_ENCODE derivation above -
 * canonical group order, no manual byte reversal - and this check makes
 * any residual digit typo fail loudly on first boot.)                    */
/* ------------------------------------------------------------------ */
static void uuid128_render(const uint8_t *v, char *out, size_t out_len)
{
	/* v[0] = LSB (last canonical hex pair) */
	snprintk(out, out_len,
		 "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
		 v[15], v[14], v[13], v[12], v[11], v[10], v[9], v[8],
		 v[7], v[6], v[5], v[4], v[3], v[2], v[1], v[0]);
}

static void uuid_to_str(const struct bt_uuid *u, char *out, size_t out_len)
{
	/* Fix (review round 1, Critical C1): never dereference a 16-bit UUID
	 * as bt_uuid_128 - the previous unconditional cast read val[0..15]
	 * out of a 3-byte struct bt_uuid_16 (out-of-bounds rodata read) and
	 * rendered garbage for the service UUID, so uuid_selfcheck() failed
	 * and suota_init() panicked deterministically at every boot.  Branch
	 * on the type tag instead; 16-bit UUIDs are expanded into the SIG
	 * base form 0000xxxx-0000-1000-8000-00805f9b34fb so the expected
	 * canonical strings of the self-check table stay unchanged.
	 * (Zephyr's bt_uuid_to_str() was not usable directly: it renders a
	 * 16-bit UUID as just "%04x", not the canonical SIG-base string.) */
	if (u->type == BT_UUID_TYPE_16) {
		static const struct bt_uuid_128 sig_base = BT_UUID_INIT_128(
			BT_UUID_128_ENCODE(0x00000000, 0x0000, 0x1000, 0x8000,
					   0x00805f9b34fb));
		struct bt_uuid_128 tmp = sig_base;

		tmp.val[12] = (uint8_t)(BT_UUID_16(u)->val & 0xFF);
		tmp.val[13] = (uint8_t)(BT_UUID_16(u)->val >> 8);
		uuid128_render(tmp.val, out, out_len);
		return;
	}

	__ASSERT(u->type == BT_UUID_TYPE_128, "unsupported UUID type %d",
		 (int)u->type);
	uuid128_render(((const struct bt_uuid_128 *)u)->val, out, out_len);
}

static int uuid_selfcheck(void)
{
	static const struct {
		const struct bt_uuid *uuid;
		const char *expect;
	} table[] = {
		{ &suota_uuid_svc.uuid, "0000fef5-0000-1000-8000-00805f9b34fb" },
		{ &uuid_mem_dev.uuid, "8082caa8-41a6-4021-91c6-56f9b954cc34" },
		{ &uuid_gpio_map.uuid, "724249f0-5ec3-4b5f-8804-42345af08651" },
		{ &uuid_mem_info.uuid, "6c53db25-47a1-45fe-a022-7c92fb334fd4" },
		{ &uuid_patch_len.uuid, "9d84b9a3-000c-49d8-9183-855b673fda31" },
		{ &uuid_patch_data.uuid, "457871e8-d516-4ca1-9116-57d0b17b9cb2" },
		{ &uuid_serv_status.uuid, "5f78df94-798c-46f5-990a-b3eb6a065c88" },
		{ &uuid_version.uuid, "64b4e8b5-0de5-401b-a21d-acc8db3b913a" },
		{ &uuid_chunk_size.uuid, "42c3dfdd-77be-4d9c-8454-8f875267fb3b" },
		{ &uuid_mtu.uuid, "b7de1eea-823d-43bb-a3af-c4903dfce23c" },
		{ &uuid_psm.uuid, "61c8849c-f639-4765-946e-5c3419bebb2a" },
	};
	char got[BT_UUID_STR_LEN];
	int failures = 0;

	for (size_t i = 0; i < ARRAY_SIZE(table); i++) {
		uuid_to_str(table[i].uuid, got, sizeof(got));
		if (strcmp(got, table[i].expect) != 0) {
			LOG_ERR("UUID selfcheck FAILED: got %s want %s", got,
				table[i].expect);
			failures++;
		}
	}
	return failures == 0 ? 0 : -EIO;
}

/* ------------------------------------------------------------------ */
/* Public API                                                             */
/* ------------------------------------------------------------------ */
void suota_init(void)
{
	int rc = uuid_selfcheck();

	__ASSERT(rc == 0, "SUOTA UUID table is mis-spelled");
	if (rc != 0) {
		LOG_ERR("refusing to start with broken UUID table");
		k_panic();
	}

	sys_put_le16((uint16_t)CONFIG_SUOTA_SIM_MAX_CHUNK, chunk_size_val);
	sys_put_le16((uint16_t)CONFIG_SUOTA_SIM_ATT_MTU_INFO, mtu_val);
	sys_put_le16((uint16_t)CONFIG_SUOTA_SIM_PSM, psm_val);
	sys_put_le32((uint32_t)CONFIG_SUOTA_SIM_MEM_INFO, mem_info_val);

	k_work_init_delayable(&ack_work, ack_work_handler);
	k_work_init_delayable(&reboot_work, reboot_work_handler);

	LOG_INF("SUOTA slave ready: chunk<=%d block<=%d ack_delay=%d ms "
		"same_image=%s expected_size=%u xor_check=%s",
		CONFIG_SUOTA_SIM_MAX_CHUNK, CONFIG_SUOTA_SIM_BLOCK_MAX,
		CONFIG_SUOTA_SIM_BLOCK_ACK_DELAY_MS,
		IS_ENABLED(CONFIG_SUOTA_SIM_SAME_IMAGE) ? "on" : "off",
		(uint32_t)CONFIG_SUOTA_SIM_EXPECTED_SIZE,
		IS_ENABLED(CONFIG_SUOTA_SIM_CHECK_XOR) ? "on" : "off");
}

void suota_connected(struct bt_conn *conn)
{
	k_spinlock_key_t key = k_spin_lock(&sess_lock);

	cur_conn = bt_conn_ref(conn);
	/* No resume across connections: fresh session. */
	sess.active = false;
	sess.image_done = false;
	sess.block_error = false;
	sess.reboot_pending = false;
	sess.subscribed = false;
	sess.patch_len = 0;
	sess.block_received = 0;
	sess.total_bytes = 0;
	sess.running_xor = 0;
	/* Fix (review round 1, Minor 1): ack_kind is guarded by sess_lock
	 * everywhere else (schedule_ack/ack_work_handler) - the reset used
	 * to happen outside the lock and could race a concurrently running
	 * ack_work_handler. */
	ack_kind = ACK_NONE;
	ack_code = SUOTA_STATUS_OK;
	k_spin_unlock(&sess_lock, key);

	k_work_cancel_delayable(&ack_work);
	k_work_cancel_delayable(&reboot_work);
}

uint32_t suota_disconnected(struct bt_conn *conn)
{
	ARG_UNUSED(conn);
	uint32_t delay_ms = 0;
	struct bt_conn *ref = NULL;

	k_spinlock_key_t key = k_spin_lock(&sess_lock);
	if (cur_conn != NULL) {
		ref = cur_conn;
		cur_conn = NULL;
	}
	sess.subscribed = false;
	sess.active = false;
	sess.block_received = 0;
	if (sess.reboot_pending) {
		/* Reference device re-appears ~10 s after REBOOT. */
		delay_ms = (uint32_t)CONFIG_SUOTA_SIM_REBOOT_DELAY_S * 1000U;
	}
	k_spin_unlock(&sess_lock, key);

	if (ref != NULL) {
		bt_conn_unref(ref);
	}

	k_work_cancel_delayable(&ack_work);
	k_work_cancel_delayable(&reboot_work);
	LOG_INF("session dropped, adv restart delay %u ms", delay_ms);
	return delay_ms;
}
