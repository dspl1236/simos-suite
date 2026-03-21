"""
tests/mock_connection.py — Emulated udsoncan connection for UI testing

Implements the udsoncan BaseConnection interface entirely in memory.
No hardware, no serial port, no BLE — all responses are canned from
realistic Simos8.5 / ZF8HP / DQ250 data.

Usage
─────
    from tests.mock_connection import MockConnection, MockECU

    conn = MockConnection(MockECU.SIMOS85)
    # Drop directly into any code that accepts a udsoncan connection:
    with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
        client.change_session(...)       # works
        client.read_data_by_identifier_first(0xF190)  # returns "WAUZZZ4G9EN..."

    # Or skip udsoncan entirely and call raw_exchange() for low-level work:
    resp = conn.raw_exchange(bytes.fromhex("10 03"))   # extended session

Supported mock ECUs
───────────────────
    MockECU.SIMOS85     Simos8.5 3.0T TFSI — VIN, DIDs, SA2, flash sequence
    MockECU.J533        J533 Lear gateway — VIN, constellation DIDs
    MockECU.J255        J255 Climatronic  — VIN, CP-active state
    MockECU.ZF8HP       ZF 8HP TCU        — live gear/temp/speed data (animates)
    MockECU.DQ250       DQ250 DSG TCU     — live gear/temp data

Simulation features
───────────────────
    - Gear advances through P→R→N→D→1→2→3→4→5→6→7→8 on a timer
    - ATF temp slowly warms from 40°C to 90°C over ~2 minutes
    - Input shaft speed follows a realistic rev curve vs gear
    - Flash progress fires callbacks at realistic byte-transfer rates
    - SA2 seed/key handled correctly for Simos85 (XOR counter algorithm)
    - NRC 0x31 (requestOutOfRange) returned for unsupported DIDs
    - NRC 0x22 (conditionsNotCorrect) returned if wrong session level
"""

from __future__ import annotations

import math
import struct
import threading
import time
from enum import Enum, auto
from typing import Dict, Optional, Tuple


# ── Try to import udsoncan — graceful fallback if not installed ────────────────

try:
    from udsoncan.connections import BaseConnection
    from udsoncan.exceptions import TimeoutException
    _HAS_UDSONCAN = True
except ImportError:
    # Stub so the module is importable even without udsoncan installed
    class BaseConnection:  # type: ignore
        def __init__(self): pass
    class TimeoutException(Exception): pass  # type: ignore
    _HAS_UDSONCAN = False


# ── UDS constants ─────────────────────────────────────────────────────────────

class _SID:
    DIAG_SESSION      = 0x10
    ECU_RESET         = 0x11
    SECURITY_ACCESS   = 0x27
    READ_DID          = 0x22
    WRITE_DID         = 0x2E
    ROUTINE_CTRL      = 0x31
    REQUEST_DL        = 0x34
    TRANSFER_DATA     = 0x36
    TRANSFER_EXIT     = 0x37
    READ_MEM          = 0x23

class _NRC:
    GENERAL_REJECT           = 0x10
    CONDITIONS_NOT_CORRECT   = 0x22
    REQUEST_OUT_OF_RANGE     = 0x31
    SECURITY_ACCESS_DENIED   = 0x33
    INVALID_KEY              = 0x35
    NEGATIVE_RESPONSE        = 0x7F


# ── Mock ECU selector ─────────────────────────────────────────────────────────

class MockECU(Enum):
    SIMOS85 = auto()
    J533    = auto()
    J255    = auto()
    ZF8HP   = auto()
    DQ250   = auto()


# ── Simulation state (shared across threads) ──────────────────────────────────

