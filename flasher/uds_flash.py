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
from udsoncan.client import Client  # noqa: F401
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

def _prepare_block_data(ecu: ECUDef, block_num: int, raw_bytes: bytes,
                         asw1_bytes: bytes = None) -> bytes:
    """
    Prepare a block for UDS TransferData:
      1. Fix ECM3 checksum (CAL only, 64-bit summation)
      2. Fix CRC32 checksum (VW 0x4C11DB7 poly, all blocks)
      3. LZSS compress
      4. XOR encrypt (Simos8) or AES-CBC (Simos12/18)

    DataFormatIdentifier sent in RequestDownload: compression=0xA, encryption=0xA
    Both flags mean the same algorithm (LZSS then XOR for Simos8).
    """
    from flasher.checksum_simos import fix_crc32, fix_ecm3, xor_encrypt
    from flasher.lzss_compress  import lzss_compress

    data = raw_bytes

    # Step 1: Fix ECM3 checksum for CAL block
    if ecu.crypto == CryptoType.XOR_COUNTER and block_num == 3:
        try:
            data = fix_ecm3(data, asw1_bytes)
            log.debug("ECM3 checksum fixed for block %d", block_num)
        except Exception as e:
            log.warning("ECM3 fix failed (block %d): %s — continuing", block_num, e)

    # Step 2: Fix CRC32 checksum
    if block_num < 6:
        try:
            data = fix_crc32(data, block_num)
            log.debug("CRC32 fixed for block %d", block_num)
        except Exception as e:
            log.warning("CRC32 fix failed (block %d): %s — continuing", block_num, e)

    # Step 3: LZSS compress
    try:
        compressed = lzss_compress(data)
        log.debug("LZSS: block %d: %d -> %d bytes", block_num, len(data), len(compressed))
    except Exception as e:
        log.warning("LZSS failed (block %d): %s — sending uncompressed", block_num, e)
        compressed = data

    # Step 4: Encrypt
    if ecu.crypto == CryptoType.XOR_COUNTER:
        return xor_encrypt(compressed)
    elif ecu.crypto == CryptoType.AES_CBC:
        from Crypto.Cipher import AES
        pad = (16 - len(compressed) % 16) % 16
        padded = compressed + b"\x00" * pad
        cipher = AES.new(ecu.crypto_key, AES.MODE_CBC, ecu.crypto_iv)
        return cipher.encrypt(padded)
    else:
        return compressed


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

    # Clear DTCs via OBD2 before/after flash (VW_Flash pattern)
    # Uses OBD functional address 0x700 → 0x7E8 directly, not the ECU connection.
    # This matches VW_Flash's send_obd() — a separate short-lived connection
    # that avoids touching the main ECU channel.
    def _obd_clear(iface, ipath):
        try:
            if iface.upper() == "J2534":
                from lib.connections.j2534_connection import J2534Connection
                import math
                dll = ipath or (
                    "C:/Program Files (x86)/OpenECU/OpenPort 2.0/drivers/openport 2.0/op20pt32.dll"
                )
                c = J2534Connection(windll=dll, rxid=0x7E8, txid=0x700)
            elif iface.upper().startswith("SOCKETCAN"):
                from udsoncan.connections import IsoTPSocketConnection
                iface_name = ipath or iface.split("_", 1)[-1]
                c = IsoTPSocketConnection(iface_name, rxid=0x7E8, txid=0x700,
                                          params={"tx_padding": 0x55})
            else:
                return  # BLE/USB/mock — skip OBD clear
            c.open()
            c.specific_send(bytes([0x04]))
            try: c.specific_wait_frame(timeout=1.0)
            except Exception: pass
            try: c.specific_wait_frame(timeout=0.5)
            except Exception: pass
            c.close()
        except Exception as e:
            log.debug("OBD DTC clear: %s", e)
    _obd_clear(interface, interface_path)

    conn = _make_connection(ecu, interface, interface_path)

    # Check if adapter supports STMIN_TX — required for reliable multi-frame flash
    if hasattr(conn, 'stmin_tx_supported') and not conn.stmin_tx_supported:
        callback(FlashProgress(
            "ERROR",
            "This J2534 adapter does not support STMIN_TX timing control. "
            "Flash transfers will likely fail mid-block. "
            "Use a Tactrix OpenPort 2.0 or Switchleg ESP32 (BridgeLEG firmware) for flashing.",
            0,
        ))
        return False

    cfg = dict(configs.default_client_config)
    cfg["security_algo"]        = _make_security_algo(ecu.sa2_script)
    cfg["security_algo_params"] = None
    cfg["data_identifiers"]     = {}
    cfg["request_timeout"]      = 30

    with Client(conn, request_timeout=30, config=cfg) as client:

        def _st(t=30):
            try: client.session_timing["p2_server_max"] = t
            except TypeError: client.session_timing.p2_server_max = t
            client.config["request_timeout"] = t

        # 1. Extended session
        callback(FlashProgress("CONNECT", "Opening extended session…", 5))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Session refused: {e}", 0)); return False
        _st(30)

        # Read VIN
        vin = "UNKNOWN"
        try:
            class _SC(udsoncan.DidCodec):
                def encode(self, v): return bytes(v)
                def decode(self, p): return p.decode("ascii", errors="replace")
                def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData
            client.config["data_identifiers"][0xF190] = _SC
            vin = client.read_data_by_identifier_first(0xF190)
        except Exception: pass
        callback(FlashProgress("CONNECT", f"VIN: {vin}", 10))

        if verify_only:
            return _verify_checksum(client, cal_block, callback)

        # 2. Programming precondition check 0x0203
        try:
            client.start_routine(0x0203)
        except Exception as e:
            log.warning("Precondition 0x0203: %s (continuing)", e)
        client.tester_present()

        # 3. Programming session
        callback(FlashProgress("CONNECT", "Entering programming session…", 15))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.programmingSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Programming session refused: {e}", 0)); return False
        _st(30)
        client.tester_present()

        # 4. SA2 security access level 17
        callback(FlashProgress("CONNECT", "Security access SA2…", 20))
        try:
            client.unlock_security_access(0x11)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"SA2 denied: {e}", 0)); return False
        client.tester_present()

        # 5. Workshop code 0xF15A
        try:
            from flasher.workshop_code import build_workshop_code
            wc = build_workshop_code(cal_data=cal_bytes)
            client.write_data_by_identifier(0xF15A, wc)
        except Exception as e:
            log.debug("Workshop code: %s", e)
        client.tester_present()

        # 6–10. Erase / download / transfer / checksum — delegated to _flash_one_block
        ok = _flash_one_block(
            client     = client,
            ecu        = ecu,
            block_num  = cal_block.number,
            raw_bytes  = cal_bytes,
            asw1_bytes = None,   # CAL-only flash — no ASW1 available for ECM3
            dry_run    = dry_run,
            callback   = callback,
        )
        if not ok:
            return False
        client.tester_present()

        # 11. CheckProgrammingDependencies
        try:
            client.start_routine(udsoncan.Routine.CheckProgrammingDependencies)
        except Exception as e:
            log.warning("CheckProgrammingDependencies: %s", e)
        client.tester_present()

        # 12. ECU hard reset
        callback(FlashProgress("DONE", "Flash complete — resetting ECU", 99, "CAL"))
        try:
            client.ecu_reset(services.ECUReset.ResetType.hardReset)
        except Exception as e:
            log.debug("ECU reset: %s", e)

        callback(FlashProgress("DONE", f"CAL flashed — {vin}", 100, "CAL"))
        log.info("flash_cal complete — VIN=%s block=%d bytes=%d",
                 vin, cal_block.number, len(cal_bytes))
        _obd_clear(interface, interface_path)
        return True

