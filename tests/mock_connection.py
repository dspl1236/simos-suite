"""
tests/mock_connection.py — Simulated UDS connection for UI testing

A udsoncan-compatible BaseConnection that responds to UDS requests with
canned but realistic data, without any hardware attached.

Simulates:
  - DiagnosticSessionControl  (0x10) — accepts extended + programming
  - SecurityAccess            (0x27) — always grants access
  - ReadDataByIdentifier      (0x22) — returns per-ECU DID values
  - RoutineControl            (0x31) — erase, checksum verify
  - RequestDownload           (0x34) — accepts, returns max_length
  - TransferData              (0x36) — ACKs every block
  - RequestTransferExit       (0x37) — accepts

Usage:
    from tests.mock_connection import MockConnection, SimulatedECU, SimulatedTCU
    from core.ecu_defs import SIMOS85
    from core.trans_defs import ZF8HP

    conn = MockConnection(SimulatedECU(SIMOS85))
    # or
    conn = MockConnection(SimulatedTCU(ZF8HP))

    import udsoncan
    with udsoncan.Client(conn, request_timeout=5) as client:
        client.change_session(...)
        info = client.read_data_by_identifier([0xF190])
"""

from __future__ import annotations

import queue
import struct
import threading
import time
import logging
import random
import math
from typing import Dict, Optional, Any

log = logging.getLogger("SimosSuite.Mock")


# ── UDS service IDs ────────────────────────────────────────────────────────────
SID_SESSION        = 0x10
SID_SECURITY       = 0x27
SID_READ_DID       = 0x22
SID_WRITE_DID      = 0x2E
SID_ROUTINE        = 0x31
SID_REQ_DOWNLOAD   = 0x34
SID_TRANSFER_DATA  = 0x36
SID_TRANSFER_EXIT  = 0x37
SID_ECU_RESET      = 0x11
NRC_REQUEST_OUT_OF_RANGE = 0x31
NRC_CONDITIONS_NOT_CORRECT = 0x22


# ── ISO-TP framing helpers ─────────────────────────────────────────────────────

def _isotp_frame(sid_response: int, payload: bytes) -> bytes:
    """Wrap a UDS positive response in a minimal ISO-TP single frame."""
    data = bytes([sid_response]) + payload
    length = len(data)
    if length <= 7:
        return bytes([length]) + data + bytes(8 - length - 1)
    else:
        # First frame
        first = bytes([0x10 | (length >> 8), length & 0xFF]) + data[:6]
        return first  # MockConnection handles reassembly internally

def _nrc_frame(sid: int, nrc: int) -> bytes:
    """ISO-TP negative response."""
    return bytes([0x03, 0x7F, sid, nrc, 0, 0, 0, 0])


# ══════════════════════════════════════════════════════════════════════════════
# Simulated device base
# ══════════════════════════════════════════════════════════════════════════════

