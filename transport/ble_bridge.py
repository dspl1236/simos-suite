"""
transport/ble_bridge.py — BLE connection layer for the ESP32 ISO-TP bridge

Connects to the dspl1236/esp32-isotp-ble-bridge-c7vag firmware via Bluetooth LE
and exposes a udsoncan-compatible connection object so the rest of the suite
can use it transparently — identical interface to J2534 or SocketCAN.

Protocol (confirmed from ble_server.c / ble_server.h in the firmware):

  BLE GATT service UUID:    0xABF0
  Write characteristic:     0xABF1  (tester → ESP32, write-without-response)
  Notify characteristic:    0xABF2  (ESP32 → tester, notify)
  Command characteristic:   0xABF3  (settings/command writes)
  Status characteristic:    0xABF4  (status notifications)

  Default advertised device name: "BLE_TO_ISOTP20"
  (configurable via BRG_SETTING_GAP — check your firmware's stored GAP name)

Packet framing (ble_header_t, 8 bytes, prepended to every payload):

    Offset  Size  Field
    0       1     hdID     — 0xF1 for normal frame, 0xF2 for split continuation
    1       1     cmdFlags — flag bits (see BLE_COMMAND_FLAG_* in constants.h)
    2       2     rxID     — CAN RX ID (little-endian)
    4       2     txID     — CAN TX ID (little-endian)
    6       2     cmdSize  — payload length (little-endian)
    [8...]        payload  — ISO-TP frame bytes

Split packet reassembly:
    If cmdFlags & 0x08 (BLE_COMMAND_FLAG_SPLIT_PK): first chunk, more follow.
    Continuation chunks start with hdID=0xF2, chunk_number (1-indexed).
    Reassemble until no more 0xF2 chunks arrive.

Multiple frames per BLE notification:
    A single BLE notification can carry multiple concatenated framed messages
    (each with its own 8-byte header) if they fit within the MTU window.
    The parser loops until all bytes are consumed.

Device identification (Simos Tools APK behavior, reconstructed):
    The APK scans for BLE devices advertising service UUID 0xABF0.
    It matches by service UUID first, then optionally by GAP name prefix.
    The firmware's default GAP name is "BLE_TO_ISOTP20" (14 chars max).
    If you renamed your device via BRG_SETTING_GAP, scan by UUID instead of name.

Usage:
    from transport.ble_bridge import BLEBridgeSync, BLEBridgeConnection

    bridge = BLEBridgeSync()
    devices = bridge.scan(timeout=5.0)
    ok = bridge.connect(devices[0])
    conn = bridge.make_connection(rx_id=0x77A, tx_id=0x710)

    with udsoncan.Client(conn, ...) as client:
        client.change_session(...)

    bridge.disconnect()
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import queue
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

log = logging.getLogger("SimosSuite.BLE")

# ─── UUIDs (confirmed from firmware ble_server.c) ────────────────────────────

BLE_SERVICE_UUID      = "0000abf0-0000-1000-8000-00805f9b34fb"
BLE_CHAR_WRITE_UUID   = "0000abf1-0000-1000-8000-00805f9b34fb"  # tester → ESP32
BLE_CHAR_NOTIFY_UUID  = "0000abf2-0000-1000-8000-00805f9b34fb"  # ESP32 → tester
BLE_CHAR_CMD_UUID     = "0000abf3-0000-1000-8000-00805f9b34fb"  # command writes
BLE_CHAR_STATUS_UUID  = "0000abf4-0000-1000-8000-00805f9b34fb"  # status notify

# Default GAP name advertised by firmware
BLE_DEFAULT_GAP_NAME  = "BLE_TO_ISOTP20"

# ─── Packet framing constants (from ble_server.h) ────────────────────────────

BLE_HEADER_ID         = 0xF1   # normal frame header byte
BLE_PARTIAL_ID        = 0xF2   # split packet continuation byte
BLE_HEADER_SIZE       = 8      # sizeof(ble_header_t)

# cmdFlags bits
FLAG_PER_ENABLE       = 0x01
FLAG_PER_CLEAR        = 0x02
FLAG_PER_ADD          = 0x04
FLAG_SPLIT_PK         = 0x08   # this chunk is split, more follow
FLAG_SETTINGS_GET     = 0x40
FLAG_SETTINGS         = 0x80

# Setting IDs (from constants.h BRG_SETTING_*)
SETTING_ISOTP_STMIN    = 1
SETTING_LED_COLOR      = 2
SETTING_PERSIST_DELAY  = 3
SETTING_PERSIST_QDELAY = 4
SETTING_BLE_SEND_DELAY = 5
SETTING_BLE_MULTI_DELAY= 6
SETTING_PASSWORD       = 7
SETTING_GAP            = 8
SETTING_RAW_SNIFF      = 9
RAW_SNIFF_CAN_ID       = 0xCAFE  # txID/rxID used for raw CAN sniff frames


# ─── State ───────────────────────────────────────────────────────────────────

class BridgeState(Enum):
    DISCONNECTED  = auto()
    SCANNING      = auto()
    CONNECTING    = auto()
    CONNECTED     = auto()
    DISCONNECTING = auto()
    ERROR         = auto()


@dataclass
class BLEDeviceInfo:
    """A discovered BLE bridge device."""
    device:   BLEDevice
    adv_data: AdvertisementData
    name:     str
    address:  str
    rssi:     int

    def __str__(self) -> str:
        return f"{self.name}  [{self.address}]  RSSI={self.rssi}dBm"


# ─── BLEBridge (async core) ──────────────────────────────────────────────────

class BLEBridge:
    """
    Manages the BLE connection to the ESP32 ISO-TP bridge.
    Runs an internal asyncio loop in a background daemon thread.
    """

    def __init__(self):
        self._client:    Optional[BleakClient] = None
        self._state:     BridgeState = BridgeState.DISCONNECTED
        self._loop:      Optional[asyncio.AbstractEventLoop] = None
        self._thread:    Optional[threading.Thread] = None

        # Per-channel receive queues keyed by (tx_id, rx_id)
        self._rx_queues: Dict[Tuple[int,int], queue.Queue] = {}
        self._raw_queue: Optional[queue.Queue] = None

        # Split packet reassembly state
        self._split_buf: bytes = b""
        self._split_hdr: Optional[Tuple] = None

        # GUI callbacks
        self._on_state_change: Optional[Callable[[BridgeState], None]] = None
        self._on_error:        Optional[Callable[[str], None]] = None

        self._last_scan: List[BLEDeviceInfo] = []

    # ── Callbacks ─────────────────────────────────────────────────────────

    def set_state_callback(self, cb: Callable[[BridgeState], None]):
        self._on_state_change = cb

    def set_error_callback(self, cb: Callable[[str], None]):
        self._on_error = cb

    def _set_state(self, state: BridgeState):
        self._state = state
        log.info("BLE → %s", state.name)
        if self._on_state_change:
            self._on_state_change(state)

    @property
    def state(self) -> BridgeState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == BridgeState.CONNECTED

    @property
    def connected_address(self) -> Optional[str]:
        return self._client.address if self._client else None

    # ── Loop management ───────────────────────────────────────────────────

    def _ensure_loop(self):
        if self._loop is None or not self._loop.is_running():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                name="BLEBridge-loop",
                daemon=True,
            )
            self._thread.start()

    def _run(self, coro):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)

    # ── Scan ─────────────────────────────────────────────────────────────

    def scan(self, timeout: float = 5.0,
             name_filter: Optional[str] = None) -> List[BLEDeviceInfo]:
        return self._run(self._scan(timeout, name_filter))

    async def _scan(self, timeout: float,
                    name_filter: Optional[str]) -> List[BLEDeviceInfo]:
        self._set_state(BridgeState.SCANNING)
        found: List[BLEDeviceInfo] = []

        def _cb(device: BLEDevice, adv: AdvertisementData):
            svc_uuids = [u.lower() for u in (adv.service_uuids or [])]
            if not any("abf0" in u for u in svc_uuids):
                return
            name = device.name or adv.local_name or BLE_DEFAULT_GAP_NAME
            if name_filter and not name.upper().startswith(name_filter.upper()):
                return
            if not any(d.address == device.address for d in found):
                info = BLEDeviceInfo(device=device, adv_data=adv,
                                     name=name, address=device.address,
                                     rssi=adv.rssi or -999)
                found.append(info)
                log.info("Found: %s", info)

        scanner = BleakScanner(detection_callback=_cb,
                               service_uuids=[BLE_SERVICE_UUID])
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()

        found.sort(key=lambda d: d.rssi, reverse=True)
        self._last_scan = found
        self._set_state(BridgeState.DISCONNECTED)
        return found

    @property
    def last_scan_results(self) -> List[BLEDeviceInfo]:
        return self._last_scan

    # ── Connect ───────────────────────────────────────────────────────────

    def connect(self, device) -> bool:
        address = device.address if isinstance(device, BLEDeviceInfo) else device
        return self._run(self._connect(address))

    async def _connect(self, address: str) -> bool:
        if self._state == BridgeState.CONNECTED:
            return True
        self._set_state(BridgeState.CONNECTING)
        try:
            self._client = BleakClient(address,
                                       disconnected_callback=self._on_disconnected)
            await self._client.connect()
            await self._client.start_notify(BLE_CHAR_NOTIFY_UUID, self._on_notify)
            # Authenticate with v1.03+ firmware — send password via CMD characteristic
            # Default password is "BLE2" (PASSWORD_CHECK enabled in v1.03)
            await self._authenticate()
            try:
                await self._client.request_mtu(517)
            except Exception:
                pass
            self._set_state(BridgeState.CONNECTED)
            return True
        except Exception as e:
            msg = f"BLE connect failed: {e}"
            log.error(msg)
            self._set_state(BridgeState.ERROR)
            if self._on_error:
                self._on_error(msg)
            return False

    def _on_disconnected(self, client: BleakClient):
        log.warning("BLE disconnected")
        self._set_state(BridgeState.DISCONNECTED)
        for q in self._rx_queues.values():
            q.put(None)
        if self._raw_queue:
            self._raw_queue.put(None)

    # ── Disconnect ────────────────────────────────────────────────────────

    def disconnect(self):
        self._run(self._disconnect())

    async def _disconnect(self):
        if self._client and self._client.is_connected:
            self._set_state(BridgeState.DISCONNECTING)
            try:
                await self._client.stop_notify(BLE_CHAR_NOTIFY_UUID)
                await self._client.disconnect()
            except Exception as e:
                log.warning("Disconnect error (ignored): %s", e)
        self._set_state(BridgeState.DISCONNECTED)
        self._client = None

    # ── Send ─────────────────────────────────────────────────────────────

    def send_frame(self, tx_id: int, rx_id: int, payload: bytes, flags: int = 0):
        if not self.is_connected:
            raise ConnectionError("BLE bridge not connected")
        self._run(self._send_frame(tx_id, rx_id, payload, flags))

    async def _send_frame(self, tx_id: int, rx_id: int,
                           payload: bytes, flags: int):
        header = struct.pack("<BBHHH",
            BLE_HEADER_ID, flags, rx_id, tx_id, len(payload))
        await self._client.write_gatt_char(
            BLE_CHAR_WRITE_UUID, header + payload, response=False)
        log.debug("TX tx=%#06x rx=%#06x len=%d", tx_id, rx_id, len(payload))

    def send_settings(self, setting_id: int, value: bytes):
        if not self.is_connected:
            raise ConnectionError("BLE bridge not connected")
        self._run(self._send_settings(setting_id, value))

    async def _send_settings(self, setting_id: int, value: bytes):
        payload = bytes([FLAG_SETTINGS, setting_id]) + value
        await self._client.write_gatt_char(BLE_CHAR_CMD_UUID, payload,
                                           response=False)

    async def _authenticate(self):
        """
        FunkBridge firmware has PASSWORD_CHECK disabled — no auth needed.
        This is a no-op kept for interface compatibility.
        Switchleg v0.90 also needs no auth.
        Switchleg v1.03+ ignores unknown command flags gracefully.
        """
        await asyncio.sleep(0.1)   # brief settle after subscribe
        log.debug("BLE auth: no password required (FunkBridge firmware)")

    def _on_notify(self, char_handle, data: bytearray):
        raw = bytes(data)
        log.debug("RX %d bytes: %s", len(raw), raw.hex())
        offset = 0
        while offset < len(raw):
            hd_id = raw[offset]

            if hd_id == BLE_PARTIAL_ID:
                if len(raw) < offset + 2:
                    break
                chunk_num  = raw[offset + 1]
                chunk_data = raw[offset + 2:]
                self._split_buf += chunk_data
                log.debug("Split chunk %d +%d bytes", chunk_num, len(chunk_data))
                if self._split_hdr:
                    _, _, rx_id, tx_id = self._split_hdr
                    self._dispatch(tx_id, rx_id, self._split_buf)
                    self._split_buf = b""
                    self._split_hdr = None
                break

            if hd_id != BLE_HEADER_ID:
                log.warning("Bad header byte %#04x at offset %d", hd_id, offset)
                break
            if offset + BLE_HEADER_SIZE > len(raw):
                break

            _, flags, rx_id, tx_id, cmd_size = struct.unpack_from(
                "<BBHHH", raw, offset)
            offset += BLE_HEADER_SIZE

            if offset + cmd_size > len(raw):
                break
            payload = raw[offset:offset + cmd_size]
            offset += cmd_size

            if flags & FLAG_SPLIT_PK:
                self._split_buf = payload
                self._split_hdr = (hd_id, flags, rx_id, tx_id)
            else:
                self._dispatch(tx_id, rx_id, payload)

    def _dispatch(self, tx_id: int, rx_id: int, payload: bytes):
        log.debug("RX ← tx=%#06x rx=%#06x len=%d", tx_id, rx_id, len(payload))
        key = (tx_id, rx_id)
        if key in self._rx_queues:
            self._rx_queues[key].put(payload)
        if self._raw_queue is not None:
            self._raw_queue.put((tx_id, rx_id, payload))

    # ── Queue registration ────────────────────────────────────────────────

    def register_channel(self, tx_id: int, rx_id: int) -> queue.Queue:
        key = (tx_id, rx_id)
        if key not in self._rx_queues:
            self._rx_queues[key] = queue.Queue(maxsize=256)
        return self._rx_queues[key]

    def unregister_channel(self, tx_id: int, rx_id: int):
        self._rx_queues.pop((tx_id, rx_id), None)

    def enable_raw_queue(self) -> queue.Queue:
        self._raw_queue = queue.Queue(maxsize=4096)
        return self._raw_queue

    def disable_raw_queue(self):
        self._raw_queue = None


# ─── BLEBridgeConnection — udsoncan interface ─────────────────────────────────

class BLEBridgeConnection:
    """
    udsoncan-compatible connection backed by BLEBridge.
    Drop-in for IsoTPSocketConnection or J2534Connection.
    """

    def __init__(self, bridge: BLEBridge, rx_id: int, tx_id: int,
                 timeout: float = 5.0):
        self._bridge  = bridge
        self._rx_id   = rx_id
        self._tx_id   = tx_id
        self._timeout = timeout
        self._queue:  Optional[queue.Queue] = None

    def open(self):
        if not self._bridge.is_connected:
            raise ConnectionError("BLE bridge not connected — call bridge.connect() first")
        self._queue = self._bridge.register_channel(self._tx_id, self._rx_id)

    def close(self):
        if self._queue is not None:
            self._bridge.unregister_channel(self._tx_id, self._rx_id)
            self._queue = None

    def send(self, payload: bytes):
        self._bridge.send_frame(self._tx_id, self._rx_id, payload)

    def wait_frame(self, timeout: Optional[float] = None) -> bytes:
        t = timeout if timeout is not None else self._timeout
        if self._queue is None:
            raise ConnectionError("Connection not open")
        try:
            frame = self._queue.get(timeout=t)
        except queue.Empty:
            raise TimeoutError(
                f"No BLE response within {t}s "
                f"(tx={self._tx_id:#06x} rx={self._rx_id:#06x})")
        if frame is None:
            raise ConnectionError("BLE bridge disconnected mid-operation")
        return frame

    def empty(self) -> bool:
        return self._queue is None or self._queue.empty()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


# ─── Raw sniff frame ──────────────────────────────────────────────────────────

@dataclass
class RawCANFrame:
    """Raw CAN frame from BRG_SETTING_RAW_SNIFF mode (txID=rxID=0xCAFE)."""
    can_id:    int
    dlc:       int
    data:      bytes
    timestamp: float = field(default_factory=time.monotonic)

    def __str__(self) -> str:
        return (f"CAN {self.can_id:#05x}  [{self.dlc}]  "
                f"{self.data.hex(' ').upper()}")


def parse_raw_sniff_frame(payload: bytes) -> Optional[RawCANFrame]:
    """
    Parse payload from raw sniff mode.
    Format: [id_hi][id_lo][dlc][d0..d7]  (11-bit CAN ID, big-endian)
    """
    if len(payload) < 3:
        return None
    can_id = struct.unpack_from(">H", payload, 0)[0]
    dlc    = payload[2]
    data   = payload[3:3 + min(dlc, 8)]
    return RawCANFrame(can_id=can_id, dlc=dlc, data=bytes(data))


# ─── Sync wrapper for GUI ─────────────────────────────────────────────────────

class BLEBridgeSync:
    """
    Synchronous wrapper for GUI use (Qt slots, tkinter callbacks, etc).
    All calls block until completion. Background asyncio loop is managed internally.

    Quick start:
        bridge = BLEBridgeSync()
        bridge.set_state_callback(my_gui_update_fn)   # optional
        devices = bridge.scan()
        ok = bridge.connect(devices[0])
        conn = bridge.make_connection(rx_id=0x77A, tx_id=0x710)
        # conn → pass to udsoncan.Client
        bridge.disconnect()
    """

    def __init__(self):
        self._bridge = BLEBridge()

    def set_state_callback(self, cb: Callable[[BridgeState], None]):
        """Hook for GUI — called on every state transition."""
        self._bridge.set_state_callback(cb)

    def set_error_callback(self, cb: Callable[[str], None]):
        """Hook for GUI — called with error string on connection failure."""
        self._bridge.set_error_callback(cb)

    @property
    def is_connected(self) -> bool:
        return self._bridge.is_connected

    @property
    def state(self) -> BridgeState:
        return self._bridge.state

    @property
    def connected_address(self) -> Optional[str]:
        return self._bridge.connected_address

    @property
    def last_scan_results(self) -> List[BLEDeviceInfo]:
        return self._bridge.last_scan_results

    def scan(self, timeout: float = 5.0,
             name_filter: Optional[str] = None) -> List[BLEDeviceInfo]:
        """
        Scan for bridge devices. Blocks for timeout seconds.
        Returns list sorted by RSSI (strongest first).
        Identifies devices by service UUID 0xABF0.
        GAP name is "BLE_TO_ISOTP20" by default — pass name_filter to narrow down
        if you have multiple BLE devices nearby.
        """
        return self._bridge.scan(timeout=timeout, name_filter=name_filter)

    def connect(self, device) -> bool:
        """Connect to device (BLEDeviceInfo or address string). Returns True on success."""
        return self._bridge.connect(device)

    def disconnect(self):
        """Disconnect cleanly."""
        self._bridge.disconnect()

    def make_connection(self, rx_id: int, tx_id: int,
                        timeout: float = 5.0) -> BLEBridgeConnection:
        """
        Return a udsoncan-compatible connection for a CAN channel.
        Must be connected first.

        Args:
            rx_id: CAN ID we expect responses FROM (e.g. 0x77A for J533)
            tx_id: CAN ID we send requests TO   (e.g. 0x710 for J533)
        """
        return BLEBridgeConnection(
            self._bridge, rx_id=rx_id, tx_id=tx_id, timeout=timeout)

    def enable_raw_sniff(self) -> queue.Queue:
        """
        Enable raw CAN frame capture (requires BRG_SETTING_RAW_SNIFF=1 on bridge).
        Returns queue of (tx_id, rx_id, payload) tuples.
        Use parse_raw_sniff_frame(payload) to decode.
        """
        return self._bridge.enable_raw_queue()

    def disable_raw_sniff(self):
        self._bridge.disable_raw_queue()

    def send_settings(self, setting_id: int, value: bytes):
        """Send a BRG_SETTING_* command to the bridge firmware."""
        self._bridge.send_settings(setting_id, value)

    def set_password(self, password: str):
        """Set the BLE password used during connect (default: 'BLE2')."""
        self._bridge._password = password
