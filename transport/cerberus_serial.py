"""
transport/cerberus_serial.py — host driver for the CerberusCAN (Teensy 4.1 tri-CAN) firmware.

CerberusCAN speaks a simple ASCII line protocol at 115200 (see CerberusCAN/src/main.cpp):

    PING                           -> PONG
    INFO                           -> CERBERUS:<ver> CAN1=.. CAN2=.. tmo=.. respmax=..
    <TXID>:<RXID>:<HEX>            UDS on Head 1 (shorthand)        -> OK:<resphex> | ERR:..
    UDS:<bus>:<TXID>:<RXID>:<HEX>  UDS on bus 1|2 (full ISO-TP)     -> OK:<resphex> | ERR:..
    RAW:<bus>:<ID>:<HEX>          send ONE raw frame (<=8B)        -> OK:sent | ERR:..
    SCAN:<bus>[:lo:hi[:winms]]     TesterPresent sweep              -> FOUND:<tx>:<rx>:<data> .. DONE:<n>
    SNIFF:<bus>:<ms>               passive dump (ms=0 = until any byte) -> RX:<ms>:<id>:<data> .. DONE:<n>
    MON:on[:idlo:idhi] | MON:off   Head-2 always-on background logger -> M2:<ms>:<id>:<hex>[:OVR]
    TP:<bus>:<TXID>:<ms>          background TesterPresent keep-alive | TP:STOP

It is a *request-level* VCI: the firmware runs the full ISO-TP transaction (incl. flow
control) on-device and returns the assembled response. So `uds()` is request->response,
not raw-frame send/recv.

Dual-head capture (firmware >= 0.6.0): Head 1 (CAN1, 500k, OBD 6/14) is the ACTIVE VCI;
Head 2 (CAN2) is a SECOND transceiver tapped on the SAME 6/14 bus, held listen-only as an
always-on background logger. With MON on, the firmware streams every Head-2 frame as an
`M2:` line *interleaved with command replies* — so you can drive an active UDS/CP exchange
on Head 1 and capture the unmasked wire on Head 2 at the same time. This driver demuxes:
`M2:` frames are routed to the `on_mon` callback; command replies stay clean.

Pair with cp_tools.can_decode to turn a capture into labeled UDS/KWP exchanges, and see
transport.cerberus_bridge for a udsoncan-compatible Connection built on top of this.
"""
from __future__ import annotations

import time

try:
    import serial  # pyserial
except Exception:  # pragma: no cover
    serial = None

BUS_DRIVE = 1   # Head 1 — active VCI on the Diagnostic CAN, 500k (OBD 6/14)
BUS_CONV = 2    # Head 2 — 2nd tap on the SAME 6/14 bus, listen-only background logger (>=0.6.0)
TEENSY_VID = 0x16C0


class CerberusError(Exception):
    pass


def detect_ports():
    """[(label, port)] for serial ports that look like a Teensy / CerberusCAN board."""
    found = []
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "").lower()
            if p.vid == TEENSY_VID or "teensy" in desc or "cerberus" in desc:
                lab = "%s  %s" % (p.device, p.description or "")
                if p.vid:
                    lab += "  [%04X:%04X]" % (p.vid, p.pid)
                found.append((lab.strip(), p.device))
    except Exception:
        pass
    return found


