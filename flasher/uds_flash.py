"""
flasher/uds_flash.py — UDS flash layer for the Simos tuning suite

Handles the full UDS programming sequence:
  1. Extended diagnostic session
  2. SA2 seed/key security access (using sa2_seed_key bytecode interpreter)
  3. Block erase (RoutineControl 0xFF00)
  4. RequestDownload + TransferData + RequestTransferExit
  5. Checksum verification routine (0xFF01)

Supports Simos8.5 (XOR crypto) and Simos12/18 (AES) through the ECUDef abstraction.
Interface: J2534 (BridgeLEG ESP32) or SocketCAN.

Designed to be used by both the GUI and CLI.
"""

from __future__ import annotations

import logging
import time
import struct
from typing import Optional, Callable, Dict
from dataclasses import dataclass

import udsoncan
from udsoncan.client import Client
from udsoncan import services, configs, exceptions

from sa2_seed_key.sa2_seed_key import Sa2SeedKey

from core.ecu_defs import ECUDef, CryptoType, BlockDef, SIMOS85

log = logging.getLogger("SimosSuite.Flash")

# ─── Progress callback contract ──────────────────────────────────────────────

@dataclass
class FlashProgress:
    step:     str    # "CONNECT" | "ERASE" | "TRANSFER" | "VERIFY" | "DONE" | "ERROR"
    message:  str
    pct:      int    # 0–100
    block:    Optional[str] = None

ProgressCallback = Callable[[FlashProgress], None]


def _noop(p: FlashProgress):
    log.info(f"[{p.step}] {p.message} ({p.pct}%)")


# ─── Connection setup ─────────────────────────────────────────────────────────

