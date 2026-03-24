"""
lib/connections/j2534_sniffer.py — Passive CAN bus sniffer via J2534

Opens the J2534 adapter in raw CAN mode (Protocol_ID.CAN = 5) with a
pass-all filter, reads every frame on the bus without transmitting.

Designed for use with an OBD-II splitter: VCDS/Ross-Tech on one port
driving the diagnostic session, Mongoose/Tactrix/VNCI on the other port
passively capturing the full exchange.

The J2534 spec requires:
  - PassThruConnect with CAN protocol at 500 kbps
  - PassThruStartMsgFilter with PASS_FILTER and mask=0x00000000
  - PassThruReadMsgs returns PASSTHRU_MSG where:
      Data[0:4] = 4-byte CAN ID (big-endian, 11-bit or 29-bit)
      Data[4:DataSize] = CAN payload (0-8 bytes for classic CAN)
      Timestamp = microseconds since channel opened
      RxStatus bit 0 = TX_MSG_TYPE (1 = echo of our own TX, 0 = from bus)

Since we never transmit, all frames will have RxStatus TX_MSG_TYPE = 0.

Usage:
    from lib.connections.j2534_sniffer import J2534CanSniffer

    sniffer = J2534CanSniffer("path/to/j2534.dll")
    sniffer.open()
    for frame in sniffer.read_frames():
        print(f"[{frame.can_id:03X}] {frame.data.hex()}")
    sniffer.close()
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import c_ulong, c_long, byref, POINTER
from dataclasses import dataclass
from typing import Iterator, List, Optional

from .j2534 import (
    J2534,
    PASSTHRU_MSG,
    Protocol_ID,
    Filter,
    Ioctl_ID,
    Error_ID,
)

log = logging.getLogger("SimosSuite.CAN_Sniffer")

# ── Known VAG diagnostic CAN ID ranges ───────────────────────────────────────
# On the C7 platform, tester → ECU requests are typically in the lower range
# and ECU responses are at a fixed offset.
#
# Common pairs:
#   J533 Gateway:    tester=0x710  ECU=0x77A
#   J255 HVAC:       tester=0x746  ECU=0x7B0  (via gateway routing)
#   J623 Engine:     tester=0x7E0  ECU=0x7E8
#   J217 Trans:      tester=0x7E1  ECU=0x7E9
#   OBD-II generic:  tester=0x7DF  ECU=0x7E8 (broadcast)
#
# VAG uses both 11-bit and extended addresses. UDS on CAN uses:
#   tester → ECU: lower CAN ID
#   ECU → tester: higher CAN ID (typically +0x08 or +0x06A etc)

KNOWN_TESTER_IDS = {
    0x710, 0x711, 0x712, 0x713, 0x714, 0x715, 0x716, 0x717,
    0x718, 0x719, 0x71A, 0x71B, 0x71C, 0x71D, 0x71E, 0x71F,
    0x720, 0x746, 0x7E0, 0x7E1, 0x7DF,
    # VCDS may also use extended addressing
}

# UDS service IDs — first byte of CAN payload after ISO-TP header
UDS_SERVICES = {
    0x10: "DiagSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDTCInfo",
    0x19: "ReadDTCInfo",
    0x22: "ReadDataByID",
    0x23: "ReadMemByAddr",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByID",
    0x2F: "IOControlByID",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "TransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}


def _uds_label(payload: bytes) -> str:
    """Decode UDS service ID from ISO-TP single/first frame payload."""
    if not payload:
        return ""

    # ISO-TP single frame: byte[0] high nibble = 0, low nibble = length
    # ISO-TP first frame:  byte[0] high nibble = 1
    # Consecutive frame:   byte[0] high nibble = 2
    # Flow control:        byte[0] high nibble = 3
    pci = (payload[0] >> 4) & 0x0F

    if pci == 0:
        # Single frame — UDS SID at byte[1]
        if len(payload) >= 2:
            sid = payload[1]
            if sid >= 0x40:
                return f"+{UDS_SERVICES.get(sid - 0x40, f'0x{sid:02X}')}"
            if sid == 0x7F and len(payload) >= 4:
                return f"NRC(0x{payload[3]:02X})"
            return UDS_SERVICES.get(sid, f"SID 0x{sid:02X}")
        return "SF"
    elif pci == 1:
        # First frame — UDS SID at byte[2]
        if len(payload) >= 3:
            sid = payload[2]
            if sid >= 0x40:
                return f"+{UDS_SERVICES.get(sid - 0x40, f'0x{sid:02X}')}(FF)"
            return f"{UDS_SERVICES.get(sid, f'SID 0x{sid:02X}')}(FF)"
        return "FF"
    elif pci == 2:
        return f"CF seq={payload[0] & 0x0F}"
    elif pci == 3:
        fs = payload[0] & 0x0F
        fs_str = {0: "CTS", 1: "WAIT", 2: "OVFL"}.get(fs, f"FS={fs}")
        return f"FC {fs_str}"
    return ""


@dataclass
class CANFrame:
    """A single raw CAN frame from the bus."""
    timestamp_us: int       # microseconds since channel opened
    can_id:       int       # 11-bit or 29-bit CAN ID
    is_extended:  bool      # True if 29-bit extended ID
    data:         bytes     # 0–8 byte payload
    is_tx_echo:   bool      # True if this is an echo of our own TX (should be False)
    direction:    str = ""  # "TX" or "RX" (tester vs ECU perspective)
    uds_label:    str = ""  # decoded UDS service name if applicable


class J2534CanSniffer:
    """
    Passive CAN bus monitor using any J2534 PassThru adapter.

    Opens the adapter in raw CAN mode with a pass-all filter.
    Never transmits — purely listens.
    """

    def __init__(self, dll_path: str, baudrate: int = 500_000):
        self.dll_path = dll_path
        self.baudrate = baudrate
        self._j2534: Optional[J2534] = None
        self._devID: Optional[c_ulong] = None
        self._chanID: Optional[c_ulong] = None
        self._opened = False
        self._start_time: float = 0.0

    def open(self):
        """Open the J2534 device and start the raw CAN channel."""
        # Create a J2534 instance — rxid/txid don't matter for CAN mode
        # but the constructor requires them, so pass dummies
        self._j2534 = J2534(windll=self.dll_path, rxid=0x000, txid=0x000)

        # Open device
        result, self._devID = self._j2534.PassThruOpen()
        if result != Error_ID.ERR_SUCCESS:
            raise IOError(f"PassThruOpen failed: {result}")
        log.info("J2534 device opened (ID=%d)", self._devID.value)

        # Connect with CAN protocol (raw frames, not ISO-TP)
        result, self._chanID = self._j2534.PassThruConnect(
            self._devID, Protocol_ID.CAN.value, self.baudrate
        )
        if result != Error_ID.ERR_SUCCESS:
            raise IOError(f"PassThruConnect CAN failed: {result}")
        log.info("CAN channel opened (ID=%d) at %d bps", self._chanID.value, self.baudrate)

        # Set up pass-all filter — mask = 0x00000000 passes everything
        self._setup_pass_filter()

        # Clear any stale data in the receive buffer
        self._j2534.PassThruIoctl(self._chanID, Ioctl_ID.CLEAR_RX_BUFFER)

        self._opened = True
        self._start_time = time.time()
        log.info("CAN sniffer started — listening on all IDs")

    def _setup_pass_filter(self):
        """Configure a PASS_FILTER with mask=0 to receive all CAN IDs."""
        from .j2534 import dllPassThruStartMsgFilter

        mask_msg = PASSTHRU_MSG()
        mask_msg.ProtocolID = Protocol_ID.CAN.value
        mask_msg.DataSize = 4
        # All mask bytes = 0x00 → match everything
        for i in range(4):
            mask_msg.Data[i] = 0x00

        pattern_msg = PASSTHRU_MSG()
        pattern_msg.ProtocolID = Protocol_ID.CAN.value
        pattern_msg.DataSize = 4
        # Pattern = 0x00000000 (matched against mask=0 → always passes)
        for i in range(4):
            pattern_msg.Data[i] = 0x00

        msg_id = c_ulong(0)

        result = dllPassThruStartMsgFilter(
            self._chanID,
            c_ulong(Filter.PASS_FILTER.value),
            byref(mask_msg),
            byref(pattern_msg),
            None,               # No flow control msg for PASS_FILTER
            byref(msg_id),
        )

        if Error_ID(result) != Error_ID.ERR_SUCCESS:
            raise IOError(f"PassThruStartMsgFilter PASS_FILTER failed: {Error_ID(result)}")
        log.info("Pass-all CAN filter installed (filter ID=%d)", msg_id.value)

    def read_frame(self, timeout_ms: int = 100) -> Optional[CANFrame]:
        """
        Read a single CAN frame from the bus.
        Returns None on timeout (no frame available).
        """
        if not self._opened:
            raise RuntimeError("Sniffer is not open")

        from .j2534 import dllPassThruReadMsgs

        msg = PASSTHRU_MSG()
        msg.ProtocolID = Protocol_ID.CAN.value
        num_msgs = c_ulong(1)

        result = dllPassThruReadMsgs(
            self._chanID, byref(msg), byref(num_msgs), c_ulong(timeout_ms)
        )

        err = Error_ID(result)
        if err == Error_ID.ERR_BUFFER_EMPTY or num_msgs.value == 0:
            return None
        if err == Error_ID.ERR_TIMEOUT:
            return None
        if err != Error_ID.ERR_SUCCESS:
            log.warning("PassThruReadMsgs error: %s", err)
            return None

        # Parse the raw PASSTHRU_MSG
        # Data[0:4] = CAN ID (big-endian)
        # Data[4:DataSize] = CAN payload
        if msg.DataSize < 4:
            return None

        can_id_bytes = bytes(msg.Data[0:4])
        can_id = int.from_bytes(can_id_bytes, "big")

        # Check for 29-bit extended frame flag (bit 31 in RxStatus on some adapters,
        # or CAN ID > 0x7FF)
        is_extended = can_id > 0x7FF
        if is_extended:
            can_id &= 0x1FFFFFFF   # mask to 29 bits

        data = bytes(msg.Data[4:msg.DataSize])
        is_tx_echo = bool(msg.RxStatus & 0x01)  # bit 0 = TX_MSG_TYPE

        # Classify direction based on known CAN ID ranges
        if can_id in KNOWN_TESTER_IDS:
            direction = "TX"
        else:
            direction = "RX"

        # Try to decode UDS label
        uds = _uds_label(data) if data else ""

        return CANFrame(
            timestamp_us = msg.Timestamp,
            can_id       = can_id,
            is_extended  = is_extended,
            data         = data,
            is_tx_echo   = is_tx_echo,
            direction    = direction,
            uds_label    = uds,
        )

    def read_frames(self, timeout_ms: int = 100) -> Iterator[CANFrame]:
        """
        Generator that yields CAN frames continuously.
        Yields None-free — only actual frames come through.
        Check sniffer.is_open to stop.
        """
        while self._opened:
            frame = self.read_frame(timeout_ms)
            if frame is not None:
                yield frame

    @property
    def is_open(self) -> bool:
        return self._opened

    def close(self):
        """Close the CAN channel and J2534 device."""
        self._opened = False
        if self._chanID is not None:
            try:
                self._j2534.PassThruDisconnect(self._chanID)
            except Exception:
                pass
        if self._devID is not None:
            try:
                self._j2534.PassThruClose(self._devID)
            except Exception:
                pass
        log.info("CAN sniffer closed")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
