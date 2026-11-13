"""Unit tests for transport.dpdu_bridge — the hardware-independent half.

Covers the software ISO-TP client and the DPduConnection seam over a fake ISO-TP ECU,
including the multi-frame-burst buffering (the case where several consecutive frames land
in one raw-CAN poll). The ctypes D-PDU layer (_DPduRawCan) needs a real VNCI and is not
exercised here; import must not require the DLL.
"""
from __future__ import annotations

import platform
from typing import Callable, List, Tuple

from transport.dpdu_bridge import (
    DPduConnection, RawCan, _IsoTpClient, detect_dpdu_dll, dpdu_available,
)

TX = 0x710  # tester -> ECU
RX = 0x77A  # ECU -> tester


class FakeIsoTpEcu(RawCan):
    """Loopback ISO-TP ECU on RX that answers a request on TX with responder(req).

    Models an ECU that (a) sends a Flow Control CTS after a tester First Frame, and
    (b) emits all response frames at once — so recv() hands the client a burst, exercising
    the _IsoTpClient buffer. recv() never echoes the tester's own TX frames.
    """

    def __init__(self, responder: Callable[[bytes], bytes], pad: int = 0x00):
        self.responder = responder
        self.pad = pad
        self.opened = False
        self._to_tester: List[Tuple[int, bytes]] = []
        self._req = bytearray()
        self._req_total = 0
        self._seq = 0
        self._awaiting_cf = False
        self.sent_frames: List[Tuple[int, bytes]] = []  # everything the tester sent

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.opened = False

    def _pad(self, b: bytes) -> bytes:
        return b + bytes([self.pad]) * (8 - len(b)) if len(b) < 8 else b

    def send(self, can_id: int, data: bytes) -> None:
        assert can_id == TX, "tester must transmit on TX"
        self.sent_frames.append((can_id, bytes(data)))
        pci = data[0] & 0xF0
        if pci == 0x00:  # single-frame request
            n = data[0] & 0x0F
            self._respond(bytes(data[1:1 + n]))
        elif pci == 0x10:  # first frame of a multi-frame request
            self._req_total = ((data[0] & 0x0F) << 8) | data[1]
            self._req = bytearray(data[2:8])
            self._seq = 1
            self._awaiting_cf = True
            self._to_tester.append((RX, self._pad(bytes([0x30, 0x00, 0x00]))))  # FC CTS
        elif pci == 0x20 and self._awaiting_cf:  # consecutive frame
            self._req.extend(data[1:8])
            if len(self._req) >= self._req_total:
                self._awaiting_cf = False
                self._respond(bytes(self._req[:self._req_total]))
        elif pci == 0x30:
            pass  # tester FC during our TX; we use BS=0 so nothing to do

    def _respond(self, req: bytes) -> None:
        resp = self.responder(req)
        if len(resp) <= 7:
            self._to_tester.append((RX, self._pad(bytes([len(resp)]) + resp)))
            return
        total = len(resp)
        self._to_tester.append((RX, self._pad(bytes([0x10 | ((total >> 8) & 0x0F),
                                                     total & 0xFF]) + resp[:6])))
        idx, seq = 6, 1
        while idx < total:
            self._to_tester.append((RX, self._pad(bytes([0x20 | (seq & 0x0F)]) +
                                                  resp[idx:idx + 7])))
            idx += 7
            seq = (seq + 1) & 0x0F

    def recv(self, timeout: float) -> List[Tuple[int, bytes]]:
        out = self._to_tester
        self._to_tester = []
        return out


def _tp(responder):
    ecu = FakeIsoTpEcu(responder)
    ecu.open()
    return _IsoTpClient(ecu, TX, RX), ecu


def test_single_frame_roundtrip():
    tp, _ = _tp(lambda req: b"\x62\xf1\x87" + b"ABC")  # RDBI positive, 6 bytes
    tp.send(b"\x22\xf1\x87")
    assert tp.recv(timeout=1.0) == b"\x62\xf1\x87ABC"


def test_multiframe_response_reassembly():
    # 40-byte response forces FF + multiple CFs delivered as one burst.
    payload = bytes([0x62, 0xF1, 0x90]) + bytes(range(37))
    tp, ecu = _tp(lambda req: payload)
    tp.send(b"\x22\xf1\x90")
    assert tp.recv(timeout=1.0) == payload
    # tester must have emitted exactly one FC CTS during reassembly
    fcs = [d for cid, d in ecu.sent_frames if cid == TX and (d[0] & 0xF0) == 0x30]
    assert len(fcs) == 1 and fcs[0][0] == 0x30


def test_multiframe_request_segmentation():
    seen = {}

    def responder(req):
        seen["req"] = req
        return b"\x6e\xf1\x5a"  # WDBI positive

    tp, _ = _tp(responder)
    long_req = bytes([0x2E, 0xF1, 0x5A]) + bytes(range(30))  # 33 bytes -> FF + CFs
    tp.send(long_req, timeout=1.0)
    assert tp.recv(timeout=1.0) == b"\x6e\xf1\x5a"
    assert seen["req"] == long_req  # ECU reassembled the segmented request intact


def test_consecutive_frame_sequence_error():
    import pytest

    class BadSeqEcu(FakeIsoTpEcu):
        def _respond(self, req):
            # FF declaring total=20 (0x10|0x00, 0x14), then a CF with the WRONG
            # sequence number (2 instead of the expected 1).
            self._to_tester.append((RX, self._pad(bytes([0x10, 0x14]) + bytes(6))))
            self._to_tester.append((RX, self._pad(bytes([0x22]) + bytes(7))))

    ecu = BadSeqEcu(lambda r: b"")
    ecu.open()
    tp = _IsoTpClient(ecu, TX, RX)
    tp.send(b"\x22\xf1\x90")
    with pytest.raises(Exception):
        tp.recv(timeout=0.5)


def test_connection_seam():
    ecu = FakeIsoTpEcu(lambda req: b"\x62" + req[1:] + b"\x99")
    conn = DPduConnection(TX, RX, raw=ecu)
    assert not conn.is_open()
    conn.open()
    assert conn.is_open() and ecu.opened
    conn.specific_send(b"\x22\xf1\x87")
    assert conn.specific_wait_frame(timeout=1.0) == b"\x62\xf1\x87\x99"
    conn.close()
    assert not conn.is_open()


def test_import_without_dll():
    # Importing and detecting must never require the DLL.
    assert detect_dpdu_dll() is None or isinstance(detect_dpdu_dll(), str)
    assert isinstance(dpdu_available(), bool)
    if platform.system() != "Windows":
        assert detect_dpdu_dll() is None


if __name__ == "__main__":
    import sys
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-v"]))
    except ImportError:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print("ok", name)
        print("all passed")
