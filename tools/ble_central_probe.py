#!/usr/bin/env python3
"""BLE central-role probe for the blebridge peripheral (FR-H verification).

Phases: scan -> connect -> N discovery rounds (watchdog) -> optional CCCD
subscribe -> optional notify-receive window -> optional hold -> disconnect.
Exit 0 when every discovery round passed.

  ble_central_probe.py [--rounds N] [--watchdog S] [--subscribe]
                       [--notify-wait S] [--hold S] [--tag T]

## FR-H 根因备忘（2026-09-26，勿删）

"板子 GATT 服务器对服务发现 100% 无响应"的整场调查最终定性为 **central 验证
脚本的 pyobjc 选择器缺陷**，不是固件缺陷：macOS 26 的 CoreBluetooth 对
`peripheral:didDiscoverServices:` 调用的是**经典 2 参选择器**；只实现
`peripheral:didDiscoverServices:error:`（3 参变体）的 delegate 类
（/tmp/bridge_renotify3.py，以及本次排查的第一版探针）回调**永不触发**，
发现看门狗必然假超时——表象恰似"GATT 服务器对发现零响应"（连接保持、
读写订阅等其它步骤若不依赖该回调则照常）。本文件同时实现两个变体
（经典 + error 变体互相委托），并由
host/tests/test_central_probe_source.py 源码契约测试守护，防止再次退化。

## board-side 串口驱动提示（复跑时）

host 侧驱动脚本若与事件排空线程并发使用 BridgeHost（非线程安全），命令
响应记账会错乱——命令与排空须单线程串行化，或以 central 侧空中观测
（本脚本的 DISCOVERY/NOTIFY VALUE 行）为准。
"""
import functools
import sys
import time

import CoreBluetooth as CB
from Foundation import NSObject, NSRunLoop, NSDate
import objc

MODE = "NSDefaultRunLoopMode"
TARGET_NAME = "blebridge"
FFE0 = CB.CBUUID.UUIDWithString_("0000FFE0-0000-1000-8000-00805F9B34FB")

argv = sys.argv[1:]


def arg(name, default, cast=str):
    if name in argv:
        return cast(argv[argv.index(name) + 1])
    return default


ROUNDS = arg("--rounds", 3, int)
WATCHDOG = arg("--watchdog", 45.0, float)
DO_SUBSCRIBE = "--subscribe" in argv
NOTIFY_WAIT = arg("--notify-wait", 0.0, float)
HOLD = arg("--hold", 0.0, float)
TAG = arg("--tag", "probe")

t_start = time.time()


def log(msg):
    print("[%8.3f][%s] %s" % (time.time() - t_start, TAG, msg), flush=True)


def guard(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            log("DELEGATE-ERROR %s: %r" % (fn.__name__, e))
    return wrapper


class Events(NSObject):
    def init(self):
        self = objc.super(Events, self).init()
        self.seen = {}          # identifier -> peripheral
        self.named_seen = False
        self.connected = False
        self.connect_failed = None
        self.disconnected = False
        self.services_done = False
        self.chars_pending = set()
        self.notifying = []
        self.notify_values = []
        return self

    @guard
    def centralManagerDidUpdateState_(self, central):
        log("central state -> %s" % central.state())

    @guard
    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, central, per, adv, rssi):
        uid = str(per.identifier())
        if uid not in self.seen:
            self.seen[uid] = per
        name = str(adv.get("kCBAdvDataLocalName", ""))
        if name == TARGET_NAME:
            if not self.named_seen:
                log("ADV target seen rssi=%d svc=%s" % (
                    int(rssi), [str(u) for u in adv.get("kCBAdvDataServiceUUIDs", [])]))
            self.named_seen = True

    @guard
    def centralManager_didConnectPeripheral_(self, central, per):
        log("connected")
        self.connected = True

    @guard
    def centralManager_didFailToConnectPeripheral_error_(self, central, per, err):
        self.connect_failed = str(err)
        log("connect FAILED: %s" % err)

    @guard
    def centralManager_didDisconnectPeripheral_error_(self, central, per, err):
        self.disconnected = True
        log("disconnected err=%s" % err)

    @guard
    def peripheral_didDiscoverServices_(self, per, err):
        # macOS 26/pyobjc 12 invokes the CLASSIC selector (V-script log:
        # "classic sel"); without this variant the callback never fires.
        self.peripheral_didDiscoverServices_error_(per, None, err)

    @guard
    def peripheral_didDiscoverServices_error_(self, per, extra, err):
        svcs = [str(s.UUID()) for s in (per.services() or [])]
        log("services discovered: %s err=%s" % (svcs, err))
        self.services_done = True

    @guard
    def peripheral_didDiscoverCharacteristicsForService_(self, per, svc):
        self.peripheral_didDiscoverCharacteristicsForService_error_(per, svc, None)

    @guard
    def peripheral_didDiscoverCharacteristicsForService_error_(self, per, svc, err):
        chars = [(str(c.UUID()), c.properties()) for c in (svc.characteristics() or [])]
        log("chars of %s: %s err=%s" % (svc.UUID(), chars, err))
        self.chars_pending.discard(str(svc.UUID()))

    @guard
    def peripheral_didUpdateNotificationStateForCharacteristic_(self, per, ch):
        self.peripheral_didUpdateNotificationStateForCharacteristic_error_(per, ch, None)

    @guard
    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, per, ch, err):
        log("notify state %s -> %s err=%s" % (ch.UUID(), ch.isNotifying(), err))
        self.notifying.append((str(ch.UUID()), ch.isNotifying()))

    @guard
    def peripheral_didUpdateValueForCharacteristic_(self, per, ch):
        self.peripheral_didUpdateValueForCharacteristic_error_(per, ch, None)

    @guard
    def peripheral_didUpdateValueForCharacteristic_error_(self, per, ch, err):
        raw = None
        try:
            raw = bytes(ch.value())
        except Exception:
            pass
        log("NOTIFY VALUE %s hex=%s err=%s" % (ch.UUID(), raw.hex() if raw else None, err))
        self.notify_values.append(raw)


