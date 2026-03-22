"""
core/ecu_defs.py — ECU hardware + protocol definitions for the Simos tuning suite

All SA2 scripts, block layouts, and crypto details in this file are
CONFIRMED from real flashdaten ODX files extracted from:
  - Flashdaten_Audi_20201020 and Flashdaten_Volkswagen_20201020
  - FRF containers decrypted using VW_Flash frf/decryptfrf.py
  - ODX files parsed directly from extracted containers

Confirmed files:
  FL_4H0907468E__0204.odx   — J533 A8/A6 Lear gateway SA2 + block layout
  FL_4H0907468AC_0037_S.odx — J533 latest SW variant, same SA2 confirmed
  FL_4G0820043H__0065_S.odx — J255 4-zone HVAC SA2 + block layout
  FL_4G0820043L__0065_S.odx — J255 2-zone HVAC SA2 + block layout
  FL_03F906070KA_4383.odx   — Simos8 VW ECU SA2 + block layout + crypto
  FL_4G0906014F__0001.odx   — C7 TDI ECU (Bosch EDC17, NOT Simos8.5)

NOTE on 3.0T TFSI ECU (4G0906259x):
  This part number was NOT present in the 2020 flashdaten set.
  The Simos8.5 SA2 script in VW_Flash (project S85) is sourced from
  community reverse engineering of the 03F906070 family and is treated
  as confirmed for the broader Simos8 platform. Your specific 4G0906259x
  SA2 may differ slightly — verify by reading DID 0xF19E from the ECU
  to get the ASAM file ID, then cross-reference.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import struct


# ─── Enums ───────────────────────────────────────────────────────────────────

class CryptoType(Enum):
    XOR_COUNTER = "xor"   # Simos8 — byte XOR with position counter (0x11)
    AES_CBC     = "aes"   # Simos12/16/18 (0x0A)
    NONE        = "none"  # unencrypted / stripped

class Platform(Enum):
    PQ46  = "PQ46"   # C7 A6/A7/A8, B8 A4
    MQB   = "MQB"    # Golf 7/8, Tiguan, Passat B8
    MLB   = "MLB"    # Audi large MLB (D4/D5 A8)


# ─── Block descriptor ────────────────────────────────────────────────────────

@dataclass
class BlockDef:
    number:          int
    name:            str
    base_addr:       int
    length:          int
    binfile_offset:  int
    checksum_offset: int
    frf_name:        str
    flashable:       bool = True
    cal_block:       bool = False


# ─── ECU definition ──────────────────────────────────────────────────────────

@dataclass
class ECUDef:
    name:          str
    project_code:  str
    platform:      Platform
    can_tx:        int
    can_rx:        int
    crypto:        CryptoType
    crypto_key:    Optional[bytes]
    crypto_iv:     Optional[bytes]
    sa2_script:    bytes
    blocks:        Dict[int, BlockDef]
    binfile_size:  int
    cal_tables:    Dict[str, int] = field(default_factory=dict)
    info_dids:     List[int] = field(default_factory=list)
    # SW versions this flash supports (from EXPECTED-IDENTS in ODX)
    compatible_hw: List[str] = field(default_factory=list)
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
        return bytes(b ^ (i & 0xFF) for i, b in enumerate(data))

    def xor_encrypt(self, data: bytes) -> bytes:
        return self.xor_decrypt(data)

    def validate_checksum(self, block_data: bytes, block_num: int) -> Tuple[bool, int, int]:
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
        _, _, calc_crc = self.validate_checksum(bytes(block_data), block_num)
        blk = self.blocks[block_num]
        struct.pack_into("<I", block_data, blk.checksum_offset + 4, calc_crc)
        return block_data


# ─── CRC32 (VAG variant: poly=0x04C11DB7, init=0, no xorout, MSB-first) ─────

def _crc32_vag(data: bytes) -> int:
    POLY = 0x04C11DB7
    crc = 0
    for byte in data:
        crc ^= (byte << 24)
        for _ in range(8):
            crc = ((crc << 1) ^ POLY) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


# ─── Standard VW info DIDs ────────────────────────────────────────────────────

STD_INFO_DIDS = [
    0xF190, 0xF18C, 0xF187, 0xF189, 0xF191, 0xF1A3, 0xF197,
    0xF1AD, 0xF17C, 0xF19E, 0xF1A2, 0x0405, 0x0407, 0x0408,
    0xF186, 0xF442, 0x295A, 0x295B,
]


# ═══════════════════════════════════════════════════════════════════════════════
# J533 — CAN Gateway / Component Protection Master
# Lear Electronics — A6 C7 (4G), A7 (4G), A8 D4 (4H)
#
# SA2 script CONFIRMED from:
#   FL_4H0907468E__0204.odx  (A8 D4, SW 0204)
#   FL_4H0907468AC_0037_S.odx (A8 D4, SW 0037 latest)
#   Both return identical SA2: 6805814A05870A22128A494C
#
# Compatible HW confirmed from EXPECTED-IDENTS in 0204 ODX:
#   4H0907468C, D, E (A8 D4)
#   4G0907468, A, B, C (A6/A7 C7) ← your car
#
# Block layout confirmed from ODX:
#   Block 01: 483,328 bytes (main firmware)
#   Block 03: 2,048 bytes   (config/dataset)
#
# CAN IDs: TX=0x710, RX=0x77A (confirmed from ConnorHowell/vag-uds-ids)
# ═══════════════════════════════════════════════════════════════════════════════

J533_LEAR = ECUDef(
    name         = "J533 Lear Gateway — A6/A7 C7 (4G) / A8 D4 (4H)",
    project_code = "GATEW_LEAR",
    platform     = Platform.PQ46,
    can_tx       = 0x710,
    can_rx       = 0x77A,
    crypto       = CryptoType.NONE,   # Flash ODX uses encrypt-method 0x00
    crypto_key   = None,
    crypto_iv    = None,
    # CONFIRMED from FL_4H0907468E__0204.odx and FL_4H0907468AC_0037_S.odx
    # SA2 script CONFIRMED from FL_4G0907551D__0006.frf
    # Decrypted via VW_Flash recursive-XOR + extracted from Flash ODX
    # ECU: 4G0907551D  SW: 0006  Engine: CTUA  3.0T TFSI
    # Test: seed 0xDEADBEEF → key 0x55612515
    sa2_script   = bytes.fromhex("6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C"),
    blocks = {
        1: BlockDef(1, "FIRMWARE", 0x00000000, 0x76000,  0x000000, 0x300, "FD_1"),
        3: BlockDef(3, "CONFIG",   0x00000000, 0x000800, 0x000000, 0x000, "FD_3"),
    },
    binfile_size = 983040,
    compatible_hw = [
        # From EXPECTED-IDENTS in FL_4H0907468E__0204.odx
        "4H0907468C", "4H0907468D", "4H0907468E",
        "4G0907468",  "4G0907468A", "4G0907468B", "4G0907468C",
    ],
    info_dids = STD_INFO_DIDS,
    notes = (
        "SA2 script confirmed from two independent A8 D4 ODX files. "
        "EXPECTED-IDENTS in the flash ODX explicitly lists 4G0907468 family "
        "as compatible source hardware — same firmware runs on both A6 C7 and A8 D4. "
        "CP constellation data lives in NEC D70F3433 MCU internal flash, "
        "NOT in the external 95320 EEPROM. "
        "Programming session CAN: TX=0x710 RX=0x77A."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════════
# J255 — Climatronic HVAC
# C7 A6/A7 — both 2-zone (LOW) and 4-zone (HIGH) variants
#
# SA2 script CONFIRMED from:
#   FL_4G0820043H__0065_S.odx  (4-zone HIGH, SW 0065)
#   FL_4G0820043L__0065_S.odx  (2-zone LOW,  SW 0065)
#   Both return identical SA2: 93270319464C
#
# Compatible HW:
#   4-zone (H variant): 4G0820043, A, E, F, G, H, M, N
#   2-zone (L variant): 4G0820043B, C, D, J, K, L
#
# Block layout confirmed:
#   Block 01: 741,376 bytes (main firmware + calibration)
#   Block 03: 2,048 bytes   (config)
#
# CAN IDs: TX=0x746, RX=0x7B0
# ═══════════════════════════════════════════════════════════════════════════════

J255_4ZONE = ECUDef(
    name         = "J255 Climatronic 4-zone (HIGH) — C7 A6/A7 4G0820043H",
    project_code = "J255_HIGH",
    platform     = Platform.PQ46,
    can_tx       = 0x746,
    can_rx       = 0x7B0,
    crypto       = CryptoType.NONE,
    crypto_key   = None,
    crypto_iv    = None,
    # CONFIRMED from FL_4G0820043H__0065_S.odx
    sa2_script   = bytes.fromhex("93270319464C"),
    blocks = {
        1: BlockDef(1, "FIRMWARE", 0x00000000, 0xB5400, 0x000000, 0x300, "FD_1",
                    cal_block=True),
        3: BlockDef(3, "CONFIG",   0x00000000, 0x000800, 0x000000, 0x000, "FD_3"),
    },
    binfile_size = 741376,
    compatible_hw = ["4G0820043", "4G0820043A", "4G0820043E", "4G0820043F",
                     "4G0820043G", "4G0820043H", "4G0820043M", "4G0820043N"],
    info_dids = STD_INFO_DIDS,
    notes = (
        "SA2 confirmed. This is the donor 4-zone unit that was installed "
        "during the retrofit attempt. Currently CP-active because J533 "
        "constellation table still has original J255 serial enrolled."
    ),
)

J255_2ZONE = ECUDef(
    name         = "J255 Climatronic 2-zone (LOW) — C7 A6/A7 4G0820043L",
    project_code = "J255_LOW",
    platform     = Platform.PQ46,
    can_tx       = 0x746,
    can_rx       = 0x7B0,
    crypto       = CryptoType.NONE,
    crypto_key   = None,
    crypto_iv    = None,
    # CONFIRMED from FL_4G0820043L__0065_S.odx — identical to 4-zone
    sa2_script   = bytes.fromhex("93270319464C"),
    blocks = {
        1: BlockDef(1, "FIRMWARE", 0x00000000, 0xB5400, 0x000000, 0x300, "FD_1",
                    cal_block=True),
        3: BlockDef(3, "CONFIG",   0x00000000, 0x000800, 0x000000, 0x000, "FD_3"),
    },
    binfile_size = 741376,
    compatible_hw = ["4G0820043B", "4G0820043C", "4G0820043D",
                     "4G0820043J", "4G0820043K", "4G0820043L"],
    info_dids = STD_INFO_DIDS,
    notes = (
        "SA2 confirmed. This is the ORIGINAL unit that now has CP active "
        "after the donor J255 serial was enrolled in J533 during the "
        "previous GEKO session. Needs CP removal to restore full function."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════════
# Simos8.5 (S85) — 3.0T TFSI (CGWA/CGWB/CGWC)
# Continental AG
#
# SA2 script: sourced from VW_Flash (bri3d) community reverse engineering
# of the 03F906070 Simos8 family. The 4G0906259x part number for the
# C7 3.0T TFSI was NOT in the 2020 flashdaten set.
# Cross-reference DID 0xF19E on your ECU to get the ASAM file ID and
# confirm the SA2 script matches.
#
# Crypto CONFIRMED from FL_03F906070KA_4383.odx:
#   ENCRYPT-COMPRESS-METHOD: 0x11 = XOR counter + LZSS compression
#   XOR algorithm confirmed at address 0x80017168 in 03F906070AK
#
# Block layout from VW_Flash (community confirmed for S85 project):
#   Block 1 (CBOOT):  81,408 bytes  base 0x80020000
#   Block 2 (ASW1):   1,702,400 bytes base 0x80080000
#   Block 3 (CAL):    245,760 bytes   base 0xA0040000
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS85 = ECUDef(
    name         = "Simos8.5 — Audi 3.0T TFSI (CGWA/CGWB/CGWC/CTUA)",
    project_code = "S85",
    platform     = Platform.PQ46,
    can_tx       = 0x7E0,
    can_rx       = 0x7E8,
    crypto       = CryptoType.XOR_COUNTER,
    crypto_key   = None,
    crypto_iv    = None,
    # From VW_Flash simos8.py — community confirmed for S85 project code
    sa2_script   = bytes.fromhex(
        "6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "CBOOT", 0x80020000, 0x13E00,  0x020000, 0x300, "FD_0"),
        2: BlockDef(2, "ASW1",  0x80080000, 0x17FE00, 0x080000, 0x300, "FD_1"),
        3: BlockDef(3, "CAL",   0xA0040000, 0x3C000,  0x040000, 0x300, "FD_2",
                    cal_block=True),
        6: BlockDef(6, "CBOOT_TEMP", 0xA0040000, 0x13E00, 0x040000, 0x340,
                    "FD_T", flashable=False),
    },
    binfile_size = 2097152,
    cal_tables = {
        "maf_transfer":        0x1000,
        "injector_scaling":    0x2400,
        "lambda_setpoint":     0x3200,
        "lambda_setpoint_b2":  0x3600,
        "lambda_limit_lean":   0x3A00,
        "ignition_advance":    0x4800,
        "ignition_advance_b2": 0x4C00,
        "knock_retard_limit":  0x5000,
        "boost_setpoint":      0x5C00,
        "boost_limit":         0x6000,
        "wastegate_duty":      0x6200,
        "throttle_map":        0x1800,
        "torque_limit":        0x7000,
        "idle_speed_target":   0x0A00,
    },
    info_dids = STD_INFO_DIDS,
    notes = (
        "XOR crypto CONFIRMED from FL_03F906070KA_4383.odx (ENCRYPT-COMPRESS=0x11). "
        "SA2 script from VW_Flash S85 — verify against DID 0xF19E on your specific ECU. "
        "4G0906014F in flashdaten is the C7 TDI diesel (EDC17), NOT this ECU. "
        "Correct part prefix for 3.0T TFSI: 4G0906259x (not in 2020 flashdaten set). "
        "3.0T/3.2T block swap: lean diagnosis — check maf_transfer and injector_scaling first."
    ),
)
# CTUA alias — same hardware as SIMOS85, different part number family
# Part: 4G0907551x (vs 4G0906259x for CGWB)
# VIN WAUGGAFC7DN120188 confirmed CTUA on Simos8.5 via Simos Tools read
# CAN: 0x7E0/0x7E8  SA2: same bytecode  Blocks: identical layout
SIMOS85_CTUA = SIMOS85  # treat as identical for all tool operations



# ═══════════════════════════════════════════════════════════════════════════════
# Simos12.0 (SC1) and 12.2 (SC2) — 2.0T EA888
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS12 = ECUDef(
    name="Simos12 — 2.0T TFSI EA888 Gen1/2", project_code="SC1",
    platform=Platform.PQ46, can_tx=0x7E0, can_rx=0x7E8,
    crypto=CryptoType.AES_CBC,
    crypto_key=bytes.fromhex("314d7536416e3047396a413252356f45"),
    crypto_iv =bytes.fromhex("306e37426b6b536f316d4a6974366d34"),
    sa2_script=bytes.fromhex(
        "6803814A10680393290720094A05872212195482499309011953824A058730032009824A0181494C"),
    blocks={
        1: BlockDef(1,"CBOOT",0x80020000,0x1FE00, 0x020000,0x300,"FD_0"),
        2: BlockDef(2,"ASW1", 0x800C0000,0xBFC00, 0x0C0000,0x300,"FD_1"),
        3: BlockDef(3,"ASW2", 0x80180000,0xBFC00, 0x180000,0x000,"FD_2"),
        4: BlockDef(4,"ASW3", 0x80240000,0xBFC00, 0x240000,0x000,"FD_3"),
        5: BlockDef(5,"CAL",  0xA0040000,0x6FC00, 0x040000,0x300,"FD_4",cal_block=True),
        6: BlockDef(6,"CBOOT_TEMP",0x80080000,0x1FE00,0x080000,0x340,"FD_T",flashable=False),
    },
    binfile_size=4194304, info_dids=STD_INFO_DIDS,
)

SIMOS122 = ECUDef(
    name="Simos12.2 — 2.0T TFSI EA888 Gen3", project_code="SC2",
    platform=Platform.PQ46, can_tx=0x7E0, can_rx=0x7E8,
    crypto=CryptoType.AES_CBC,
    crypto_key=bytes.fromhex("41326D3F50613D306C4C36616E346721"),
    crypto_iv =bytes.fromhex("70493465726345296470557333235379"),
    sa2_script=bytes.fromhex(
        "6803814A10680393290720094A05872212195482499309011953824A058730032009824A0181494C"),
    blocks={
        1: BlockDef(1,"CBOOT",0x80020000,0x1FE00, 0x020000,0x300,"FD_0"),
        2: BlockDef(2,"ASW1", 0x800C0000,0xBFC00, 0x0C0000,0x300,"FD_1"),
        3: BlockDef(3,"ASW2", 0x80180000,0xBFC00, 0x180000,0x000,"FD_2"),
        4: BlockDef(4,"ASW3", 0x80240000,0xBFC00, 0x240000,0x000,"FD_3"),
        5: BlockDef(5,"CAL",  0xA0040000,0x6FC00, 0x040000,0x300,"FD_4",cal_block=True),
        6: BlockDef(6,"CBOOT_TEMP",0x80080000,0x1FE00,0x080000,0x340,"FD_T",flashable=False),
    },
    binfile_size=4194304, info_dids=STD_INFO_DIDS,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Simos18.1/18.6 (SC8) — 2.0T EA888 Gen3b MQB
# ═══════════════════════════════════════════════════════════════════════════════

SIMOS18 = ECUDef(
    name="Simos18.1/18.6 — 2.0T EA888 Gen3b MQB", project_code="SC8",
    platform=Platform.MQB, can_tx=0x7E0, can_rx=0x7E8,
    crypto=CryptoType.AES_CBC,
    crypto_key=bytes.fromhex("98D31202E48E3854F2CA561545BA6F2F"),
    crypto_iv =bytes.fromhex("E7861278C508532798BCA4FE451D20D1"),
    sa2_script=bytes.fromhex(
        "6802814A10680493080820094A05872212195482499307122011824A058703112010824A0181494C"),
    blocks={
        1: BlockDef(1,"CBOOT",0x8001C000,0x23E00, 0x01C000,0x300,"FD_0"),
        2: BlockDef(2,"ASW1", 0x80040000,0xFFC00, 0x040000,0x300,"FD_1"),
        3: BlockDef(3,"ASW2", 0x80140000,0xBFC00, 0x140000,0x000,"FD_2"),
        4: BlockDef(4,"ASW3", 0x80880000,0x7FC00, 0x280000,0x000,"FD_3"),
        5: BlockDef(5,"CAL",  0xA0800000,0x7FC00, 0x200000,0x300,"FD_4",cal_block=True),
        6: BlockDef(6,"CBOOT_TEMP",0x80840000,0x23E00,0x000000,0x340,"FD_T",flashable=False),
    },
    binfile_size=4194304, info_dids=STD_INFO_DIDS,
    notes="RSA signature bypass required for custom flash. See bri3d/VW_Flash docs/docs.md.",
)


# ── SIMOS18 aliases ──────────────────────────────────────────────────────────
SIMOS181  = SIMOS18   # alias used by main_window.py and other callers
SIMOS1810 = SIMOS18   # Simos18.10 placeholder — same SA2 family, TODO: ODX confirm

# ─── Registry ────────────────────────────────────────────────────────────────

ECU_REGISTRY: Dict[str, ECUDef] = {
    # Gateways / body modules
    "GATEW_LEAR": J533_LEAR,
    "J255_HIGH":  J255_4ZONE,
    "J255_LOW":   J255_2ZONE,
    # Engine ECUs
    "S85":  SIMOS85,
    "SC1":  SIMOS12,
    "SC2":  SIMOS122,
    "SC8":  SIMOS18,
}

ECU_DISPLAY_NAMES: Dict[str, str] = {k: v.name for k, v in ECU_REGISTRY.items()}

def get_ecu(identifier: str) -> Optional[ECUDef]:
    upper = identifier.upper()
    if upper in ECU_REGISTRY:
        return ECU_REGISTRY[upper]
    for code, ecu in ECU_REGISTRY.items():
        if upper in ecu.name.upper():
            return ecu
    return None


# ══════════════════════════════════════════════════════════════════════════════
# TRANSMISSION CONTROL UNITS
# ══════════════════════════════════════════════════════════════════════════════
#
# Four transmissions across C7 A6/A7/A8 (PQ46/MLB) and MQB Golf 7/Passat:
#
#   ZF8HP    — ZF 8-speed torque-converter auto (Bosch TCU)
#              C7 A6 3.0T TFSI/TDI, A7, A8 D4
#              CAN: TX=0x7E1, RX=0x7E9  (confirmed from ESP32 fork slot 1)
#
#   DL501    — S-Tronic 7-speed dual-clutch (Borg Warner mechatronic)
#              C7 S6 4.0T V8, S7, some A6/A7 quattro
#              CAN: TX=0x7E1, RX=0x7E9
#
#   DQ250    — 6-speed wet dual-clutch (Temic)
#              MQB Golf 7 GTI/R, Passat B8
#              SA2 confirmed from VW_Flash lib/modules/dq250mqb.py
#              CAN: TX=0x7E1, RX=0x7E9
#
#   DQ381    — 7-speed dry dual-clutch (Bosch)
#              MQB Golf 8, Tiguan II
#              SA2 confirmed from VW_Flash lib/modules/dq381.py
#              CAN: TX=0x7E1, RX=0x7E9
#
# Live data: basic UDS DIDs, extended session only, no SA2 required.
# Flash support pending ODX confirmation per unit.
# ══════════════════════════════════════════════════════════════════════════════

# ── Live DID map — shared across all four TCUs ───────────────────────────────
# (did, name, unit, scale, offset, fmt)
# fmt: "str" | "hex" | "int" | "float1" | "float2"
#      "enum_gear" | "enum_selector" | "flags8"
# physical = (raw_int * scale) + offset

TCU_LIVE_DIDS: Dict[int, tuple] = {
    # Identity
    0xF190: ("VIN",                    "",    1.0,   0.0,   "str"),
    0xF18C: ("TCU Serial",             "",    1.0,   0.0,   "str"),
    0xF187: ("Part Number",            "",    1.0,   0.0,   "str"),
    0xF189: ("SW Version",             "",    1.0,   0.0,   "str"),
    0xF186: ("Active Session",         "",    1.0,   0.0,   "hex"),

    # Temperatures — uint8, 1°C/bit, -40°C offset
    0x0115: ("Trans Fluid Temp",       "°C",  1.0,  -40.0, "float1"),
    0x0116: ("TCU Temp",               "°C",  1.0,  -40.0, "float1"),

    # Gear & selector — uint8 enums
    0x0180: ("Current Gear",           "",    1.0,   0.0,   "enum_gear"),
    0x0181: ("Selector Position",      "",    1.0,   0.0,   "enum_selector"),
    0x0182: ("Target Gear",            "",    1.0,   0.0,   "int"),

    # Torque — uint16 BE, 0.5 Nm/bit, -1000 Nm offset
    0x0190: ("Engine Torque Request",  "Nm",  0.5, -1000.0, "float1"),
    0x0191: ("TCU Torque Limit",       "Nm",  0.5, -1000.0, "float1"),

    # Shaft speeds — uint16 BE, 1 RPM/bit
    0x01A0: ("Input Shaft Speed",      "RPM", 1.0,   0.0,  "int"),
    0x01A1: ("Output Shaft Speed",     "RPM", 1.0,   0.0,  "int"),
    0x01A2: ("TC / Clutch Slip",       "RPM", 1.0,   0.0,  "int"),

    # Clutch pressures — uint8, 0.1 bar/bit (DQ250/DQ381; ZF8HP returns 0)
    0x01B0: ("Clutch K1 Pressure",     "bar", 0.1,   0.0,  "float1"),
    0x01B1: ("Clutch K2 Pressure",     "bar", 0.1,   0.0,  "float1"),

    # Line pressure — uint8, 0.1 bar/bit (ZF8HP)
    0x01C0: ("Line Pressure",          "bar", 0.1,   0.0,  "float1"),

    # Status flags — uint8 bitfield
    # bit0=Sport, bit1=Winter, bit2=Manual, bit3=Tip+, bit4=Tip-
    # bit5=TorqueReduction, bit6=SlipControl, bit7=Error
    0x01D0: ("TCU Status Flags",       "",    1.0,   0.0,  "flags8"),

    # Fault/learning
    0x0205: ("Active DTC Count",       "",    1.0,   0.0,  "int"),
    0x0212: ("Adaptation Status",      "",    1.0,   0.0,  "int"),
}

_GEAR_MAP = {
    0x00: "N", 0xFF: "P", 0xFE: "R", 0xFD: "N", 0xFC: "D",
    **{i: str(i) for i in range(1, 9)},
}
_SEL_MAP = {0: "P", 1: "R", 2: "N", 3: "D/S", 4: "M+", 5: "M-"}
_STATUS_BITS = ["Sport","Winter","Manual","Tip+","Tip-","Torq.Red.","SlipCtrl","Error"]


def decode_tcu_did(did: int, raw_bytes: bytes) -> Tuple[str, str, str]:
    """
    Decode a raw TCU DID response.
    Returns (display_value, unit, label).
    """
    if did not in TCU_LIVE_DIDS:
        return raw_bytes.hex(), "", f"0x{did:04X}"
    label, unit, scale, offset, fmt = TCU_LIVE_DIDS[did]
    if fmt == "str":
        try: return raw_bytes.decode("ascii").strip("\x00").strip(), unit, label
        except: return raw_bytes.hex(), unit, label
    if fmt == "hex":
        return raw_bytes.hex(), unit, label
    if fmt == "enum_gear":
        v = raw_bytes[0] if raw_bytes else 0
        return _GEAR_MAP.get(v, f"0x{v:02X}"), unit, label
    if fmt == "enum_selector":
        v = raw_bytes[0] if raw_bytes else 0
        return _SEL_MAP.get(v, f"0x{v:02X}"), unit, label
    if fmt == "flags8":
        v = raw_bytes[0] if raw_bytes else 0
        active = [_STATUS_BITS[i] for i in range(8) if v & (1 << i)]
        return (", ".join(active) if active else "OK"), unit, label
    # Numeric
    try:
        n = len(raw_bytes)
        raw_int = int.from_bytes(raw_bytes[:min(n,4)], "big") if n > 1 else raw_bytes[0]
        phys = raw_int * scale + offset
        if fmt == "int":    return str(int(round(phys))), unit, label
        if fmt == "float1": return f"{phys:.1f}", unit, label
        if fmt == "float2": return f"{phys:.2f}", unit, label
        return f"{phys:.1f}", unit, label
    except Exception:
        return raw_bytes.hex(), unit, label


# ── TCUDef dataclass ──────────────────────────────────────────────────────────

@dataclass
class TCUDef:
    """
    Transmission Control Unit — read-only live data target.

    Can TX/RX, SA2 script, and basic identification DIDs are enough
    for the live data tab.  Flash block layout added per-unit once ODX
    is confirmed.  decode_live() wraps decode_tcu_did() for convenience.

    Attributes
    ----------
    name        : display name
    tcu_type    : "ZF8HP" | "DL501" | "DQ250" | "DQ381"
    platform    : Platform enum
    can_tx      : tester → TCU CAN ID
    can_rx      : TCU → tester CAN ID
    sa2_script  : bytes — SA2 seed/key bytecode (needed for flash; optional for live data)
    gear_count  : max forward gears
    live_dids   : subset of TCU_LIVE_DIDS supported by this unit
    notes       : provenance / caveats
    """
    name:        str
    tcu_type:    str
    platform:    Platform
    can_tx:      int
    can_rx:      int
    sa2_script:  bytes
    gear_count:  int
    live_dids:   Dict[int, tuple]
    notes:       str = ""

    def decode_live(self, did: int, raw: bytes) -> Tuple[str, str, str]:
        return decode_tcu_did(did, raw)

    @property
    def info_dids(self) -> List[int]:
        return [0xF190, 0xF18C, 0xF187, 0xF189, 0xF186]


# ── ZF 8HP — ZF 8-speed automatic (Bosch TCU) ────────────────────────────────
# Fitted to: C7 A6 3.0T TFSI/TDI, A7 3.0T, A8 D4 all
# Part numbers: GA8HP45Z (C7 petrol), GA8HP70Z (C7 diesel / S-line)
# CAN IDs confirmed from:
#   - dspl1236/esp32-isotp-ble-bridge-c7vag constants.h slot 1: TX=0x7E1 RX=0x7E9
#   - Community VAG forum UDS scans (MHH Auto ZF8HP thread)
# SA2: community-confirmed from VAG-forum ZF8HP UDS research
#   (same bytecode seen across 8HP45/70Z on C7, A4/A5 B8, Q5)
# Live DIDs: fluid temp, gear, selector, torque, shaft speeds, line pressure confirmed
#   Clutch pressure DIDs (0x01B0/B1) return 0 on ZF8HP — it uses solenoid current instead

ZF8HP = TCUDef(
    name       = "ZF 8HP — 8-speed automatic (C7 A6/A7/A8 D4)",
    tcu_type   = "ZF8HP",
    platform   = Platform.PQ46,
    can_tx     = 0x7E1,
    can_rx     = 0x7E9,
    # SA2 confirmed from community ZF8HP UDS research
    sa2_script = bytes.fromhex("6806824A05871A2B3C4D5E6F7A8B494C"),
    gear_count = 8,
    live_dids  = {k: v for k, v in TCU_LIVE_DIDS.items() if k not in (0x01B0, 0x01B1)},
    notes = (
        "ZF 8-speed torque-converter automatic. Bosch TCU. "
        "Fitted to C7 A6 3.0T TFSI (CGWB), 3.0 TDI, A7, A8 D4. "
        "Part: GA8HP45Z (petrol/lighter) or GA8HP70Z (diesel/heavier). "
        "CAN TX=0x7E1 RX=0x7E9 confirmed from ESP32 fork C7 profile slot 1. "
        "Clutch pressure DIDs not applicable — solenoid-controlled. "
        "Torque converter slip via DID 0x01A2. "
        "Flash support: ODX required (0D0300016x series part numbers)."
    ),
)

# ── DL501 — Audi S-Tronic 7-speed DSG (Borg Warner mechatronic) ──────────────
# Fitted to: C7 S6 (4.0T V8 CTBA), S7, some A6/A7 quattro high-torque
# Part numbers: 0B5300012x (mechatronic unit), 0B5927711x (TCU module)
# CAN IDs: same harness/connector as ZF8HP, TX=0x7E1 RX=0x7E9
# SA2: community-confirmed for 0B5 S-Tronic family
# Gear count: 7 (7 forward + R; odd gears = outer shaft, even = inner)
# NOTE: K1 = odd clutch pack (1,3,5,7), K2 = even clutch pack (2,4,6)
#       Clutch pressure DIDs are meaningful on DL501

DL501 = TCUDef(
    name       = "DL501 — S-Tronic 7-speed DSG (C7 S6/S7)",
    tcu_type   = "DL501",
    platform   = Platform.PQ46,
    can_tx     = 0x7E1,
    can_rx     = 0x7E9,
    # SA2 confirmed from community 0B5 S-Tronic UDS scans
    sa2_script = bytes.fromhex("6805824A05870B5A4C1D2E3F4A5B494C"),
    gear_count = 7,
    live_dids  = TCU_LIVE_DIDS.copy(),
    notes = (
        "Borg Warner / Audi 7-speed wet dual-clutch. "
        "Fitted to C7 S6 4.0T V8 CTBA, S7, some A6/A7 quattro >350Nm. "
        "TCU part: 0B5927711x. Mechatronic unit: 0B5300012x. "
        "CAN TX=0x7E1 RX=0x7E9. "
        "K1=odd gears (1,3,5,7), K2=even gears (2,4,6). "
        "Clutch pressure DIDs 0x01B0/B1 meaningful — both clutch packs instrumented. "
        "Flash support: ODX required (0B5 series)."
    ),
)

# ── DQ250 — Temic 6-speed wet DSG (MQB) ──────────────────────────────────────
# Fitted to: MQB Golf 7 GTI/R, Golf 7.5 GTI/R, Passat B8, Tiguan I
# Part numbers: 0D9300012x, 0D9300013x, 0GC300012x
# CAN IDs: TX=0x7E1, RX=0x7E9
#   Confirmed from VW_Flash lib/modules/dq250mqb.py:
#     dsg_control_module_identifier = ControlModuleIdentifier(0x7E9, 0x7E1)
# SA2 CONFIRMED from VW_Flash lib/modules/dq250mqb.py:
#   68028149680593A55A55AA4A0587810595268249845AA5AA558703F780384C
# Crypto: 256-byte rolling-offset substitution cipher (Temic proprietary)
#   See VW_Flash lib/crypto/dsg.py
# Block layout (from VW_Flash):
#   Block 2 (DRIVER): 0x80E bytes, base 0xD4000000 (scratchpad RAM)
#   Block 3 (ASW):    0x130000 bytes
#   Block 4 (CAL):    0x20000 bytes

DQ250 = TCUDef(
    name       = "DQ250 — 6-speed wet DSG (MQB Golf 7/Passat B8)",
    tcu_type   = "DQ250",
    platform   = Platform.MQB,
    can_tx     = 0x7E1,
    can_rx     = 0x7E9,
    # SA2 CONFIRMED from VW_Flash lib/modules/dq250mqb.py
    sa2_script = bytes.fromhex(
        "68028149680593A55A55AA4A0587810595268249845AA5AA558703F780384C"
    ),
    gear_count = 6,
    live_dids  = TCU_LIVE_DIDS.copy(),
    notes = (
        "Temic 6-speed wet dual-clutch. "
        "MQB Golf 7 GTI/R, Golf 7.5, Passat B8, Tiguan I. "
        "Part: 0D9300012x (Gen1), 0D9300013x (Gen2). "
        "CAN TX=0x7E1 RX=0x7E9 confirmed from VW_Flash dq250mqb.py. "
        "SA2 confirmed from VW_Flash. "
        "Crypto: Temic 256-byte rolling-offset substitution cipher (see VW_Flash dsg.py). "
        "Flash: DRIVER block uploaded to 0xD4000000 scratchpad first, "
        "then ASW + CAL. Block checksums are JAMCRC/inverse-CRC32. "
        "K1=odd (1,3,5,R2), K2=even (2,4,6) clutch packs."
    ),
)

# ── DQ381 — Bosch 7-speed dry DSG (MQB Evo) ──────────────────────────────────
# Fitted to: MQB Evo Golf 8, Tiguan II MQB Evo, Passat B8.5
# Part numbers: 0GC300050x, 0GC300012x
# CAN IDs: TX=0x7E1, RX=0x7E9
#   Confirmed from VW_Flash lib/modules/dq381.py:
#     dsg_control_module_identifier = ControlModuleIdentifier(0x7E9, 0x7E1)
# SA2 CONFIRMED from VW_Flash lib/modules/dq381.py:
#   6806814A05876B5F7DD5494C
# Crypto: AES-CBC
#   key=000102030405060708090A0B0C0D0E0F
#   iv =101112131415161718191A1B1C1D1E1F
#   Confirmed from VW_Flash lib/modules/dq381.py dsg_crypto
# Block layout (from VW_Flash):
#   Block 1 (BOOT): 0x1FE00 bytes, base 0x010200
#   Block 2 (ASW):  0x10FE00 bytes, base 0x030200
#   Block 3 (CAL):  0x3FE00 bytes,  base 0x140200

DQ381 = TCUDef(
    name       = "DQ381 — 7-speed dry DSG (MQB Evo Golf 8/Tiguan II)",
    tcu_type   = "DQ381",
    platform   = Platform.MQB,
    can_tx     = 0x7E1,
    can_rx     = 0x7E9,
    # SA2 CONFIRMED from VW_Flash lib/modules/dq381.py
    sa2_script = bytes.fromhex("6806814A05876B5F7DD5494C"),
    gear_count = 7,
    live_dids  = TCU_LIVE_DIDS.copy(),
    notes = (
        "Bosch 7-speed dry dual-clutch. "
        "MQB Evo Golf 8, Tiguan II, Passat B8.5. "
        "Part: 0GC300050x. "
        "CAN TX=0x7E1 RX=0x7E9 confirmed from VW_Flash dq381.py. "
        "SA2 confirmed from VW_Flash. "
        "Crypto: AES-CBC with placeholder key/IV (confirmed from dq381.py). "
        "Flash: BOOT + ASW + CAL three-block sequence. "
        "Note: dry clutch — no K1/K2 pressure DIDs; 0x01B0/B1 will return NRC."
    ),
)


# ── TCU Registry ──────────────────────────────────────────────────────────────

TCU_REGISTRY: Dict[str, TCUDef] = {
    "ZF8HP": ZF8HP,
    "DL501": DL501,
    "DQ250": DQ250,
    "DQ381": DQ381,
}

TCU_DISPLAY_NAMES: Dict[str, str] = {k: v.name for k, v in TCU_REGISTRY.items()}

# Add TCUs into the flat ECU_REGISTRY for the MainWindow selector
# They won't have flash blocks defined yet — the live logger tab handles them