def _verify_checksum(client: Client, block: BlockDef,
                     callback: ProgressCallback) -> bool:
    """Checksum via routine 0x0202 (VW_Flash pattern)."""
    callback(FlashProgress("VERIFY", "Running checksum routine 0x0202…", 95))
    try:
        data = bytearray([0x01, block.number, 0x00, 0x04]) + bytes(4)
        client.start_routine(0x0202, data=bytes(data))
        callback(FlashProgress("VERIFY", "Checksum OK", 98))
        return True
    except exceptions.NegativeResponseException as e:
        callback(FlashProgress("ERROR", f"Checksum failed: {e}", 0))
        return False
# ─── Single-block transfer (shared by flash_cal and flash_blocks) ────────────

def _flash_one_block(
    client,
    ecu:        "ECUDef",
    block_num:  int,
    raw_bytes:  bytes,
    asw1_bytes: bytes,
    dry_run:    bool,
    callback:   "ProgressCallback",
) -> bool:
    """
    Erase, download, and transfer one block.
    Session must already be open and SA2 unlocked before calling.
    Returns True on success.
    """
    from flasher.checksum_simos import fix_crc32, fix_ecm3, xor_encrypt
    from flasher.lzss_compress  import lzss_compress

    blk = ecu.blocks[block_num]
    label = blk.name

    if not blk.flashable:
        log.info("Block %d (%s) is marked non-flashable — skipping", block_num, label)
        return True

    # ── Prepare data: checksum → compress → encrypt ────────────────────────
    data = raw_bytes

    if ecu.crypto == CryptoType.XOR_COUNTER and block_num == blk.number and blk.cal_block:
        try:
            data = fix_ecm3(data, asw1_bytes)
        except Exception as e:
            log.warning("ECM3 fix skipped for block %d: %s", block_num, e)

    try:
        data = fix_crc32(data, block_num)
    except Exception as e:
        log.warning("CRC32 fix skipped for block %d: %s", block_num, e)

    try:
        compressed = lzss_compress(data)
    except Exception as e:
        log.warning("LZSS failed for block %d: %s — sending uncompressed", block_num, e)
        compressed = data

    if ecu.crypto == CryptoType.XOR_COUNTER:
        encrypted = xor_encrypt(compressed)
    elif ecu.crypto == CryptoType.AES_CBC:
        from Crypto.Cipher import AES
        pad = (16 - len(compressed) % 16) % 16
        padded = compressed + b"\x00" * pad
        cipher = AES.new(ecu.crypto_key, AES.MODE_CBC, ecu.crypto_iv)
        encrypted = cipher.encrypt(padded)
    else:
        encrypted = compressed

    total = len(encrypted)

    # ── Erase ──────────────────────────────────────────────────────────────
    callback(FlashProgress("ERASE", f"Erasing {label} (block {block_num})…", 0, label))
    if not dry_run:
        try:
            client.start_routine(udsoncan.Routine.EraseMemory,
                                 data=bytes([0x01, block_num]))
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Erase failed ({label}): {e}", 0, label))
            return False

    # ── RequestDownload ────────────────────────────────────────────────────
    callback(FlashProgress("TRANSFER", f"Requesting download for {label}…", 2, label))
    if not dry_run:
        try:
            dfi = udsoncan.DataFormatIdentifier(
                compression=0xA,  # Always 0xA — ECU expects LZSS+XOR regardless of variant
                encryption=0xA,   # Confirmed from VW_Flash simos_flash_utils PreparedBlockData
            )
            mem_loc = udsoncan.MemoryLocation(
                address=block_num, memorysize=total,
                address_format=8, memorysize_format=32,
            )
            resp = client.request_download(mem_loc, dfi)
            max_block = resp.service_data.max_length
        except Exception as e:
            callback(FlashProgress("ERROR", f"RequestDownload failed ({label}): {e}", 0, label))
            return False
    else:
        max_block = 0xFFD

    # ── TransferData with keepalive ────────────────────────────────────────
    KEEPALIVE_EVERY = 50
    block_size  = max_block - 2
    counter     = 1
    offset      = 0
    chunk_count = 0

    while offset < total:
        chunk = encrypted[offset:offset + block_size]
        pct = 5 + int(90 * offset / total)
        callback(FlashProgress("TRANSFER",
                               f"{label}: {offset:#08x}/{total:#08x} ({pct}%)", pct, label))
        if not dry_run:
            try:
                client.transfer_data(counter, chunk)
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR",
                                       f"TransferData failed at {offset:#x} ({label}): {e}",
                                       0, label))
                return False
        offset      += len(chunk)
        counter      = (counter + 1) & 0xFF
        if counter == 0: counter = 1
        chunk_count += 1
        if chunk_count % KEEPALIVE_EVERY == 0 and not dry_run:
            try:
                client.tester_present()
            except Exception as e:
                log.warning("tester_present keepalive failed at %#x (%s): %s", offset, label, e)

    # ── RequestTransferExit ────────────────────────────────────────────────
    callback(FlashProgress("TRANSFER", f"{label}: transfer complete", 96, label))
    if not dry_run:
        try:
            client.request_transfer_exit()
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"TransferExit failed ({label}): {e}", 0, label))
            return False
    client.tester_present()

    # ── Checksum routine 0x0202 ────────────────────────────────────────────
    if not dry_run:
        callback(FlashProgress("VERIFY", f"Checksumming {label}…", 98, label))
        try:
            data_bytes = bytearray([0x01, block_num, 0x00, 0x04]) + bytes(4)
            client.start_routine(0x0202, data=bytes(data_bytes))
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Checksum failed ({label}): {e}", 0, label))
            return False

    callback(FlashProgress("TRANSFER", f"{label} done", 100, label))
    log.info("Block %d (%s) flashed OK — %d bytes", block_num, label, total)
    return True


