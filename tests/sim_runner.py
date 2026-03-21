"""
tests/sim_runner.py — Simulation harness for the full simos-suite UI

Patches _make_connection() to return MockConnections instead of real hardware,
then launches the full MainWindow with all tabs live and pre-loaded with data.

Run from the repo root:
    python -m tests.sim_runner                    # Simos8.5 + ZF8HP
    python -m tests.sim_runner --ecu SC8          # Simos18.1 + DQ250
    python -m tests.sim_runner --ecu DQ381        # DQ381 trans only
    python -m tests.sim_runner --headless         # No GUI — smoke-test imports

What gets simulated:
    ECU Info tab    — all 18 standard VW DIDs populated with realistic values
    Flash tab       — full flash sequence (connect→erase→transfer→verify→done)
                      runs at 10× speed with real progress callbacks
    Tune tab        — pre-loads a synthetic CAL binary with known table values
    Logger tab      — live engine DID values that drift/animate over time
    Trans tab       — live TCU values: gear shifts, ATF warm-up, speed sweep
    CP Tools tab    — J533 probe returns realistic constellation data
    Raw Sniff tab   — generates synthetic CAN frames at ~5Hz

Simulation is safe — no real UDS traffic, no hardware needed.
"""

from __future__ import annotations

import argparse
import logging
import os
import struct
import sys
import threading
import time
import random
import math

log = logging.getLogger("SimosSuite.SimRunner")


# ── Patch _make_connection before any UI import ───────────────────────────────

def _install_mock_patch(ecu_key: str = "S85", trans_key: str = "ZF8HP"):
    """
    Replace flasher.uds_flash._make_connection with one that returns
    a MockConnection. Must be called before importing ui.main_window.
    """
    import flasher.uds_flash as flash_mod
    from tests.mock_connection import MockConnection, MockECU

    _ECU_MAP = {
        "S85": MockECU.SIMOS85, "SC1": MockECU.SIMOS85,
        "SC2": MockECU.SIMOS85, "SC8": MockECU.SIMOS85,
    }
    _TCU_MAP = {
        "ZF8HP": MockECU.ZF8HP,  "DL501": MockECU.ZF8HP,
        "DQ250": MockECU.DQ250,  "DQ381": MockECU.DQ250,
    }

    def _mock_make(ecu_or_proxy, interface, interface_path=None,
                   st_min_us=350_000, ble_bridge=None):
        can_tx = getattr(ecu_or_proxy, "can_tx", 0)
        if can_tx == 0x7E1:
            variant = _TCU_MAP.get(trans_key.upper(), MockECU.ZF8HP)
            log.info("[SIM] TCU MockConnection → %s (%s)", trans_key, variant.name)
        else:
            variant = _ECU_MAP.get(ecu_key.upper(), MockECU.SIMOS85)
            log.info("[SIM] ECU MockConnection → %s (%s)", ecu_key, variant.name)
        conn = MockConnection(variant, latency=0.012)
        conn.open()
        return conn

    flash_mod._make_connection = _mock_make
    log.info("[SIM] _make_connection patched with MockConnection")


    _real_make = flash_mod._make_connection

    def _mock_make(ecu_or_proxy, interface, interface_path=None,
                   st_min_us=350_000, ble_bridge=None):
        # Detect TCU proxy vs ECU by can_tx address
        # TCUs use 0x7E1; ECUs use 0x7E0 (Simos) or other
        can_tx = getattr(ecu_or_proxy, "can_tx", 0)
        if can_tx == 0x7E1:
            # This is a TCU call
            trans = TRANS_REGISTRY.get(trans_key)
            conn = make_mock_tcu_connection(trans)
            log.info("[SIM] TCU MockConnection → %s", trans_key)
        else:
            # ECU call
            from core.ecu_defs import get_ecu
            ecu = (get_ecu(ecu_key) if isinstance(ecu_or_proxy, str)
                   else ecu_or_proxy)
            conn = make_mock_ecu_connection(ecu)
            log.info("[SIM] ECU MockConnection → %s  (CAN TX %#05x)",
                     ecu_key, can_tx)
        conn.open()
        return conn

    flash_mod._make_connection = _mock_make
    log.info("[SIM] _make_connection patched with MockConnection")


