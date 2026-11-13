"""
flasher/tcu_flash.py — DIY UDS flasher/tuner for the ZF 8HP TCU (C7 A6/A7, 4G0927158).

Marries the `vag-tcu-tools` ZF 8HP codec (method 0x22 = repeating-XOR + LZZ, CRC32) with the
seat-style UDS-over-Cerberus plumbing. Read / tune / reflash the transmission over OBD→gateway
on CAN 0x7E1/0x7E9.

Pipeline (all pieces confirmed in vag-tcu-tools):
  decode:  FRF block  --XOR decrypt-->  LZZ stream  --decompress-->  raw firmware
  encode:  raw firmware --LZZ compress--> stream  --XOR encrypt-->  method-0x22 block
  CAL CRC: zlib.crc32 (big-endian) over 0x190000..0x1FFD5F, stored at 0x180244 (in CAL_HDR)

The LZZ compressor (the piece vag-tcu-tools was missing) is verified to round-trip real
4G0927158 firmware. XOR key `CyA2008ZFVAGtcuxsam`, SA2 and the memory map come from
vag-tcu-tools/platforms/zf8hp.

SAFETY: every write entry point defaults to dry_run=True. Confirm on a bench/parts TCU first.
ON-CAR UNKNOWNS (parameterised, like hvac_flash/seat_flash):
  * SA2 script — two candidates exist (the TCU's own vs the simos-suite one); pass sa2_script=.
  * RequestDownload DFI for method 0x22, the erase routine id, and the CheckMemory layout —
    defaulted to the in-repo VW_Flash pattern; confirm against a sniff.
  * Whether to reflash the whole ASW or just the CAL (tuning only needs CAL + its CRC).
"""
from __future__ import annotations

import logging
import os
import sys
import zlib
import dataclasses
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple, Callable

from core.ecu_defs import ECUDef, Platform, CryptoType, STD_INFO_DIDS

log = logging.getLogger("SimosSuite.TCUFlash")

ProgressCallback = Callable[[object], None]
def _noop(*_a, **_k) -> None: return None

# ── vag-tcu-tools bridge ─────────────────────────────────────────────────────
# Both repos have a top-level `core` package, so we load the ZF8HP LZZ codec by FILE PATH
# (unique module name, no package collision) and vendor the small constants below. The codec
# (decompress_lzz + the compress_lzz added to vag-tcu-tools) is the cracked crypto this flasher
# marries with the simos-suite UDS/Cerberus plumbing.
_VTT = os.environ.get("VAG_TCU_TOOLS", r"C:\Users\Power\Documents\vag-tcu-tools")

# Constants mirror vag-tcu-tools/platforms/zf8hp (method 0x22, 4G0927158 AL551):
XOR_KEY = b"CyA2008ZFVAGtcuxsam"
SA2_4G0 = bytes.fromhex("6802814993A55A55AA4A05878105952668058249845AA5AA558703F7805C4C")
SA2_SIMOS_SUITE = bytes.fromhex(
    "6805824A10680284100819734A05872506200382499318111973824A058712082001824A0181494C")
MEMORY_MAP = {
    "BOOT":     {"start": 0x000000, "size": 0x040000},
    "ASW":      {"start": 0x040080, "size": 0x13FAF0},
    "CAL_HDR":  {"start": 0x180000, "size": 0x000280},
    "CAL_CODE": {"start": 0x180280, "size": 0x00FD80},
    "CAL_DATA": {"start": 0x190000, "size": 0x06FD60},
}
CAL_CRC_OFFSET = 0x180244
CAL_CRC_RANGE = (0x190000, 0x1FFD5F)
CAN_TX, CAN_RX = 0x7E1, 0x7E9


def _xor(data: bytes) -> bytes:
    k = XOR_KEY; n = len(k)
    return bytes(b ^ k[i % n] for i, b in enumerate(data))


