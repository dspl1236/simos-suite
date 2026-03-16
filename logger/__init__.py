"""
logger/ — Live data logging engine

Provides a polling logger that reads UDS DIDs from any connected ECU or TCU
on a configurable interval, formats values using the known scaling from
ecu_defs / trans_defs, and writes to CSV + in-memory ring buffer.

Public API
──────────
    from logger import Logger, Channel, LogSession

    session = LogSession(
        ecu       = SIMOS85,
        interface = "BLE",
        channels  = [Channel(0xF442, "Battery", "V", 0.001),
                     Channel(0x2000, "RPM",     "rpm", 0.25)],
        interval_ms = 200,
    )
    session.start(callback=lambda row: print(row))
    ...
    session.stop()
    session.save_csv("log_001.csv")

The LoggerTab in ui/main_window.py drives this directly.
"""

from __future__ import annotations

import csv
import io
import logging
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

log = logging.getLogger("SimosSuite.Logger")


# ── Channel descriptor ────────────────────────────────────────────────────────

@dataclass
class Channel:
    """
    A single DID to poll.

    did        : UDS DID (ReadDataByIdentifier)
    name       : display name
    unit       : physical unit string
    scale      : multiply raw integer by this → physical value
    offset     : add after scale
    length     : expected byte length of DID response payload
    signed     : True if raw integer is signed
    fmt        : display format string (e.g. "{:.1f}")
    enabled    : False to skip this channel without removing it
    """
    did:     int
    name:    str
    unit:    str
    scale:   float   = 1.0
    offset:  float   = 0.0
    length:  int     = 2
    signed:  bool    = False
    fmt:     str     = "{:.2f}"
    enabled: bool    = True

    def decode(self, raw: bytes) -> Optional[float]:
        """Decode raw DID response bytes to a physical float."""
        try:
            n = min(len(raw), self.length)
            raw_int = int.from_bytes(raw[:n], "big", signed=self.signed)
            return raw_int * self.scale + self.offset
        except Exception:
            return None

    def format(self, value: Optional[float]) -> str:
        if value is None:
            return "—"
        try:
            return self.fmt.format(value)
        except Exception:
            return str(value)


# ── Simos8.5 default channel set ─────────────────────────────────────────────
# Uses ReadDataByIdentifier DIDs that are accessible in extended session.

SIMOS85_CHANNELS: List[Channel] = [
    # Standard VW info DIDs (static, polled once at session open)
    Channel(0xF190, "VIN",           "",      1.0,    0.0,  17, False, "{}"),
    Channel(0xF186, "Session",       "",      1.0,    0.0,   1, False, "{:#04x}"),
    Channel(0xF442, "Battery",       "V",     0.001,  0.0,   2, False, "{:.2f}"),
    Channel(0x295A, "Mileage",       "km",    1.0,    0.0,   4, False, "{:.0f}"),

    # Live engine values — available in extended session on Simos8.5
    Channel(0x2000, "RPM",           "rpm",   0.25,   0.0,   2, False, "{:.0f}"),
    Channel(0x2001, "Boost",         "kPa",   0.1,    0.0,   2, False, "{:.1f}"),
    Channel(0x2002, "MAF",           "g/s",   0.01,   0.0,   2, False, "{:.2f}"),
    Channel(0x2003, "IAT",           "°C",    0.5,  -40.0,   2, False, "{:.1f}"),
    Channel(0x2004, "Lambda B1",     "λ",     0.001,  0.0,   2, False, "{:.3f}"),
    Channel(0x2005, "Inj PW",        "ms",    0.004,  0.0,   2, False, "{:.2f}"),
    Channel(0x2006, "Throttle",      "%",     0.1,    0.0,   2, False, "{:.1f}"),
    Channel(0x2007, "Torque req",    "Nm",    0.5,    0.0,   2, True,  "{:.0f}"),
    Channel(0x2008, "Coolant",       "°C",    0.5,  -40.0,   2, False, "{:.1f}"),
    Channel(0x2009, "Oil temp",      "°C",    0.5,  -40.0,   2, False, "{:.1f}"),
    Channel(0x200A, "Ign advance",   "°",     0.1,    0.0,   2, True,  "{:.1f}"),
    Channel(0x200B, "LTFT B1",       "%",     0.01, -100.0,  2, False, "{:.1f}"),
    Channel(0x200C, "STFT B1",       "%",     0.01, -100.0,  2, True,  "{:.1f}"),
]


# ── Log row ───────────────────────────────────────────────────────────────────

@dataclass
class LogRow:
    """One snapshot across all channels at a given timestamp."""
    timestamp:  float
    wall_time:  str
    values:     Dict[int, Optional[float]]
    raw:        Dict[int, bytes]

    def to_dict(self, channels: List[Channel]) -> Dict[str, str]:
        row: Dict[str, str] = {
            "timestamp": f"{self.timestamp:.3f}",
            "wall_time": self.wall_time,
        }
        for ch in channels:
            row[f"{ch.name} ({ch.unit})"] = ch.format(self.values.get(ch.did))
        return row


# ── Log session ───────────────────────────────────────────────────────────────

