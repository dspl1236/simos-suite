"""
transport/dpdu_bridge.py — udsoncan Connection over a VNCI 6154A / VAS 6154 using the
VW D-PDU API (ISO 22900-2 / MVCI). Windows only.

WHY A RAW-CAN TWO-LINK PATH
---------------------------
On a VNCI 6154A driven through ``PDUAPI_VW.dll`` a single plain raw-CAN ComLogicalLink
sends but never receives. The working arrangement (independently documented by the
community ContiTool project, reimplemented here from that described behaviour — no code
copied) is **two links on the raw-CAN resource of pins 6/14**:

  * a TX link created RawMode (CllCreateFlag ``0x80000000``): STARTCOMM with zero send
    cycles, then one SENDRECV ComPrimitive per frame (1 send, 0 receives) whose payload
    is the 4-byte big-endian CAN id followed by the data bytes; and
  * an RX link created RawMode + monitor (CllCreateFlag ``0x80000001``, the way the
    driver's own CAN trace opens it): CP_MonitoredChannels / CP_DataPacketSize /
    CP_CollectTimeout / CP_Baudrate set, then a long-lived SENDRECV with 0 sends and -1
    receives. Every bus frame arrives as a result item: 4-byte id + data, with an RxFlag
    whose low bits mark our-own-frame and 29-bit-id.

On top of that raw-CAN pipe this module runs a small ISO-TP (ISO 15765-2) client and
presents the result as a udsoncan ``BaseConnection``, exactly like
``transport.cerberus_bridge.CerberusConnection`` — so the suite's UDS stack (DID read/
write, routines, SecurityAccess, the CP probe) drives a module through a VNCI unchanged.

STATUS
------
The ISO-TP segmentation/reassembly and the BaseConnection seam are unit-tested over a
fake raw-CAN (``tests/test_dpdu_bridge.py``) and are hardware-independent. The ctypes
D-PDU layer (``_DPduRawCan``) targets ISO 22900-2; the few driver-specific numeric values
(ComParam ids, CoPType codes, the raw-CAN resource id) are marked ``# VERIFY`` and must be
confirmed against the ``D_PDUAPI.h`` shipped with your VNCI driver before the live path is
trusted. Importing this module never touches the DLL; only ``DPduRawCan.open()`` does.

Licence: MIT (this file), reimplemented from the public ISO 22900-2 API and the *described*
VNCI behaviour; it contains no code from the GPL ContiTool project.
"""
from __future__ import annotations

import ctypes as C
import os
import platform
import struct
import time
from typing import List, Optional, Tuple

from udsoncan.connections import BaseConnection
from udsoncan.exceptions import TimeoutException


# --------------------------------------------------------------------------------------
# ISO 22900-2 D-PDU API constants.  Standard values where known; anything a specific
# driver may override is flagged VERIFY (cross-check D_PDUAPI.h / the known-good community
# dpdu.py you already have).
# --------------------------------------------------------------------------------------
PDU_STATUS_NOERROR = 0x00000000

# ComLogicalLink create flags (bit field).  Bit31 = RawMode, bit0 = monitor/echo.
CLL_FLAG_RAWMODE = 0x80000000
CLL_FLAG_MONITOR = 0x80000001  # RawMode + monitor (RX link)

# ComPrimitive types (PDU_COPT_*).  VERIFY against D_PDUAPI.h.
PDU_COPT_STARTCOMM = 0x8001      # VERIFY
PDU_COPT_STOPCOMM = 0x8002       # VERIFY
PDU_COPT_SENDRECV = 0x8003       # VERIFY

# Raw CAN resource id for pins 6/14 (ISO 11898-2 500k) — VERIFY per driver resource table.
PDU_RSC_RAWCAN_6_14 = 0x00000101  # VERIFY

# ComParam ids used on the monitor (RX) link — VERIFY names->ids against D_PDUAPI.h.
CP_BAUDRATE = 0x00000120          # VERIFY
CP_MONITORED_CHANNELS = 0x00000121  # VERIFY  (bitmask of CAN channels to monitor)
CP_DATA_PACKET_SIZE = 0x00000122    # VERIFY
CP_COLLECT_TIMEOUT = 0x00000123     # VERIFY  (microseconds)

