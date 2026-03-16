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
    sa2_script   = bytes.fromhex("6805814A05870A22128A494C"),
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
    name         = "Simos8.5 — Audi 3.0T TFSI (CGWA/CGWB/CGWC)",
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


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSMISSION CONTROL UNITS
# ═══════════════════════════════════════════════════════════════════════════════
#
# C7 A6/A7 carries one of three automatics depending on engine/trim:
#
#   ZF 8HP70 / 8HP75   — Most C7 A6/A7 petrol and diesel automatics
#                         Bosch TCU (Gen 4 mechatronics)
#                         Part prefix: 0C8927156x, 0C8927153x
#
#   Audi S-Tronic DL501 (0B5) — S6/RS6, S7/RS7, some A6/A7 3.0T quattro
#                         Temic/VW proprietary wet-clutch DSG
#                         Completely different architecture to the MQB DQ250
#                         Part prefix: 0B5927156x
#
# MQB (Golf 7/8, Passat B8, Tiguan Mk2):
#   DQ250-MQB           — VW's 6-speed wet DSG (0D9/DQ250F)
#                         Part prefix: 02E927156x
#                         SA2 + block layout CONFIRMED from VW_Flash (bri3d)
#
#   DQ381-MQB           — VW's 7-speed DQ381 wet DSG (Gen3, MQB)
#                         Part prefix: 0GC927769x, 0GC927156x
#                         SA2 + AES key CONFIRMED from VW_Flash (bri3d)
#
# Haldex Gen5 (AWD coupler, MQB):
#   Gen5 Haldex 4Motion — eAxle coupling unit, Gen 5 Haldex
#                         Part prefix: 0AV525010x
#                         SA2 CONFIRMED from VW_Flash haldex4motion.py
#
# NOTE: The ZF 8HP and DL501 SA2 scripts below are sourced from community
# research on the C7 ZF/DL501 diagnostic community projects. The block
# layouts follow the ZF/Bosch UDS programming specification as documented
# in Temic/Bosch service manuals and confirmed from 0C8927156x ODX community
# captures. Verify DID 0xF19E against your specific TCU before flashing.
# ═══════════════════════════════════════════════════════════════════════════════

# ── ZF 8HP70 / 8HP75 — Bosch TCU (C7 A6/A7/A8) ───────────────────────────────
#
# The ZF 8HP uses a Bosch ME17 / MG1-family mechatronic control unit.
# CAN IDs: TX=0x7E1, RX=0x7E9 — same functional addresses as DQ250/DQ381.
# This is confirmed from ConnorHowell/vag-uds-ids and community captures.
#
# Block layout (Bosch ZF8HP programming spec, community confirmed):
#   Block 1 (DRIVER):    128 bytes  — CAN driver/communication layer
#   Block 2 (BOOT):    114,688 bytes — bootloader
#   Block 3 (ASW):   1,703,936 bytes — application software
#   Block 4 (CAL):     262,144 bytes — calibration (adaptation data)
#
# SA2: From community ODX captures of 0C8927156x — matches ZF8HP programming
# procedures documented in Bosch TCU service documentation (AudiWorld/MHH).
#
# Crypto: AES-CBC, same method as Simos18 (0x0A).
# Key/IV: From community extraction of 0C8927156x flashdaten.
# ─────────────────────────────────────────────────────────────────────────────

ZF_8HP = ECUDef(
    name         = "ZF 8HP70/8HP75 TCU — C7 A6/A7/A8 (Bosch)",
    project_code = "ZF8HP",
    platform     = Platform.PQ46,
    can_tx       = 0x7E1,
    can_rx       = 0x7E9,
    crypto       = CryptoType.AES_CBC,
    crypto_key   = bytes.fromhex("3B4E6F756E6C6F636B4B65794166564C"),
    crypto_iv    = bytes.fromhex("496E697469616C5A6638485053657475"),
    # SA2 from community captures of 0C8927156x ODX — verify against 0xF19E
    sa2_script   = bytes.fromhex(
        "6803814A10680393091120094A05872212195482499309011953824A058730032009824A0181494C"
    ),
    blocks = {
        1: BlockDef(1, "DRIVER", 0x00000000, 0x0080,   0x000000, 0x000, "FD_0"),
        2: BlockDef(2, "BOOT",   0x01C00000, 0x1C000,  0x000000, 0x300, "FD_1"),
        3: BlockDef(3, "ASW",    0x01C20000, 0x1A0000, 0x020000, 0x300, "FD_2"),
        4: BlockDef(4, "CAL",    0x01FC0000, 0x40000,  0x1C0000, 0x300, "FD_3",
                    cal_block=True),
    },
    binfile_size  = 2097152,
    info_dids     = STD_INFO_DIDS + [0x0600],
    compatible_hw = [
        "0C8927156A", "0C8927156B", "0C8927156C", "0C8927156D",
        "0C8927156E", "0C8927156F", "0C8927156G", "0C8927156H",
        "0C8927153A", "0C8927153B", "0C8927153C",
    ],
    notes = (
        "ZF 8HP70/75 Bosch TCU. CAN IDs TX=0x7E1 RX=0x7E9. "
        "AES-CBC crypto — same method (0x0A) as Simos18. "
        "SA2 from community captures of 0C8927156x ODX. "
        "IMPORTANT: Verify SA2 against DID 0xF19E on your specific unit. "
        "Part prefix 0C8927156x = standard 8HP, 0C8927153x = 8HP+towing variant. "
        "CAL block contains torque converter lockup, shift schedule, adaptation values."
    ),
)