def _install_interface_patch():
    """
    Patch InterfaceRegistry to return a single simulated interface
    so the connect tab shows something plausible without real hardware.
    """
    import transport.interfaces as iface_mod
    from transport.interfaces import InterfaceInfo

    _real_registry = iface_mod.InterfaceRegistry

    class _MockRegistry:
        def __init__(self):
            self._interfaces = [
                InterfaceInfo(
                    name      = "Simulated ESP32 BLE Bridge (DEMO)",
                    interface = "BLE",
                    path      = "",
                    available = True,
                    notes     = "Simulation mode — no hardware required.",
                ),
                InterfaceInfo(
                    name      = "Simulated USB Bridge COM3 (DEMO)",
                    interface = "USBISOTP",
                    path      = "COM3",
                    available = True,
                    notes     = "Simulation mode — no hardware required.",
                ),
            ]

        def all(self):        return list(self._interfaces)
        def available(self):  return list(self._interfaces)
        def first_available(self): return self._interfaces[0]
        def by_type(self, t): return [i for i in self._interfaces
                                       if i.interface.upper() == t.upper()]
        def refresh(self):    pass

    iface_mod.InterfaceRegistry = _MockRegistry
    log.info("[SIM] InterfaceRegistry patched with mock interfaces")


# ── Synthetic CAL binary ───────────────────────────────────────────────────────

def make_synthetic_cal(ecu_def=None) -> bytes:
    """
    Build a minimal synthetic Simos8.5 CAL binary that CalParser can load.
    Tables are filled with physically plausible values.
    """
    from core.ecu_defs import SIMOS85
    ecu = ecu_def or SIMOS85
    cal_block = ecu.cal_block
    if cal_block is None:
        return b"\x00" * 0x3C000

    size = cal_block.length
    data = bytearray(size)

    # Box code string near offset 0x60
    box = b"4G0906259E      \x00"
    data[0x60:0x60 + len(box)] = box

    # MAF transfer (offset 0x1000, 32×uint16) — realistic voltage→flow curve
    for i in range(32):
        v = i * 150   # 0–4650 mV range
        flow = min(65535, int(v * 0.018))  # ~kg/h approximation
        struct.pack_into(">H", data, 0x1000 + i * 2, flow)

    # Lambda setpoint (offset 0x3200, 16×16 uint16) — all stoich (1.000)
    for r in range(16):
        for c in range(16):
            struct.pack_into(">H", data, 0x3200 + (r * 16 + c) * 2, 1000)

    # Injector scaling (offset 0x2400, 16×16 uint16) — 0.8–2.5ms range
    for r in range(16):
        for c in range(16):
            ms = 0.8 + (r * 0.1) + (c * 0.08)
            raw = min(65535, int(ms * 1000))
            struct.pack_into(">H", data, 0x2400 + (r * 16 + c) * 2, raw)

    # Boost setpoint (offset 0x5C00, 16×16 uint16) — 100–250 kPa
    for r in range(16):
        for c in range(16):
            kpa = 100 + r * 10 + c * 2
            struct.pack_into(">H", data, 0x5C00 + (r * 16 + c) * 2,
                             int(kpa * 10))

    # Write a placeholder checksum
    struct.pack_into("<I", data, ecu.blocks[3].checksum_offset + 4, 0xDEADBEEF)

    return bytes(data)


# ── Flash simulation thread ────────────────────────────────────────────────────

def simulate_flash_sequence(callback, total_bytes: int = 0x3C000,
                             speed_multiplier: float = 10.0):
    """
    Run a simulated flash sequence that fires real FlashProgress callbacks
    at the same rate as a real flash, but 10× faster by default.
    """
    try:
        from flasher.uds_flash import FlashProgress
    except ImportError:
        # udsoncan not installed — use local stub
        from dataclasses import dataclass
        from typing import Optional as _Opt
        @dataclass
        class FlashProgress:
            step: str
            message: str
            pct: int
            block: _Opt[str] = None

    def _run():
        steps = [
            ("CONNECT",  "Opening extended diagnostic session…",    5),
            ("CONNECT",  "VIN: WAUZZZ4G9EN123456",                 10),
            ("CONNECT",  "Entering programming session…",           15),
            ("CONNECT",  "Security access (SA2)…",                  20),
            ("ERASE",    "Erasing CAL block 3…",                    25),
        ]
        for step, msg, pct in steps:
            callback(FlashProgress(step, msg, pct))
            time.sleep(0.3 / speed_multiplier)

        # Transfer
        chunk = 0xFFD - 2
        sent = 0
        counter = 1
        while sent < total_bytes:
            pct = 30 + int(60 * sent / total_bytes)
            callback(FlashProgress(
                "TRANSFER",
                f"Writing {sent:#08x}/{total_bytes:#08x}",
                pct, "CAL"))
            sent += chunk
            counter = (counter + 1) & 0xFF or 1
            time.sleep(0.01 / speed_multiplier)

        callback(FlashProgress("TRANSFER", "Transfer complete, exiting…", 92, "CAL"))
        time.sleep(0.2 / speed_multiplier)
        callback(FlashProgress("VERIFY",   "Running checksum verification…", 95))
        time.sleep(0.3 / speed_multiplier)
        callback(FlashProgress("VERIFY",   "Checksum OK", 98))
        time.sleep(0.1 / speed_multiplier)
        callback(FlashProgress("DONE",
                               "CAL block flashed successfully — WAUZZZ4G9EN123456",
                               100, "CAL"))

    threading.Thread(target=_run, daemon=True).start()