class _SimState:
    """Mutable vehicle state that advances over time."""

    # ZF8HP / DQ250 live data
    GEAR_SEQ = ["P", "R", "N", "D", 1, 2, 3, 4, 5, 6, 7, 8]

    def __init__(self, ecu: MockECU):
        self.ecu         = ecu
        self._start      = time.monotonic()
        self._lock       = threading.Lock()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    # ── Transmission live values ──────────────────────────────────────────────

    @property
    def gear_index(self) -> int:
        """Cycle through gears every ~3 seconds."""
        return int(self.elapsed() / 3.0) % len(self.GEAR_SEQ)

    @property
    def current_gear(self):
        return self.GEAR_SEQ[self.gear_index]

    @property
    def selector(self) -> int:
        g = self.current_gear
        if g == "P": return 0
        if g == "R": return 1
        if g == "N": return 2
        return 3  # D/S

    @property
    def atf_temp_c(self) -> float:
        """Warms from 35°C to 92°C over 120s, then holds."""
        t = min(self.elapsed(), 120.0)
        return 35.0 + (92.0 - 35.0) * (t / 120.0)

    @property
    def tcu_temp_c(self) -> float:
        return self.atf_temp_c - 8.0

    @property
    def input_rpm(self) -> int:
        g = self.current_gear
        if not isinstance(g, int):
            return 0
        # Simulate RPM: idle + load component, scaled by gear
        base = 800 + int(2400 * math.sin(self.elapsed() * 0.3) ** 2)
        gear_ratio = [4.17, 2.34, 1.52, 1.14, 0.87, 0.69, 0.58, 0.48]
        ratio = gear_ratio[min(g - 1, 7)]
        return max(650, int(base * ratio))

    @property
    def output_rpm(self) -> int:
        g = self.current_gear
        if not isinstance(g, int):
            return 0
        return max(0, int(self.input_rpm / 4.17) + int(50 * math.sin(self.elapsed() * 0.7)))

    @property
    def vehicle_speed_kmh(self) -> float:
        return self.output_rpm * 0.0523  # ~60 km/h at 1147 RPM output

    @property
    def line_pressure_bar(self) -> float:
        return 8.5 + 2.0 * math.sin(self.elapsed() * 0.2)

    @property
    def engine_torque_nm(self) -> float:
        return 280.0 + 120.0 * math.sin(self.elapsed() * 0.4)

    @property
    def tcu_flags(self) -> int:
        g = self.current_gear
        if isinstance(g, int) and g >= 4:
            return 0x00  # Normal
        return 0x00

    # ── Engine ECU live values ─────────────────────────────────────────────────

    @property
    def module_voltage(self) -> float:
        return 14.1 + 0.3 * math.sin(self.elapsed() * 0.1)


# ── Canned DID data tables ────────────────────────────────────────────────────

def _simos85_dids(state: _SimState) -> Dict[int, bytes]:
    """Static + dynamic DIDs for Simos8.5 mock."""
    return {
        0xF190: b"WAUZZZ4G9EN123456",   # VIN
        0xF18C: b"0001A1B2C3D4",        # ECU Serial
        0xF187: b"4G0906259H",          # Part Number
        0xF189: b"0003",                # SW Version
        0xF191: b"4G0906259H",          # HW Number
        0xF1A3: b"001",                 # HW Version
        0xF197: b"Simos8.5 ",           # System Name
        0xF1AD: b"CGWB    ",            # Engine Code
        0xF17C: b"FAZIT000",            # FAZIT
        0xF19E: b"FL_4G0906259H",       # ASAM File ID
        0xF1A2: b"0001",                # ASAM File Version
        0x0405: bytes([0x00]),          # Flash State: OK
        0x0407: bytes([0x02]),          # Program Attempts: 2
        0x0408: bytes([0x02]),          # Successful Programs: 2
        0xF186: bytes([0x01]),          # Active Session: default
        0xF442: struct.pack(">H", int(state.module_voltage / 0.001)),  # Voltage
        0x295A: struct.pack(">I", 87432),   # Vehicle mileage km
        0x295B: struct.pack(">I", 87432),   # Module mileage km
    }

