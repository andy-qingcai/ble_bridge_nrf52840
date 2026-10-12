/*
 * suota_svc.h - SUOTA (0xFEF5) slave emulation for suota_sim (Task 11).
 *
 * Contract with main.c:
 *   - suota_init()          once at boot (self-tests, work items).
 *   - suota_connected()     from the application connected callback.
 *   - suota_disconnected()  from the application disconnected callback;
 *                           returns how long (ms) advertising should wait
 *                           before restarting (REBOOT emulation), 0 = now.
 */

#ifndef SUOTA_SIM_SUOTA_SVC_H_
#define SUOTA_SIM_SUOTA_SVC_H_

#include <stdint.h>

struct bt_conn;

void suota_init(void);
void suota_connected(struct bt_conn *conn);
uint32_t suota_disconnected(struct bt_conn *conn);

#endif /* SUOTA_SIM_SUOTA_SVC_H_ */