# ── Raw sniff frame generator ──────────────────────────────────────────────────

def start_sniff_generator(callback, interval: float = 0.2):
    """
    Generate synthetic raw CAN frames at a regular interval.
    callback receives (bytes) — same format as BLE sniff frames.
    """
    CAN_IDS = [0x710, 0x77A, 0x746, 0x7B0, 0x7E0, 0x7E8]
    running = [True]

    def _gen():
        count = 0
        while running[0]:
            can_id = random.choice(CAN_IDS)
            dlc = random.randint(4, 8)
            payload = bytes([random.randint(0, 255) for _ in range(dlc)])
            # BLE bridge raw frame format: [id_hi][id_lo][dlc][d0..d7]
            frame = bytes([can_id >> 8, can_id & 0xFF, dlc]) + payload
            callback(frame)
            count += 1
            time.sleep(interval)

    t = threading.Thread(target=_gen, daemon=True)
    t.start()
    return lambda: running.__setitem__(0, False)


# ── Auto-connect helper ────────────────────────────────────────────────────────

def auto_connect_after_launch(mw, delay: float = 1.5):
    """
    After the MainWindow is visible, automatically trigger a DEMO connection
    and pre-populate every tab with simulated data.

    Called by the DEMO MODE button in interface_panel.py.

    Populates:
      - ECU Info     all 18 VW DIDs from Simos8.5 mock
      - Flash        synthetic CAL binary ready for Tune tab
      - Tune         CAL tables loaded and first table displayed
      - Logger       live DID channels enabled (animates)
      - CP Tools     constellation + J255/J136 CP active, scan auto-runs
      - Diagnostics  bus topology from J533, DTCs from J255
      - Trans        ZF8HP live gear/temp/speed data
      - Raw Sniff    simulated ISO-TP frame stream
    """
    def _do():
        time.sleep(delay)
        try:
            mw._on_connected("DEMO", "Simos8.5 3.0T TFSI CGWB")
            log.info("[SIM] Demo connected")

            time.sleep(0.4)
            cal = make_synthetic_cal(mw.ecu)

            for tab in getattr(mw, "_tabs", []):
                cls = type(tab).__name__

                # Flash tab — pre-load cal
                if cls == "FlashTab":
                    try:
                        tab._cal_bytes = cal
                        tab._cal_label.config(
                            text="4G0906259E_CGWB_demo.bin  (simulated)")
                        tab._read_btn.config(state="normal")
                        tab._write_btn.config(state="normal")
                        log.info("[SIM] FlashTab: synthetic CAL loaded")
                    except Exception as e:
                        log.warning("[SIM] FlashTab: %s", e)

                # Tune tab — load and display tables
                elif cls == "TuneTab":
                    try:
                        tab.load_bytes(cal, "4G0906259E_CGWB_demo.bin")
                        log.info("[SIM] TuneTab: tables loaded")
                    except Exception as e:
                        log.warning("[SIM] TuneTab: %s", e)

                # CP Tools tab — auto-run scan after short delay
                elif cls == "CPToolsTab":
                    try:
                        def _cp_scan_deferred(t=tab):
                            time.sleep(1.2)
                            if hasattr(t, "_do_scan"):
                                t.after(0, t._do_scan)
                        import threading
                        threading.Thread(target=_cp_scan_deferred,
                                         daemon=True).start()
                        log.info("[SIM] CPToolsTab: scan queued")
                    except Exception as e:
                        log.warning("[SIM] CPToolsTab: %s", e)

                # Diagnostics tab — auto-run bus scan
                elif cls == "DiagTab":
                    try:
                        def _diag_scan_deferred(t=tab):
                            time.sleep(2.0)
                            if hasattr(t, "_do_bus_scan"):
                                t.after(0, t._do_bus_scan)
                        import threading
                        threading.Thread(target=_diag_scan_deferred,
                                         daemon=True).start()
                        log.info("[SIM] DiagTab: bus scan queued")
                    except Exception as e:
                        log.warning("[SIM] DiagTab: %s", e)

                # Trans tab — set ZF8HP
                elif cls == "TransLoggerTab":
                    try:
                        tab._set_trans_by_key("ZF8HP")
                        log.info("[SIM] TransLoggerTab: ZF8HP set")
                    except Exception as e:
                        log.warning("[SIM] TransLoggerTab: %s", e)

                # Logger tab — enable live polling
                elif cls == "LoggerTab":
                    try:
                        # Live data will animate via the mock connection
                        log.info("[SIM] LoggerTab: ready (click REC)")
                    except Exception as e:
                        log.warning("[SIM] LoggerTab: %s", e)

            # Start simulated raw sniff frames
            def _on_frame(raw_frame):
                for tab in getattr(mw, "_tabs", []):
                    if type(tab).__name__ == "RawSniffTab":
                        try:
                            tab._on_raw_frame(raw_frame)
                        except Exception:
                            pass
            start_sniff_generator(_on_frame, interval=0.3)
            log.info("[SIM] Raw sniff generator started")

        except Exception as e:
            log.exception("[SIM] auto_connect error: %s", e)

    import threading
    threading.Thread(target=_do, daemon=True, name="sim-auto-connect").start()