# RxFlag bit meanings in a returned result item.
RXFLAG_TX_ECHO = 0x01   # byte3 bit0 set => this is our own transmitted frame
RXFLAG_29BIT = 0x01     # byte2 bit0 set => 29-bit id


class DPduError(Exception):
    pass


class DPduUnavailable(DPduError):
    """The DLL / VNCI is not present or refused the required RawMode link."""


# --------------------------------------------------------------------------------------
# DLL discovery
# --------------------------------------------------------------------------------------
_DPDU_DLL_HINTS = (
    r"C:\Program Files (x86)\VNCI\VW_PDUAPI",
    r"C:\Program Files\VNCI\VW_PDUAPI",
    r"C:\Program Files (x86)\Softing\D-PDU API",
)


def detect_dpdu_dll() -> Optional[str]:
    """Return the path to a PDUAPI*.dll if one can be located, else None.

    Honours the ``SIMOS_DPDU_DLL`` env var first (full path to the DLL), then a few
    well-known VNCI / Softing install locations. Windows only.
    """
    if platform.system() != "Windows":
        return None
    env = os.environ.get("SIMOS_DPDU_DLL")
    if env and os.path.isfile(env):
        return env
    for base in _DPDU_DLL_HINTS:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                low = f.lower()
                if low.startswith("pduapi") and low.endswith(".dll"):
                    return os.path.join(root, f)
    return None


def dpdu_available() -> bool:
    return detect_dpdu_dll() is not None


# --------------------------------------------------------------------------------------
# Raw-CAN layer.  _DPduRawCan is the only part that touches the DLL; _FakeRawCan (tests)
# and _DPduRawCan share the same tiny interface so ISO-TP + the connection are testable.
# --------------------------------------------------------------------------------------
class RawCan:
    """Minimal raw-CAN interface: open/close + single-frame send + drain receive."""

    def open(self) -> "RawCan":
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def send(self, can_id: int, data: bytes) -> None:
        raise NotImplementedError

    def recv(self, timeout: float) -> List[Tuple[int, bytes]]:
        """Return (id, data) frames seen since the last call, within ``timeout`` s.
        Must NOT return our own transmitted frames (filter the TX echo)."""
        raise NotImplementedError


class _DPduRawCan(RawCan):
    """Two-link RawMode raw-CAN over the VW D-PDU API. HARDWARE-VALIDATION PENDING.

    Kept deliberately small: everything the ISO-TP layer needs is send()/recv(). The
    link lifecycle (module connect, TX link, RX monitor link, STARTCOMM) happens in
    open(); SENDRECV per frame in send(); event polling in recv().
    """

    def __init__(self, dll_path: str, bitrate: int = 500_000):
        self.dll_path = dll_path
        self.bitrate = bitrate
        self._dll: Optional[C.CDLL] = None
        self._h_mod = None
        self._h_tx = None
        self._h_rx = None
        self._opened = False

    # -- lifecycle ---------------------------------------------------------------------
    def open(self) -> "_DPduRawCan":
        if platform.system() != "Windows":
            raise DPduUnavailable("D-PDU transport is Windows-only")
        try:
            self._dll = C.WinDLL(self.dll_path)  # type: ignore[attr-defined]
        except OSError as e:  # pragma: no cover - needs a real DLL
            raise DPduUnavailable(f"cannot load {self.dll_path}: {e}")
        # The concrete PDUConstruct / PDUGetModuleIds / PDUModuleConnect /
        # PDUCreateComLogicalLink(flag) / PDUConnect / PDUStartComPrimitive(STARTCOMM)
        # sequence is intentionally isolated here. The values flagged VERIFY above feed
        # these calls. See the PR notes: confirm on the bench VNCI before trusting.
        raise DPduUnavailable(  # pragma: no cover
            "live D-PDU link not yet wired — fill in _open_links() against your "
            "D_PDUAPI.h (TX RawMode 0x80000000, RX monitor 0x80000001). The raw-CAN "
            "interface, ISO-TP and UDS layers above this are complete and tested.")

    def close(self) -> None:  # pragma: no cover - needs a real DLL
        self._opened = False
        # PDUStopComPrimitive / PDUDestroyComLogicalLink(tx,rx) / PDUModuleDisconnect /
        # PDUDestruct in reverse order.

    def send(self, can_id: int, data: bytes) -> None:  # pragma: no cover - needs DLL
        # One SENDRECV ComPrimitive on the TX link: payload = 4-byte BE id + data,
        # 1 send / 0 receives.
        raise DPduUnavailable("not open")

    def recv(self, timeout: float) -> List[Tuple[int, bytes]]:  # pragma: no cover - DLL
        # Poll PDUGetEventItem on the RX link until timeout; each result item is
        # 4-byte BE id + data; drop items whose RxFlag marks a TX echo.
        return []