# ─── Multi-block flash entry point ───────────────────────────────────────────

def flash_blocks(
    ecu:            "ECUDef",
    blocks:         "Dict[int, bytes]",
    interface:      str = "J2534",
    interface_path: "Optional[str]" = None,
    callback:       "ProgressCallback" = _noop,
    dry_run:        bool = False,
) -> bool:
    """
    Flash one or more blocks to the ECU.

    Args:
        ecu:            ECUDef (use SIMOS85 for the 3.0T)
        blocks:         Dict of {block_number: raw_bytes}, e.g.:
                            {2: asw1_bytes, 3: cal_bytes}
                        Block numbers for Simos8.5:
                            1 = CBOOT  (use with extreme caution)
                            2 = ASW1   (application software)
                            3 = CAL    (calibration — normal tune target)
                        Non-flashable blocks (6 = CBOOT_TEMP) are silently skipped.
        interface:      "J2534", "SocketCAN_can0", "BLE", "USBISOTP_COM3"
        interface_path: DLL path or port
        callback:       Progress callback
        dry_run:        If True, go through all motions but don't write to ECU

    Returns:
        True if all blocks flashed successfully.

    Block order:
        Blocks are always flashed in ascending block number order regardless of
        dict insertion order, matching VW_Flash behaviour. For a full reflash
        this means CBOOT → ASW1 → CAL. For a tune-only flash, just pass {3: cal}.

    Safety:
        - CBOOT (block 1) requires the same SA2 unlock as other blocks but is
          far more dangerous to flash incorrectly. The function will flash it if
          you pass it, but consider using flash_cal() for CAL-only operations.
        - If any block fails, flashing stops immediately. Partial flashes leave
          the ECU in an indeterminate state — always have a recovery plan (bench
          setup or Tactrix cable + VW_Flash).
    """
    if not blocks:
        log.warning("flash_blocks: no blocks provided")
        return False

    # Validate all block numbers before connecting
    for bnum in blocks:
        if bnum not in ecu.blocks:
            callback(FlashProgress("ERROR",
                                   f"Block {bnum} not defined for {ecu.name}", 0))
            return False
        blk = ecu.blocks[bnum]
        if not blk.flashable:
            log.info("Block %d (%s) is non-flashable — will be skipped", bnum, blk.name)

    # Pull ASW1 bytes from the block dict if provided — needed for ECM3 fix on CAL
    asw1_bytes = blocks.get(2, None)

    def _obd_clear(iface, ipath):
        try:
            if iface.upper() == "J2534":
                from lib.connections.j2534_connection import J2534Connection
                dll = ipath or (
                    "C:/Program Files (x86)/OpenECU/OpenPort 2.0/drivers/openport 2.0/op20pt32.dll"
                )
                c = J2534Connection(windll=dll, rxid=0x7E8, txid=0x700)
            elif iface.upper().startswith("SOCKETCAN"):
                from udsoncan.connections import IsoTPSocketConnection
                iface_name = ipath or iface.split("_", 1)[-1]
                c = IsoTPSocketConnection(iface_name, rxid=0x7E8, txid=0x700,
                                          params={"tx_padding": 0x55})
            else:
                return
            c.open()
            c.specific_send(bytes([0x04]))
            try: c.specific_wait_frame(timeout=1.0)
            except Exception: pass
            try: c.specific_wait_frame(timeout=0.5)
            except Exception: pass
            c.close()
        except Exception as e:
            log.debug("OBD DTC clear: %s", e)

    callback(FlashProgress("CONNECT", f"Connecting to {ecu.name}…", 0))
    _obd_clear(interface, interface_path)

    conn = _make_connection(ecu, interface, interface_path)

    # STMIN_TX gate — same check as flash_cal
    if hasattr(conn, 'stmin_tx_supported') and not conn.stmin_tx_supported:
        callback(FlashProgress(
            "ERROR",
            "J2534 adapter does not support STMIN_TX — flash will fail mid-block. "
            "Use Tactrix OpenPort 2.0 or Switchleg ESP32 (BridgeLEG firmware).",
            0,
        ))
        return False

    cfg = dict(configs.default_client_config)
    cfg["security_algo"]        = _make_security_algo(ecu.sa2_script)
    cfg["security_algo_params"] = None
    cfg["data_identifiers"]     = {}
    cfg["request_timeout"]      = 30

    block_order = sorted(b for b in blocks if ecu.blocks[b].flashable)
    total_blocks = len(block_order)

    with Client(conn, request_timeout=30, config=cfg) as client:

        def _st(t=30):
            try: client.session_timing["p2_server_max"] = t
            except TypeError: client.session_timing.p2_server_max = t
            client.config["request_timeout"] = t

        # ── 1. Extended session ────────────────────────────────────────────
        callback(FlashProgress("CONNECT", "Opening extended session…", 5))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Extended session refused: {e}", 0))
            return False
        _st(30)

        # Read VIN for logging
        vin = "UNKNOWN"
        try:
            class _SC(udsoncan.DidCodec):
                def encode(self, v): return bytes(v)
                def decode(self, p): return p.decode("ascii", errors="replace")
                def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData
            client.config["data_identifiers"][0xF190] = _SC
            vin = client.read_data_by_identifier_first(0xF190)
        except Exception: pass
        callback(FlashProgress("CONNECT", f"VIN: {vin}", 8))
        log.info("flash_blocks: VIN=%s blocks=%s", vin, block_order)

        # ── 2. Programming precondition 0x0203 ────────────────────────────
        try:
            client.start_routine(0x0203)
        except Exception as e:
            log.warning("Precondition 0x0203: %s (continuing)", e)
        client.tester_present()

        # ── 3. Programming session ─────────────────────────────────────────
        callback(FlashProgress("CONNECT", "Entering programming session…", 12))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.programmingSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Programming session refused: {e}", 0))
            return False
        _st(30)
        client.tester_present()

        # ── 4. SA2 security access level 17 ───────────────────────────────
        callback(FlashProgress("CONNECT", "SA2 security access…", 18))
        try:
            client.unlock_security_access(0x11)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"SA2 denied: {e}", 0))
            return False
        client.tester_present()

        # ── 5. Workshop code 0xF15A ────────────────────────────────────────
        try:
            from flasher.workshop_code import build_workshop_code
            cal_bytes_for_wc = blocks.get(3) or next(iter(blocks.values()))
            wc = build_workshop_code(cal_data=cal_bytes_for_wc)
            client.write_data_by_identifier(0xF15A, wc)
        except Exception as e:
            log.debug("Workshop code: %s", e)
        client.tester_present()

        # ── 6. Flash each block in order ───────────────────────────────────
        for i, bnum in enumerate(block_order):
            blk_label = ecu.blocks[bnum].name
            overall_pct = 20 + int(75 * i / total_blocks)
            callback(FlashProgress("FLASHING",
                                   f"Block {i+1}/{total_blocks}: {blk_label}",
                                   overall_pct, blk_label))

            ok = _flash_one_block(
                client    = client,
                ecu       = ecu,
                block_num = bnum,
                raw_bytes = blocks[bnum],
                asw1_bytes= asw1_bytes,
                dry_run   = dry_run,
                callback  = callback,
            )
            if not ok:
                return False

            client.tester_present()

        # ── 7. Verify programming dependencies ────────────────────────────
        callback(FlashProgress("VERIFY", "Verifying programming dependencies…", 96))
        try:
            client.start_routine(udsoncan.Routine.CheckProgrammingDependencies)
        except Exception as e:
            log.warning("CheckProgrammingDependencies: %s", e)
        client.tester_present()

        # ── 8. ECU hard reset ──────────────────────────────────────────────
        callback(FlashProgress("DONE", "Resetting ECU…", 99))
        try:
            client.ecu_reset(services.ECUReset.ResetType.hardReset)
        except Exception as e:
            log.debug("ECU reset: %s", e)

        callback(FlashProgress("DONE", f"All blocks flashed — {vin}", 100))
        log.info("flash_blocks complete — VIN=%s blocks=%s", vin, block_order)
        _obd_clear(interface, interface_path)
        return True