def _make_connection(ecu: ECUDef, interface: str, interface_path: Optional[str] = None,
                     st_min_us: int = 350_000, ble_bridge=None):
    """
    Create a udsoncan connection for the given interface.

    interface options:
        "BLE"            — ESP32 BLE bridge (pass ble_bridge= a connected BLEBridge instance)
        "J2534"          — J2534 DLL (Tactrix, VNCI, etc.)
        "SocketCAN_can0" — Linux SocketCAN (replace can0 with your interface name)
    """
    params = {"tx_padding": 0x55}

    if interface.upper() == "BLE":
        # ESP32 ISO-TP BLE bridge (dspl1236/esp32-isotp-ble-bridge-c7vag)
        # ble_bridge must be a connected BLEBridgeSync instance.
        # Device identifies by service UUID 0xABF0, GAP name "BLE_TO_ISOTP20".
        # Packet framing: 8-byte header (0xF1 + flags + rxID + txID + size).
        if ble_bridge is None:
            raise ValueError(
                "interface='BLE' requires ble_bridge= a connected BLEBridgeSync. "
                "Example:\n"
                "    from transport.ble_bridge import BLEBridgeSync\n"
                "    bridge = BLEBridgeSync()\n"
                "    bridge.connect(bridge.scan()[0])\n"
                "    conn = _make_connection(ecu, 'BLE', ble_bridge=bridge)"
            )
        from transport.ble_bridge import BLEBridgeConnection, SETTING_ISOTP_STMIN
        # Push STmin setting to firmware if non-zero
        if st_min_us > 0:
            stmin_ms = max(1, st_min_us // 1000)
            try:
                ble_bridge.send_settings(SETTING_ISOTP_STMIN,
                                         stmin_ms.to_bytes(2, "little"))
            except Exception:
                pass  # non-fatal — bridge uses its stored default
        return BLEBridgeConnection(
            bridge  = ble_bridge._bridge,
            tx_id   = ecu.can_tx,
            rx_id   = ecu.can_rx,
            timeout = 5.0,
        )

    elif interface.upper().startswith("SOCKETCAN"):
        from udsoncan.connections import IsoTPSocketConnection
        iface = interface_path or interface.split("_", 1)[-1]
        conn = IsoTPSocketConnection(iface, rxid=ecu.can_rx, txid=ecu.can_tx, params=params)
        conn.tpsock.set_opts(txpad=0x55, tx_stmin=st_min_us)
        return conn

    elif interface.upper() == "MOCK":
        # Virtual mock connection — Simos8.5 simulation, no hardware needed
        # _install_mock_patch already called by interface_panel before connect
        from tests.mock_connection import MockConnection, MockECU
        conn = MockConnection(MockECU.SIMOS85)
        conn.open()
        return conn

    elif interface.upper() == "J2534":
        # J2534 PassThru DLL — Windows only, 32-bit DLL
        #
        # Tested hardware (in order of recommendation):
        #   Tactrix OpenPort 2.0  — most reliable for large block flashes
        #   Mongoose J2534        — legacy Drew Tech / Bosch cable, works fine
        #                           DLL: C:/Program Files (x86)/Drew Technologies, Inc/Mongoose/monj2534.dll
        #   VNCI 6154A            — clone ODIS cable, reliable for UDS/read
        #                           DLL: C:/Program Files (x86)/OpenShell/vcdc.dll
        #
        # interface_path: full path to the J2534 DLL.
        # If None, uses the Tactrix default (same as VW_Flash default).
        # Use transport.interfaces.detect_j2534_dll() to auto-detect.
        from lib.connections.j2534_connection import J2534Connection
        import math
        def _us_to_stmin(us):
            if us > 1_000_000:
                return math.ceil(us / 1_000_000)
            return 0xF0 + math.ceil(us / 100_000)

        dll = interface_path or (
            "C:/Program Files (x86)/OpenECU/OpenPort 2.0/drivers/openport 2.0/op20pt32.dll"
        )
        return J2534Connection(
            windll=dll,
            rxid=ecu.can_rx,
            txid=ecu.can_tx,
            st_min=_us_to_stmin(st_min_us),
        )

    elif interface.upper().startswith("USBISOTP"):
        # ESP32 ISO-TP bridge over USB serial (same hardware as BLE mode,
        # but plugged in via USB-C cable instead of Bluetooth).
        # Packet format is identical to BLE (0xF1 header + rxID/txID/size/payload)
        # but transported over pyserial at 250000 baud instead of GATT.
        #
        # interface_path: serial port, e.g. "COM3" (Windows) or "/dev/ttyUSB0" (Linux)
        # If not provided, uses the port suffix from interface string,
        # e.g. "USBISOTP_COM3" → COM3
        #
        # Note: DTR/RTS must NOT be toggled on connect — the firmware comment
        # in usb_isotp_connection.py explains this avoids putting the ESP32
        # into programming mode. The connection class handles this automatically.
        from lib.connections.usb_isotp_connection import USBISOTPConnection

        if interface_path:
            port = interface_path
        elif "_" in interface:
            port = interface.split("_", 1)[1]
        else:
            raise ValueError(
                "USBISOTP requires a port. "
                "Use interface='USBISOTP_COM3' or interface_path='COM3'."
            )

        return USBISOTPConnection(
            interface_name = port,
            rxid           = ecu.can_rx,
            txid           = ecu.can_tx,
            tx_stmin       = max(1, st_min_us // 1000),   # convert µs → ms
        )

    else:
        raise ValueError(
            f"Unknown interface: '{interface}'. "
            f"Use 'BLE', 'USBISOTP_COM3', 'J2534', or 'SocketCAN_can0'."
        )


# ─── Security access ─────────────────────────────────────────────────────────

def _make_security_algo(sa2_script: bytes):
    """Build a udsoncan-compatible security_algo function from an SA2 script."""
    def algo(level: int, seed: bytes, params=None) -> bytes:
        seed_int = int.from_bytes(seed, "big")
        key_int  = Sa2SeedKey(sa2_script, seed_int).execute()
        return key_int.to_bytes(4, "big")
    return algo


# ─── Block crypto ────────────────────────────────────────────────────────────

def _prepare_block_data(ecu: ECUDef, block_num: int, raw_bytes: bytes) -> bytes:
    """
    Encrypt a block for transmission.
    Simos8: XOR counter (in-place, symmetric).
    Simos12/18: AES-CBC.
    """
    if ecu.crypto == CryptoType.XOR_COUNTER:
        return ecu.xor_encrypt(raw_bytes)
    elif ecu.crypto == CryptoType.AES_CBC:
        from Crypto.Cipher import AES
        cipher = AES.new(ecu.crypto_key, AES.MODE_CBC, ecu.crypto_iv)
        # Pad to 16-byte boundary
        pad = (16 - len(raw_bytes) % 16) % 16
        padded = raw_bytes + b"\x00" * pad
        return cipher.encrypt(padded)
    else:
        return raw_bytes


# ─── Main flash routine ───────────────────────────────────────────────────────

def flash_cal(
    ecu:            ECUDef,
    cal_bytes:      bytes,
    interface:      str = "J2534",
    interface_path: Optional[str] = None,
    callback:       ProgressCallback = _noop,
    dry_run:        bool = False,
    verify_only:    bool = False,
) -> bool:
    """
    Flash the CAL block to the ECU.

    Args:
        ecu:            ECUDef (use SIMOS85 for the 3.0T)
        cal_bytes:      Raw, already-checksummed CAL bytes (decrypted)
        interface:      "J2534" or "SocketCAN_can0"
        interface_path: Path to J2534 DLL or SocketCAN interface name
        callback:       Progress callback
        dry_run:        If True, go through the motions but don't actually write
        verify_only:    Only verify checksums, don't flash

    Returns:
        True on success.
    """
    cal_block = ecu.cal_block
    if cal_block is None:
        raise ValueError(f"{ecu.name}: no CAL block defined")

    if len(cal_bytes) < cal_block.length:
        raise ValueError(
            f"CAL too short: {len(cal_bytes)} bytes, expected {cal_block.length:#x}")

    callback(FlashProgress("CONNECT", f"Connecting to {ecu.name}…", 0))

    conn = _make_connection(ecu, interface, interface_path)

    cfg = dict(configs.default_client_config)
    cfg["security_algo"]     = _make_security_algo(ecu.sa2_script)
    cfg["security_algo_params"] = None
    cfg["data_identifiers"]  = {}  # we don't need DID codecs for flashing
    cfg["request_timeout"]   = 30
    cfg["p2_timeout"]        = 30
    cfg["p2_star_timeout"]   = 30

    with Client(conn, request_timeout=30, config=cfg) as client:

        # ── 1. Extended diagnostic session ───────────────────────────────────
        callback(FlashProgress("CONNECT", "Opening extended session…", 5))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Session refused: {e}", 0))
            return False

        try:
            client.session_timing["p2_server_max"] = 30
        except TypeError:
            client.session_timing.p2_server_max = 30

        # ── Read VIN for confirmation ─────────────────────────────────────────
        vin = "UNKNOWN"
        try:
            class _StrCodec(udsoncan.DidCodec):
                def encode(self, v): return bytes(v)
                def decode(self, p): return p.decode("ascii", errors="replace")
                def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData
            client.config["data_identifiers"][0xF190] = _StrCodec
            vin = client.read_data_by_identifier_first(0xF190)
        except Exception:
            pass
        callback(FlashProgress("CONNECT", f"VIN: {vin}", 10))

        if verify_only:
            return _verify_checksum(client, cal_block, callback)

        # ── 2. Programming session + security access ──────────────────────────
        callback(FlashProgress("CONNECT", "Entering programming session…", 15))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.programmingSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Programming session refused: {e}", 0))
            return False

        callback(FlashProgress("CONNECT", "Security access (SA2)…", 20))
        try:
            client.unlock_security_access(0x11)   # Level 0x11 for Simos programming
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Security access denied: {e}", 0))
            return False

        # ── 3. Erase CAL block ────────────────────────────────────────────────
        callback(FlashProgress("ERASE", f"Erasing CAL block {cal_block.number}…", 25))
        if not dry_run:
            try:
                client.start_routine(
                    udsoncan.Routine.EraseMemory,
                    data=bytes([0x01, cal_block.number])
                )
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"Erase failed: {e}", 0))
                return False
        else:
            log.info("[DRY RUN] Would erase block %d", cal_block.number)

        # ── 4. RequestDownload ────────────────────────────────────────────────
        encrypted = _prepare_block_data(ecu, cal_block.number, cal_bytes)
        total     = len(encrypted)

        callback(FlashProgress("TRANSFER", "Requesting download…", 30, "CAL"))
        if not dry_run:
            try:
                dfi = udsoncan.DataFormatIdentifier(
                    compression=0xA if ecu.crypto != CryptoType.XOR_COUNTER else 0x0,
                    encryption=0xA  if ecu.crypto != CryptoType.XOR_COUNTER else 0x0,
                )
                mem_loc = udsoncan.MemoryLocation(
                    address=cal_block.base_addr,
                    memorysize=total,
                    address_format=32,
                    memorysize_format=32,
                )
                resp = client.request_download(mem_loc, dfi)
                max_block = resp.service_data.max_length
            except Exception as e:
                callback(FlashProgress("ERROR", f"RequestDownload failed: {e}", 0))
                return False
        else:
            max_block = 0xFFD
            log.info("[DRY RUN] Would RequestDownload %d bytes to %#010x",
                     total, cal_block.base_addr)

        # ── 5. TransferData ───────────────────────────────────────────────────
        block_size = max_block - 2   # minus 1 byte SID + 1 byte block counter
        counter    = 1
        offset     = 0
        sent       = 0

        while offset < total:
            chunk = encrypted[offset:offset + block_size]
            pct   = 30 + int(60 * offset / total)
            callback(FlashProgress(
                "TRANSFER",
                f"Writing {offset:#08x}/{total:#08x} ({pct}%)",
                pct, "CAL"))

            if not dry_run:
                try:
                    client.transfer_data(counter, chunk)
                except exceptions.NegativeResponseException as e:
                    callback(FlashProgress("ERROR",
                                           f"TransferData failed at {offset:#x}: {e}", 0))
                    return False

            offset  += len(chunk)
            sent    += len(chunk)
            counter  = (counter + 1) & 0xFF
            if counter == 0:
                counter = 1

        # ── 6. RequestTransferExit ────────────────────────────────────────────
        callback(FlashProgress("TRANSFER", "Transfer complete, exiting…", 92, "CAL"))
        if not dry_run:
            try:
                client.request_transfer_exit()
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"TransferExit failed: {e}", 0))
                return False

        # ── 7. Verify checksum ────────────────────────────────────────────────
        if not dry_run:
            ok = _verify_checksum(client, cal_block, callback)
            if not ok:
                return False

        # ── Done ──────────────────────────────────────────────────────────────
        callback(FlashProgress("DONE",
                               f"CAL block flashed successfully to {vin}", 100, "CAL"))
        log.info("Flash complete — VIN %s, block %d, %d bytes",
                 vin, cal_block.number, total)
        return True