class LogSession:
    """
    Manages a live polling loop against a connected ECU.

    Parameters
    ----------
    ecu           ECUDef or TCUDef with .can_tx / .can_rx
    interface     interface string ("BLE", "USBISOTP", "J2534", ...)
    iface_path    port or DLL path
    ble_bridge    BLEBridgeSync instance if interface == "BLE"
    channels      list of Channel objects to poll
    interval_ms   poll interval in milliseconds (min 50)
    ring_size     max rows kept in memory (default 10,000 ~= 33min at 5Hz)
    """

    def __init__(
        self,
        ecu,
        interface:    str,
        iface_path:   Optional[str]  = None,
        ble_bridge                   = None,
        channels:     Optional[List[Channel]] = None,
        interval_ms:  int            = 200,
        ring_size:    int            = 10_000,
    ):
        self.ecu         = ecu
        self.interface   = interface
        self.iface_path  = iface_path
        self.ble_bridge  = ble_bridge
        self.channels    = [c for c in (channels or SIMOS85_CHANNELS) if c.enabled]
        self.interval    = max(50, interval_ms) / 1000.0
        self._ring: Deque[LogRow] = deque(maxlen=ring_size)
        self._running    = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[LogRow], None]] = None
        self._start_time: float = 0.0
        self._lock       = threading.Lock()
        self.error:  Optional[str] = None

    def start(self, callback: Optional[Callable[[LogRow], None]] = None):
        """Start polling. callback(row) called on every new row (from logger thread)."""
        if self._running:
            return
        self._callback = callback
        self._running  = True
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("LogSession started  interface=%s  channels=%d  interval=%.0fms",
                 self.interface, len(self.channels), self.interval * 1000)

    def stop(self):
        """Stop polling gracefully."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        log.info("LogSession stopped  rows=%d", len(self._ring))

    def rows(self) -> List[LogRow]:
        """Return a snapshot of the ring buffer (thread-safe)."""
        with self._lock:
            return list(self._ring)

    def latest(self) -> Optional[LogRow]:
        """Return the most recent row, or None."""
        with self._lock:
            return self._ring[-1] if self._ring else None

    def save_csv(self, path: str) -> int:
        """Write all rows to a CSV file. Returns row count."""
        rows = self.rows()
        if not rows:
            return 0
        fieldnames = list(rows[0].to_dict(self.channels).keys())
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in rows:
                w.writerow(row.to_dict(self.channels))
        log.info("Saved %d rows → %s", len(rows), path)
        return len(rows)

    def to_csv_string(self) -> str:
        """Return CSV as a string (for clipboard / display)."""
        rows = self.rows()
        if not rows:
            return ""
        buf = io.StringIO()
        fieldnames = list(rows[0].to_dict(self.channels).keys())
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row.to_dict(self.channels))
        return buf.getvalue()

    def _loop(self):
        try:
            import udsoncan
            from flasher.uds_flash import _make_connection
        except ImportError as e:
            self.error = f"Import error: {e}"
            log.error("LogSession import error: %s", e)
            self._running = False
            return

        try:
            conn = _make_connection(
                self.ecu,
                self.interface,
                interface_path = self.iface_path,
                ble_bridge     = self.ble_bridge,
            )
        except Exception as e:
            self.error = f"Connection failed: {e}"
            log.error("LogSession connect error: %s", e)
            self._running = False
            return

        class _Raw(udsoncan.DidCodec):
            def __init__(self, length):
                self._len = length
            def encode(self, v): return bytes(v)
            def decode(self, p): return bytes(p[:self._len])
            def __len__(self): return self._len

        cfg = dict(udsoncan.configs.default_client_config)
        cfg["data_identifiers"] = {
            ch.did: _Raw(ch.length) for ch in self.channels
        }
        cfg["request_timeout"] = max(2.0, self.interval * 3)

        with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
            try:
                client.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)
            except Exception as e:
                log.warning("LogSession session warning: %s", e)

            log.info("LogSession: extended session open, polling...")

            while self._running:
                t_start = time.monotonic()
                values: Dict[int, Optional[float]] = {}
                raw:    Dict[int, bytes]            = {}

                for ch in self.channels:
                    try:
                        resp = client.read_data_by_identifier_first(ch.did)
                        raw_bytes = bytes(resp) if resp else b""
                        raw[ch.did]    = raw_bytes
                        values[ch.did] = ch.decode(raw_bytes)
                    except udsoncan.exceptions.NegativeResponseException:
                        values[ch.did] = None
                    except Exception as e:
                        values[ch.did] = None
                        log.debug("DID 0x%04X error: %s", ch.did, e)

                elapsed   = time.monotonic() - self._start_time
                wall      = time.strftime("%H:%M:%S") + f".{int((elapsed % 1) * 1000):03d}"
                row       = LogRow(elapsed, wall, values, raw)

                with self._lock:
                    self._ring.append(row)

                if self._callback:
                    try:
                        self._callback(row)
                    except Exception as e:
                        log.debug("Logger callback error: %s", e)

                elapsed_poll = time.monotonic() - t_start
                sleep_time   = max(0.0, self.interval - elapsed_poll)
                time.sleep(sleep_time)

        log.info("LogSession loop exited")


# ── Channel preset re-exports ─────────────────────────────────────────────────
# from logger import CHANNELS_FUEL, CHANNELS_BOOST, PRESETS

try:
    from logger.channels_s85 import (
        CHANNELS_ESSENTIAL, CHANNELS_FUEL, CHANNELS_BOOST,
        CHANNELS_IGNITION, CHANNELS_LEAN_DIAG, CHANNELS_FULL,
        PRESETS,
    )
except ImportError:
    CHANNELS_ESSENTIAL = CHANNELS_FUEL = CHANNELS_BOOST = []
    CHANNELS_IGNITION  = CHANNELS_LEAN_DIAG = CHANNELS_FULL = []
    PRESETS: dict = {}