# ─── Multi-block flash ────────────────────────────────────────────────────────

def flash_blocks(
    ecu:             ECUDef,
    blocks:          Dict[str, bytes],
    interface:       str = "J2534",
    interface_path:  Optional[str] = None,
    callback:        ProgressCallback = _noop,
    dry_run:         bool = False,
    stmin_override:  Optional[int] = None,
) -> bool:
    """
    Flash one or more blocks to the ECU.

    This is the full multi-block entry point, mirroring VW_Flash flash_blocks().
    Use this for full re-flashes (CBOOT + ASW1 + CAL) or to restore a single
    block. flash_cal() is a convenience wrapper for CAL-only tunes.

    Args:
        ecu:            ECUDef (use SIMOS85 for the 3.0T)
        blocks:         Dict mapping block name -> raw decrypted bytes.
                        Keys: "CBOOT", "ASW1", "CAL" (or block numbers as str).
                        Example: {"ASW1": asw1_bytes, "CAL": cal_bytes}
        interface:      "J2534", "SocketCAN_can0", "BLE", "USBISOTP_COM3"
        interface_path: J2534 DLL path or SocketCAN interface name
        callback:       Progress callback receiving FlashProgress
        dry_run:        Go through session setup but skip all writes
        stmin_override: Override STMIN_TX in us (None = use adapter default)

    Returns:
        True on success, False on any failure.

    Block order:
        Always flashes in canonical order regardless of dict order:
        CBOOT(1) -> ASW1(2) -> ASW2(3) -> ASW3(4) -> CAL(5)
        CBOOT_TEMP (block 6) is never flashed via this path.
    """
    # Resolve block names to numbers
    block_queue: Dict[int, tuple] = {}
    for name, data in blocks.items():
        if isinstance(name, int):
            num = name
        elif isinstance(name, str) and name.isdigit():
            num = int(name)
        else:
            num = next(
                (k for k, v in ecu.blocks.items() if v.name.upper() == name.upper()),
                None
            )
            if num is None:
                callback(FlashProgress("ERROR", f"Unknown block name: {name!r}", 0))
                return False
        if num not in ecu.blocks:
            callback(FlashProgress("ERROR", f"Block {num} not in ECU definition", 0))
            return False
        block_queue[num] = (name, data)

    if not block_queue:
        callback(FlashProgress("ERROR", "No blocks specified", 0))
        return False

    ordered = sorted(block_queue.items(), key=lambda x: x[0])

    # Grab ASW1 bytes for ECM3 fix even if ASW1 is not being flashed
    asw1_bytes: Optional[bytes] = block_queue.get(2, (None, None))[1]

    callback(FlashProgress("SETUP", f"Preparing {len(ordered)} block(s)...", 0))
    _obd_clear(interface, interface_path)

    conn = _make_connection(ecu, interface, interface_path,
                            st_min_us=stmin_override or 350_000)

    if hasattr(conn, 'stmin_tx_supported') and not conn.stmin_tx_supported:
        callback(FlashProgress(
            "ERROR",
            "J2534 adapter does not support STMIN_TX. Flash will likely fail. "
            "Use Tactrix OpenPort 2.0 or Switchleg ESP32 (BridgeLEG firmware).",
            0,
        ))
        return False

    cfg = dict(configs.default_client_config)
    cfg["security_algo"]        = _make_security_algo(ecu.sa2_script)
    cfg["security_algo_params"] = None
    cfg["data_identifiers"]     = {}
    cfg["request_timeout"]      = 30

    with Client(conn, request_timeout=30, config=cfg) as client:

        def _st(t=30):
            try:    client.session_timing["p2_server_max"] = t
            except TypeError: client.session_timing.p2_server_max = t
            client.config["request_timeout"] = t

        # Extended session
        callback(FlashProgress("CONNECT", "Opening extended session...", 2))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Extended session refused: {e}", 0))
            return False
        _st(30)

        vin = "UNKNOWN"
        try:
            class _SC(udsoncan.DidCodec):
                def encode(self, v): return bytes(v)
                def decode(self, p): return p.decode("ascii", errors="replace").strip("\x00 ")
                def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData
            client.config["data_identifiers"][0xF190] = _SC
            vin = client.read_data_by_identifier_first(0xF190)
        except Exception:
            pass
        callback(FlashProgress("CONNECT", f"VIN: {vin}", 5))
        log.info("flash_blocks: VIN=%s blocks=%s", vin, [ecu.blocks[n].name for n, _ in ordered])

        try:
            client.start_routine(0x0203)
        except Exception as e:
            log.warning("Precondition 0x0203: %s (continuing)", e)
        client.tester_present()

        # Programming session with Switchpatch fallback
        callback(FlashProgress("CONNECT", "Entering programming session...", 8))
        try:
            client.change_session(
                services.DiagnosticSessionControl.Session.programmingSession)
        except exceptions.NegativeResponseException:
            try:
                def _sp_payload(payload):
                    return bytes([0x3E, 0x10, 0x02])
                with client.payload_override(_sp_payload):
                    _st(30)
                    client.change_session(
                        services.DiagnosticSessionControl.Session.programmingSession)
                log.info("Programming session via Switchpatch fallback")
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"Programming session refused: {e}", 0))
                return False
        _st(30)
        client.tester_present()

        # SA2 security access level 17
        callback(FlashProgress("CONNECT", "SA2 security access...", 12))
        try:
            client.unlock_security_access(0x11)
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Security access denied: {e}", 0))
            return False
        client.tester_present()

        # Workshop code
        try:
            from flasher.workshop_code import build_workshop_code
            cal_raw = (block_queue.get(3) or block_queue.get(5) or (None, None))[1]
            wc = build_workshop_code(cal_data=cal_raw)
            client.write_data_by_identifier(0xF15A, wc)
        except Exception as e:
            log.debug("Workshop code: %s", e)
        client.tester_present()

        # Flash each block in order
        total_blocks = len(ordered)
        for idx, (block_num, (block_name, raw_bytes)) in enumerate(ordered):
            blk = ecu.blocks[block_num]
            base_pct = 15 + int(80 * idx / total_blocks)
            block_span = int(80 / total_blocks)

            # Erase
            callback(FlashProgress("ERASE", f"Erasing {blk.name}...", base_pct, blk.name))
            if not dry_run:
                try:
                    client.start_routine(
                        udsoncan.Routine.EraseMemory,
                        data=bytes([0x01, blk.number]))
                except exceptions.NegativeResponseException as e:
                    callback(FlashProgress("ERROR", f"Erase {blk.name} failed: {e}", 0))
                    return False
            client.tester_present()

            # Prepare: checksum + compress + encrypt
            callback(FlashProgress("TRANSFER", f"Preparing {blk.name}...",
                                   base_pct + 1, blk.name))
            encrypted = _prepare_block_data(ecu, block_num, raw_bytes, asw1_bytes)
            total_bytes = len(encrypted)

            # RequestDownload
            callback(FlashProgress("TRANSFER", f"RequestDownload {blk.name}...",
                                   base_pct + 2, blk.name))
            if not dry_run:
                try:
                    dfi = udsoncan.DataFormatIdentifier(compression=0xA, encryption=0xA)
                    mem_loc = udsoncan.MemoryLocation(
                        address=blk.number,
                        memorysize=total_bytes,
                        address_format=8,
                        memorysize_format=32,
                    )
                    resp = client.request_download(mem_loc, dfi)
                    max_block = resp.service_data.max_length
                except Exception as e:
                    callback(FlashProgress("ERROR",
                                           f"RequestDownload {blk.name} failed: {e}", 0))
                    return False
            else:
                max_block = 0xFFD

            # TransferData with periodic tester_present keepalive
            KEEPALIVE_EVERY = 50
            chunk_size  = max_block - 2
            counter     = 1
            offset      = 0
            chunk_count = 0
            while offset < total_bytes:
                chunk = encrypted[offset:offset + chunk_size]
                pct = base_pct + 2 + int(block_span * 0.9 * offset / total_bytes)
                callback(FlashProgress(
                    "TRANSFER",
                    f"{blk.name}: {offset:#08x}/{total_bytes:#08x}",
                    pct, blk.name))
                if not dry_run:
                    try:
                        client.transfer_data(counter, chunk)
                    except exceptions.NegativeResponseException as e:
                        callback(FlashProgress(
                            "ERROR",
                            f"TransferData {blk.name} at {offset:#x}: {e}", 0))
                        return False
                offset      += len(chunk)
                counter      = (counter + 1) & 0xFF
                if counter == 0: counter = 1
                chunk_count += 1
                if chunk_count % KEEPALIVE_EVERY == 0 and not dry_run:
                    try:
                        client.tester_present()
                    except Exception as e:
                        log.warning("Keepalive failed %s at %#x: %s", blk.name, offset, e)

            # RequestTransferExit
            if not dry_run:
                try:
                    client.request_transfer_exit()
                except exceptions.NegativeResponseException as e:
                    callback(FlashProgress("ERROR",
                                           f"TransferExit {blk.name} failed: {e}", 0))
                    return False
            client.tester_present()

            # Per-block checksum 0x0202
            if not dry_run:
                if not _verify_checksum(client, blk, callback):
                    return False

            callback(FlashProgress("TRANSFER", f"{blk.name} complete",
                                   base_pct + block_span, blk.name))
            log.info("flash_blocks: block %d (%s) done — %d bytes",
                     block_num, blk.name, total_bytes)

        # Post-flash
        callback(FlashProgress("VERIFY", "CheckProgrammingDependencies...", 96))
        try:
            client.start_routine(udsoncan.Routine.CheckProgrammingDependencies)
        except Exception as e:
            log.warning("CheckProgrammingDependencies: %s", e)
        client.tester_present()

        time.sleep(2)

        callback(FlashProgress("DONE", "Resetting ECU...", 99))
        try:
            client.ecu_reset(services.ECUReset.ResetType.hardReset)
        except Exception as e:
            log.debug("ECU reset: %s", e)

        callback(FlashProgress("DONE", f"flash_blocks complete — {vin}", 100))
        log.info("flash_blocks complete: VIN=%s blocks=%s", vin, [n for n, _ in ordered])

    _obd_clear(interface, interface_path)
    return True

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
        # Standard VW identification DIDs (from VW_Flash constants)
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
        0xF1F4: "Boot Loader ID",
        0xF1AB: "SW Block Version",
        0xF1F1: "Tuning Protection SO2",
        0xF15B: "Programming Date",
        0xF1A5: "Coding Fingerprint",
        0x0405: "Flash State",
        0x0407: "Program Attempts",
        0x0408: "Successful Programs",
        0x0600: "VW Coding Value",
        0xEF90: "Immobilizer Status",
        0xF1DF: "ECU Programming Info",
        0xF186: "Active Session",
        0xF442: "Module Voltage",
        0x295A: "Vehicle Mileage",
        0x295B: "Module Mileage",
        0xF15A: "Flash Tool Log",
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
