"""
lib/connections/can_sniffer.py — Passive CAN bus sniffer via J2534

Opens a raw CAN channel (Protocol_ID.CAN = 5) with a pass-all filter
and reads every frame on the bus without transmitting anything.

Designed for use with an OBD splitter: VCDS / ODIS on one port does
the talking, while our Mongoose on the other port passively captures
the full exchange.

Usage:
    from lib.connections.can_sniffer import J2534CANSniffer, ISOTPReassembler

    sniffer = J2534CANSniffer("C:/path/to/j2534.dll")
    sniffer.open()
    reassembler = ISOTPReassembler()
    for frame in sniffer.read_frames():
        msg = reassembler.feed(frame)
        if msg:
            print(msg.decode_uds())
    sniffer.close()
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set, Tuple

log = logging.getLogger("SimosSuite.CANSniffer")

# ── Known VAG diagnostic CAN IDs ─────────────────────────────────────────────
# TX = tester → ECU (requests), RX = ECU → tester (responses)

VAG_DIAG_IDS: Dict[int, str] = {
    # ── J533 gateway-routed addresses (C7 A6/A7/A8) ──
    # These are the CAN IDs seen on the OBD diagnostic port.
    # Confirmed from AU57X MWB dump and CP_MODULES.
    0x710: "J533 Gateway",
    0x77A: "J533 Gateway",
    0x746: "J255 HVAC",
    0x7B0: "J255 HVAC",
    0x714: "J285 Instruments",  # C7: J285 shares 0x714/0x77E with J623
    0x77E: "J285 Instruments",
    0x715: "J234 Airbag",
    0x77F: "J234 Airbag",
    0x773: "J794 MMI",
    0x7DD: "J794 MMI",
    0x74C: "J136 Seat DrvL",    # KWP2000 module
    0x7B6: "J136 Seat DrvL",
    0x74D: "J521 Seat Pass",    # KWP2000 module
    0x7B7: "J521 Seat Pass",
    0x732: "J518 KESSY",        # KWP2000 module
    0x79C: "J518 KESSY",
    0x70E: "J519 CentElect",    # KWP2000 module
    0x778: "J519 CentElect",
    0x70D: "J393 Comfort",      # KWP2000 module
    0x777: "J393 Comfort",
    0x740: "J217 TCM",
    0x7A8: "J217 TCM",
    # ── Generic VAG / OBD2 ──
    0x7E0: "OBD2 Func",
    0x7E8: "OBD2 Func",
    # ── Additional known IDs (non-C7 platforms or direct-bus) ──
    0x712: "J104 ABS/ESP",
    0x77C: "J104 ABS/ESP",
    0x716: "J500 Steering",
    0x780: "J500 Steering",
    0x713: "J527 SteerAngle",
    0x77D: "J527 SteerAngle",
    0x742: "J393 Comfort (alt)",
    0x7AC: "J393 Comfort (alt)",
    0x764: "J532 Headlamp",
    0x7CE: "J532 Headlamp",
}

# TX CAN IDs (tester → ECU, i.e. diagnostic requests)
# Includes both C7 gateway-routed and generic VAG TX addresses
_TX_IDS: Set[int] = {
    0x710, 0x7E0, 0x714, 0x715, 0x716, 0x732, 0x740, 0x742,
    0x746, 0x74C, 0x74D, 0x70D, 0x70E, 0x712, 0x713, 0x764,
    0x773,
}


# ── UDS service decoder ──────────────────────────────────────────────────────

UDS_SERVICES = {
    0x10: "DiagSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDTCs",
    0x19: "ReadDTCInfo",
    0x22: "ReadDID",
    0x23: "ReadMemByAddr",
    0x27: "SecurityAccess",
    0x28: "CommControl",
    0x2E: "WriteDID",
    0x2F: "IOControl",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "TransferExit",
    0x3D: "WriteMemByAddr",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
    0x7F: "NegativeResponse",
}

UDS_NRCS = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMsgLenOrFormat",
    0x14: "responseTooLong",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "responsePending",
    0x7E: "subFuncNotSupportedInSession",
    0x7F: "serviceNotSupportedInSession",
}


def _decode_uds_payload(payload: bytes) -> str:
    """Decode a UDS payload into a human-readable string."""
    if not payload:
        return ""

    sid = payload[0]

    # Negative response
    if sid == 0x7F and len(payload) >= 3:
        rejected_sid = payload[1]
        nrc = payload[2]
        svc = UDS_SERVICES.get(rejected_sid, f"0x{rejected_sid:02X}")
        nrc_name = UDS_NRCS.get(nrc, f"0x{nrc:02X}")
        return f"NegativeResponse {svc} → {nrc_name}"

    is_resp = bool(sid & 0x40)
    base_sid = sid & ~0x40 if is_resp else sid
    svc_name = UDS_SERVICES.get(base_sid, f"SID_0x{base_sid:02X}")
    prefix = "+" if is_resp else "→"

    if base_sid == 0x10:
        session = payload[1] if len(payload) > 1 else 0
        sess_map = {1: "default", 2: "programming", 3: "extended"}
        return f"{prefix} {svc_name} {sess_map.get(session, f'0x{session:02X}')}"

    if base_sid == 0x22:
        if len(payload) >= 3:
            did = (payload[1] << 8) | payload[2]
            extra = ""
            if is_resp and len(payload) > 3:
                data = payload[3:]
                if did in (0xF190, 0xF18C, 0xF187, 0xF189, 0xF191, 0xF197):
                    try:
                        s = data.decode("ascii").strip("\x00 ")
                        if s.isprintable():
                            extra = f' "{s}"'
                    except Exception:
                        pass
                if not extra:
                    h = data[:16].hex().upper()
                    extra = f" [{h}{'...' if len(data)>16 else ''}]"
            return f"{prefix} {svc_name} 0x{did:04X}{extra}"

    if base_sid == 0x2E:
        if len(payload) >= 3:
            did = (payload[1] << 8) | payload[2]
            return f"{prefix} {svc_name} 0x{did:04X} ({len(payload)-3}B)"

    if base_sid == 0x27:
        if len(payload) >= 2:
            sub = payload[1]
            kind = "seed" if (sub % 2 == 1) else "key"
            level = (sub + 1) // 2
            return f"{prefix} {svc_name} L{level} {kind} ({len(payload)-2}B)"

    if base_sid == 0x31:
        if len(payload) >= 4:
            sub = payload[1]
            rid = (payload[2] << 8) | payload[3]
            sub_map = {1: "start", 2: "stop", 3: "result"}
            return f"{prefix} {svc_name} {sub_map.get(sub, f'sub{sub}')} 0x{rid:04X}"

    if base_sid == 0x36:
        blk = payload[1] if len(payload) > 1 else 0
        return f"{prefix} {svc_name} block {blk} ({len(payload)-2}B)"

    if base_sid == 0x3E:
        return f"{prefix} TesterPresent"

    if base_sid == 0x19:
        sub = payload[1] if len(payload) > 1 else 0
        return f"{prefix} ReadDTCInfo sub=0x{sub:02X}"

    return f"{prefix} {svc_name}"


# ── CAN frame ────────────────────────────────────────────────────────────────

@dataclass
class CANFrame:
    timestamp_us: int
    can_id:       int
    data:         bytes
    is_extended:  bool = False

    @property
    def id_hex(self) -> str:
        return f"{self.can_id:08X}" if self.is_extended else f"{self.can_id:03X}"

    @property
    def data_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    @property
    def dlc(self) -> int:
        return len(self.data)

    @property
    def direction(self) -> str:
        """TX = tester → ECU (request), RX = ECU → tester (response)."""
        return "TX" if self.can_id in _TX_IDS else "RX"

    @property
    def label(self) -> str:
        """Human-readable module name for this CAN ID."""
        return VAG_DIAG_IDS.get(self.can_id, "")


# ── ISO-TP reassembled message ────────────────────────────────────────────────

@dataclass
class ISOTPMessage:
    can_id:       int
    payload:      bytes
    timestamp_us: int
    frame_count:  int = 1

    @property
    def service_id(self) -> int:
        return self.payload[0] if self.payload else 0

    @property
    def is_response(self) -> bool:
        return bool(self.service_id & 0x40)

    @property
    def direction(self) -> str:
        return "TX" if self.can_id in _TX_IDS else "RX"

    @property
    def label(self) -> str:
        return VAG_DIAG_IDS.get(self.can_id, "")

    def decode_uds(self) -> str:
        return _decode_uds_payload(self.payload)


# ── ISO-TP reassembly ─────────────────────────────────────────────────────────

class ISOTPReassembler:
    """
    Lightweight ISO-TP (ISO 15765-2) reassembly for passive sniffing.

    Handles SF, FF, CF. FC frames are ignored (we're passive).
    Stale partial transfers are flushed after timeout_ms.
    """

    def __init__(self, timeout_ms: int = 3000):
        self._sessions: Dict[int, dict] = {}
        self._timeout_us = timeout_ms * 1000

    def feed(self, frame: CANFrame) -> Optional[ISOTPMessage]:
        if len(frame.data) < 1:
            return None

        pci_type = (frame.data[0] >> 4) & 0x0F

        if pci_type == 0:
            # Single Frame
            sf_len = frame.data[0] & 0x0F
            if sf_len == 0 or sf_len > 7:
                return None
            payload = frame.data[1:1 + sf_len]
            self._sessions.pop(frame.can_id, None)
            return ISOTPMessage(
                can_id=frame.can_id,
                payload=bytes(payload),
                timestamp_us=frame.timestamp_us,
                frame_count=1,
            )

        elif pci_type == 1:
            # First Frame
            if len(frame.data) < 2:
                return None
            ff_len = ((frame.data[0] & 0x0F) << 8) | frame.data[1]
            self._sessions[frame.can_id] = {
                "expected_len": ff_len,
                "buffer": bytearray(frame.data[2:8]),
                "seq": 1,
                "ts": frame.timestamp_us,
                "last_ts": frame.timestamp_us,
                "frames": 1,
            }
            return None

        elif pci_type == 2:
            # Consecutive Frame
            session = self._sessions.get(frame.can_id)
            if session is None:
                return None
            session["buffer"].extend(frame.data[1:8])
            session["seq"] += 1
            session["frames"] += 1
            session["last_ts"] = frame.timestamp_us

            if len(session["buffer"]) >= session["expected_len"]:
                payload = bytes(session["buffer"][:session["expected_len"]])
                frames = session["frames"]
                ts = session["ts"]
                del self._sessions[frame.can_id]
                return ISOTPMessage(
                    can_id=frame.can_id,
                    payload=payload,
                    timestamp_us=ts,
                    frame_count=frames,
                )
            return None

        elif pci_type == 3:
            # Flow Control — passive, ignore
            return None

        return None

    def flush_stale(self, now_us: int) -> List[ISOTPMessage]:
        """Return partial messages that have timed out."""
        stale = []
        to_delete = []
        for can_id, session in self._sessions.items():
            if now_us - session["last_ts"] > self._timeout_us:
                # Yield what we have as a partial message
                payload = bytes(session["buffer"][:session["expected_len"]])
                stale.append(ISOTPMessage(
                    can_id=can_id,
                    payload=payload,
                    timestamp_us=session["ts"],
                    frame_count=session["frames"],
                ))
                to_delete.append(can_id)
        for cid in to_delete:
            del self._sessions[cid]
        return stale

    def reset(self):
        self._sessions.clear()


# ── J2534 raw CAN sniffer ────────────────────────────────────────────────────

class J2534CANSniffer:
    """
    Passive CAN bus listener via J2534 raw CAN channel.

    Opens Protocol_ID.CAN (5) with a PASS_FILTER matching all CAN IDs.
    Never transmits — purely receive-only. Safe to use on a splitter
    alongside an active diagnostic tool (VCDS, ODIS).

    The sniffer opens its own device handle — it does NOT share the
    J2534 connection used by the rest of the app. Make sure no other
    tab has an active J2534 session before starting the sniffer.
    """

    def __init__(self, dll_path: str):
        self.dll_path = dll_path
        self._dev_id = None
        self._ch_id = None
        self._j2534 = None
        self._running = False

    def open(self):
        """Open J2534 device and start raw CAN channel with pass-all filter."""
        from lib.connections.j2534 import (
            J2534, PASSTHRU_MSG, Protocol_ID, Filter,
            Ioctl_ID, Error_ID,
        )

        # J2534 class needs rxid/txid — use dummies, we only RX
        self._j2534 = J2534(windll=self.dll_path, rxid=0x7FF, txid=0x000)

        result, self._dev_id = self._j2534.PassThruOpen()
        if result != Error_ID.ERR_SUCCESS:
            raise IOError(f"PassThruOpen failed: {result}")

        result, self._ch_id = self._j2534.PassThruConnect(
            self._dev_id, Protocol_ID.CAN.value, 500000)
        if result != Error_ID.ERR_SUCCESS:
            raise IOError(f"PassThruConnect CAN failed: {result}")

        # Pass-all filter: mask = 0x00000000
        mask_msg = PASSTHRU_MSG()
        mask_msg.ProtocolID = Protocol_ID.CAN.value
        mask_msg.DataSize = 4
        for i in range(4):
            mask_msg.Data[i] = 0x00

        pattern_msg = PASSTHRU_MSG()
        pattern_msg.ProtocolID = Protocol_ID.CAN.value
        pattern_msg.DataSize = 4
        for i in range(4):
            pattern_msg.Data[i] = 0x00

        filter_id = ctypes.c_ulong(0)

        from lib.connections.j2534 import dllPassThruStartMsgFilter
        result = dllPassThruStartMsgFilter(
            self._ch_id,
            ctypes.c_ulong(Filter.PASS_FILTER.value),
            ctypes.byref(mask_msg),
            ctypes.byref(pattern_msg),
            None,
            ctypes.byref(filter_id),
        )
        if Error_ID(result) != Error_ID.ERR_SUCCESS:
            raise IOError(f"PassThruStartMsgFilter failed: {Error_ID(result)}")

        self._j2534.PassThruIoctl(self._ch_id, Ioctl_ID.CLEAR_RX_BUFFER)
        self._running = True
        log.info("CAN sniffer opened (raw CAN, pass-all filter)")

    def read_frame(self, timeout_ms: int = 100) -> Optional[CANFrame]:
        """
        Read one raw CAN frame. Returns None on timeout/empty.

        J2534 raw CAN frame layout:
          Data[0:4] = CAN ID (big-endian)
          Data[4:DataSize] = payload (0–8 bytes)
          Timestamp = microseconds from adapter power-on
        """
        if not self._running or self._ch_id is None:
            return None

        from lib.connections.j2534 import (
            PASSTHRU_MSG, Protocol_ID, Error_ID,
            dllPassThruReadMsgs,
        )

        msg = PASSTHRU_MSG()
        msg.ProtocolID = Protocol_ID.CAN.value
        num_msgs = ctypes.c_ulong(1)

        result = dllPassThruReadMsgs(
            self._ch_id,
            ctypes.byref(msg),
            ctypes.byref(num_msgs),
            ctypes.c_ulong(timeout_ms),
        )

        if Error_ID(result) == Error_ID.ERR_BUFFER_EMPTY or num_msgs.value == 0:
            return None

        if msg.DataSize < 4:
            return None

        can_id = int.from_bytes(bytes(msg.Data[0:4]), "big")
        is_ext = bool(can_id & 0x80000000)
        can_id = (can_id & 0x1FFFFFFF) if is_ext else (can_id & 0x7FF)

        data = bytes(msg.Data[4:msg.DataSize])

        return CANFrame(
            timestamp_us=msg.Timestamp,
            can_id=can_id,
            data=data,
            is_extended=is_ext,
        )

    def read_frames(self, timeout_ms: int = 100) -> Iterator[CANFrame]:
        """Generator that yields CAN frames until stop() is called."""
        while self._running:
            frame = self.read_frame(timeout_ms)
            if frame is not None:
                yield frame

    def stop(self):
        self._running = False

    def close(self):
        self._running = False
        if self._j2534 and self._ch_id is not None:
            try:
                self._j2534.PassThruDisconnect(self._ch_id)
            except Exception:
                pass
        if self._j2534 and self._dev_id is not None:
            try:
                self._j2534.PassThruClose(self._dev_id)
            except Exception:
                pass
        self._ch_id = None
        self._dev_id = None
        log.info("CAN sniffer closed")

    @property
    def is_running(self) -> bool:
        return self._running