def _verify_checksum(client: Client, block: BlockDef,
                     callback: ProgressCallback) -> bool:
    callback(FlashProgress("VERIFY", "Running checksum verification routine…", 95))
    try:
        client.start_routine(
            udsoncan.Routine.CheckProgrammingDependencies,  # 0xFF01
            data=bytes([0x01, block.number])
        )
        callback(FlashProgress("VERIFY", "Checksum OK", 98))
        return True
    except exceptions.NegativeResponseException as e:
        callback(FlashProgress("ERROR", f"Checksum verification failed: {e}", 0))
        return False


# ─── Read ECU info ────────────────────────────────────────────────────────────

def read_ecu_info(
    ecu:            ECUDef,
    interface:      str = "J2534",
    interface_path: Optional[str] = None,
) -> Dict[str, str]:
    """
    Connect and read the standard VW identification DIDs.
    Returns a dict of {description: value}.
    """
    conn = _make_connection(ecu, interface, interface_path)

    class _StrCodec(udsoncan.DidCodec):
        def encode(self, v): return bytes(v)
        def decode(self, p):
            # Try ASCII for string DIDs (VIN, part numbers etc)
            try:
                s = p.decode("ascii").strip("\x00 \t\r\n")
                if s and all(32 <= ord(c) < 127 for c in s):
                    return s
            except Exception:
                pass
            if len(p) == 1:
                return str(p[0])
            if len(p) <= 4:
                return str(int.from_bytes(p, "big"))
            return p.hex().upper()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    # DID-specific post-processing for known numeric DIDs
    DID_SCALE = {
        0xF442: lambda v: f"{int(v)/1000:.3f} V",     # Module voltage mV→V
        0x0407: lambda v: str(int(v)),                  # Program attempts
        0x0408: lambda v: str(int(v)),                  # Successful programs
        0x295A: lambda v: f"{int(v):,} km",             # Vehicle mileage
        0x295B: lambda v: f"{int(v):,} km",             # Module mileage
        0xF186: lambda v: {1:"default",3:"extended",
                           2:"programming"}.get(int(v), str(v)),  # Active session
    }

    # Per-DID codecs — handle binary/numeric DIDs correctly
    class _VoltageCodec(udsoncan.DidCodec):
        """0xF442 — uint16 big-endian, 0.001V per bit"""
        def encode(self, v): return bytes(v)
        def decode(self, p):
            if len(p) >= 2:
                return f"{int.from_bytes(p[:2], 'big') / 1000:.3f} V"
            return p.hex()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    class _MileageCodec(udsoncan.DidCodec):
        """0x295A/0x295B — uint32 big-endian, km"""
        def encode(self, v): return bytes(v)
        def decode(self, p):
            if len(p) >= 4:
                km = int.from_bytes(p[:4], 'big')
                return f"{km:,} km"
            if len(p) >= 2:
                return f"{int.from_bytes(p, 'big'):,} km"
            return p.hex()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    class _ByteCodec(udsoncan.DidCodec):
        """Single-byte numeric DIDs"""
        def encode(self, v): return bytes(v)
        def decode(self, p): return str(p[0]) if p else "0"
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    class _CounterCodec(udsoncan.DidCodec):
        """Program attempt counters — uint16 or uint32"""
        def encode(self, v): return bytes(v)
        def decode(self, p):
            if len(p) == 1: return str(p[0])
            if len(p) == 2: return str(int.from_bytes(p, 'big'))
            if len(p) >= 4: return str(int.from_bytes(p[:4], 'big'))
            return str(int.from_bytes(p, 'big'))
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    did_codecs = {did: _StrCodec for did in ecu.info_dids}
    # Override with specific codecs
    did_codecs[0xF442] = _VoltageCodec
    did_codecs[0x295A] = _MileageCodec
    did_codecs[0x295B] = _MileageCodec
    did_codecs[0x0405] = _ByteCodec
    did_codecs[0x0407] = _CounterCodec
    did_codecs[0x0408] = _CounterCodec
    did_codecs[0xF186] = _ByteCodec

    DID_LABELS = {
        0xF190: "VIN",
        0xF18C: "ECU Serial",
        0xF187: "Part Number",
        0xF189: "SW Version",
        0xF191: "HW Number",
        0xF1A3: "HW Version",
        0xF197: "System Name",
        0xF1AD: "Engine Code",
        0xF17C: "FAZIT",
        0xF19E: "ASAM File ID",
        0xF1A2: "ASAM File Version",
        0x0405: "Flash State",
        0x0407: "Program Attempts",
        0x0408: "Successful Programs",
        0xF186: "Active Session",
        0xF442: "Module Voltage",
        0x295A: "Vehicle Mileage",
        0x295B: "Module Mileage",
    }

    cfg = dict(configs.default_client_config)
    cfg["data_identifiers"] = did_codecs
    cfg["request_timeout"]  = 10

    result = {}
    with Client(conn, request_timeout=10, config=cfg) as client:
        client.change_session(
            services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        try:
            client.session_timing["p2_server_max"] = 30
        except TypeError:
            client.session_timing.p2_server_max = 30
        client.config["request_timeout"] = 30

        for did in ecu.info_dids:
            label = DID_LABELS.get(did, f"DID_{did:04X}")
            try:
                val = client.read_data_by_identifier_first(did)
                # Apply known scaling for numeric DIDs
                if did in DID_SCALE:
                    try:
                        result[label] = DID_SCALE[did](val)
                    except Exception:
                        result[label] = str(val)
                else:
                    result[label] = str(val)
            except Exception as e:
                result[label] = f"<{type(e).__name__}>"

    return result
