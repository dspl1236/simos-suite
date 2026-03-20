"""
transport/ws_bridge.py — WebSocket ISO-TP bridge transport

Connects to FunkBridge WiFi firmware (AP or Station mode) over WebSocket.
Implements the same connection interface as BLEBridgeConnection so the
rest of the suite works identically regardless of transport.

Frame format: identical to BLE transport
    [0]     0xF1    hdID
    [1]     flags   cmdFlags
    [2-3]   rxID    LE uint16
    [4-5]   txID    LE uint16
    [6-7]   size    LE uint16
    [8...]  payload ISO-TP bytes

Usage:
    bridge = WSBridge("ws://funkbridge.local/ws")
    bridge.connect()
    conn = bridge.make_connection(rx_id=0x77A, tx_id=0x710)
    # conn → pass to udsoncan.Client
    bridge.disconnect()
"""

from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from typing import Optional

try:
    import websocket  # websocket-client
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

log = logging.getLogger(__name__)

BLE_HEADER_ID  = 0xF1
BLE_HEADER_SZ  = 8
DEFAULT_TIMEOUT = 5.0

# Default URLs to try when auto-detecting FunkBridge
FUNKBRIDGE_URLS = [
    "ws://funkbridge.local/ws",
    "ws://192.168.4.1/ws",
]


def ws_available() -> bool:
    return _WS_AVAILABLE


def detect_funkbridge_url(timeout: float = 2.0) -> Optional[str]:
    """
    Try each default URL and return the first one that responds.
    Returns None if none are reachable.
    """
    if not _WS_AVAILABLE:
        return None
    import socket
    for url in FUNKBRIDGE_URLS:
        # Quick TCP reachability check before WebSocket handshake
        host = url.split("//")[1].split("/")[0]
        port = 80
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            log.debug("FunkBridge reachable at %s", url)
            return url
        except (OSError, socket.timeout):
            continue
    return None


class WSBridgeConnection:
    """
    udsoncan-compatible connection over WebSocket.
    Drop-in replacement for BLEBridgeConnection.
    """

    def __init__(self, bridge: "WSBridge", rx_id: int, tx_id: int,
                 timeout: float = DEFAULT_TIMEOUT):
        self._bridge  = bridge
        self._rx_id   = rx_id
        self._tx_id   = tx_id
        self._timeout = timeout
        self._queue: Optional[queue.Queue] = None

    def open(self):
        self._queue = self._bridge.register_channel(self._tx_id, self._rx_id)

    def close(self):
        self._bridge.unregister_channel(self._tx_id, self._rx_id)
        self._queue = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

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
                f"No WebSocket response within {t}s "
                f"(tx={self._tx_id:#06x} rx={self._rx_id:#06x})"
            )
        if frame is None:
            raise ConnectionError("WebSocket bridge disconnected")
        return frame

    def empty(self) -> bool:
        return self._queue is None or self._queue.empty()


class WSBridge:
    """
    Synchronous WebSocket bridge to FunkBridge WiFi firmware.

    Thread-safe: receive loop runs in a background thread.
    All public methods are safe to call from any thread.
    """

    def __init__(self, url: Optional[str] = None):
        self._url        = url or FUNKBRIDGE_URLS[0]
        self._ws         = None
        self._ws_thread  = None
        self._connected  = False
        self._rx_queues: dict[tuple, queue.Queue] = {}
        self._raw_queue: Optional[queue.Queue]    = None
        self._lock       = threading.Lock()

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self, timeout: float = 8.0) -> bool:
        if not _WS_AVAILABLE:
            raise ImportError(
                "websocket-client not installed. "
                "Run: pip install websocket-client"
            )
        if self._connected:
            return True

        ready_event = threading.Event()
        error: list[Exception] = []

        def on_open(ws):
            self._connected = True
            log.info("WSBridge connected: %s", self._url)
            ready_event.set()

        def on_message(ws, data):
            if isinstance(data, str):
                return  # ignore text frames
            self._on_binary(bytes(data))

        def on_error(ws, exc):
            log.error("WSBridge error: %s", exc)
            if not ready_event.is_set():
                error.append(exc)
                ready_event.set()

        def on_close(ws, code, reason):
            self._connected = False
            log.info("WSBridge closed: %s %s", code, reason)
            # Unblock any waiting readers
            with self._lock:
                for q in self._rx_queues.values():
                    q.put(None)

        import websocket as ws_mod
        self._ws = ws_mod.WebSocketApp(
            self._url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        self._ws_thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
            name="ws-bridge-recv"
        )
        self._ws_thread.start()

        ready_event.wait(timeout=timeout)
        if error:
            raise ConnectionError(f"WebSocket connection failed: {error[0]}")
        if not self._connected:
            raise TimeoutError(f"WebSocket connect timeout ({timeout}s): {self._url}")
        return True

    def disconnect(self):
        if self._ws:
            self._ws.close()
        self._connected = False

    def _on_binary(self, raw: bytes):
        offset = 0
        while offset < len(raw):
            if raw[offset] != BLE_HEADER_ID:
                offset += 1
                continue
            if offset + BLE_HEADER_SZ > len(raw):
                break
            hd_id, flags, rx_id, tx_id, size = struct.unpack_from(
                "<BBHHH", raw, offset
            )
            offset += BLE_HEADER_SZ
            if offset + size > len(raw):
                break
            payload = raw[offset: offset + size]
            offset += size
            self._dispatch(tx_id, rx_id, payload)

    def _dispatch(self, tx_id: int, rx_id: int, payload: bytes):
        key = (tx_id, rx_id)
        with self._lock:
            if key in self._rx_queues:
                self._rx_queues[key].put(payload)
            if self._raw_queue is not None:
                self._raw_queue.put((tx_id, rx_id, payload))

    def send_frame(self, tx_id: int, rx_id: int, payload: bytes, flags: int = 0):
        if not self._connected or not self._ws:
            raise ConnectionError("WebSocket not connected")
        header = struct.pack("<BBHHH",
            BLE_HEADER_ID, flags, rx_id, tx_id, len(payload))
        self._ws.send_bytes(header + payload)
        log.debug("WS TX tx=%#06x rx=%#06x len=%d", tx_id, rx_id, len(payload))

    def make_connection(self, rx_id: int, tx_id: int,
                         timeout: float = DEFAULT_TIMEOUT) -> WSBridgeConnection:
        if not self._connected:
            raise ConnectionError("WSBridge not connected")
        conn = WSBridgeConnection(self, rx_id, tx_id, timeout)
        conn.open()
        return conn

    def register_channel(self, tx_id: int, rx_id: int) -> queue.Queue:
        key = (tx_id, rx_id)
        with self._lock:
            if key not in self._rx_queues:
                self._rx_queues[key] = queue.Queue(maxsize=256)
            return self._rx_queues[key]

    def unregister_channel(self, tx_id: int, rx_id: int):
        with self._lock:
            self._rx_queues.pop((tx_id, rx_id), None)

    def enable_raw_queue(self) -> queue.Queue:
        with self._lock:
            self._raw_queue = queue.Queue(maxsize=4096)
        return self._raw_queue

    def disable_raw_queue(self):
        with self._lock:
            self._raw_queue = None