def pump_until(ev, pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        NSRunLoop.currentRunLoop().runMode_beforeDate_(MODE, NSDate.dateWithTimeIntervalSinceNow_(0.05))
        if pred():
            log("-- %s satisfied after %.2fs" % (what, time.time() - t0))
            return True
    log("-- %s TIMEOUT after %.1fs" % (what, timeout))
    return False


def discovery_round(ev, per, k):
    """One service+char discovery with the watchdog.  Returns elapsed s or None."""
    ev.services_done = False
    t0 = time.time()
    per.discoverServices_([FFE0])
    if not pump_until(ev, lambda: ev.services_done, WATCHDOG, "discovery-round-%d" % k):
        return None
    services = list(per.services() or [])
    for s in services:
        u = str(s.UUID())
        ev.chars_pending.add(u)
        per.discoverCharacteristics_forService_(None, s)
        pump_until(ev, lambda u=u: u not in ev.chars_pending, 6, "chars-%s-r%d" % (u, k))
    log("discovery round %d done: %s" % (k, [str(s.UUID()) for s in services]))
    return time.time() - t0


def main():
    ev = Events.alloc().init()
    mgr = CB.CBCentralManager.alloc().initWithDelegate_queue_(ev, None)
    pump_until(ev, lambda: mgr.state() in (4, 5), 8, "power-on")
    if mgr.state() != 5:
        log("FATAL: bluetooth not powered on")
        return 2

    mgr.scanForPeripheralsWithServices_options_([FFE0], None)
    if not pump_until(ev, lambda: ev.named_seen, 15, "target-in-adv"):
        log("FATAL: blebridge not advertised")
        return 1
    per = next((p for p in ev.seen.values()), None)
    if per is None:
        log("FATAL: no peripheral handle")
        return 1
    per.retain()
    ev.peripheral = per
    per.setDelegate_(ev)

    mgr.connectPeripheral_options_(per, None)
    if not pump_until(ev, lambda: ev.connected or ev.connect_failed, 25, "connect"):
        log("FATAL: connect failed %s" % ev.connect_failed)
        return 1
    mgr.stopScan()

    results = []
    for k in range(1, ROUNDS + 1):
        dt = discovery_round(ev, per, k)
        results.append(dt)
        log("DISCOVERY round %d: %s" % (k, "PASS %.2fs" % dt if dt else "FAIL (watchdog %.0fs)" % WATCHDOG))
        time.sleep(0.5)

    ffe2 = None
    for s in (per.services() or []):
        for c in (s.characteristics() or []):
            if str(c.UUID()).upper() == "FFE2":
                ffe2 = c
    if DO_SUBSCRIBE and ffe2 is not None:
        log("subscribing FFE2 CCCD")
        per.setNotifyValue_forCharacteristic_(True, ffe2)
        pump_until(ev, lambda: any(u == "FFE2" for u, _ in ev.notifying), 5, "subscribe-cccd")
        log("subscribed=%s" % ffe2.isNotifying())

    if NOTIFY_WAIT > 0:
        log("waiting %.0fs for notifications (host: send CMD.notify now)" % NOTIFY_WAIT)
        t0 = time.time()
        while time.time() - t0 < NOTIFY_WAIT:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(MODE, NSDate.dateWithTimeIntervalSinceNow_(0.1))
        log("notify wait done: %d value(s) received" % len(ev.notify_values))

    if HOLD > 0:
        log("holding connection %.0fs" % HOLD)
        t0 = time.time()
        while time.time() - t0 < HOLD and not ev.disconnected:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(MODE, NSDate.dateWithTimeIntervalSinceNow_(0.2))

    log("FINAL rounds=%s notify_values=%d" % (results, len(ev.notify_values)))
    ok = all(r is not None for r in results)
    mgr.cancelPeripheralConnection_(per)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