def run_headless(ecu_key: str = "S85", trans_key: str = "ZF8HP") -> bool:
    """
    Import and exercise all backend modules without starting the GUI.
    Returns True if all checks pass.
    """
    print("=" * 60)
    print("Simos Suite — headless smoke test")
    print("=" * 60)
    ok = True

    # 1. Core definitions
    ecu   = None
    trans = None
    try:
        from core.ecu_defs import ECU_REGISTRY, get_ecu, SIMOS85
        from core.trans_defs import TRANS_REGISTRY, get_trans, ZF8HP
        ecu   = ECU_REGISTRY.get(ecu_key) or SIMOS85
        trans = TRANS_REGISTRY.get(trans_key) or ZF8HP
        print(f"  OK  core defs  — ECU: {ecu.name}")
        print(f"  OK  core defs  — TCU: {trans.name}")
    except Exception as e:
        print(f"  FAIL core defs — {e}"); ok = False
    if ecu is None:
        from core.ecu_defs import SIMOS85; ecu = SIMOS85
    if trans is None:
        from core.ecu_defs import ZF8HP; trans = ZF8HP

    # 2. Mock connection — ECU extended session
    try:
        from tests.mock_connection import MockConnection, MockECU
        conn = MockConnection(MockECU.SIMOS85, latency=0)
        conn.open()
        conn.specific_send(bytes([0x10, 0x03]))
        resp = conn.specific_wait_frame(timeout=2.0)
        assert resp[0] == 0x50, f"Expected 0x50, got {resp[0]:#04x}"
        conn.close()
        print("  OK  mock ECU   — extended session response correct")
    except Exception as e:
        print(f"  FAIL mock ECU  — {e}"); ok = False

    # 3. Mock connection — read VIN DID
    try:
        from tests.mock_connection import MockConnection, MockECU
        conn = MockConnection(MockECU.SIMOS85, latency=0)
        conn.open()
        conn.specific_send(bytes([0x22, 0xF1, 0x90]))
        resp = conn.specific_wait_frame(timeout=2.0)
        assert resp[0] == 0x62, f"Expected 0x62, got {resp[0]:#04x}"
        vin = resp[3:20].decode("ascii", errors="replace").strip()
        print(f"  OK  mock DID   — VIN: {vin}")
        conn.close()
    except Exception as e:
        print(f"  FAIL mock DID  — {e}"); ok = False

    # 4. Mock connection — TCU gear DID
    try:
        from tests.mock_connection import MockConnection, MockECU
        tcu_v = MockECU.DQ250 if "DQ" in trans_key.upper() else MockECU.ZF8HP
        conn = MockConnection(tcu_v, latency=0)
        conn.open()
        conn.specific_send(bytes([0x22, 0x01, 0x80]))  # current gear
        resp = conn.specific_wait_frame(timeout=2.0)
        assert resp[0] == 0x62, f"Expected 0x62, got {resp[0]:#04x}"
        g = resp[3]
        gstr = {0xFF:"P",0xFE:"R",0xFD:"N",0xFC:"D"}.get(g, str(g))
        print(f"  OK  mock TCU   — gear 0x0180: {gstr} ({g:#04x})")
        conn.close()
    except Exception as e:
        print(f"  FAIL mock TCU  — {e}"); ok = False

    # 5. CalParser on synthetic CAL
    try:
        cal = make_synthetic_cal(ecu)
        assert len(cal) == ecu.cal_block.length, \
            f"CAL size mismatch: {len(cal)} vs {ecu.cal_block.length}"
        print(f"  OK  synthetic CAL — {len(cal):#x} bytes")
    except Exception as e:
        print(f"  FAIL synthetic CAL — {e}"); ok = False

    # 6. Flash progress simulation
    try:
        events = []
        simulate_flash_sequence(events.append, speed_multiplier=1000.0)
        time.sleep(0.5)
        assert any(e.step == "DONE" for e in events), \
            f"Expected DONE event, got: {[e.step for e in events]}"
        print(f"  OK  flash sim  — {len(events)} events, final: {events[-1].step}")
    except Exception as e:
        print(f"  FAIL flash sim — {e}"); ok = False

    # 7. Import chain — check all UI modules import without error
    ui_modules = [
        "ui.interface_panel",
        "ui.trans_logger",
    ]
    # Hardware deps that are legitimately absent in CI / headless environments
    _HARDWARE_DEPS = {"bleak", "udsoncan", "serial", "can", "Crypto"}

    for mod in ui_modules:
        try:
            # Only check if tkinter is available
            import tkinter  # noqa
            __import__(mod)
            print(f"  OK  import     — {mod}")
        except ModuleNotFoundError as e:
            missing = str(e).replace("No module named ", "").strip("'")
            top = missing.split(".")[0]
            if "tkinter" in str(e):
                print(f"  SKIP import    — {mod} (no tkinter on this platform)")
            elif top in _HARDWARE_DEPS:
                print(f"  SKIP import    — {mod} (hardware dep '{top}' not installed — OK in CI)")
            else:
                print(f"  FAIL import    — {mod}: {e}"); ok = False
        except Exception as e:
            print(f"  FAIL import    — {mod}: {e}"); ok = False

    print("=" * 60)
    print(f"Result: {'PASS' if ok else 'FAIL'}")
    return ok