class SimulatedDevice:
    """
    Base class for a simulated UDS device.
    Subclass and override did_values to provide device-specific responses.
    """

    def __init__(self):
        self._session = 0x01   # default session
        self._security_level = 0
        self._start_time = time.monotonic()

    def uptime(self) -> float:
        return time.monotonic() - self._start_time

    def handle_request(self, request_bytes: bytes) -> bytes:
        """
        Parse a raw UDS request and return a raw UDS response (no ISO-TP header).
        Returns bytes of the response payload (positive or negative).
        """
        if not request_bytes:
            return self._nrc(0x00, NRC_CONDITIONS_NOT_CORRECT)

        sid = request_bytes[0]
        data = request_bytes[1:]

        if sid == SID_SESSION:
            return self._session_control(data)
        elif sid == SID_SECURITY:
            return self._security_access(data)
        elif sid == SID_READ_DID:
            return self._read_did(data)
        elif sid == SID_WRITE_DID:
            return bytes([0x6E]) + data[:2]   # positive, echo DID
        elif sid == SID_ROUTINE:
            return self._routine_control(data)
        elif sid == SID_REQ_DOWNLOAD:
            return self._request_download(data)
        elif sid == SID_TRANSFER_DATA:
            return bytes([0x76, data[0] if data else 0x01])
        elif sid == SID_TRANSFER_EXIT:
            return bytes([0x77])
        elif sid == SID_ECU_RESET:
            return bytes([0x51, data[0] if data else 0x01])
        else:
            return self._nrc(sid, NRC_REQUEST_OUT_OF_RANGE)

    def _nrc(self, sid: int, code: int) -> bytes:
        return bytes([0x7F, sid, code])

    def _session_control(self, data: bytes) -> bytes:
        mode = data[0] if data else 0x01
        self._session = mode
        # P2=25ms, P2*=5000ms (standard timing params in response)
        return bytes([0x50, mode, 0x00, 0x19, 0x01, 0xF4])

    def _security_access(self, data: bytes) -> bytes:
        level = data[0] if data else 0x01
        if level % 2 == 1:
            # Seed request — return a fixed seed
            seed = bytes([0xDE, 0xAD, 0xBE, 0xEF])
            return bytes([0x67, level]) + seed
        else:
            # Key response — always grant
            self._security_level = level - 1
            return bytes([0x67, level])

    def _read_did(self, data: bytes) -> bytes:
        if len(data) < 2:
            return self._nrc(SID_READ_DID, NRC_REQUEST_OUT_OF_RANGE)
        did = (data[0] << 8) | data[1]
        values = self.did_values()
        if did in values:
            payload = values[did]
            if isinstance(payload, str):
                payload = payload.encode("ascii", errors="replace").ljust(17)[:17]
            return bytes([0x62]) + bytes([data[0], data[1]]) + bytes(payload)
        return self._nrc(SID_READ_DID, NRC_REQUEST_OUT_OF_RANGE)

    def _routine_control(self, data: bytes) -> bytes:
        if len(data) < 3:
            return self._nrc(SID_ROUTINE, NRC_CONDITIONS_NOT_CORRECT)
        subf = data[0]
        rid  = (data[1] << 8) | data[2]
        # Erase memory (0xFF00), verify checksum (0xFF01) — both succeed
        return bytes([0x71, subf, data[1], data[2], 0x00])

    def _request_download(self, data: bytes) -> bytes:
        # Respond with max block length of 0xFFD bytes
        return bytes([0x74, 0x20, 0x0F, 0xFD])

    def did_values(self) -> Dict[int, Any]:
        """Override in subclass to provide device-specific DID responses."""
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Simulated Simos ECU
# ══════════════════════════════════════════════════════════════════════════════

class SimulatedECU(SimulatedDevice):
    """
    Simulates a Simos ECU. Responds to all standard VW info DIDs
    and a set of engine live-data values that gently drift over time.
    """

    def __init__(self, ecu_def=None):
        super().__init__()
        self._ecu = ecu_def
        # Baseline engine live values (drift slightly each call to feel alive)
        self._rpm_base      = 850.0    # idle
        self._coolant_base  = 88.0     # °C
        self._map_base      = 98.0     # kPa (idle vacuum)
        self._iat_base      = 32.0     # °C
        self._lambda_base   = 1.000
        self._bat_base      = 14.1     # V

    def _live(self, base: float, variance: float) -> float:
        """Return a slightly randomised reading around base."""
        return base + random.uniform(-variance, variance)

    def did_values(self) -> Dict[int, Any]:
        project = self._ecu.project_code if self._ecu else "S85"
        part    = (self._ecu.compatible_hw[0]
                   if self._ecu and self._ecu.compatible_hw
                   else "4G0906259E")

        # Build all standard VW identity DIDs
        d = {
            0xF190: b"WAUZZZ4G9EN123456",          # VIN (17 bytes)
            0xF18C: b"1234567890       ",           # ECU serial
            0xF187: (part + "      ")[:17].encode(),# part number
            0xF189: b"0001             ",           # SW version
            0xF191: b"H13              ",           # HW number
            0xF1A3: b"0001             ",           # HW version
            0xF197: (f"Simos8.5 {project}  ")[:17].encode(),
            0xF1AD: b"CGWB             ",           # engine code
            0xF17C: b"FAZIT_DEMO       ",           # FAZIT
            0xF19E: b"EV_ECM30TFS_001  ",           # ASAM file ID
            0xF1A2: b"0001             ",           # ASAM version
            0x0405: bytes([0x00]),                  # flash state: OK
            0x0407: bytes([0x03]),                  # program attempts: 3
            0x0408: bytes([0x03]),                  # successful programs: 3
            0xF186: bytes([0x03]),                  # active session: extended
            0xF442: struct.pack(">H",
                int(self._live(self._bat_base, 0.05) * 1000)),  # voltage mV
            0x295A: struct.pack(">I", 87432),       # vehicle mileage km
            0x295B: struct.pack(">I", 87432),       # module mileage km
        }

        # Engine live data (ReadMemoryByAddress style DIDs where available)
        t = self.uptime()
        rpm  = self._live(850 + 20 * math.sin(t * 0.3), 30)
        cool = self._live(88 + 2 * math.sin(t * 0.05), 0.5)
        lam  = self._live(1.000 + 0.003 * math.sin(t * 0.7), 0.002)

        # Pack as big-endian u16 scaled values (matches ReadMemoryByAddress $23 format)
        d[0xF405] = struct.pack(">H", int(rpm * 4))        # rpm raw (÷4 to get rpm)
        d[0xF406] = struct.pack(">H", int(cool + 40))      # coolant (scale+offset)
        d[0xF410] = struct.pack(">H", int(lam * 32768))    # lambda (÷32768)

        return d