def _load_lzz():
    """Load vag-tcu-tools' lzz.py (decompress + compress) by path, isolated from the `core`
    package-name clash."""
    import importlib.util
    path = os.path.join(_VTT, "core", "lzz.py")
    if not os.path.exists(path):
        raise RuntimeError(f"vag-tcu-tools lzz codec not found at {path!r} "
                           "(set the VAG_TCU_TOOLS env var to the repo path).")
    spec = importlib.util.spec_from_file_location("vtt_lzz", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.decompress_lzz, mod.compress_lzz


# ── ZF 8HP TCU definition ────────────────────────────────────────────────────
def _tcu_ecu() -> ECUDef:
    return ECUDef(
        name="ZF 8HP TCU — C7 A6/A7 4G0927158 (AL551/8HP55)", project_code="ZF8HP_4G0",
        platform=Platform.PQ46, can_tx=CAN_TX, can_rx=CAN_RX,
        crypto=CryptoType.NONE, crypto_key=None, crypto_iv=None,
        sa2_script=SA2_SIMOS_SUITE,         # override with sa2_script= if the unit wants SA2_4G0
        blocks={}, binfile_size=0x200000,
        compatible_hw=["4G0927158", "4H1927158"],
        info_dids=STD_INFO_DIDS,
        notes="method 0x22 XOR+LZZ, CRC32. CAN 0x7E1/0x7E9 via gateway routing. "
              "Not RSA-signed (CRC-only) -> OBD-flashable/tunable.")


# ── codec ────────────────────────────────────────────────────────────────────
def decode_frf_blocks(frf_path: str) -> Dict[int, bytes]:
    """FRF -> {block_num: raw firmware} (XOR decrypt then LZZ decompress)."""
    from flasher.frf_loader import FrfLoader
    import re
    decompress_lzz, _comp = _load_lzz()
    L = FrfLoader()
    enc_blocks = L.extract_blocks(frf_path)
    odx = L.get_odx(frf_path).decode("latin-1")
    us = [int(x) for x in re.findall(r"<UNCOMPRESSED-SIZE>(\d+)</UNCOMPRESSED-SIZE>", odx)]
    out: Dict[int, bytes] = {}
    for idx, (num, enc) in enumerate(sorted(enc_blocks.items())):
        want = us[2 * idx + 1] if 2 * idx + 1 < len(us) else 0   # 22-method size follows the 00
        out[num] = bytes(decompress_lzz(_xor(enc), want))
    return out


def encode_block(raw: bytes) -> bytes:
    """raw firmware -> method-0x22 block (LZZ compress + XOR encrypt). Round-trips to `raw`."""
    _decomp, compress_lzz = _load_lzz()
    return _xor(compress_lzz(raw))


def cal_crc(image_abs: bytes, base: int = 0x000000) -> int:
    """zlib.crc32 over the CAL checksum range 0x190000..0x1FFD5F. `image_abs` is an image whose
    byte 0 maps to flash `base`."""
    lo, hi = CAL_CRC_RANGE
    return zlib.crc32(image_abs[lo - base: hi - base + 1]) & 0xFFFFFFFF


def fix_cal_crc(image_abs: bytearray, base: int = 0x000000) -> Tuple[bytearray, int]:
    """Recompute the CAL CRC32 and patch it into the image at 0x180244 (big-endian).
    `image_abs` must span both CAL_HDR (holds the CRC) and the CAL range. Returns (image, crc)."""
    import struct
    crc = cal_crc(image_abs, base)
    out = bytearray(image_abs)
    struct.pack_into(">I", out, CAL_CRC_OFFSET - base, crc)
    return out, crc


# ── live UDS flow (udsoncan Client; dry_run default) ─────────────────────────
def _with_can(ecu, can_tx, can_rx):
    if can_tx is None and can_rx is None:
        return ecu
    return dataclasses.replace(ecu, can_tx=can_tx or ecu.can_tx, can_rx=can_rx or ecu.can_rx)


def identify_tcu(ecu: Optional[ECUDef] = None, *, can_tx: Optional[int] = None,
                 can_rx: Optional[int] = None, interface: str = "CERBERUS_COM7",
                 interface_path: Optional[str] = None) -> Dict[str, str]:
    """Read id DIDs (extended session, no SA2). Confirm part#/SW before any flash."""
    import udsoncan
    from udsoncan.client import Client
    from udsoncan import services, configs
    from flasher.uds_flash import _make_connection
    ecu = _with_can(ecu or _tcu_ecu(), can_tx, can_rx)
    conn = _make_connection(ecu, interface, interface_path)

    class _Str(udsoncan.DidCodec):
        def encode(self, v): return bytes(v)
        def decode(self, p):
            try:
                s = p.decode("ascii").strip("\x00 \t\r\n")
                return s if s and all(32 <= ord(c) < 127 for c in s) else p.hex().upper()
            except Exception:
                return p.hex().upper()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    cfg = dict(configs.default_client_config)
    cfg["data_identifiers"] = {d: _Str for d in ecu.info_dids}
    cfg["request_timeout"] = 8
    out: Dict[str, str] = {}
    with Client(conn, request_timeout=8, config=cfg) as client:
        client.change_session(services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        for did in ecu.info_dids:
            try: out[f"{did:#06x}"] = str(client.read_data_by_identifier_first(did))
            except Exception as e: out[f"{did:#06x}"] = f"<{type(e).__name__}>"
    log.info("identify_tcu: %s", out)
    return out


def flash_tcu_block(ecu: Optional[ECUDef], block_id: int, encoded: bytes, crc: int, *,
                    can_tx: Optional[int] = None, can_rx: Optional[int] = None,
                    interface: str = "CERBERUS_COM7", interface_path: Optional[str] = None,
                    sa2_level: int = 0x11, sa2_script: Optional[bytes] = None,
                    erase_routine: int = 0xFF00, checkmemory_routine: int = 0x0202,
                    dfi_compression: int = 0x2, dfi_encryption: int = 0x2,
                    address_format: int = 32, dry_run: bool = True,
                    callback: ProgressCallback = _noop) -> bool:
    """Flash one method-0x22 block and CheckMemory it with `crc` (zlib.crc32). dry_run default."""
    import udsoncan
    from udsoncan.client import Client
    from udsoncan import services, exceptions
    from flasher.uds_flash import _make_connection, _make_security_algo, FlashProgress

    ecu = _with_can(ecu or _tcu_ecu(), can_tx, can_rx)
    if sa2_script:
        ecu = dataclasses.replace(ecu, sa2_script=sa2_script)
    conn = _make_connection(ecu, interface, interface_path)
    from udsoncan import configs
    cfg = dict(configs.default_client_config)
    cfg["security_algo"] = _make_security_algo(ecu.sa2_script)
    cfg["security_algo_params"] = None; cfg["data_identifiers"] = {}; cfg["request_timeout"] = 30
    tag = "DRY-RUN " if dry_run else ""
    callback(FlashProgress("CONNECT", f"{tag}Flashing TCU block {block_id} ({len(encoded):,} B)…", 0))

    with Client(conn, request_timeout=30, config=cfg) as client:
        def _st(t=30):
            try: client.session_timing["p2_server_max"] = t
            except TypeError: client.session_timing.p2_server_max = t
            client.config["request_timeout"] = t
        try:
            client.change_session(services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
            _st(30)
            try: client.start_routine(0x0203)
            except Exception as e: log.debug("precondition 0x0203: %s", e)
            client.tester_present()
            client.change_session(services.DiagnosticSessionControl.Session.programmingSession)
            _st(30); client.tester_present()
            client.unlock_security_access(sa2_level); client.tester_present()
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Session/SA2 failed: {e}", 0)); return False

        callback(FlashProgress("ERASE", f"{tag}Erase block {block_id}…", 15))
        if not dry_run:
            try: client.start_routine(erase_routine, data=bytes([0x01, block_id]))
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"Erase failed: {e}", 0)); return False
        client.tester_present()

        max_block = 0xFFD
        callback(FlashProgress("TRANSFER", f"{tag}RequestDownload ({len(encoded):,} B, DFI {dfi_compression:x}/{dfi_encryption:x})…", 18))
        if not dry_run:
            try:
                dfi = udsoncan.DataFormatIdentifier(compression=dfi_compression, encryption=dfi_encryption)
                mem = udsoncan.MemoryLocation(address=block_id, memorysize=len(encoded),
                                              address_format=address_format, memorysize_format=32)
                resp = client.request_download(mem, dfi); max_block = resp.service_data.max_length
            except Exception as e:
                callback(FlashProgress("ERROR", f"RequestDownload failed: {e}", 0)); return False

        chunk = max_block - 2; counter = 1; off = 0; n = len(encoded)
        while off < n:
            if not dry_run:
                try: client.transfer_data(counter, encoded[off:off + chunk])
                except exceptions.NegativeResponseException as e:
                    callback(FlashProgress("ERROR", f"TransferData failed at {off:#x}: {e}", 0)); return False
            off += chunk; counter = (counter + 1) & 0xFF
            if counter == 0: counter = 1
            callback(FlashProgress("TRANSFER", f"{tag}{min(off,n):#08x}/{n:#08x}", 18 + int(70 * min(off,n) / n)))
            if not dry_run and (off // chunk) % 50 == 0:
                try: client.tester_present()
                except Exception: pass

        if not dry_run:
            try: client.request_transfer_exit()
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"TransferExit failed: {e}", 0)); return False
            client.tester_present()

        callback(FlashProgress("VERIFY", f"CheckMemory CRC32=0x{crc:08X}…", 95))
        if not dry_run:
            try:
                data = bytes([0x01, block_id, 0x00, 0x04]) + (crc & 0xFFFFFFFF).to_bytes(4, "big")
                client.start_routine(checkmemory_routine, data=data)
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"CheckMemory rejected (CRC 0x{crc:08X}): {e}", 0)); return False
            try: client.start_routine(0xFF01)
            except Exception as e: log.debug("CheckProgrammingDependencies: %s", e)
            client.tester_present()
            try: client.ecu_reset(services.ECUReset.ResetType.hardReset)
            except Exception as e: log.debug("ECU reset: %s", e)

    callback(FlashProgress("DONE", f"{tag}TCU block {block_id} {'simulated' if dry_run else 'flashed'}", 100))
    return True