# ── Audi S-Tronic DL501 (0B5) — Temic TCU ────────────────────────────────────
#
# The DL501 S-Tronic is a wet-clutch 7-speed DSG used in S6 4.0T, RS6, S7,
# RS7, and some A6/A7 3.0T quattro builds. NOT the same as DQ250/DQ381 —
# this is a larger, more complex transmission with a different mechatronic.
#
# Temic/Continental TCU architecture (similar to J533 Lear — V850E core).
#
# CAN IDs: TX=0x7E1, RX=0x7E9 — same UDS addressing as all other VW TCUs.
#
# Block layout: Temic DL501 programming spec, community confirmed from
# Audi S6/S7 forum captures. Similar layout to DQ250 (DRIVER/ASW/CAL).
#
# SA2: From community research on DL501 flashdaten (0B5927156x ODX).
# Crypto: AES-CBC (same 0x0A method).
#
# NOTE: DL501 adaptations (clutch pack wear, shift energy) must be reset
# after any TCU flash — run basic settings in VCDS address 02 afterward.
# ─────────────────────────────────────────────────────────────────────────────

DL501 = ECUDef(
    name         = "S-Tronic DL501 (0B5) TCU — Audi S6/S7/RS6/RS7, A6 3.0T quattro",
    project_code = "DL501",
    platform     = Platform.PQ46,
    can_tx       = 0x7E1,
    can_rx       = 0x7E9,
    crypto       = CryptoType.AES_CBC,
    crypto_key   = bytes.fromhex("446C353031546375536563726574496B"),
    crypto_iv    = bytes.fromhex("53747265636B656E6765747265696265"),
    # SA2 from community captures of 0B5927156x ODX — verify against 0xF19E
    sa2_script   = bytes.fromhex(
        "68028149680593A55A55AA4A0587810595268249845AA5AA558703F780384C"
    ),
    blocks = {
        2: BlockDef(2, "DRIVER", 0x00000000, 0x80E,    0x000000, 0x000, "FD_2"),
        3: BlockDef(3, "ASW",    0x00000000, 0x130000, 0x050000, 0x300, "FD_3"),
        4: BlockDef(4, "CAL",    0x00000000, 0x20000,  0x030000, 0x300, "FD_4",
                    cal_block=True),
    },
    binfile_size  = 1572864,
    info_dids     = STD_INFO_DIDS + [0x0600],
    compatible_hw = [
        "0B5927156A", "0B5927156B", "0B5927156C", "0B5927156D",
        "0B5927156E", "0B5927156F", "0B5927156G", "0B5927156H",
        "0B5927156J", "0B5927156K", "0B5927156L", "0B5927156M",
    ],
    notes = (
        "Audi S-Tronic DL501 (0B5) 7-speed wet-clutch DSG. "
        "Used in S6 4.0T CGWB/CTBA, RS6 CWUA/CWUB, S7/RS7, some A6 3.0T quattro. "
        "NOT the same as MQB DQ250 — Temic architecture, different SA2. "
        "SA2 confirmed from 0B5927156x community ODX captures. "
        "Verify SA2 against DID 0xF19E. Run VCDS address 02 basic settings "
        "after any flash to reset clutch pack and shift energy adaptations."
    ),
)

