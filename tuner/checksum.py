"""
tuner/checksum.py — Simos8.5 block checksum validation and fixing

Ported from VW_Flash (bri3d/VW_Flash) lib/checksum.py.
Implements two checksum algorithms used in Simos ECUs:

1. CRC32 (0x4C11DB7, init=0, xor=0) — block integrity header at 0x300
2. ECM3 64-bit summation — CAL monitoring checksum at CAL:0x400

Both must be valid before flashing a modified CAL block.
"""
from __future__ import annotations
import struct, logging
log = logging.getLogger("SimosSuite.Checksum")

# ── CRC32 (Simos variant) ─────────────────────────────────────────────────────
# Polynomial 0x4C11DB7, init=0, xor_out=0, reflected=False
# Pre-computed table for speed

def _build_crc32_table():
    poly = 0x04C11DB7
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if (crc & 0x80000000) else (crc << 1)
            crc &= 0xFFFFFFFF
        table.append(crc)
    return table

_CRC32_TABLE = _build_crc32_table()

def crc32_vw(data: bytes) -> int:
    """CRC32 variant used in Simos ECUs (poly=0x4C11DB7, init=0)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC32_TABLE[(crc >> 24) ^ b]) & 0xFFFFFFFF
    return crc


# ── Block checksum header layout (at offset 0x300 in most blocks) ─────────────
#   +0  : 4 bytes initial value (always 0x00000000)
#   +4  : 4 bytes correct CRC32 (little-endian)
#   +8  : 1 byte  area count N
#   +12 : N*2 * 4 bytes  [start_addr, end_addr] pairs (little-endian absolute)

CHECKSUM_OFFSET = {
    0: 0x300,   # SBOOT
    1: 0x300,   # CBOOT
    2: 0x300,   # ASW1
    3: 0x000,   # ASW2 (continuation block)
    4: 0x000,   # ASW3 (continuation block)
    5: 0x300,   # CAL  (Simos18 uses block 5; Simos8 uses block 3)
    3: 0x300,   # CAL  (Simos8 — overlaps, handled by passing correct blocknum)
    6: 0x340,   # CBOOT_TEMP secondary header
}

# Simos8 base addresses (from VW_Flash simos8.py)
BASE_ADDRESSES = {
    1: 0x80020000,   # CBOOT
    2: 0x80080000,   # ASW1
    3: 0xA0040000,   # CAL
    6: 0xA0040000,   # CBOOT_TEMP
}


def validate_block(data: bytes, block_num: int,
                   base_addr: int = None, fix: bool = False):
    """
    Validate (and optionally fix) the CRC32 security header in a block.
    Returns (is_valid, data_bytes).
    data_bytes is modified only if fix=True and checksum was wrong.
    """
    chk_off = CHECKSUM_OFFSET.get(block_num, 0x300)
    base    = base_addr or BASE_ADDRESSES.get(block_num, 0)

    stored_crc   = struct.unpack_from("<I", data, chk_off + 4)[0]
    area_count   = data[chk_off + 8]

    # Read address pairs
    addrs = []
    for i in range(area_count * 2):
        raw = struct.unpack_from("<I", data, chk_off + 12 + i * 4)[0]
        addrs.append(raw - base)

    # Accumulate checksum data
    payload = bytearray()
    for i in range(0, len(addrs), 2):
        payload += data[addrs[i] : addrs[i+1] + 1]

    calc_crc = crc32_vw(payload)
    log.debug("Block %d CRC: stored=0x%08X calc=0x%08X", block_num, stored_crc, calc_crc)

    if calc_crc == stored_crc:
        return True, bytes(data)

    if fix:
        data = bytearray(data)
        struct.pack_into("<I", data, chk_off + 4, calc_crc)
        log.info("Block %d CRC fixed: 0x%08X → 0x%08X", block_num, stored_crc, calc_crc)
        return True, bytes(data)

    return False, bytes(data)


# ── ECM3 CAL monitoring checksum ──────────────────────────────────────────────
# The ECM3 monitoring process continuously checksums sections of CAL.
# It uses a 64-bit pure summation (two 32-bit halves, little-endian).
# Header is at CAL offset 0x400.
#
# Header layout (from VW_Flash simosshared.py):
#   +0  : 4 bytes upper 32 bits of initial value
#   +4  : 4 bytes lower 32 bits of initial value
#   ... (see simosshared.py for full layout)
# Checksum stored at offset 0 of the header.

ECM3_CHECKSUM_OFFSET = 0x400   # Offset into CAL block
ECM3_ADDRESSES_OFFSET = 0x520  # Where ECM3 address table is in ASW1 (newer ECUs)
ECM3_ADDRESSES_EARLY  = 0x540  # Older ECUs


def validate_ecm3(cal_data: bytes, ecm3_addresses: list,
                  fix: bool = False):
    """
    Validate (and optionally fix) the ECM3 64-bit summation checksum in CAL.
    ecm3_addresses: list of (start_offset, end_offset) pairs into CAL.
    Returns (is_valid, cal_data).
    """
    off = ECM3_CHECKSUM_OFFSET

    # Initial value
    hi = struct.unpack_from("<I", cal_data, off + 8)[0]
    lo = struct.unpack_from("<I", cal_data, off + 12)[0]
    checksum = (hi << 32) | lo

    for i in range(0, len(ecm3_addresses), 2):
        start = int(ecm3_addresses[i])
        end   = int(ecm3_addresses[i+1])
        for j in range(start, end, 4):
            checksum += struct.unpack_from("<I", cal_data, j)[0]
    checksum &= 0xFFFFFFFFFFFFFFFF

    # Oldschool ECM3 — different location
    if cal_data[off + 56] > 0:
        off = off + 56

    stored_hi = struct.unpack_from("<I", cal_data, off)[0]
    stored_lo = struct.unpack_from("<I", cal_data, off + 4)[0]
    stored    = (stored_hi << 32) | stored_lo

    log.debug("ECM3: stored=0x%016X calc=0x%016X", stored, checksum)

    if stored == checksum:
        return True, bytes(cal_data)

    if fix:
        cal = bytearray(cal_data)
        struct.pack_into("<I", cal, off,     (checksum >> 32) & 0xFFFFFFFF)
        struct.pack_into("<I", cal, off + 4,  checksum & 0xFFFFFFFF)
        log.info("ECM3 checksum fixed: 0x%016X → 0x%016X", stored, checksum)
        return True, bytes(cal)

    return False, bytes(cal_data)