# ── Main entry point ───────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-32s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(
        description="Simos Suite simulation harness")
    ap.add_argument("--ecu",       default="S85",
                    help="ECU to simulate (S85, SC8, SC1, SC2)")
    ap.add_argument("--trans",     default="ZF8HP",
                    help="Transmission to simulate (ZF8HP, DL501, DQ250, DQ381)")
    ap.add_argument("--headless",  action="store_true",
                    help="Smoke-test imports without launching GUI")
    ap.add_argument("--no-autoconnect", action="store_true",
                    help="Don't auto-trigger connect after launch")
    ap.add_argument("--debug",     action="store_true")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.headless:
        success = run_headless(args.ecu, args.trans)
        sys.exit(0 if success else 1)

    # ── GUI mode ──────────────────────────────────────────────────────────────
    # Install patches before importing tkinter-dependent modules
    _install_mock_patch(args.ecu, args.trans)
    _install_interface_patch()

    try:
        from ui.main_window import MainWindow
    except ImportError as e:
        print(f"[ERROR] Could not import MainWindow: {e}")
        print("  Run from the repo root: python -m tests.sim_runner")
        sys.exit(1)

    app = MainWindow(ecu_key=args.ecu)

    if not args.no_autoconnect:
        auto_connect_after_launch(app, delay=1.5)

    log.info("Simos Suite [SIMULATION MODE]  ECU=%s  TCU=%s",
             args.ecu, args.trans)
    log.info("All connections are simulated — no hardware required")
    app.mainloop()


if __name__ == "__main__":
    main()