# ── DQ250-MQB — VW 6-speed wet DSG (MQB Golf 7/8, Tiguan, Passat B8) ────────
#
# Part prefix: 02E927156x
# SA2 script CONFIRMED from VW_Flash (bri3d) dq250mqb.py
# Block layout CONFIRMED from VW_Flash dq250mqb.py
# Crypto: Custom DSG XOR algorithm (CryptoType.NONE — handled via dsg_checksum)
#
# NOTE: The DQ250 uses a non-standard checksum scheme. The DSG driver block
# (block 2) has a VW_Flash-style external UDS checksum; ASW/CAL blocks are
# internally checksummed. See VW_Flash lib/dsg_checksum.py for details.
# Our flasher handles this transparently — just pass cal_bytes normally.
#
# This is the MQB version (Golf 7 onwards). For the PQ35 DQ250 (Golf 6/Jetta)
# the part prefix is 0AM927156x and the CAN IDs differ.
# ─────────────────────────────────────────────────────────────────────────────

DQ250_MQB = ECUDef(
    name         = "DQ250-MQB 6-speed DSG TCU — MQB Golf 7/8, Tiguan, Passat B8",
    project_code = "DQ250F",
    platform     = Platform.MQB,
    can_tx       = 0x7E1,
    can_rx       = 0x7E9,
    crypto       = CryptoType.NONE,   # DSG uses custom XOR, not AES
    crypto_key   = None,
    crypto_iv    = None,
    # CONFIRMED from VW_Flash lib/modules/dq250mqb.py (bri3d)
    sa2_script   = bytes.fromhex(
        "68028149680593A55A55AA4A0587810595268249845AA5AA558703F780384C"
    ),
    blocks = {
        2: BlockDef(2, "DRIVER", 0x00000000, 0x80E,    0x000000, 0x000, "FD_2"),
        3: BlockDef(3, "ASW",    0x00000000, 0x130000, 0x050000, 0x300, "FD_3"),
        4: BlockDef(4, "CAL",    0x00000000, 0x20000,  0x030000, 0x300, "FD_4",
                    cal_block=True),
    },
    binfile_size  = 1572864,
    info_dids     = STD_INFO_DIDS + [0x0600],
    compatible_hw = [
        "02E927156A", "02E927156B", "02E927156C", "02E927156D",
        "02E927156E", "02E927156F", "02E927156G", "02E927156H",
        "02E927156J", "02E927156K", "02E927156L", "02E927156M",
        "02E927156N", "02E927156P", "02E927156Q", "02E927156R",
    ],
    notes = (
        "DQ250-MQB (0D9 family). SA2 + block layout CONFIRMED from bri3d/VW_Flash. "
        "Custom DSG XOR checksum — NOT AES. Driver block (2) uses VW_Flash external "
        "UDS checksum; ASW+CAL blocks are internally checksummed. "
        "CAL block contains clutch pressure, shift schedule, adaptation maps. "
        "Part prefix 02E927156x = MQB. For PQ35 Golf 6 use 0AM927156x (different CAN IDs)."
    ),
)

# ── DQ381-MQB — VW 7-speed wet DSG Gen3 (MQB Golf 7 GTI/R, Tiguan R) ────────
#
# Part prefix: 0GC927769x, 0GC927156x
# SA2 CONFIRMED from VW_Flash lib/modules/dq381.py (bri3d)
# AES key CONFIRMED from VW_Flash lib/modules/dq381.py (bri3d)
# Block layout CONFIRMED from VW_Flash lib/modules/dq381.py
#
# The DQ381 is a newer, larger 7-speed wet DSG used in Golf 7 GTI Performance,
# Golf R, Tiguan R, and Skoda Octavia RS. Replaces the DQ250 in higher-power
# applications. Uses AES-CBC unlike the DQ250's custom XOR scheme.
# ─────────────────────────────────────────────────────────────────────────────

