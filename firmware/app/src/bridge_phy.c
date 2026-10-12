/*
 * bridge_phy.c - physical layer: USB CDC ACM (primary) / UART0 (backup),
 * Task 9.  See bridge_phy.h for the contract.
 */

#include "bridge_phy.h"

#include <errno.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/ring_buffer.h>

#if defined(BRIDGE_PHY_MODE_CDC)
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/usb/usbd.h>
#endif

/* ---- shared byte pipes ------------------------------------------------ */

#define RX_RING_SIZE 1024U /* > several max frames of slack for the RX thread */
#define FIFO_CHUNK 64U

static struct ring_buf rx_ring;
static uint8_t rx_pool[RX_RING_SIZE];

static struct k_sem rx_data_sem; /* RX ring has data                      */

static const struct device *phy_dev;
static volatile uint32_t rx_overflows;

static int64_t deadline_ms(k_timeout_t t)
{
    return k_uptime_get() + (int64_t)k_ticks_to_ms_floor64(t.ticks);
}

static k_timeout_t remaining_ms(int64_t deadline)
{
    int64_t left = deadline - k_uptime_get();

    return (left <= 0) ? K_NO_WAIT : K_MSEC((uint32_t)left);
}

static void phy_isr(const struct device *dev, void *user_data)
{
    ARG_UNUSED(user_data);

    while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
        if (uart_irq_rx_ready(dev)) {
            uint8_t tmp[FIFO_CHUNK];
            int n = uart_fifo_read(dev, tmp, (int)sizeof(tmp));

            if (n > 0) {
                uint32_t put = ring_buf_put(&rx_ring, tmp, (uint32_t)n);

                if (put < (uint32_t)n) {
                    rx_overflows += (uint32_t)n - put;
                }
                if (put > 0U) {
                    k_sem_give(&rx_data_sem);
                }
            }
        }
    }
}

/* ---- CDC backend (primary) -------------------------------------------- */

#if defined(BRIDGE_PHY_MODE_CDC)

/* pid.codes test VID/PID pair (0x1209:0x0001, generic test product);
 * replace before any release hardware. */
#define BRIDGE_USB_VID 0x1209U
#define BRIDGE_USB_PID 0x0001U

USBD_DEVICE_DEFINE(bridge_usbd, DEVICE_DT_GET(DT_NODELABEL(zephyr_udc0)),
                   BRIDGE_USB_VID, BRIDGE_USB_PID);
USBD_DESC_LANG_DEFINE(bridge_lang);
USBD_DESC_MANUFACTURER_DEFINE(bridge_mfr, "blebridge");
USBD_DESC_PRODUCT_DEFINE(bridge_product, "blebridge");
USBD_DESC_SERIAL_NUMBER_DEFINE(bridge_sn);
USBD_DESC_CONFIG_DEFINE(bridge_fs_cfg_desc, "FS Configuration");
USBD_CONFIGURATION_DEFINE(bridge_fs_config, 0U, 100U, &bridge_fs_cfg_desc);

static void usbd_msg_cb(struct usbd_context *const ctx,
                        const struct usbd_msg *msg)
{
    if (msg->type == USBD_MSG_VBUS_READY) {
        (void)usbd_enable(ctx);
    } else if (msg->type == USBD_MSG_VBUS_REMOVED) {
        (void)usbd_disable(ctx);
    }
}

static int cdc_init(void)
{
    int err;

    phy_dev = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
    if (!device_is_ready(phy_dev)) {
        return -ENODEV;
    }

    err = usbd_add_descriptor(&bridge_usbd, &bridge_lang);
    if (err == 0) {
        err = usbd_add_descriptor(&bridge_usbd, &bridge_mfr);
    }
    if (err == 0) {
        err = usbd_add_descriptor(&bridge_usbd, &bridge_product);
    }
    if (err == 0) {
        err = usbd_add_descriptor(&bridge_usbd, &bridge_sn);
    }
    if (err != 0) {
        return err;
    }

    err = usbd_add_configuration(&bridge_usbd, USBD_SPEED_FS,
                                 &bridge_fs_config);
    if (err != 0) {
        return err;
    }
    err = usbd_register_all_classes(&bridge_usbd, USBD_SPEED_FS, 1, NULL);
    if (err != 0) {
        return err;
    }
    /* Interfaces come from the class descriptors (IAD triple). */
    usbd_device_set_code_triple(&bridge_usbd, USBD_SPEED_FS,
                                USB_BCC_MISCELLANEOUS, 0x02, 0x01);

    /* Register the message callback BEFORE init/enable (official sample
     * order, samples/subsys/usb/common/sample_usbd_init.c) so no VBUS
     * message can be dropped in the init-to-register window. */
    err = usbd_msg_register_cb(&bridge_usbd, usbd_msg_cb);
    if (err != 0) {
        return err;
    }
    err = usbd_init(&bridge_usbd);
    if (err != 0) {
        return err;
    }

    if (!usbd_can_detect_vbus(&bridge_usbd)) {
        err = usbd_enable(&bridge_usbd);
        if (err != 0) {
            return err;
        }
    }

    /* Bounded wait for the host to open the port; boot without a host
     * attached must still succeed (events queue up meanwhile). */
    int64_t deadline = k_uptime_get() + CONFIG_BRIDGE_USB_DTR_WAIT_MS;

    while (k_uptime_get() < deadline) {
        uint32_t dtr = 0U;

        if (uart_line_ctrl_get(phy_dev, UART_LINE_CTRL_DTR, &dtr) == 0 &&
            dtr != 0U) {
            break;
        }
        k_msleep(50);
    }
    return 0;
}