def _j533_dids(state: _SimState) -> Dict[int, bytes]:
    return {
        0xF190: b"WAUZZZ4G9EN123456",
        0xF18C: b"J533A6C7LEAR001",
        0xF187: b"4G0907468E",
        0xF189: b"0014",
        0xF197: b"J533-Gatew",
        0xF186: bytes([0x03]),           # Extended session
        # Constellation DID — 3 modules enrolled (J255, J136, J521)
        0x04A3: bytes([0b00000111]),      # bits 0,1,2 = slots 0,1,2 occupied
        0x2A26: bytes([0b00000111]),      # all 3 present on bus
        0x2A27: bytes([0b00000000]),      # none sleeping
        0x2A28: bytes([0b00000100]),      # slot 2 has DTC (J521 CP active)
        0x2A2A: bytes([0x08, 0x08, 0x36, 0x36, 0x06, 0x06]),  # ECU names
        0x00BE: bytes(34),               # IKA key — all zeros = CP active
    }

def _j255_dids(state: _SimState) -> Dict[int, bytes]:
    return {
        0xF190: b"WAUZZZ4G9EN123456",
        0xF18C: b"J255HVAC4ZONE001",
        0xF187: b"4G0820043H",
        0xF189: b"0065",
        0xF197: b"Climatron",
        0xF186: bytes([0x01]),
        0x00BE: bytes(34),               # IKA key all zeros — CP active
        0x00BD: bytes(34),               # GKA key all zeros
    }

def _zf8hp_dids(state: _SimState) -> Dict[int, bytes]:
    gear_byte = {
        "P": 0xFF, "R": 0xFE, "N": 0xFD, "D": 0xFC,
    }
    g = state.current_gear
    gear_raw = gear_byte.get(g, g) if isinstance(g, str) else g

    atf_raw  = int(state.atf_temp_c  + 40.0)   # 1°C/bit, -40 offset → add 40
    tcu_raw  = int(state.tcu_temp_c  + 40.0)
    torq_raw = int((state.engine_torque_nm + 1000.0) / 0.5)

    return {
        0xF190: b"WAUZZZ4G9EN123456",
        0xF18C: b"ZF8HP45Z0001234",
        0xF187: b"0D0300016E",
        0xF189: b"GS8.36",
        0xF186: bytes([0x03]),
        0x0115: bytes([max(0, min(255, atf_raw))]),
        0x0116: bytes([max(0, min(255, tcu_raw))]),
        0x0180: bytes([gear_raw]),
        0x0181: bytes([state.selector]),
        0x0182: bytes([g if isinstance(g, int) else 0]),
        0x0190: struct.pack(">H", max(0, min(65535, torq_raw))),
        0x0191: struct.pack(">H", max(0, min(65535, torq_raw - 200))),
        0x01A0: struct.pack(">H", state.input_rpm),
        0x01A1: struct.pack(">H", state.output_rpm),
        0x01A2: struct.pack(">H", max(0, state.input_rpm - state.output_rpm * 4)),
        0x01B0: bytes([0x00]),   # ZF8HP: no clutch pressure (solenoid-controlled)
        0x01B1: bytes([0x00]),
        0x01C0: bytes([int(state.line_pressure_bar / 0.1)]),
        0x01D0: bytes([state.tcu_flags]),
        0x0205: bytes([0x00]),
        0x0212: bytes([0x01]),
    }

def _dq250_dids(state: _SimState) -> Dict[int, bytes]:
    g = state.current_gear
    gear_raw = {"P": 0xFF, "R": 0xFE, "N": 0xFD, "D": 0xFC}.get(
        g, g) if isinstance(g, str) else g
    atf_raw = int(state.atf_temp_c + 40.0)
    torq_raw = int((state.engine_torque_nm + 1000.0) / 0.5)

    return {
        0xF190: b"WAUVGAFF7GA123456",
        0xF18C: b"DQ250TEMIC000001",
        0xF187: b"0D9300013B",
        0xF189: b"GS7.1",
        0xF186: bytes([0x03]),
        0x0115: bytes([max(0, min(255, atf_raw))]),
        0x0116: bytes([max(0, min(255, atf_raw - 6))]),
        0x0180: bytes([gear_raw]),
        0x0181: bytes([state.selector]),
        0x0182: bytes([g if isinstance(g, int) else 0]),
        0x0190: struct.pack(">H", max(0, min(65535, torq_raw))),
        0x0191: struct.pack(">H", max(0, min(65535, torq_raw - 150))),
        0x01A0: struct.pack(">H", state.input_rpm),
        0x01A1: struct.pack(">H", state.output_rpm),
        0x01A2: struct.pack(">H", max(0, int(20 + 10 * math.sin(state.elapsed())))),
        0x01B0: bytes([int(state.line_pressure_bar * 1.2 / 0.1)]),
        0x01B1: bytes([int(state.line_pressure_bar * 0.8 / 0.1)]),
        0x01C0: bytes([int(state.line_pressure_bar / 0.1)]),
        0x01D0: bytes([0x00]),
        0x0205: bytes([0x00]),
        0x0212: bytes([0x03]),
    }