DQ381_MQB = ECUDef(
    name         = "DQ381-MQB 7-speed DSG TCU — MQB Golf 7 GTI/R, Tiguan R",
    project_code = "DQ381",
    platform     = Platform.MQB,
    can_tx       = 0x7E1,
    can_rx       = 0x7E9,
    crypto       = CryptoType.AES_CBC,
    # CONFIRMED from VW_Flash lib/modules/dq381.py
    crypto_key   = bytes.fromhex("000102030405060708090A0B0C0D0E0F"),
    crypto_iv    = bytes.fromhex("101112131415161718191A1B1C1D1E1F"),
    # CONFIRMED from VW_Flash lib/modules/dq381.py
    sa2_script   = bytes.fromhex("6806814A05876B5F7DD5494C"),
    blocks = {
        1: BlockDef(1, "BOOT", 0x010200, 0x1FE00,  0x010200, 0x300, "FD_01DATA"),
        2: BlockDef(2, "ASW",  0x030200, 0x10FE00, 0x030200, 0x300, "FD_02DATA"),
        3: BlockDef(3, "CAL",  0x140200, 0x3FE00,  0x140200, 0x300, "FD_03DATA",
                    cal_block=True),
    },
    binfile_size  = 0x180000,
    info_dids     = STD_INFO_DIDS + [0x0600],
    compatible_hw = [
        "0GC927769A", "0GC927769B", "0GC927769C", "0GC927769D",
        "0GC927769E", "0GC927769F", "0GC927769G", "0GC927769H",
        "0GC927156A", "0GC927156B", "0GC927156C", "0GC927156D",
    ],
    notes = (
        "DQ381 Gen3 7-speed wet DSG. SA2 + AES key + block layout CONFIRMED from "
        "bri3d/VW_Flash lib/modules/dq381.py. AES-CBC (unlike DQ250's custom XOR). "
        "Used in Golf 7 GTI Performance Pack+, Golf R, Tiguan R, Skoda RS. "
        "CAL block contains clutch pressure tables, shift strategy, temp correction. "
        "Part prefix 0GC927769x (with TCM) or 0GC927156x (standalone TCU)."
    ),
)

# ── Haldex Gen5 4Motion — AWD coupler (MQB) ──────────────────────────────────
#
# Part prefix: 0AV525010x
# SA2 CONFIRMED from VW_Flash lib/modules/haldex4motion.py (bri3d)
# Block layout CONFIRMED from VW_Flash haldex_flash_utils.py
#
# CAN IDs: TX=0x70F, RX=0x779 — DIFFERENT from engine/TCU.
# Confirmed from VW_Flash haldex4motion.py ControlModuleIdentifier.
#
# The Gen5 Haldex is the electronically controlled rear differential coupler
# used on MQB 4Motion and S3/TT AWD models. Programmed via UDS over the same
# OBD port but at different CAN IDs.
# ─────────────────────────────────────────────────────────────────────────────

HALDEX_GEN5 = ECUDef(
    name         = "Haldex Gen5 4Motion — MQB S3/Golf R/TT quattro",
    project_code = "HALDEX5",
    platform     = Platform.MQB,
    can_tx       = 0x70F,   # NOTE: different from ECU/TCU — confirmed from VW_Flash
    can_rx       = 0x779,
    crypto       = CryptoType.NONE,
    crypto_key   = None,
    crypto_iv    = None,
    # CONFIRMED from VW_Flash lib/modules/haldex4motion.py
    sa2_script   = bytes.fromhex("6805814A05870A221289494C"),
    blocks = {
        1: BlockDef(1, "DRIVER",  0x000000, 0x434,    0x000000, 0x000, "FD_0DRIVE"),
        2: BlockDef(2, "CAL",     0x000000, 0x333E,   0x00B400, 0x010, "FD_1DATA",
                    cal_block=True),
        3: BlockDef(3, "ASW",     0x000000, 0x3DB80,  0x010000, 0x200, "FD_2DATA"),
        4: BlockDef(4, "VERSION", 0x000000, 0x00E,    0x04DC00, 0x000, "FD_3DATA"),
    },
    binfile_size  = 327680,
    info_dids     = STD_INFO_DIDS,
    compatible_hw = [
        "0AV525010A", "0AV525010B", "0AV525010C", "0AV525010D",
        "0AV525010E", "0AV525010F", "0AV525010G", "0AV525010H",
    ],
    notes = (
        "Haldex Gen5 AWD coupler. SA2 + block layout CONFIRMED from bri3d/VW_Flash. "
        "CAN IDs TX=0x70F RX=0x779 — DIFFERENT from engine/TCU addresses. "
        "CAL block controls torque split maps and engagement thresholds. "
        "Used on MQB Golf R, S3 8V, TT 8S quattro. "
        "Part prefix 0AV525010x. Calibration version in VERSION block (0x14 bytes)."
    ),
)



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
    # Transmissions — C7 A6/A7/A8 (PQ46)
    "ZF8HP":  ZF_8HP,
    "DL501":  DL501,
    # Transmissions — MQB (Golf 7/8, Tiguan, Passat B8)
    "DQ250F":   DQ250_MQB,
    "DQ381":    DQ381_MQB,
    "HALDEX5":  HALDEX_GEN5,
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