# ══════════════════════════════════════════════════════════════════════════════
# Simulated TCU (transmission control unit)
# ══════════════════════════════════════════════════════════════════════════════

class SimulatedTCU(SimulatedDevice):
    """
    Simulates a transmission TCU. Responds to standard VW DIDs
    plus the full TCU_LIVE_DIDS set from trans_defs.py.
    Values animate over time: gear shifts, temp warm-up, speed sweep.
    """

    def __init__(self, trans_def=None):
        super().__init__()
        self._trans = trans_def

    def did_values(self) -> Dict[int, Any]:
        t = self.uptime()
        trans_name = self._trans.name if self._trans else "ZF 8HP Demo"
        project    = self._trans.project if self._trans else "ZF8HP"

        # Animate: slowly cycle through gears, warm up fluid, vary speeds
        gear   = max(1, min(8, int(1 + (t % 60) / 8)))   # shift every ~8s
        atf    = min(90.0, 45.0 + t * 0.15)               # warm up to 90°C
        vspeed = max(0.0, min(130.0, (t % 40) * 3.5))     # 0–130 km/h cycle
        in_rpm = max(700.0, vspeed * 35 + 700)             # rough input shaft
        out_rpm = max(0.0, vspeed * 28)                    # output shaft
        line_p  = 8.5 + gear * 0.8                        # pressure rises with gear
        sel    = 3   # Drive (P=0, R=1, N=2, D=3)
        bat    = 14.1 + 0.05 * math.sin(t * 0.3)

        # Identity DIDs
        d = {
            0xF190: b"WAUZZZ4G9EN123456",
            0xF18C: (f"{project}_SIM_001").encode().ljust(17)[:17],
            0xF187: b"0B8927156E       ",
            0xF189: b"0012             ",
            0xF186: bytes([0x03]),
        }

        # ── Live values (matches TCU_LIVE_DIDS keys in trans_defs.py) ─────────
        # Temperatures (raw uint8, physical = raw - 40)
        d[0x0115] = struct.pack(">B", int(atf + 40))        # Trans Fluid Temp
        d[0x0116] = struct.pack(">B", int(atf + 35 + 40))   # TCU Temp

        # Gear & selector
        d[0x0180] = struct.pack(">B", gear)                  # Current Gear
        d[0x0181] = struct.pack(">B", gear)                  # Target Gear
        d[0x0182] = struct.pack(">B", sel)                   # Selector Position

        # Speeds (uint16, raw = physical value, rpm)
        d[0x0190] = struct.pack(">H", int(in_rpm))           # Input Speed
        d[0x0191] = struct.pack(">H", int(out_rpm))          # Output Speed
        d[0x0192] = struct.pack(">H", int(vspeed * 100))     # Vehicle Speed ×100

        # TCC (ZF8HP specific)
        d[0x01A0] = struct.pack(">H", max(0, int(
            20 * math.sin(t * 2) if gear >= 4 else 80)))    # TCC Slip rpm
        d[0x01A1] = struct.pack(">B", int(
            95 if gear >= 4 else 0))                         # TCC Duty %

        # Pressures (uint16, raw = bar × 100)
        d[0x01B0] = struct.pack(">H", int(line_p * 100))     # Line Pressure
        d[0x01B1] = struct.pack(">H", int(line_p * 100))     # Line Pressure Target

        # Torque (int16, raw Nm)
        torque_req = int(80 + 30 * math.sin(t * 0.5))
        d[0x01C0] = struct.pack(">h", torque_req)            # Engine Torque Req
        d[0x01C1] = struct.pack(">h", torque_req - 5)        # TCC Torque
        d[0x01C2] = struct.pack(">h", torque_req - 8)        # Output Torque

        # Electrical
        d[0x01D0] = struct.pack(">B", int(bat * 10))         # Voltage ×10

        # Wear / adaptation
        d[0x01E0] = struct.pack(">B", 12)                    # Wear index %
        d[0x01E1] = struct.pack(">I", 142867)                # Shift count
        d[0x01E2] = struct.pack(">I", 18000)                 # Oil service km

        return d


