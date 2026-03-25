"""
flasher/checksum_simos.py — Simos8/12/18 checksum validation and fixing

Ported from VW_Flash (bri3d/VW_Flash) lib/checksum.py and lib/fastcrc.py.

Two checksum systems must be fixed before flashing a modified CAL:

1. CRC32 (per block)
   - Header at offset 0x300 in each block
   - CRC32 polynomial 0x4C11DB7, init 0x0, xor 0x0 (NOT standard Python crc32)
   - Covers areas listed in the header itself

2. ECM3 (CAL only)
   - 64-bit pure summation over CAL regions listed in ASW1 (offset 0x520)
   - Header at offset 0x400 in CAL
   - Needed because ECU monitoring process runs this check continuously at runtime

Both must be fixed or the ECU will reject the CAL after programming.
"""
from __future__ import annotations
import struct, logging

log = logging.getLogger("SimosSuite.Checksum")

# ── CRC32 table (0x4C11DB7 polynomial, NOT standard zlib CRC32) ────────────────
_CRC_TAB = [
    0x00000000,0x04C11DB7,0x09823B6E,0x0D4326D9,0x130476DC,0x17C56B6B,0x1A864DB2,0x1E475005,
    0x2608EDB8,0x22C9F00F,0x2F8AD6D6,0x2B4BCB61,0x350C9B64,0x31CD86D3,0x3C8EA00A,0x384FBDBD,
    0x4C11DB70,0x48D0C6C7,0x4593E01E,0x4152FDA9,0x5F15ADAC,0x5BD4B01B,0x569796C2,0x52568B75,
    0x6A1936C8,0x6ED82B7F,0x639B0DA6,0x675A1011,0x791D4014,0x7DDC5DA3,0x709F7B7A,0x745E66CD,
    0x9823B6E0,0x9CE2AB57,0x91A18D8E,0x95609039,0x8B27C03C,0x8FE6DD8B,0x82A5FB52,0x8664E6E5,
    0xBE2B5B58,0xBAEA46EF,0xB7A96036,0xB3687D81,0xAD2F2D84,0xA9EE3033,0xA4AD16EA,0xA06C0B5D,
    0xD4326D90,0xD0F37027,0xDDB056FE,0xD9714B49,0xC7361B4C,0xC3F706FB,0xCEB42022,0xCA753D95,
    0xF23A8028,0xF6FB9D9F,0xFBB8BB46,0xFF79A6F1,0xE13EF6F4,0xE5FFEB43,0xE8BCCD9A,0xEC7DD02D,
    0x34867077,0x30476DC0,0x3D044B19,0x39C556AE,0x278206AB,0x23431B1C,0x2E003DC5,0x2AC12072,
    0x128E9DCF,0x164F8078,0x1B0CA6A1,0x1FCDBB16,0x018AEB13,0x054BF6A4,0x0808D07D,0x0CC9CDCA,
    0x7897AB07,0x7C56B6B0,0x71159069,0x75D48DDE,0x6B93DDDB,0x6F52C06C,0x6211E6B5,0x66D0FB02,
    0x5E9F46BF,0x5A5E5B08,0x571D7DD1,0x53DC6066,0x4D9B3063,0x495A2DD4,0x44190B0D,0x40D816BA,
    0xACA5C697,0xA864DB20,0xA527FDF9,0xA1E6E04E,0xBFA1B04B,0xBB60ADFC,0xB6238B25,0xB2E29692,
    0x8AAD2B2F,0x8E6C3698,0x832F1041,0x87EE0DF6,0x99A95DF3,0x9D684044,0x902B669D,0x94EA7B2A,
    0xE0B41DE7,0xE4750050,0xE9362689,0xEDF73B3E,0xF3B06B3B,0xF771768C,0xFA325055,0xFEF34DE2,
    0xC6BCF05F,0xC27DEDE8,0xCF3ECB31,0xCBFFD686,0xD5B88683,0xD1799B34,0xDC3ABDED,0xD8FBA05A,
    0x690CE0EE,0x6DCDFD59,0x608EDB80,0x644FC637,0x7A089632,0x7EC98B85,0x738AAD5C,0x774BB0EB,
    0x4F040D56,0x4BC510E1,0x46863638,0x42472B8F,0x5C007B8A,0x58C1663D,0x558240E4,0x51435D53,
    0x251D3B9E,0x21DC2629,0x2C9F00F0,0x285E1D47,0x36194D42,0x32D850F5,0x3F9B762C,0x3B5A6B9B,
    0x0315D626,0x07D4CB91,0x0A97ED48,0x0E56F0FF,0x1011A0FA,0x14D0BD4D,0x19939B94,0x1D528623,
    0xF12F560E,0xF5EE4BB9,0xF8AD6D60,0xFC6C70D7,0xE22B20D2,0xE6EA3D65,0xEBA91BBC,0xEF68060B,
    0xD727BBB6,0xD3E6A601,0xDEA580D8,0xDA649D6F,0xC423CD6A,0xC0E2D0DD,0xCDA1F604,0xC960EBB3,
    0xBD3E8D7E,0xB9FF90C9,0xB4BCB610,0xB07DABA7,0xAE3AFBA2,0xAAFBE615,0xA7B8C0CC,0xA379DD7B,
    0x9B3660C6,0x9FF77D71,0x92B45BA8,0x9675461F,0x8832161A,0x8CF30BAD,0x81B02D74,0x857130C3,
    0x5D8A9099,0x594B8D2E,0x5408ABF7,0x50C9B640,0x4E8EE645,0x4A4FFBF2,0x470CDD2B,0x43CDC09C,
    0x7B827D21,0x7F436096,0x7200464F,0x76C15BF8,0x68860BFD,0x6C47164A,0x61043093,0x65C52D24,
    0x119B4BE9,0x155A565E,0x18197087,0x1CD86D30,0x029F3D35,0x065E2082,0x0B1D065B,0x0FDC1BEC,
    0x3793A651,0x3352BBE6,0x3E119D3F,0x3AD08088,0x2497D08D,0x2056CD3A,0x2D15EBE3,0x29D4F654,
    0xC5A92679,0xC1683BCE,0xCC2B1D17,0xC8EA00A0,0xD6AD50A5,0xD26C4D12,0xDF2F6BCB,0xDBEE767C,
    0xE3A1CBC1,0xE760D676,0xEA23F0AF,0xEEE2ED18,0xF0A5BD1D,0xF464A0AA,0xF9278673,0xFDE69BC4,
    0x89B8FD09,0x8D79E0BE,0x803AC667,0x84FBDBD0,0x9ABC8BD5,0x9E7D9662,0x933EB0BB,0x97FFAD0C,
    0xAFB010B1,0xAB710D06,0xA6322BDF,0xA2F33668,0xBCB4666D,0xB8757BDA,0xB5365D03,0xB1F740B4,
]

