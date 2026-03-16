"""
transport/ble_bridge.py — BLE client for the ESP32 ISO-TP BLE Bridge

Connects to the ESP32 running esp32-isotp-ble-bridge-c7vag firmware and
presents a udsoncan-compatible connection interface to the rest of the suite.

───── Firmware protocol (from ble_server.c / ble_server.h) ─────────────────

BLE advertisement:
  Device name:  "BLE_TO_ISOTP20"  (DEFAULT_GAP_NAME, user-configurable via
                                   BRG_SETTING_GAP)
  Service UUID: 0xABF0            (spp_service_uuid)

GATT characteristics:
  0xABF1  DATA_RECEIVE  — write here to send data TO the bridge (tester→ECU)
  0xABF2  DATA_NOTIFY   — subscribe here to receive data FROM the bridge (ECU→tester)
  0xABF3  COMMAND       — write settings/commands to the bridge
  0xABF4  STATUS        — bridge status notifications

Packet format (ble_header_t):
  Byte 0:     hdID      = 0xF1  (BLE_HEADER_ID)
  Byte 1:     cmdFlags  — split packet flags, settings flags
  Bytes 2–3:  rxID      — ISO-TP receive CAN ID (little-endian uint16)
  Bytes 4–5:  txID      — ISO-TP transmit CAN ID (little-endian uint16)
  Bytes 6–7:  cmdSize   — payload length (little-endian uint16)
  Bytes 8+:   payload   — UDS frame bytes

Split packets:
  If BLE_COMMAND_FLAG_SPLIT_PK (0x08) is set in cmdFlags, more chunks follow.
  Continuation chunks start with [0xF2][chunk_num] (BLE_PARTIAL_ID).

Raw CAN sniff mode:
  When BRG_SETTING_RAW_SNIFF=9 is set, all CAN frames are forwarded with
  txID=rxID=0xCAFE (BLE_RAW_SNIFF_ID). These are filtered out of the UDS
  receive path and dispatched to a separate sniff callback.

─────────────────────────────────────────────────────────────────────────────

Usage:
    from transport.ble_bridge import BLEBridge, BLEBridgeConnection
    import asyncio

    bridge = BLEBridge()
    devices = asyncio.run(bridge.scan(timeout=5.0))
    asyncio.run(bridge.connect(devices[0]))

    # udsoncan connection — drop this into _make_connection()
    conn = BLEBridgeConnection(bridge, tx_id=0x7E0, rx_id=0x7E8)

    # GUI usage:
    bridge.on_connect    = lambda dev: update_status_bar("Connected", "green")
    bridge.on_disconnect = lambda:     update_status_bar("Disconnected", "red")
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import bleak
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

log = logging.getLogger("SimosSuite.BLE")

# ── Firmware-defined constants (from ble_server.h / constants.h) ─────────────

BLE_SERVICE_UUID       = "0000abf0-0000-1000-8000-00805f9b34fb"
BLE_CHAR_DATA_RECV     = "0000abf1-0000-1000-8000-00805f9b34fb"  # write (tester→bridge)
BLE_CHAR_DATA_NOTIFY   = "0000abf2-0000-1000-8000-00805f9b34fb"  # notify (bridge→tester)
BLE_CHAR_CMD_RECV      = "0000abf3-0000-1000-8000-00805f9b34fb"  # write commands
BLE_CHAR_CMD_NOTIFY    = "0000abf4-0000-1000-8000-00805f9b34fb"  # status notifications

DEFAULT_DEVICE_NAME    = "BLE_TO_ISOTP20"   # DEFAULT_GAP_NAME in firmware

BLE_HEADER_ID          = 0xF1
BLE_PARTIAL_ID         = 0xF2
BLE_COMMAND_FLAG_SPLIT = 0x08

# Settings command IDs (BRG_SETTING_* in constants.h)
BRG_SETTING_ISOTP_STMIN    = 1
BRG_SETTING_LED_COLOR      = 2
BRG_SETTING_PERSIST_DELAY  = 3
BRG_SETTING_PERSIST_Q_DELAY= 4
BRG_SETTING_BLE_SEND_DELAY = 5
BRG_SETTING_BLE_MULTI_DELAY= 6
BRG_SETTING_PASSWORD       = 7
BRG_SETTING_GAP            = 8
BRG_SETTING_RAW_SNIFF      = 9

BLE_RAW_SNIFF_ID           = 0xCAFE    # magic CAN ID used for raw sniff frames


# ── BLE packet header (mirrors ble_header_t from ble_server.h) ───────────────

_HDR_FMT  = "<BBHHHxx"     # hdID, cmdFlags, rxID, txID, cmdSize, 2 pad
_HDR_SIZE = struct.calcsize(_HDR_FMT)   # = 10 bytes

def _pack_header(tx_id: int, rx_id: int, payload: bytes, flags: int = 0) -> bytes:
    hdr = struct.pack(_HDR_FMT,
                      BLE_HEADER_ID,
                      flags,
                      rx_id,      # rxID — the CAN ID the ECU sends FROM
                      tx_id,      # txID — the CAN ID we send TO the ECU
                      len(payload))
    return hdr + payload

def _unpack_header(data: bytes) -> Optional[Tuple[int, int, int, int, bytes]]:
    """
    Returns (hd_id, flags, rx_id, tx_id, payload) or None if malformed.
    hd_id will be BLE_HEADER_ID (0xF1) for normal frames,
    BLE_PARTIAL_ID (0xF2) for split continuation chunks.
    """
    if len(data) < _HDR_SIZE:
        return None
    hd_id, flags, rx_id, tx_id, cmd_size = struct.unpack_from(_HDR_FMT, data)
    payload = data[_HDR_SIZE:_HDR_SIZE + cmd_size]
    return hd_id, flags, rx_id, tx_id, payload


# ── Discovered device wrapper ─────────────────────────────────────────────────

@dataclass
class FoundDevice:
    name:    str
    address: str
    rssi:    int
    device:  BLEDevice

    def __str__(self):
        return f"{self.name}  [{self.address}]  RSSI={self.rssi} dBm"


# ── Main bridge class ─────────────────────────────────────────────────────────

class BLEBridge:
    """
    Manages the BLE connection to the ESP32 ISO-TP bridge.

    Thread-safe: all asyncio work is run on a background event loop thread.
    The public API (scan, connect, disconnect, send) can be called from any
    thread, including the GUI main thread.

    Callbacks (set these before connecting):
        on_connect(device: FoundDevice)  — called when connection is established
        on_disconnect()                  — called on unexpected or clean disconnect
        on_sniff_frame(can_id, data)     — raw CAN sniff frames (if sniff enabled)
    """

    def __init__(self):
        self._client:    Optional[BleakClient] = None
        self._device:    Optional[FoundDevice] = None
        self._connected: bool = False
        self._loop:      asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread:    threading.Thread = threading.Thread(
            target=self._run_loop, daemon=True, name="BLE-EventLoop")
        self._thread.start()

        # Per-channel receive queues: keyed by (tx_id, rx_id)
        # BLEBridgeConnection registers its queue here
        self._rx_queues: Dict[Tuple[int,int], asyncio.Queue] = {}
        self._rx_lock    = threading.Lock()

        # Reassembly buffer for split packets
        self._split_buf: bytearray = bytearray()
        self._split_header: Optional[Tuple] = None

        # Callbacks — set by owner
        self.on_connect:    Optional[Callable[[FoundDevice], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_sniff_frame:Optional[Callable[[int, bytes], None]] = None

    # ── Event loop thread ─────────────────────────────────────────────────────

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro):
        """Submit a coroutine to the BLE event loop and block until done."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=30)

    def _run_nowait(self, coro):
        """Submit a coroutine to the BLE event loop without waiting."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── Scan ──────────────────────────────────────────────────────────────────

    def scan(self, timeout: float = 5.0,
             name_filter: Optional[str] = DEFAULT_DEVICE_NAME) -> List[FoundDevice]:
        """
        Scan for BLE devices. Returns a list of FoundDevice objects.
        name_filter: if set, only return devices whose name contains this string.
                     Pass None to return all BLE devices in range.
        """
        return self._run(self._async_scan(timeout, name_filter))

    async def _async_scan(self, timeout: float,
                          name_filter: Optional[str]) -> List[FoundDevice]:
        log.info("BLE scan (%.1fs, filter=%r)…", timeout, name_filter)
        found = []
        devices = await BleakScanner.discover(timeout=timeout,
                                              return_adv=True)
        for dev, adv in devices.values():
            name = dev.name or adv.local_name or ""
            if name_filter and name_filter.lower() not in name.lower():
                continue
            fd = FoundDevice(
                name    = name or "(no name)",
                address = dev.address,
                rssi    = adv.rssi or 0,
                device  = dev,
            )
            found.append(fd)
            log.info("  Found: %s", fd)
        log.info("Scan complete — %d device(s) found", len(found))
        return found

    # ── Connect ───────────────────────────────────────────────────────────────

    def connect(self, device: FoundDevice) -> bool:
        """Connect to a discovered device. Returns True on success."""
        return self._run(self._async_connect(device))

    async def _async_connect(self, device: FoundDevice) -> bool:
        if self._connected:
            log.warning("Already connected — disconnect first")
            return False

        log.info("Connecting to %s…", device)
        try:
            self._client = BleakClient(
                device.device,
                disconnected_callback=self._on_disconnected,
            )
            await self._client.connect(timeout=10.0)

            # Validate the service is present
            svcs = self._client.services
            svc = svcs.get_service(BLE_SERVICE_UUID)
            if svc is None:
                log.error("Service 0xABF0 not found — wrong device?")
                await self._client.disconnect()
                return False

            # Subscribe to data notifications
            await self._client.start_notify(
                BLE_CHAR_DATA_NOTIFY, self._on_data_notify)

            # Subscribe to status/command notifications
            await self._client.start_notify(
                BLE_CHAR_CMD_NOTIFY, self._on_status_notify)

            self._connected = True
            self._device    = device
            log.info("Connected to %s (MTU=%d)",
                     device.name, self._client.mtu_size)

            if self.on_connect:
                self.on_connect(device)
            return True

        except Exception as e:
            log.error("Connection failed: %s", e)
            self._client = None
            return False

    # ── Disconnect ────────────────────────────────────────────────────────────

    def disconnect(self):
        """Cleanly disconnect from the bridge."""
        self._run(self._async_disconnect())

    async def _async_disconnect(self):
        if not self._client:
            return
        try:
            await self._client.disconnect()
        except Exception as e:
            log.warning("Disconnect error (ignored): %s", e)
        finally:
            self._connected = False
            self._client    = None
            self._device    = None
            self._split_buf.clear()
            self._split_header = None
            log.info("Disconnected")

    def _on_disconnected(self, client: BleakClient):
        """Called by bleak when the connection drops unexpectedly."""
        log.warning("BLE connection lost")
        self._connected = False
        self._client    = None
        self._device    = None
        self._split_buf.clear()
        self._split_header = None
        if self.on_disconnect:
            self.on_disconnect()

    # ── Send ──────────────────────────────────────────────────────────────────

    def send(self, tx_id: int, rx_id: int, payload: bytes):
        """
        Send a UDS frame to the bridge.
        tx_id: CAN ID we send TO the ECU (e.g. 0x7E0)
        rx_id: CAN ID we expect the ECU to respond FROM (e.g. 0x7E8)
        payload: raw UDS frame bytes (before ISO-TP framing — bridge handles that)
        """
        if not self._connected or not self._client:
            raise ConnectionError("Not connected to BLE bridge")
        self._run_nowait(self._async_send(tx_id, rx_id, payload))

    async def _async_send(self, tx_id: int, rx_id: int, payload: bytes):
        packet = _pack_header(tx_id, rx_id, payload)
        mtu    = self._client.mtu_size - 3   # ATT overhead

        if len(packet) <= mtu:
            await self._client.write_gatt_char(
                BLE_CHAR_DATA_RECV, packet, response=False)
        else:
            # Split across MTU-sized chunks — set SPLIT flag on first chunk
            first  = bytearray(packet[:mtu])
            first[1] |= BLE_COMMAND_FLAG_SPLIT  # set split flag in cmdFlags
            await self._client.write_gatt_char(
                BLE_CHAR_DATA_RECV, bytes(first), response=False)

            offset = mtu
            chunk_num = 1
            while offset < len(packet):
                chunk_data = packet[offset:offset + mtu - 2]
                chunk = bytes([BLE_PARTIAL_ID, chunk_num]) + chunk_data
                await self._client.write_gatt_char(
                    BLE_CHAR_DATA_RECV, chunk, response=False)
                offset += len(chunk_data)
                chunk_num += 1

    # ── Receive ───────────────────────────────────────────────────────────────

    def _on_data_notify(self, _char, data: bytearray):
        """Called by bleak for each DATA_NOTIFY packet from the bridge."""
        data = bytes(data)

        if not data:
            return

        hd_id = data[0]

        # Continuation chunk for a split packet
        if hd_id == BLE_PARTIAL_ID:
            if len(data) > 2:
                self._split_buf.extend(data[2:])
            # Reassembly complete when no more split chunks expected —
            # We detect end by trying to parse: if we have a full header + payload, dispatch.
            self._try_dispatch_split()
            return

        # Normal or first-of-split packet
        parsed = _unpack_header(data)
        if parsed is None:
            log.warning("Malformed BLE packet: %s", data.hex())
            return

        hd_id, flags, rx_id, tx_id, payload = parsed

        if flags & BLE_COMMAND_FLAG_SPLIT:
            # First chunk of a multi-chunk packet — store header and payload so far
            self._split_header = (rx_id, tx_id)
            self._split_buf    = bytearray(payload)
            return

        # Complete single packet — dispatch immediately
        self._dispatch(rx_id, tx_id, payload)

    def _try_dispatch_split(self):
        """Attempt to dispatch a reassembled split packet."""
        if self._split_header is None:
            return
        rx_id, tx_id = self._split_header
        # For split packets the firmware sends all payload in the continuation
        # chunks after the header chunk. Treat the accumulated buffer as the full payload.
        self._dispatch(rx_id, tx_id, bytes(self._split_buf))
        self._split_buf.clear()
        self._split_header = None

    def _dispatch(self, rx_id: int, tx_id: int, payload: bytes):
        """Route a complete payload to the right queue or sniff callback."""
        # Raw sniff frames
        if rx_id == BLE_RAW_SNIFF_ID or tx_id == BLE_RAW_SNIFF_ID:
            if self.on_sniff_frame and len(payload) >= 3:
                can_id = (payload[0] << 8) | payload[1]
                frame  = payload[3:]
                self.on_sniff_frame(can_id, frame)
            return

        # UDS response — rx_id is what the ECU sent FROM, which corresponds to
        # the rx_id the connection registered with.
        key = (tx_id, rx_id)
        with self._rx_lock:
            q = self._rx_queues.get(key)
        if q is None:
            # Try reversed in case rx/tx are swapped in the response
            key2 = (rx_id, tx_id)
            with self._rx_lock:
                q = self._rx_queues.get(key2)

        if q is not None:
            asyncio.run_coroutine_threadsafe(q.put(payload), self._loop)
        else:
            log.debug("No queue for CAN IDs tx=%#x rx=%#x — dropped", tx_id, rx_id)

    def _on_status_notify(self, _char, data: bytearray):
        """Status/command notifications from bridge (informational)."""
        log.debug("Bridge status: %s", bytes(data).hex())

    # ── Queue registration (used by BLEBridgeConnection) ─────────────────────

    def register_channel(self, tx_id: int, rx_id: int) -> asyncio.Queue:
        q = asyncio.Queue()
        with self._rx_lock:
            self._rx_queues[(tx_id, rx_id)] = q
        return q

    def unregister_channel(self, tx_id: int, rx_id: int):
        with self._rx_lock:
            self._rx_queues.pop((tx_id, rx_id), None)

    # ── Settings commands ─────────────────────────────────────────────────────

    def set_stmin(self, stmin_ms: int):
        """Set ISO-TP STmin on the bridge (0–127 ms)."""
        self._send_setting(BRG_SETTING_ISOTP_STMIN, stmin_ms)

    def set_raw_sniff(self, enabled: bool):
        """Enable/disable raw CAN sniff mode (all frames forwarded with ID 0xCAFE)."""
        self._send_setting(BRG_SETTING_RAW_SNIFF, 1 if enabled else 0)

    def set_led_color(self, r: int, g: int, b: int):
        """Set the WS2812 LED color on the bridge."""
        payload = bytes([BRG_SETTING_LED_COLOR, r, g, b])
        self._run_nowait(self._async_write_cmd(payload))

    def _send_setting(self, setting_id: int, value: int):
        payload = struct.pack("<BB", setting_id, value)
        self._run_nowait(self._async_write_cmd(payload))

    async def _async_write_cmd(self, payload: bytes):
        if self._client and self._connected:
            await self._client.write_gatt_char(
                BLE_CHAR_CMD_RECV, payload, response=False)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def connected_device(self) -> Optional[FoundDevice]:
        return self._device

    @property
    def mtu(self) -> int:
        if self._client:
            return self._client.mtu_size
        return 23   # BLE default


# ── udsoncan-compatible connection class ──────────────────────────────────────

class BLEBridgeConnection:
    """
    Wraps BLEBridge as a udsoncan connection object.

    This is what _make_connection() returns when interface="BLE".
    udsoncan calls open(), send(), wait_frame(), close() on this.

    Usage:
        conn = BLEBridgeConnection(bridge, tx_id=0x7E0, rx_id=0x7E8)
        # then pass conn to udsoncan.Client(conn, ...)
    """

    def __init__(self, bridge: BLEBridge, tx_id: int, rx_id: int,
                 timeout: float = 2.0):
        self._bridge  = bridge
        self._tx_id   = tx_id
        self._rx_id   = rx_id
        self._timeout = timeout
        self._queue:  Optional[asyncio.Queue] = None

    # ── udsoncan connection interface ─────────────────────────────────────────

    def open(self):
        if not self._bridge.is_connected:
            raise ConnectionError(
                "BLE bridge is not connected. Use BLEBridge.connect() first.")
        self._queue = self._bridge.register_channel(self._tx_id, self._rx_id)
        log.debug("BLEBridgeConnection open: tx=%#x rx=%#x", self._tx_id, self._rx_id)

    def close(self):
        if self._queue is not None:
            self._bridge.unregister_channel(self._tx_id, self._rx_id)
            self._queue = None
        log.debug("BLEBridgeConnection closed")

    def send(self, payload: bytes):
        """Send a UDS request payload to the ECU via the bridge."""
        self._bridge.send(self._tx_id, self._rx_id, payload)

    def wait_frame(self, timeout: Optional[float] = None) -> bytes:
        """
        Block until a UDS response frame arrives or timeout expires.
        Raises TimeoutError on timeout (udsoncan catches this).
        """
        if self._queue is None:
            raise ConnectionError("Connection not open")

        deadline = time.monotonic() + (timeout or self._timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"No response from ECU (tx={self._tx_id:#x} rx={self._rx_id:#x})")
            try:
                # Poll the asyncio queue from a sync thread
                fut = asyncio.run_coroutine_threadsafe(
                    asyncio.wait_for(self._queue.get(),
                                     timeout=min(remaining, 0.1)),
                    self._bridge._loop)
                return fut.result(timeout=min(remaining + 0.5, 5.0))
            except (asyncio.TimeoutError, TimeoutError):
                continue
            except Exception as e:
                raise TimeoutError(f"BLE receive error: {e}") from e

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ── Convenience ──────────────────────────────────────────────────────────

    def set_timeout(self, timeout: float):
        self._timeout = timeout