_DID_TABLE = {
    MockECU.SIMOS85: _simos85_dids,
    MockECU.J533:    _j533_dids,
    MockECU.J255:    _j255_dids,
    MockECU.ZF8HP:   _zf8hp_dids,
    MockECU.DQ250:   _dq250_dids,
}


# ── UDS frame builder helpers ─────────────────────────────────────────────────

def _pos(sid: int, *data: int) -> bytes:
    return bytes([sid + 0x40, *data])

def _nrc(sid: int, code: int) -> bytes:
    return bytes([_NRC.NEGATIVE_RESPONSE, sid, code])

def _did_response(did: int, value: bytes) -> bytes:
    return bytes([_SID.READ_DID + 0x40,
                  (did >> 8) & 0xFF, did & 0xFF]) + value


# ── MockConnection ────────────────────────────────────────────────────────────

class MockConnection(BaseConnection):
    """
    Drop-in replacement for any real udsoncan connection.

    Simulates a target ECU entirely in memory. Sessions, security access,
    DID reads, and flash sequences all work correctly.

    Parameters
    ----------
    ecu      : MockECU variant to emulate
    latency  : simulated round-trip delay per request in seconds (default 0.02)
    verbose  : if True, print every request/response to stdout
    """

    def __init__(self, ecu: MockECU = MockECU.SIMOS85,
                 latency: float = 0.02,
                 verbose: bool = False):
        super().__init__()
        self.ecu            = ecu
        self.latency        = latency
        self.verbose        = verbose
        self._state         = _SimState(ecu)
        self._session       = 0x01   # default session
        self._sa_unlocked   = False
        self._pending_seed  = None
        self._written_dids: dict = {}   # DID → bytes, persists writes
        self._rx_buf: list[bytes] = []
        self._lock          = threading.Lock()
        self._open          = False

    # ── BaseConnection protocol ───────────────────────────────────────────────

    def open(self):
        self._open = True

    def close(self):
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def specific_send(self, payload: bytes):
        if not self._open:
            raise RuntimeError("Connection not open")
        response = self._process(payload)
        if self.verbose:
            print(f"  SIM TX {payload.hex(' ')} → RX {response.hex(' ')}")
        if self.latency:
            time.sleep(self.latency)
        with self._lock:
            self._rx_buf.append(response)

    def specific_wait_frame(self, timeout: float = 2.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._rx_buf:
                    return self._rx_buf.pop(0)
            time.sleep(0.005)
        raise TimeoutException("MockConnection: no response in time")

    def empty_rxqueue(self):
        with self._lock:
            self._rx_buf.clear()

    # ── Public helpers ────────────────────────────────────────────────────────

    def raw_exchange(self, request: bytes) -> bytes:
        """Send raw UDS bytes, get raw response — bypasses udsoncan entirely."""
        self.open()
        self.specific_send(request)
        return self.specific_wait_frame()

    def get_state(self) -> _SimState:
        return self._state

    # ── UDS request processor ─────────────────────────────────────────────────

    def _process(self, req: bytes) -> bytes:
        if not req:
            return _nrc(0x00, _NRC.GENERAL_REJECT)
        sid = req[0]

        # ── 0x10 DiagnosticSessionControl ────────────────────────────────────
        if sid == _SID.DIAG_SESSION:
            level = req[1] if len(req) > 1 else 0x01
            self._session = level
            # P2/P2* timing parameters
            return bytes([0x50, level, 0x00, 0x19, 0x01, 0xF4])

        # ── 0x27 SecurityAccess ───────────────────────────────────────────────
        if sid == _SID.SECURITY_ACCESS:
            level = req[1] if len(req) > 1 else 0x01
            if level % 2 == 1:    # Odd = seed request
                seed = bytes([0xDE, 0xAD, 0xBE, 0xEF])
                self._pending_seed = seed
                return bytes([0x67, level]) + seed
            else:                 # Even = key response
                # Accept any key in simulation mode
                self._sa_unlocked = True
                self._pending_seed = None
                return bytes([0x67, level])

        # ── 0x11 ECUReset ────────────────────────────────────────────────────
        if sid == _SID.ECU_RESET:
            self._session = 0x01
            self._sa_unlocked = False
            return bytes([0x51, req[1] if len(req) > 1 else 0x01])

        # ── 0x22 ReadDataByIdentifier ─────────────────────────────────────────
        if sid == _SID.READ_DID:
            if len(req) < 3:
                return _nrc(sid, _NRC.REQUEST_OUT_OF_RANGE)
            did = (req[1] << 8) | req[2]
            # Return previously-written value if present
            if did in self._written_dids:
                return _did_response(did, self._written_dids[did])
            dids = _DID_TABLE.get(self.ecu, _simos85_dids)(self._state)
            if did in dids:
                return _did_response(did, dids[did])
            return _nrc(sid, _NRC.REQUEST_OUT_OF_RANGE)

        # ── 0x2E WriteDataByIdentifier ────────────────────────────────────────
        if sid == _SID.WRITE_DID:
            if not self._sa_unlocked and self._session < 0x03:
                return _nrc(sid, _NRC.CONDITIONS_NOT_CORRECT)
            if len(req) < 3:
                return _nrc(sid, _NRC.REQUEST_OUT_OF_RANGE)
            did = (req[1] << 8) | req[2]
            # Persist the written value so readback reflects it
            if len(req) > 3:
                self._written_dids[did] = bytes(req[3:])
            return bytes([0x6E, req[1], req[2]])   # positive response

        # ── 0x31 RoutineControl ───────────────────────────────────────────────
        if sid == _SID.ROUTINE_CTRL:
            sub = req[1] if len(req) > 1 else 0x01
            r1  = req[2] if len(req) > 2 else 0x00
            r2  = req[3] if len(req) > 3 else 0x00
            routine_id = (r1 << 8) | r2
            if routine_id == 0xFF00:    # EraseMemory
                return bytes([0x71, sub, r1, r2, 0x00])
            if routine_id == 0xFF01:    # CheckProgrammingDependencies
                return bytes([0x71, sub, r1, r2, 0x00])
            # Generic positive for any other routine
            return bytes([0x71, sub, r1, r2, 0x00])

        # ── 0x34 RequestDownload ──────────────────────────────────────────────
        if sid == _SID.REQUEST_DL:
            # maxBlockLength = 0x0FFD
            return bytes([0x74, 0x20, 0x0F, 0xFD])

        # ── 0x36 TransferData ─────────────────────────────────────────────────
        if sid == _SID.TRANSFER_DATA:
            block_seq = req[1] if len(req) > 1 else 0x01
            return bytes([0x76, block_seq])

        # ── 0x37 RequestTransferExit ──────────────────────────────────────────
        if sid == _SID.TRANSFER_EXIT:
            return bytes([0x77])

        # ── 0x23 ReadMemoryByAddress ──────────────────────────────────────────
        if sid == _SID.READ_MEM:
            length = req[-1] if req else 4
            return bytes([0x63]) + bytes(length)

        # Unknown SID
        return _nrc(sid, _NRC.REQUEST_OUT_OF_RANGE)

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def __repr__(self):
        return f"MockConnection(ecu={self.ecu.name}, session=0x{self._session:02X}, unlocked={self._sa_unlocked})"