class Cerberus:
    """Host-side driver for the CerberusCAN firmware over USB serial.

    Single serial port = single-threaded: issue commands from one thread. The Head-2
    background logger does NOT need its own thread — `uds()`/`scan()` drain and route
    `M2:` frames to `on_mon` while they wait, and `pump()` drains them while idle.
    """

    def __init__(self, port, baud=115200, timeout=0.2):
        if serial is None:
            raise CerberusError("pyserial not installed (pip install pyserial)")
        self.port = port
        self._s = serial.Serial(port, baud, timeout=timeout)
        self._rxbuf = b""
        self._on_mon = None        # callback(t_ms:int, can_id:int, data:bytes) for M2: frames
        time.sleep(0.3)
        self._drain_raw()

    # ── background-logger demux ──────────────────────────────────────────────────
    def set_mon_callback(self, cb):
        """Register a callback(t_ms, can_id, data) for Head-2 background-logger frames."""
        self._on_mon = cb

    def _route(self, ln: str) -> bool:
        """If `ln` is an M2: background-logger frame, dispatch it and return True."""
        if not ln.startswith("M2:"):
            return False
        p = ln.split(":")
        if self._on_mon and len(p) >= 4:
            try:
                self._on_mon(int(p[1]), int(p[2], 16),
                             bytes.fromhex(p[3]) if p[3] else b"")
            except Exception:
                pass
        return True

    def _lines(self):
        """Read available serial, yield complete *command* lines; route M2: to the logger."""
        chunk = self._s.read(self._s.in_waiting or 1)
        if chunk:
            self._rxbuf += chunk
        while b"\n" in self._rxbuf:
            raw, self._rxbuf = self._rxbuf.split(b"\n", 1)
            ln = raw.decode(errors="replace").strip()
            if not ln or self._route(ln):
                continue
            yield ln

    def _drain_raw(self):
        """Discard any buffered input (used at open), without routing."""
        self._rxbuf = b""
        try:
            self._s.reset_input_buffer()
        except Exception:
            pass

    def pump(self, seconds: float = 0.0):
        """Drain pending serial, routing M2: frames to on_mon. Call in an idle live-view
        loop. With seconds>0, keep pumping for that long."""
        end = time.time() + seconds
        first = True
        while first or time.time() < end:
            first = False
            for _ in self._lines():
                pass  # stray non-M2 lines while idle are unexpected; drop
            if seconds <= 0:
                break
            time.sleep(0.002)

    # ── low level ──────────────────────────────────────────────────────────────
    def _send(self, line: str):
        # Drain pending input (routing M2: frames so capture isn't lost), then send.
        for _ in self._lines():
            pass
        self._s.write((line + "\n").encode())

    def _read_reply(self, deadline: float):
        """Return the first OK:/ERR: line before deadline (M2: frames routed meanwhile)."""
        while time.time() < deadline:
            for ln in self._lines():
                if ln.startswith("OK:") or ln.startswith("ERR"):
                    return ln
        raise CerberusError("timeout waiting for reply")

    # ── commands ───────────────────────────────────────────────────────────────
    def ping(self) -> bool:
        self._send("PING")
        t = time.time() + 1.0
        while time.time() < t:
            for ln in self._lines():
                if "PONG" in ln:
                    return True
        return False

    def info(self) -> str:
        self._send("INFO")
        t = time.time() + 1.0
        while time.time() < t:
            for ln in self._lines():
                if ln.startswith("CERBERUS:"):
                    return ln
        return ""

    def uds(self, bus: int, tx: int, rx: int, req: bytes, timeout: float = 7.0) -> bytes:
        """Run a full UDS request on `bus`; return the response payload bytes.
        Raises CerberusError on ERR (tx-fail / no-flow-control / no-response / partial).
        Head-2 M2: frames arriving during the wait are routed to on_mon."""
        self._send("UDS:%d:%X:%X:%s" % (bus, tx, rx, req.hex().upper()))
        ln = self._read_reply(time.time() + timeout)
        if ln.startswith("OK:"):
            return bytes.fromhex(ln[3:].strip())
        raise CerberusError(ln)

    def uds1(self, tx: int, rx: int, req: bytes, timeout: float = 7.0) -> bytes:
        """UDS on Head 1 via the TXID:RXID:HEX shorthand."""
        self._send("%X:%X:%s" % (tx, rx, req.hex().upper()))
        ln = self._read_reply(time.time() + timeout)
        if ln.startswith("OK:"):
            return bytes.fromhex(ln[3:].strip())
        raise CerberusError(ln)

    def raw(self, bus: int, can_id: int, data: bytes):
        self._send("RAW:%d:%X:%s" % (bus, can_id, data.hex().upper()))
        return self._read_reply(time.time() + 2.0)

    def scan(self, bus: int = BUS_DRIVE, lo: int = 0x700, hi: int = 0x7EF, winms: int = 120):
        """TesterPresent sweep -> [(tx_id, rx_id, resp_bytes)]."""
        self._send("SCAN:%d:%X:%X:%d" % (bus, lo, hi, winms))
        out = []
        deadline = time.time() + (hi - lo + 1) * (winms / 1000.0) + 5
        while time.time() < deadline:
            for ln in self._lines():
                if ln.startswith("FOUND:"):
                    p = ln.split(":")
                    if len(p) >= 4:
                        out.append((int(p[1], 16), int(p[2], 16),
                                    bytes.fromhex(p[3]) if p[3] else b""))
                elif ln.startswith("DONE"):
                    return out
        return out

    # ── Head-2 background logger (>= 0.6.0) ──────────────────────────────────────
    def mon_on(self, idlo: int = None, idhi: int = None):
        """Start the always-on Head-2 listen-only background logger. Register a sink with
        set_mon_callback() first; frames then flow during uds()/scan()/pump()."""
        cmd = "MON:on"
        if idlo is not None and idhi is not None:
            cmd += ":%X:%X" % (idlo, idhi)
        self._send(cmd)
        return self._read_reply(time.time() + 1.5)

    def mon_off(self):
        self._send("MON:off")
        return self._read_reply(time.time() + 1.5)

    def mon_capture(self, on_frame, stop=None, duration: float = None,
                    idlo: int = None, idhi: int = None):
        """Pure passive live-view: enable the Head-2 logger and pump frames to
        on_frame(t, can_id, data) until stop() is True / duration elapses. Returns count.
        (To capture *while driving*, instead: set_mon_callback(cb); mon_on(); then issue
        uds() calls normally — M2: frames route to cb during each transaction.)"""
        n = [0]

        def _cb(t, cid, data):
            n[0] += 1
            on_frame(t, cid, data)

        self.set_mon_callback(_cb)
        self.mon_on(idlo, idhi)
        t0 = time.time()
        try:
            while True:
                if stop and stop():
                    break
                if duration and (time.time() - t0) > duration:
                    break
                self.pump()
                time.sleep(0.002)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.mon_off()
            except Exception:
                pass
            self.set_mon_callback(None)
        return n[0]

    def sniff(self, bus: int = BUS_DRIVE, ms: int = 0, on_frame=None, stop=None):
        """Explicit passive capture via the SNIFF command (Head-1 listen-only). For the
        concurrent active+passive use case prefer MON (Head 2). ms=0 = run until stop()."""
        self._send("SNIFF:%d:%d" % (bus, ms))
        frames = []
        t0 = time.time()
        stopped = False
        stop_deadline = None
        try:
            while True:
                if ms and (time.time() - t0) > ms / 1000.0 + 5:
                    break
                if ms == 0 and not stopped and stop is not None and stop():
                    self._s.write(b"\n")          # ms=0 sniff halts on any serial byte
                    stopped = True
                    stop_deadline = time.time() + 1.5
                if stopped and time.time() > stop_deadline:
                    break
                for ln in self._lines():
                    if ln.startswith("RX:"):
                        p = ln.split(":")
                        if len(p) >= 4:
                            t, cid = int(p[1]), int(p[2], 16)
                            data = bytes.fromhex(p[3]) if p[3] else b""
                            frames.append((t, cid, data))
                            if on_frame:
                                on_frame(t, cid, data)
                    elif ln.startswith("DONE"):
                        return frames
        except KeyboardInterrupt:
            try:
                self._s.write(b"\n")
                time.sleep(0.2)
            except Exception:
                pass
        return frames

    def tp_start(self, bus: int, tx: int, ms: int = 1000):
        self._send("TP:%d:%X:%d" % (bus, tx, ms))
        return self._read_reply(time.time() + 1.5)

    def tp_stop(self):
        self._send("TP:STOP")
        return self._read_reply(time.time() + 1.5)

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def close(self):
        try:
            if self._on_mon is not None:
                try:
                    self.mon_off()
                except Exception:
                    pass
            self._s.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