# --------------------------------------------------------------------------------------
# Minimal ISO-TP (ISO 15765-2) client over a RawCan.  No external dependency.
# --------------------------------------------------------------------------------------
class _IsoTpClient:
    """Classic 11-bit ISO-TP: single/first/consecutive frames + flow control.

    Enough to carry UDS request/response for the suite's read/write/routine/CP flows.
    Padding to 8 bytes with 0x00; block size / STmin from the ECU's FC are honoured on TX
    (conservatively — we wait STmin between CFs). 29-bit and extended addressing are not
    needed for the C7/MLB diagnostic CAN and are out of scope here.
    """

    def __init__(self, raw: RawCan, tx_id: int, rx_id: int, pad: int = 0x00):
        self.raw = raw
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.pad = pad
        self._rxq: List[Tuple[int, bytes]] = []  # buffer so no CF in a poll batch is lost

    def _pad(self, b: bytes) -> bytes:
        return b + bytes([self.pad]) * (8 - len(b)) if len(b) < 8 else b

    def _next(self, deadline: float) -> Optional[bytes]:
        """Return the next frame addressed to rx_id, or None at the deadline. Frames are
        buffered across polls, so a burst of consecutive frames is never dropped."""
        while True:
            while self._rxq:
                cid, data = self._rxq.pop(0)
                if cid == self.rx_id and data:
                    return data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._rxq.extend(self.raw.recv(min(0.05, remaining)))

    def send(self, payload: bytes, timeout: float = 2.0) -> None:
        if len(payload) <= 7:
            self.raw.send(self.tx_id, self._pad(bytes([len(payload)]) + payload))
            return
        # First frame: PCI 0x1LLL, 6 data bytes.
        total = len(payload)
        ff = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + payload[:6]
        self.raw.send(self.tx_id, self._pad(ff))
        # Wait for a flow control frame (0x30 = CTS). Honour block size / STmin loosely.
        fc = self._wait_fc(timeout)
        bs = fc[1] if len(fc) > 1 else 0
        stmin = _stmin_seconds(fc[2] if len(fc) > 2 else 0)
        idx = 6
        seq = 1
        sent_in_block = 0
        while idx < total:
            cf = bytes([0x20 | (seq & 0x0F)]) + payload[idx:idx + 7]
            self.raw.send(self.tx_id, self._pad(cf))
            idx += 7
            seq = (seq + 1) & 0x0F
            sent_in_block += 1
            if bs and sent_in_block >= bs and idx < total:
                fc = self._wait_fc(timeout)
                bs = fc[1] if len(fc) > 1 else 0
                stmin = _stmin_seconds(fc[2] if len(fc) > 2 else 0)
                sent_in_block = 0
            elif stmin:
                time.sleep(stmin)

    def _wait_fc(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            fr = self._next(deadline)
            if fr is None:
                raise TimeoutException("ISO-TP: no flow control frame")
            if (fr[0] & 0xF0) != 0x30:
                continue  # not an FC (stray frame) — keep waiting
            flow = fr[0] & 0x0F
            if flow == 0x02:  # OVFLW
                raise DPduError("ISO-TP flow control: overflow (0x32)")
            if flow == 0x01:  # WAIT
                continue
            return fr  # CTS (0x30)

    def recv(self, timeout: float = 2.0) -> bytes:
        deadline = time.monotonic() + timeout
        # Wait for a single frame or first frame (skip stray non-SF/FF frames).
        first = None
        while first is None:
            fr = self._next(deadline)
            if fr is None:
                raise TimeoutException("ISO-TP: no response")
            if (fr[0] & 0xF0) in (0x00, 0x10):
                first = fr
        pci = first[0] & 0xF0
        if pci == 0x00:  # single frame
            n = first[0] & 0x0F
            return bytes(first[1:1 + n])
        total = ((first[0] & 0x0F) << 8) | first[1]
        buf = bytearray(first[2:8])
        # Send flow control: CTS, BS=0 (send all), STmin=0.
        self.raw.send(self.tx_id, self._pad(bytes([0x30, 0x00, 0x00])))
        seq = 1
        while len(buf) < total:
            cf = self._next(deadline)
            if cf is None:
                raise TimeoutException("ISO-TP: incomplete multi-frame response")
            if (cf[0] & 0xF0) != 0x20:
                continue  # not a CF — skip stray frame
            if (cf[0] & 0x0F) != (seq & 0x0F):
                raise DPduError(f"ISO-TP: bad CF seq {cf[0] & 0x0F} != {seq & 0x0F}")
            buf.extend(cf[1:8])
            seq = (seq + 1) & 0x0F
        return bytes(buf[:total])


def _stmin_seconds(stmin: int) -> float:
    """ISO 15765-2 STmin byte -> seconds. 0x00-0x7F = ms; 0xF1-0xF9 = 100-900 us."""
    if stmin <= 0x7F:
        return stmin / 1000.0
    if 0xF1 <= stmin <= 0xF9:
        return (stmin - 0xF0) / 10000.0
    return 0.0


# --------------------------------------------------------------------------------------
# udsoncan connection
# --------------------------------------------------------------------------------------
class DPduConnection(BaseConnection):
    """udsoncan connection over a VNCI via D-PDU raw-CAN + software ISO-TP.

    Mirrors CerberusConnection's request-level shape: specific_send queues the UDS
    request, specific_wait_frame drives one ISO-TP request/response and returns the
    assembled UDS payload.
    """

    def __init__(self, tx_id: int, rx_id: int, dll_path: Optional[str] = None,
                 bitrate: int = 500_000, raw: Optional[RawCan] = None, name=None):
        BaseConnection.__init__(self, name or "VNCI-DPDU")
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.bitrate = bitrate
        self._dll_path = dll_path
        self._raw = raw                # inject a RawCan (tests), else build a _DPduRawCan
        self._owns_raw = raw is None
        self._tp: Optional[_IsoTpClient] = None
        self._pending: Optional[bytes] = None

    def open(self):
        if self._raw is None:
            dll = self._dll_path or detect_dpdu_dll()
            if not dll:
                raise DPduUnavailable("no PDUAPI*.dll found (set SIMOS_DPDU_DLL)")
            self._raw = _DPduRawCan(dll, self.bitrate)
        self._raw.open()
        self._tp = _IsoTpClient(self._raw, self.tx_id, self.rx_id)
        return self

    def is_open(self):
        return self._tp is not None

    def close(self):
        if self._owns_raw and self._raw is not None:
            try:
                self._raw.close()
            except Exception:
                pass
        self._raw = None
        self._tp = None

    def specific_send(self, payload):
        self._pending = bytes(payload)

    def specific_wait_frame(self, timeout=4):
        if self._tp is None:
            raise RuntimeError("DPduConnection not open")
        if self._pending is None:
            raise TimeoutException("no request queued")
        try:
            self._tp.send(self._pending, timeout=timeout)
            return self._tp.recv(timeout=timeout)
        except DPduError as e:
            raise TimeoutException(f"D-PDU: {e}")
        finally:
            self._pending = None

    def empty_rxqueue(self):
        self._pending = None
        if self._raw is not None:
            try:
                self._raw.recv(0.0)
            except Exception:
                pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()