# ══════════════════════════════════════════════════════════════════════════════
# MockConnection — udsoncan BaseConnection compatible
# ══════════════════════════════════════════════════════════════════════════════

class MockConnection:
    """
    Drop-in replacement for any real udsoncan connection.
    Wraps a SimulatedDevice and handles the ISO-TP framing expected by
    udsoncan's BaseConnection protocol.

    Usage:
        conn = MockConnection(SimulatedECU(SIMOS85))
        with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
            result = client.read_data_by_identifier([0xF190])

    latency_ms: artificial round-trip delay (default 15ms — feels like real CAN)
    """

    def __init__(self, device: SimulatedDevice, latency_ms: float = 15.0):
        self._device    = device
        self._latency   = latency_ms / 1000.0
        self._rx_queue: queue.Queue = queue.Queue()
        self._opened    = False
        self.name       = f"MockConnection({type(device).__name__})"
        self.logger     = logging.getLogger(self.name)

    # ── udsoncan BaseConnection interface ─────────────────────────────────────

    def open(self):
        self._opened = True
        self.logger.debug("opened")

    def close(self):
        self._opened = False
        self.logger.debug("closed")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    def is_open(self):
        return self._opened

    def specific_send(self, payload: bytes):
        """
        Called by udsoncan to send a UDS request.
        We process it immediately and queue the response.
        """
        if not self._opened:
            raise RuntimeError("MockConnection not open")
        self.logger.debug("TX  %s", payload.hex())

        def _respond():
            time.sleep(self._latency)
            try:
                response = self._device.handle_request(bytes(payload))
                self.logger.debug("RX  %s", response.hex())
                self._rx_queue.put(bytes(response))
            except Exception as e:
                self.logger.error("Mock device error: %s", e)
                nrc = bytes([0x7F, payload[0] if payload else 0, 0x10])
                self._rx_queue.put(nrc)

        threading.Thread(target=_respond, daemon=True).start()

    def specific_wait_frame(self, timeout: Optional[float] = None) -> bytes:
        """Called by udsoncan to receive a UDS response."""
        try:
            frame = self._rx_queue.get(
                timeout=timeout if timeout else 5.0)
            return frame
        except queue.Empty:
            from udsoncan.exceptions import TimeoutException
            raise TimeoutException(
                f"MockConnection timeout after {timeout}s")

    def empty_rxqueue(self):
        while not self._rx_queue.empty():
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break


# ── Convenience factory ────────────────────────────────────────────────────────

def make_mock_ecu_connection(ecu_def=None) -> MockConnection:
    """Return a MockConnection wrapping a SimulatedECU."""
    return MockConnection(SimulatedECU(ecu_def), latency_ms=12.0)


def make_mock_tcu_connection(trans_def=None) -> MockConnection:
    """Return a MockConnection wrapping a SimulatedTCU."""
    return MockConnection(SimulatedTCU(trans_def), latency_ms=12.0)