#endif /* BRIDGE_PHY_MODE_CDC */

/* ---- UART0 backend (backup) ------------------------------------------- */

#if defined(BRIDGE_PHY_MODE_UART0)

static int uart0_init(void)
{
    phy_dev = DEVICE_DT_GET(DT_NODELABEL(uart0));
    if (!device_is_ready(phy_dev)) {
        return -ENODEV;
    }
    /* 921600 8N1 + RTS/CTS come from the app overlay; nothing to do. */
    return 0;
}

#endif /* BRIDGE_PHY_MODE_UART0 */

/* ---- public API -------------------------------------------------------- */

int bridge_phy_init(void)
{
    int err;

    ring_buf_init(&rx_ring, sizeof(rx_pool), rx_pool);
    k_sem_init(&rx_data_sem, 0, 1);

#if defined(BRIDGE_PHY_MODE_CDC)
    err = cdc_init();
#else
    err = uart0_init();
#endif
    if (err != 0) {
        return err;
    }

    uart_irq_callback_set(phy_dev, phy_isr);
    uart_irq_rx_enable(phy_dev);
    return 0;
}

enum bridge_phy_kind bridge_phy_kind(void)
{
#if defined(BRIDGE_PHY_MODE_CDC)
    return BRIDGE_PHY_KIND_CDC;
#else
    return BRIDGE_PHY_KIND_UART0;
#endif
}

const char *bridge_phy_name(void)
{
#if defined(BRIDGE_PHY_MODE_CDC)
    return "cdc";
#else
    return "uart0";
#endif
}

uint32_t bridge_phy_rx_overflows(void)
{
    return rx_overflows;
}

int bridge_phy_read(uint8_t *buf, size_t cap, k_timeout_t timeout,
                    size_t *out_len)
{
    if (buf == NULL || cap == 0U || out_len == NULL) {
        return -EINVAL;
    }

    int64_t deadline = deadline_ms(timeout);

    for (;;) {
        uint32_t n = ring_buf_get(&rx_ring, buf, (uint32_t)cap);

        if (n > 0U) {
            *out_len = (size_t)n;
            return 0;
        }
        int rc = k_sem_take(&rx_data_sem, remaining_ms(deadline));

        if (rc != 0 && k_uptime_get() >= deadline) {
            *out_len = 0U;
            return -EAGAIN;
        }
    }
}

int bridge_phy_write(const uint8_t *buf, size_t len, k_timeout_t timeout)
{
    if (buf == NULL || len == 0U) {
        return -EINVAL;
    }

    int64_t deadline = deadline_ms(timeout);

    /* Polling TX, no ISR involvement.  The previous design queued frames
     * into a TX ring drained by phy_isr in 64 B uart_fifo_fill chunks;
     * on nRF UARTE every frame longer than one chunk came out with its
     * chunks interleaved/reordered (int-driven TX re-armed while a DMA
     * transfer was still in flight), so long frames never survived the
     * host-side CRC while <=64 B frames were always clean.  uart_poll_out
     * is the driver's own byte-paced path and cannot re-enter: TX is only
     * ever driven from the single TX thread.  At 921600 a max frame costs
     * ~5 ms of thread time, which the TX thread's 5 s budget absorbs. */
    for (size_t pos = 0U; pos < len; pos++) {
        if (k_uptime_get() >= deadline) {
            return -ETIMEDOUT;
        }
        uart_poll_out(phy_dev, buf[pos]);
    }
    return 0;
}