def crc32_vw(data: bytes) -> int:
    """CRC32 using VW polynomial 0x4C11DB7 (NOT standard zlib CRC32)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF00) ^ _CRC_TAB[((crc >> 24) & 0xFF) ^ b]
    return crc & 0xFFFFFFFF


# ── Block base addresses for Simos8.5 ─────────────────────────────────────────
S8_BASE_ADDRESSES = {
    1: 0x80020000,  # CBOOT
    2: 0x80080000,  # ASW1
    3: 0xA0040000,  # CAL
    6: 0xA0040000,  # CBOOT_TEMP
}

S8_CHECKSUM_HEADER_OFFSETS = {
    0: 0x300,  # SBOOT
    1: 0x300,  # CBOOT
    2: 0x300,  # ASW1
    3: 0x300,  # CAL
    6: 0x340,  # CBOOT_TEMP
}

ECM3_CAL_CHECKSUM_OFFSET       = 0x400   # ECM3 checksum header in CAL
ECM3_ASW1_ADDRESSES_OFFSET_LATE  = 0x520  # ECM3 area addresses in ASW1 — late cars (2012+)
ECM3_ASW1_ADDRESSES_OFFSET_EARLY = 0x540  # ECM3 area addresses in ASW1 — early cars (pre-2012)
# Default to late variant (0x520) — confirmed correct for 2013 C7 A6/A7 3.0T TFSI (CGWB/CTUA)
# Use detect_ecm3_asw1_offset() to auto-detect if you're unsure which variant your ASW1 is
ECM3_ASW1_ADDRESSES_OFFSET = ECM3_ASW1_ADDRESSES_OFFSET_LATE


def validate_crc32(data: bytes, block_num: int,
                   base_addresses=None, header_offsets=None) -> tuple[bool, int, int]:
    """
    Read and validate the CRC32 checksum for a block.
    Returns (is_valid, stored_checksum, calculated_checksum).
    """
    base  = (base_addresses  or S8_BASE_ADDRESSES ).get(block_num, 0)
    hoff  = (header_offsets  or S8_CHECKSUM_HEADER_OFFSETS).get(block_num, 0x300)

    stored  = struct.unpack_from("<I", data, hoff + 4)[0]
    n_areas = data[hoff + 8]

    checksum_data = bytearray()
    for i in range(n_areas):
        start_abs = struct.unpack_from("<I", data, hoff + 12 + i * 8)[0]
        end_abs   = struct.unpack_from("<I", data, hoff + 16 + i * 8)[0]
        start_off = start_abs - base
        end_off   = end_abs   - base
        log.debug("CRC32 area: 0x%08X–0x%08X (offsets 0x%X–0x%X)",
                  start_abs, end_abs, start_off, end_off)
        checksum_data += data[start_off:end_off + 1]

    calculated = crc32_vw(bytes(checksum_data))
    return (stored == calculated, stored, calculated)


def fix_crc32(data: bytes, block_num: int,
              base_addresses=None, header_offsets=None) -> bytes:
    """Fix the CRC32 checksum in-place. Returns corrected bytes."""
    hoff = (header_offsets or S8_CHECKSUM_HEADER_OFFSETS).get(block_num, 0x300)
    valid, stored, calculated = validate_crc32(data, block_num, base_addresses, header_offsets)

    if valid:
        log.info("CRC32 block %d: already valid (0x%08X)", block_num, stored)
        return data

    log.info("CRC32 block %d: fixing 0x%08X -> 0x%08X", block_num, stored, calculated)
    out = bytearray(data)
    struct.pack_into("<I", out, hoff + 4, calculated)
    return bytes(out)


def detect_ecm3_asw1_offset(asw1_data: bytes,
                              cal_base: int = 0xA0040000,
                              cal_size: int = 0x3C000) -> int:
    """
    Auto-detect whether this ASW1 is an early (0x540) or late (0x520) ECM3 variant.

    Reads the first area start address from each candidate offset and checks
    whether it falls within the CAL address range. Returns the offset that
    produces valid addresses, defaulting to the late variant (0x520) if both
    or neither are valid.

    VW_Flash reference:
        ecm3_cal_monitor_addresses       = 0x520  (late, most cars from ~2012)
        ecm3_cal_monitor_addresses_early = 0x540  (early, pre-2012 builds)
    """
    cal_end = cal_base + cal_size

    def _is_valid(offset):
        if offset + 4 > len(asw1_data):
            return False
        addr = struct.unpack_from("<I", asw1_data, offset)[0]
        return cal_base <= addr < cal_end

    late_ok  = _is_valid(ECM3_ASW1_ADDRESSES_OFFSET_LATE)
    early_ok = _is_valid(ECM3_ASW1_ADDRESSES_OFFSET_EARLY)

    if late_ok and not early_ok:
        log.info("ECM3 variant: LATE (0x%X)", ECM3_ASW1_ADDRESSES_OFFSET_LATE)
        return ECM3_ASW1_ADDRESSES_OFFSET_LATE
    elif early_ok and not late_ok:
        log.info("ECM3 variant: EARLY (0x%X)", ECM3_ASW1_ADDRESSES_OFFSET_EARLY)
        return ECM3_ASW1_ADDRESSES_OFFSET_EARLY
    else:
        # Both valid or neither — default to late (correct for 2013 C7 A6/A7 3.0T)
        log.info(
            "ECM3 variant: defaulting to LATE (0x%X) — both=%s neither=%s",
            ECM3_ASW1_ADDRESSES_OFFSET_LATE, late_ok and early_ok, not late_ok and not early_ok
        )
        return ECM3_ASW1_ADDRESSES_OFFSET_LATE


def validate_ecm3(cal_data: bytes, asw1_data: bytes = None,
                  asw1_offset: int = None) -> tuple[bool, int, int]:
    """
    Validate the ECM3 64-bit summation checksum in CAL.

    ECM3 area addresses are read from ASW1. Two ASW1 offset variants exist:
      - Late  (0x520): 2012+ cars — default, correct for 2013 C7 A6/A7 3.0T TFSI
      - Early (0x540): pre-2012 builds

    Pass asw1_offset= explicitly to override auto-detection, or omit to
    let detect_ecm3_asw1_offset() pick the right variant automatically.

    Returns (is_valid, stored_checksum, calculated_checksum).
    """
    hoff = ECM3_CAL_CHECKSUM_OFFSET

    # Read number of areas and addresses
    n_areas = struct.unpack_from("<I", cal_data, hoff + 16)[0]

    # Try to read addresses from CAL first (older ECUs)
    cal_addr = struct.unpack_from("<I", cal_data, hoff + 24)[0]
    if cal_addr > 0 and asw1_data is None:
        # Addresses are embedded in CAL (very old ECUs)
        addr_data   = cal_data
        addr_offset = hoff + 24
    elif asw1_data is not None:
        # Determine which ASW1 variant to use
        if asw1_offset is not None:
            addr_offset = asw1_offset
        else:
            addr_offset = detect_ecm3_asw1_offset(asw1_data)
        addr_data = asw1_data
    else:
        log.warning("ECM3: no ASW1 provided and CAL has no embedded addresses")
        return (False, 0, 0)

    base = S8_BASE_ADDRESSES[3]  # CAL base address
    addresses = []
    for i in range(n_areas * 2):
        abs_addr = struct.unpack_from("<I", addr_data, addr_offset + i * 4)[0]
        offset   = abs_addr - base
        if offset < 0:
            # Try cached memory offset
            offset = abs_addr + 0x20000000 - base
        addresses.append(offset)

    # Calculate 64-bit summation
    checksum = 0
    for i in range(0, len(addresses), 2):
        start, end = int(addresses[i]), int(addresses[i+1])
        for j in range(start, end + 1, 4):
            checksum += struct.unpack_from("<I", cal_data, j)[0]

    checksum &= 0xFFFFFFFFFFFFFFFF  # keep 64-bit

    # Read stored checksum (64-bit little-endian)
    stored_lo = struct.unpack_from("<I", cal_data, hoff)[0]
    stored_hi = struct.unpack_from("<I", cal_data, hoff + 4)[0]
    stored    = (stored_hi << 32) | stored_lo

    return (stored == checksum, stored, checksum)


def fix_ecm3(cal_data: bytes, asw1_data: bytes = None,
             asw1_offset: int = None) -> bytes:
    """
    Fix the ECM3 64-bit checksum in CAL. Returns corrected CAL bytes.

    asw1_offset: override ECM3 ASW1 variant (0x520=late, 0x540=early).
    If omitted, auto-detected from asw1_data content.
    """
    hoff = ECM3_CAL_CHECKSUM_OFFSET
    valid, stored, calculated = validate_ecm3(cal_data, asw1_data, asw1_offset)

    if valid:
        log.info("ECM3: already valid (0x%016X)", stored)
        return cal_data

    log.info("ECM3: fixing 0x%016X -> 0x%016X", stored, calculated)
    out = bytearray(cal_data)
    struct.pack_into("<I", out, hoff,     calculated & 0xFFFFFFFF)
    struct.pack_into("<I", out, hoff + 4, (calculated >> 32) & 0xFFFFFFFF)
    return bytes(out)


def xor_encrypt(data: bytes) -> bytes:
    """
    Simos XOR encryption (from VW_Flash simos_xor.py).
    Counter XOR: byte[i] ^ (i % 256)
    Symmetric — encrypt == decrypt.
    DataFormatIdentifier: compression=0xA, encryption=0xA
    """
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ (i & 0xFF)
    return bytes(out)
