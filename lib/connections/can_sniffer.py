"""
lib/connections/can_sniffer.py — Passive CAN bus listener via J2534

Opens a raw CAN channel (Protocol_ID.CAN, not ISO15765) with a pass-all
filter so every frame on the bus is captured without transmitting anything.

Designed for use with an OBD splitter: VCDS/ODIS talks to the car on one
cable, this sniffer captures the full exchange on the other.

Includes software ISO-TP reassembly and UDS service decode so the raw CAN
frames are displayed as meaningful diagnostic messages.

Usage:
    sniffer = J2534CANSniffer(dll_path)
    sniffer.open()
    for frame in sniffer.read_frames():
        print(frame)
    sniffer.close()
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("SimosSuite.CANSniffer")

# ── UDS service names for human-readable decode ──────────────────────────────

UDS_SERVICES = {
    0x10: "DiagSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDTC",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}

UDS_SESSIONS = {
    0x01: "default",
    0x02: "programming",
    0x03: "extended",
    0x60: "EOL",
}

UDS_NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLength",
    0x14: "responseTooLong",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnet",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived_responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# Known VAG CAN IDs for labelling
VAG_CAN_LABELS = {
    0x710: "J533 Gateway",
    0x77A: "J533 Gateway",
    0x746: "J255 Climatronic",
    0x7B0: "J255 Climatronic",
    0x7E0: "Engine (func)",
    0x7E8: "Engine (func)",
    0x714: "J217 TCU",
    0x77E: "J217 TCU",
    0x740: "J104 ESP/ABS",
    0x7A4: "J104 ESP/ABS",
    0x750: "J285 Cluster",
    0x7B4: "J285 Cluster",
    0x75A: "J234 Airbag",
    0x7C4: "J234 Airbag",
    0x773: "J794 ACC",
    0x77D: "J794 ACC",
    0x760: "J386 Door FL",
    0x7C0: "J386 Door FL",
    0x761: "J387 Door FR",
    0x7C1: "J387 Door FR",
    0x76E: "J393 Central Elect",
    0x7D8: "J393 Central Elect",
    0x7A6: "J527 Steering",
    0x770: "J527 Steering",
}


@dataclass
class CANFrame:
    """Single raw CAN frame from the bus."""
    timestamp_us: int       # microseconds from J2534
    can_id:       int       # 11-bit or 29-bit CAN ID
    data:         bytes     # 0–8 bytes payload
    is_extended:  bool = False  # 29-bit ID flag

    @property
    def direction(self) -> str:
        """Guess TX/RX based on CAN ID parity (tester IDs are lower)."""
        # VAG convention: tester TX is in 0x700–0x77F, ECU RX is 0x780–0x7FF
        # (or offset +0x6A for some modules)
        if 0x700 <= self.can_id <= 0x77F:
            return "TX"
        elif 0x780 <= self.can_id <= 0x7FF:
            return "RX"
        return "??"

    @property
    def label(self) -> str:
        return VAG_CAN_LABELS.get(self.can_id, "")

    @property
    def hex_data(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    def format_line(self, t0_us: int = 0) -> str:
        """Format as a single log line."""
        rel_ms = (self.timestamp_us - t0_us) / 1000.0
        lbl = f"  {self.label}" if self.label else ""
        return (f"{rel_ms:10.1f}ms  {self.direction}  "
                f"[{self.can_id:03X}]  {self.hex_data}{lbl}")


@dataclass
class ISOTPMessage:
    """Reassembled ISO-TP message from consecutive CAN frames."""
    can_id:      int
    payload:     bytes
    timestamp_us: int
    frame_count: int = 1

    @property
    def direction(self) -> str:
        if 0x700 <= self.can_id <= 0x77F:
            return "TX"
        elif 0x780 <= self.can_id <= 0x7FF:
            return "RX"
        return "??"

    @property
    def label(self) -> str:
        return VAG_CAN_LABELS.get(self.can_id, "")

    def decode_uds(self) -> str:
        """Attempt to decode as a UDS service message."""
        if not self.payload:
            return ""
        sid = self.payload[0]

        # Negative response
        if sid == 0x7F and len(self.payload) >= 3:
            req_sid = self.payload[1]
            nrc = self.payload[2]
            svc = UDS_SERVICES.get(req_sid, f"0x{req_sid:02X}")
            nrc_name = UDS_NRC.get(nrc, f"0x{nrc:02X}")
            return f"NegativeResponse  {svc} → {nrc_name}"

        # Positive response (SID + 0x40)
        if sid >= 0x50:
            req_sid = sid - 0x40
            svc = UDS_SERVICES.get(req_sid, "")
            if svc:
                return self._decode_positive(req_sid, svc)

        # Request
        svc = UDS_SERVICES.get(sid, "")
        if svc:
            return self._decode_request(sid, svc)

        return f"SID 0x{sid:02X}"

    def _decode_request(self, sid: int, svc: str) -> str:
        p = self.payload
        if sid == 0x10 and len(p) >= 2:  # DiagSessionControl
            sess = UDS_SESSIONS.get(p[1], f"0x{p[1]:02X}")
            return f"{svc} {sess}"
        if sid == 0x22 and len(p) >= 3:  # ReadDataByIdentifier
            did = (p[1] << 8) | p[2]
            return f"{svc} DID 0x{did:04X}"
        if sid == 0x2E and len(p) >= 3:  # WriteDataByIdentifier
            did = (p[1] << 8) | p[2]
            data_len = len(p) - 3
            return f"{svc} DID 0x{did:04X}  [{data_len} bytes]"
        if sid == 0x27 and len(p) >= 2:  # SecurityAccess
            sub = p[1]
            kind = "requestSeed" if sub % 2 == 1 else "sendKey"
            return f"{svc} level=0x{sub:02X} ({kind})"
        if sid == 0x31 and len(p) >= 4:  # RoutineControl
            sub = {1: "start", 2: "stop", 3: "requestResult"}.get(p[1], f"0x{p[1]:02X}")
            rid = (p[2] << 8) | p[3]
            return f"{svc} {sub} routine=0x{rid:04X}"
        if sid == 0x36 and len(p) >= 2:  # TransferData
            blk = p[1]
            return f"{svc} block={blk}  [{len(p)-2} bytes]"
        return svc

    def _decode_positive(self, req_sid: int, svc: str) -> str:
        p = self.payload
        if req_sid == 0x10 and len(p) >= 2:
            sess = UDS_SESSIONS.get(p[1], f"0x{p[1]:02X}")
            return f"+{svc} {sess}"
        if req_sid == 0x22 and len(p) >= 3:
            did = (p[1] << 8) | p[2]
            data_len = len(p) - 3
            snippet = " ".join(f"{b:02X}" for b in p[3:3+min(8, data_len)])
            if data_len > 8:
                snippet += "..."
            return f"+{svc} DID 0x{did:04X}  [{data_len}B] {snippet}"
        if req_sid == 0x27 and len(p) >= 2:
            sub = p[1]
            kind = "seed" if sub % 2 == 1 else "keyAccepted"
            return f"+{svc} ({kind})"
        if req_sid == 0x31 and len(p) >= 4:
            sub = {1: "started", 2: "stopped", 3: "result"}.get(p[1], f"0x{p[1]:02X}")
            rid = (p[2] << 8) | p[3]
            return f"+{svc} {sub} routine=0x{rid:04X}"
        return f"+{svc}"

    def format_line(self, t0_us: int = 0) -> str:
        rel_ms = (self.timestamp_us - t0_us) / 1000.0
        lbl = f"  {self.label}" if self.label else ""
        uds = self.decode_uds()
        hex_short = " ".join(f"{b:02X}" for b in self.payload[:16])
        if len(self.payload) > 16:
            hex_short += f"... ({len(self.payload)}B)"
        return (f"{rel_ms:10.1f}ms  {self.direction}  "
                f"[{self.can_id:03X}]{lbl}  {uds}\n"
                f"{'':>14}{hex_short}")


class ISOTPReassembler:
    """
    Software ISO-TP reassembly from raw CAN frames.

    Tracks multi-frame transfers per CAN ID and emits complete
    ISOTPMessage objects when a transfer finishes.
    """

    def __init__(self, timeout_ms: float = 2000):
        self._timeout_ms = timeout_ms
        # Active transfers: can_id → {payload, expected_len, seq, ts}
        self._active: Dict[int, dict] = {}

    def feed(self, frame: CANFrame) -> Optional[ISOTPMessage]:
        """
        Feed a raw CAN frame. Returns an ISOTPMessage if a complete
        message was reassembled, otherwise None.
        """
        if len(frame.data) < 1:
            return None

        pci_type = (frame.data[0] >> 4) & 0x0F

        # Single Frame (SF): PCI type 0
        if pci_type == 0:
            sf_dl = frame.data[0] & 0x0F
            if sf_dl == 0 or sf_dl > 7:
                return None
            payload = frame.data[1:1+sf_dl]
            return ISOTPMessage(
                can_id=frame.can_id,
                payload=bytes(payload),
                timestamp_us=frame.timestamp_us,
                frame_count=1,
            )

        # First Frame (FF): PCI type 1
        if pci_type == 1 and len(frame.data) >= 2:
            ff_dl = ((frame.data[0] & 0x0F) << 8) | frame.data[1]
            payload = bytearray(frame.data[2:8])  # first 6 bytes
            self._active[frame.can_id] = {
                "payload": payload,
                "expected": ff_dl,
                "seq": 1,
                "frames": 1,
                "ts": frame.timestamp_us,
            }
            return None

        # Consecutive Frame (CF): PCI type 2
        if pci_type == 2:
            xfer = self._active.get(frame.can_id)
            if xfer is None:
                return None  # orphan CF, ignore
            seq = frame.data[0] & 0x0F
            if seq != (xfer["seq"] & 0x0F):
                # Sequence mismatch — abort this transfer
                del self._active[frame.can_id]
                return None
            xfer["payload"].extend(frame.data[1:8])
            xfer["seq"] += 1
            xfer["frames"] += 1

            if len(xfer["payload"]) >= xfer["expected"]:
                # Transfer complete
                msg = ISOTPMessage(
                    can_id=frame.can_id,
                    payload=bytes(xfer["payload"][:xfer["expected"]]),
                    timestamp_us=xfer["ts"],
                    frame_count=xfer["frames"],
                )
                del self._active[frame.can_id]
                return msg
            return None

        # Flow Control (FC): PCI type 3 — just note it, don't reassemble
        if pci_type == 3:
            return None  # FC frames are control, not data

        return None

    def flush_stale(self, now_us: int) -> List[ISOTPMessage]:
        """Return partially reassembled messages that have timed out."""
        stale = []
        timeout_us = self._timeout_ms * 1000
        for can_id in list(self._active):
            xfer = self._active[can_id]
            if (now_us - xfer["ts"]) > timeout_us:
                stale.append(ISOTPMessage(
                    can_id=can_id,
                    payload=bytes(xfer["payload"]),
                    timestamp_us=xfer["ts"],
                    frame_count=xfer["frames"],
                ))
                del self._active[can_id]
        return stale


class J2534CANSniffer:
    """
    Passive CAN bus listener using J2534 PassThru in raw CAN mode.

    Opens Protocol_ID.CAN (not ISO15765) with a PASS_FILTER that accepts
    all CAN IDs. Never transmits — purely listens.

    With an OBD splitter, sees all traffic between VCDS/ODIS and the car.
    """

    # J2534 protocol/filter constants
    PROTOCOL_CAN = 5
    FILTER_PASS   = 0x00000001

    def __init__(self, dll_path: str, baudrate: int = 500_000):
        self.dll_path = dll_path
        self.baudrate = baudrate
        self._devID = None
        self._chanID = None
        self._hDLL = None
        self._opened = False

    def open(self):
        """Open J2534 device and start raw CAN channel with pass-all filter."""
        import ctypes
        from ctypes import c_ulong, c_long, byref, POINTER, WINFUNCTYPE, c_void_p
        import os, pathlib

        # Load DLL
        dll_dir = str(pathlib.Path(self.dll_path).parent)
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(dll_dir)
        self._hDLL = ctypes.cdll.LoadLibrary(self.dll_path)

        # Bind functions we need
        self._fn_open = WINFUNCTYPE(c_long, c_void_p, POINTER(c_ulong))(
            ("PassThruOpen", self._hDLL),
            ((1, "pName", 0), (1, "pDeviceID", 0)))

        self._fn_connect = WINFUNCTYPE(
            c_long, c_ulong, c_ulong, c_ulong, c_ulong, POINTER(c_ulong))(
            ("PassThruConnect", self._hDLL),
            ((1,"DeviceID",0),(1,"ProtocolID",0),(1,"Flags",0),
             (1,"BaudRate",500000),(1,"pChannelID",0)))

        self._fn_disconnect = WINFUNCTYPE(c_long, c_ulong)(
            ("PassThruDisconnect", self._hDLL), ((1,"ChannelID",0),))

        self._fn_close = WINFUNCTYPE(c_long, c_ulong)(
            ("PassThruClose", self._hDLL), ((1,"DeviceID",0),))

        from .j2534 import PASSTHRU_MSG
        self._fn_read = WINFUNCTYPE(
            c_long, c_ulong, POINTER(PASSTHRU_MSG), POINTER(c_ulong), c_ulong)(
            ("PassThruReadMsgs", self._hDLL),
            ((1,"ChannelID",0),(1,"pMsg",0),(1,"pNumMsgs",0),(1,"Timeout",0)))

        self._fn_filter = WINFUNCTYPE(
            c_long, c_ulong, c_ulong, POINTER(PASSTHRU_MSG),
            POINTER(PASSTHRU_MSG), POINTER(PASSTHRU_MSG), POINTER(c_ulong))(
            ("PassThruStartMsgFilter", self._hDLL),
            ((1,"ChannelID",0),(1,"FilterType",0),(1,"pMaskMsg",0),
             (1,"pPatternMsg",0),(1,"pFlowControlMsg",0),(1,"pMsgID",0)))

        self._fn_ioctl = WINFUNCTYPE(c_long, c_ulong, c_ulong, c_void_p, c_void_p)(
            ("PassThruIoctl", self._hDLL),
            ((1,"Handle",0),(1,"IoctlID",0),(1,"pInput",0),(1,"pOutput",0)))

        # Open device
        devID = c_ulong()
        result = self._fn_open(byref(ctypes.c_int()), byref(devID))
        if result != 0:
            raise RuntimeError(f"PassThruOpen failed: error {result}")
        self._devID = devID

        # Connect with raw CAN protocol
        chanID = c_ulong()
        result = self._fn_connect(
            devID, self.PROTOCOL_CAN, 0, self.baudrate, byref(chanID))
        if result != 0:
            raise RuntimeError(f"PassThruConnect(CAN) failed: error {result}")
        self._chanID = chanID

        # Set pass-all filter: mask = 0x000, pattern = 0x000
        mask = PASSTHRU_MSG()
        mask.ProtocolID = self.PROTOCOL_CAN
        mask.DataSize = 4
        for i in range(4):
            mask.Data[i] = 0x00  # mask all zeros = don't care

        pattern = PASSTHRU_MSG()
        pattern.ProtocolID = self.PROTOCOL_CAN
        pattern.DataSize = 4
        for i in range(4):
            pattern.Data[i] = 0x00

        filterID = c_ulong()
        result = self._fn_filter(
            chanID, self.FILTER_PASS,
            byref(mask), byref(pattern),
            None,  # no flow control for PASS_FILTER
            byref(filterID))
        if result != 0:
            raise RuntimeError(f"PassThruStartMsgFilter(PASS) failed: error {result}")

        # Clear RX buffer
        CLEAR_RX_BUFFER = 0x08
        self._fn_ioctl(chanID, CLEAR_RX_BUFFER, None, None)

        self._opened = True
        log.info("CAN sniffer opened: device=%d channel=%d",
                 devID.value, chanID.value)

    def read_frame(self, timeout_ms: int = 100) -> Optional[CANFrame]:
        """
        Read a single raw CAN frame. Returns None on timeout/empty.
        """
        if not self._opened:
            return None

        from .j2534 import PASSTHRU_MSG
        msg = PASSTHRU_MSG()
        msg.ProtocolID = self.PROTOCOL_CAN
        numMsgs = ctypes.c_ulong(1)

        result = self._fn_read(
            self._chanID, ctypes.byref(msg),
            ctypes.byref(numMsgs), ctypes.c_ulong(timeout_ms))

        ERR_BUFFER_EMPTY = 0x10
        ERR_TIMEOUT = 0x09
        if result in (ERR_BUFFER_EMPTY, ERR_TIMEOUT) or numMsgs.value == 0:
            return None

        # Parse CAN frame: first 4 bytes = CAN ID (big-endian), rest = data
        if msg.DataSize < 4:
            return None

        raw_id = bytes(msg.Data[0:4])
        can_id = int.from_bytes(raw_id, "big")
        is_extended = can_id > 0x7FF
        data_len = msg.DataSize - 4
        data = bytes(msg.Data[4:4+data_len])

        return CANFrame(
            timestamp_us=msg.Timestamp,
            can_id=can_id,
            data=data,
            is_extended=is_extended,
        )

    def close(self):
        """Close the CAN channel and device."""
        self._opened = False
        if self._chanID is not None:
            try:
                self._fn_disconnect(self._chanID)
            except Exception:
                pass
            self._chanID = None
        if self._devID is not None:
            try:
                self._fn_close(self._devID)
            except Exception:
                pass
            self._devID = None
        log.info("CAN sniffer closed")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def is_open(self) -> bool:
        return self._opened
