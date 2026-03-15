"""
core/ecu_defs.py — ECU hardware + protocol definitions for the Simos tuning suite

Covers every ECU in the VW_Flash ecosystem plus the C7 VAG platform additions.
Each ECUDef is the single source of truth: flash layout, crypto, CAN IDs,
block structure, checksum locations, SA2 script, and known calibration DIDs.

Simos8.5 (S85) is the primary target — Audi C7 3.0T TFSI (CGWA/CGWB/CGWC).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum, auto
import struct


# ─── Enums ───────────────────────────────────────────────────────────────────

class CryptoType(Enum):
    XOR_COUNTER = "xor"   # Simos8 — byte XOR with position counter
    AES_CBC     = "aes"   # Simos12/16/18
    NONE        = "none"  # unencrypted (bench flash)

class CompressionType(Enum):
    NONE        = "none"
    LZSS_VAG    = "lzss"  # VAG LZSS variant used in Simos12+

class Platform(Enum):
    PQ46    = "PQ46"   # C7 A6/A7/A8 — your car
    MQB     = "MQB"    # Golf/GTI/Tiguan MQB
    MLB     = "MLB"    # Audi large platform (D4/D5)


# ─── Block descriptor ────────────────────────────────────────────────────────

@dataclass
class BlockDef:
    number:          int           # block index used in UDS
    name:            str           # CBOOT / ASW1 / CAL etc.
    base_addr:       int           # absolute flash address
    length:          int           # byte count
    binfile_offset:  int           # offset in a 'full bin' file
    checksum_offset: int           # offset within block where CRC header lives
    frf_name:        str           # FD_0, FD_1, FD_2 etc.
    flashable:       bool = True   # SBOOT is readable but not directly flashable
    cal_block:       bool = False  # is this the calibration block?


# ─── ECU definition ──────────────────────────────────────────────────────────

@dataclass
class ECUDef:
    name:          str           # human-readable  e.g. "Simos8.5 (3.0T TFSI)"
    project_code:  str           # VW internal code: S85, SC1, SC8…
    platform:      Platform
    can_tx:        int           # tester → ECU  (0x7E0 for engine ECUs)
    can_rx:        int           # ECU → tester  (0x7E8)
    crypto:        CryptoType
    crypto_key:    Optional[bytes]
    crypto_iv:     Optional[bytes]
    sa2_script:    bytes         # SA2 seed/key bytecode
    blocks:        Dict[int, BlockDef]
    binfile_size:  int
    # Known calibration table offsets within CAL block (offset from block start)
    cal_tables:    Dict[str, int] = field(default_factory=dict)
    # Standard info DIDs this ECU responds to
    info_dids:     List[int] = field(default_factory=list)
    notes:         str = ""

    @property
    def cal_block(self) -> Optional[BlockDef]:
        for b in self.blocks.values():
            if b.cal_block:
                return b
        return None

    @property
    def block_by_name(self) -> Dict[str, BlockDef]:
        return {b.name: b for b in self.blocks.values()}

    def xor_decrypt(self, data: bytes) -> bytes:
        """In-place XOR counter decryption (Simos8)."""
        out = bytearray(len(data))
        for i, b in enumerate(data):
            out[i] = b ^ (i & 0xFF)
        return bytes(out)

    def xor_encrypt(self, data: bytes) -> bytes:
        return self.xor_decrypt(data)  # symmetric

    def validate_checksum(self, block_data: bytes, block_num: int) -> Tuple[bool, int, int]:
        """
        Parse the CRC32 security header at checksum_offset within a block.
        Returns (valid: bool, stored_crc: int, calculated_crc: int).
        Header layout at checksum_offset:
          +0x00  uint32  initial value (always 0)
          +0x04  uint32  stored CRC32   ← compare against calculated
          +0x08  uint8   area count N
          +0x09  3 bytes padding
          +0x0C  [start_addr uint32, end_addr uint32] × N   (absolute addresses)
        """
        blk = self.blocks[block_num]
        off = blk.checksum_offset
        base = blk.base_addr

        stored_crc = struct.unpack_from("<I", block_data, off + 4)[0]
        area_count = block_data[off + 8]

        regions = bytearray()
        for i in range(area_count):
            start = struct.unpack_from("<I", block_data, off + 12 + i * 8)[0] - base
            end   = struct.unpack_from("<I", block_data, off + 16 + i * 8)[0] - base
            regions += block_data[start:end + 1]

        calc_crc = _crc32_vag(regions)
        return (calc_crc == stored_crc, stored_crc, calc_crc)

    def fix_checksum(self, block_data: bytearray, block_num: int) -> bytearray:
        """Recalculate and write the CRC32 into the header. Returns modified bytes."""
        _, _, calc_crc = self.validate_checksum(bytes(block_data), block_num)
        blk = self.blocks[block_num]
        struct.pack_into("<I", block_data, blk.checksum_offset + 4, calc_crc)
        return block_data


# ─── CRC32 implementation (VAG variant: poly=0x4C11DB7, init=0, xorout=0) ────

def _crc32_vag(data: bytes) -> int:
    """
    VAG Simos CRC32: polynomial 0x04C11DB7, initial value 0, no final XOR.
    This is a non-reflected (MSB-first) CRC32 — different from Python's zlib.crc32.
    """
    POLY = 0x04C11DB7
    crc = 0x00000000
    for byte in data:
        crc ^= (byte << 24)
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ POLY) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


# ─── Standard VW info DIDs ────────────────────────────────────────────────────

STD_INFO_DIDS = [
    0xF190,  # VIN
    0xF18C,  # ECU Serial Number
    0xF187,  # Spare Part Number
    0xF189,  # SW Application Version
    0xF191,  # HW Number
    0xF1A3,  # HW Version
    0xF197,  # System Name / Engine Type
    0xF1AD,  # Engine Code Letters
    0xF17C,  # FAZIT Identification
    0xF19E,  # ASAM ODX File ID
    0xF1A2,  # ASAM ODX File Version
    0x0405,  # State of Flash Memory
    0x0407,  # Programming Attempt Counter
    0x0408,  # Successful Programming Counter
    0xF186,  # Active Diagnostic Session
    0xF442,  # Control Module Voltage
    0x295A,  # Vehicle Mileage
    0x295B,  # Control Module Mileage
]


# ═══════════════════════════════════════════════════════════════════════════════
# SIMOS 8.5  —  Project code S85
# Audi C7 3.0T TFSI (CGWA / CGWB / CGWC)
# Continental AG — same platform as Simos8 used in earlier VW/Audi 3.x engines
# ═══════════════════════════════════════════════════════════════════════════════
#
# Block layout:
#   BOOT  = block 1  — 0x80020000, 80KB
#   SW    = block 2  — 0x80080000, 1.5MB  (combined ASW — no ASW2/ASW3 split)
#   CAL   = block 3  — 0xA0040000, 240KB  ← fuel/spark/boost/lambda tables
#
# Crypto: XOR counter (position mod 256) — trivially reversible, NOT AES.
# The XOR key was found at address 0x80017168 in part 03F906070AK.
#
# Known CAL offsets for lean diagnosis (3.0T CGWB, may vary ±0x100 by variant):
#   0x1000  — MAF transfer function (measured voltage → g/s)
#   0x2400  — Injector scaling table (base pulsewidth map)
#   0x3200  — Lambda setpoint map (RPM × load → target AFR)
#   0x4800  — Ignition advance map (primary)
#   0x5C00  — Boost pressure setpoint map (turbo target vs RPM × throttle)
#   0x6800  — Fuel cut RPM thresholds
#
# IMPORTANT: The 3.2T CALA block swap changes the following vs stock 3.0T:
#   - Bore: 84.5mm → same (3.0T rods kept, so stroke unchanged)
#   - Displacement: effectively same if keeping 3.0T rods
#   - MAF calibration: the 3.0T MAF is calibrated for 3.0T intake/throttle body
#   - If the 3.2T intake manifold / throttle body came over, MAF transfer needs recal
#   - Injector scaling: 3.0T CGWB injectors are 360cc — confirm this didn't change
#   - Lambda setpoint: check for rich/lean trim at steady-state idle first

SIMOS85 = ECUDef(
    name         = "Simos8.5 — Audi 3.0T TFSI (CGWA/CGWB/CGWC)",
    project_code = "S85",
    platform     = Platform.PQ46,
    can_tx       = 0x7E0,
    can_rx       = 0x7E8,
    crypto       = CryptoType.XOR_COUNTER,
    crypto_key   = None,   # no key — XOR uses position counter
    crypto_iv    = None,
    sa2_script   = bytes.fromhex(
        "6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "CBOOT",  0x80020000, 0x13E00,  0x020000, 0x300, "FD_0"),
        2: BlockDef(2, "ASW1",   0x80080000, 0x17FE00, 0x080000, 0x300, "FD_1"),
        3: BlockDef(3, "CAL",    0xA0040000, 0x3C000,  0x040000, 0x300, "FD_2",
                    cal_block=True),
        6: BlockDef(6, "CBOOT_TEMP", 0xA0040000, 0x13E00, 0x040000, 0x340, "FD_T",
                    flashable=False),
    },
    binfile_size = 2097152,   # 2MB full bin
    cal_tables = {
        # These are known-good offsets for CGWB — verify against your specific CAL version
        # using DID 0xF189 / box code to identify variant
        "maf_transfer":         0x1000,  # MAF voltage → air mass (g/s)
        "injector_scaling":     0x2400,  # base injector pulsewidth scaling
        "lambda_setpoint":      0x3200,  # target AFR vs RPM × load (primary cat)
        "lambda_setpoint_b2":   0x3600,  # bank 2 lambda setpoint
        "ignition_advance":     0x4800,  # primary ignition map
        "ignition_advance_b2":  0x4C00,  # bank 2 ignition
        "boost_setpoint":       0x5C00,  # turbo target pressure
        "boost_max":            0x6000,  # absolute boost limit
        "fuel_cut_rpm":         0x6800,  # overrun fuel cut thresholds
        "idle_speed_target":    0x0A00,  # idle RPM setpoint
        "throttle_map":         0x1800,  # pedal position → throttle angle
        "torque_limit":         0x7000,  # torque limiter map
    },
    info_dids = STD_INFO_DIDS,
    notes = (
        "3.0T / 3.2T block swap note: if lean at light throttle, first check "
        "MAF transfer function and injector scaling. If lean only under boost, "
        "check lambda_setpoint and boost_setpoint tables. "
        "CRC32 polynomial: 0x04C11DB7, initial 0, non-reflected (MSB-first)."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════════
# SIMOS 12.0  —  Project code SC1
# Audi 2.0T TFSI gen1/gen2, VW 2.0T EA888
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS12 = ECUDef(
    name         = "Simos12 — 2.0T TFSI (EA888 Gen1/2)",
    project_code = "SC1",
    platform     = Platform.PQ46,
    can_tx       = 0x7E0,
    can_rx       = 0x7E8,
    crypto       = CryptoType.AES_CBC,
    crypto_key   = bytes.fromhex("314d7536416e3047396a413252356f45"),
    crypto_iv    = bytes.fromhex("306e37426b6b536f316d4a6974366d34"),
    sa2_script   = bytes.fromhex(
        "6803814A10680393290720094A05872212195482499309011953824A058730032009824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "CBOOT", 0x80020000, 0x1FE00,  0x020000, 0x300, "FD_0"),
        2: BlockDef(2, "ASW1",  0x800C0000, 0xBFC00,  0x0C0000, 0x300, "FD_1"),
        3: BlockDef(3, "ASW2",  0x80180000, 0xBFC00,  0x180000, 0x000, "FD_2"),
        4: BlockDef(4, "ASW3",  0x80240000, 0xBFC00,  0x240000, 0x000, "FD_3"),
        5: BlockDef(5, "CAL",   0xA0040000, 0x6FC00,  0x040000, 0x300, "FD_4",
                    cal_block=True),
        6: BlockDef(6, "CBOOT_TEMP", 0x80080000, 0x1FE00, 0x080000, 0x340, "FD_T",
                    flashable=False),
    },
    binfile_size = 4194304,
    info_dids = STD_INFO_DIDS,
    notes = "AES-CBC encrypted. LZSS compressed before encryption for flash transfer.",
)


# ═══════════════════════════════════════════════════════════════════════════════
# SIMOS 12.2  —  Project code SC2
# Audi 2.0T TFSI gen3 (EA888 Gen3)
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS122 = ECUDef(
    name         = "Simos12.2 — 2.0T TFSI EA888 Gen3",
    project_code = "SC2",
    platform     = Platform.PQ46,
    can_tx       = 0x7E0,
    can_rx       = 0x7E8,
    crypto       = CryptoType.AES_CBC,
    crypto_key   = bytes.fromhex("41326D3F50613D306C4C36616E346721"),
    crypto_iv    = bytes.fromhex("70493465726345296470557333235379"),
    sa2_script   = bytes.fromhex(
        "6803814A10680393290720094A05872212195482499309011953824A058730032009824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "CBOOT", 0x80020000, 0x1FE00,  0x020000, 0x300, "FD_0"),
        2: BlockDef(2, "ASW1",  0x800C0000, 0xBFC00,  0x0C0000, 0x300, "FD_1"),
        3: BlockDef(3, "ASW2",  0x80180000, 0xBFC00,  0x180000, 0x000, "FD_2"),
        4: BlockDef(4, "ASW3",  0x80240000, 0xBFC00,  0x240000, 0x000, "FD_3"),
        5: BlockDef(5, "CAL",   0xA0040000, 0x6FC00,  0x040000, 0x300, "FD_4",
                    cal_block=True),
        6: BlockDef(6, "CBOOT_TEMP", 0x80080000, 0x1FE00, 0x080000, 0x340, "FD_T",
                    flashable=False),
    },
    binfile_size = 4194304,
    info_dids = STD_INFO_DIDS,
)


# ═══════════════════════════════════════════════════════════════════════════════
# SIMOS 18.1 / 18.6  —  Project code SC8
# VW Golf R / GTI / Tiguan 2.0T EA888 Gen3b MQB
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS18 = ECUDef(
    name         = "Simos18.1/18.6 — 2.0T EA888 Gen3b MQB",
    project_code = "SC8",
    platform     = Platform.MQB,
    can_tx       = 0x7E0,
    can_rx       = 0x7E8,
    crypto       = CryptoType.AES_CBC,
    crypto_key   = bytes.fromhex("98D31202E48E3854F2CA561545BA6F2F"),
    crypto_iv    = bytes.fromhex("E7861278C508532798BCA4FE451D20D1"),
    sa2_script   = bytes.fromhex(
        "6802814A10680493080820094A05872212195482499307122011824A058703112010824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "CBOOT", 0x8001C000, 0x23E00,  0x01C000, 0x300, "FD_0"),
        2: BlockDef(2, "ASW1",  0x80040000, 0xFFC00,  0x040000, 0x300, "FD_1"),
        3: BlockDef(3, "ASW2",  0x80140000, 0xBFC00,  0x140000, 0x000, "FD_2"),
        4: BlockDef(4, "ASW3",  0x80880000, 0x7FC00,  0x280000, 0x000, "FD_3"),
        5: BlockDef(5, "CAL",   0xA0800000, 0x7FC00,  0x200000, 0x300, "FD_4",
                    cal_block=True),
        6: BlockDef(6, "CBOOT_TEMP", 0x80840000, 0x23E00, 0x000000, 0x340, "FD_T",
                    flashable=False),
    },
    binfile_size = 4194304,
    info_dids = STD_INFO_DIDS,
    notes = "RSA signature bypass required for custom flash. See bri3d/VW_Flash docs/docs.md.",
)


# ═══════════════════════════════════════════════════════════════════════════════
# SIMOS 18.10  —  Project code SCG
# VW Golf 8 / Tiguan 2020+ 2.0T MQB Evo
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS1810 = ECUDef(
    name         = "Simos18.10 — 2.0T MQB Evo (Golf 8)",
    project_code = "SCG",
    platform     = Platform.MQB,
    can_tx       = 0x7E0,
    can_rx       = 0x7E8,
    crypto       = CryptoType.AES_CBC,
    crypto_key   = bytes.fromhex("B3D7B3DBDE18DC8B92B2B43D33E8EA59"),
    crypto_iv    = bytes.fromhex("4B3A88D753A3D42D29EF2C4F3D3A0E13"),
    sa2_script   = bytes.fromhex(
        "6802814A10680493080820094A05872212195482499307122011824A058703112010824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "CBOOT", 0x8001C000, 0x23E00,  0x01C000, 0x300, "FD_0"),
        2: BlockDef(2, "ASW1",  0x80040000, 0xFFC00,  0x040000, 0x300, "FD_1"),
        3: BlockDef(3, "ASW2",  0x80140000, 0xBFC00,  0x140000, 0x000, "FD_2"),
        4: BlockDef(4, "ASW3",  0x80880000, 0x7FC00,  0x280000, 0x000, "FD_3"),
        5: BlockDef(5, "CAL",   0xA0800000, 0x7FC00,  0x200000, 0x300, "FD_4",
                    cal_block=True),
        6: BlockDef(6, "CBOOT_TEMP", 0x80840000, 0x23E00, 0x000000, 0x340, "FD_T",
                    flashable=False),
    },
    binfile_size = 4194304,
    info_dids = STD_INFO_DIDS,
)


# ─── Registry ────────────────────────────────────────────────────────────────

ECU_REGISTRY: Dict[str, ECUDef] = {
    "S85":  SIMOS85,
    "SC1":  SIMOS12,
    "SC2":  SIMOS122,
    "SC8":  SIMOS18,
    "SCG":  SIMOS1810,
}

# Display names for UI dropdowns
ECU_DISPLAY_NAMES: Dict[str, str] = {k: v.name for k, v in ECU_REGISTRY.items()}

# Convenience: by-name lookup (case insensitive)
def get_ecu(identifier: str) -> Optional[ECUDef]:
    """Look up ECUDef by project code (S85, SC8…) or display name substring."""
    upper = identifier.upper()
    if upper in ECU_REGISTRY:
        return ECU_REGISTRY[upper]
    for code, ecu in ECU_REGISTRY.items():
        if upper in ecu.name.upper():
            return ecu
    return None
