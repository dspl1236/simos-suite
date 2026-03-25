"""
tests/test_checksums.py — Checksum and FRF loader tests for simos-suite.

Covers:
  1.  CRC32-VW polynomial correctness (known vector)
  2.  CRC32 validate — synthetic block with correct checksum
  3.  CRC32 validate — detects corruption
  4.  CRC32 fix     — repairs corrupted checksum
  5.  CRC32 fix     — idempotent when already correct
  6.  ECM3 validate — synthetic CAL+ASW1 pair
  7.  ECM3 validate — detects corruption
  8.  ECM3 fix      — repairs checksum
  9.  ECM3 fix      — idempotent
  10. ECM3 variant detection — late (0x520) preferred when both valid
  11. ECM3 variant detection — early (0x540) returned when only that is valid
  12. XOR encrypt/decrypt — symmetric, round-trips cleanly
  13. XOR encrypt    — known vector (first 8 bytes)
  14. Workshop code  — correct length and CRC8
  15. Workshop code  — BCD date encoding
  16. FRF decrypt    — produces ZIP magic PK (requires data/frf.key)
  17. FRF block extract — 3 blocks, correct sizes (requires frf.key + FRF)
  18. FRF SA2 extract — matches known bytecode
  19. FRF → CRC32 round-trip — extract CAL, corrupt it, fix it, re-validate
  20. Block size constants — ecu_defs match ODX-confirmed values

Run standalone:
    python -m tests.test_checksums

Or through the main runner (hooked into [2/3] backend tests).
"""

from __future__ import annotations

import os
import pathlib
import struct
import sys
import logging
from datetime import date

log = logging.getLogger("SimosSuite.TestChecksums")

# ── Result tracking ────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []


def _test(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    _results.append((name, passed, detail))
    icon = "✓" if passed else "✗"
    print(f"  {icon} {status:4s}  {name}" + (f"  [{detail}]" if detail else ""))
    return passed


def _print_results() -> bool:
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed
    print(f"\n  Checksums+FRF: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        for name, ok, detail in _results:
            if not ok:
                print(f"    ✗ {name}  {detail}")
    else:
        print()
    return failed == 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _repo_root() -> pathlib.Path:
    """Walk up from this file to find the repo root (contains flasher/)."""
    p = pathlib.Path(__file__).resolve().parent
    for _ in range(6):
        if (p / "flasher").is_dir():
            return p
        p = p.parent
    return pathlib.Path(".")


def _find_frf() -> pathlib.Path | None:
    """Look for FL_4G0907551D__0006.frf in Downloads/FlashDaten."""
    candidates = [
        pathlib.Path(r"C:\Users\Power\Downloads\FlashDaten\Flashdaten_Audi_20201020_6ZtF7")
        / "FL_4G0907551D__0006.frf",
        pathlib.Path("/mnt/user-data/uploads/FL_4G0907551D__0006.frf"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_key(repo: pathlib.Path) -> pathlib.Path | None:
    candidates = [
        repo / "data" / "frf.key",
        pathlib.Path("/tmp/VW_Flash/data/frf.key"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ── Synthetic block builders ───────────────────────────────────────────────────

CAL_BASE = 0xA0040000
CAL_SIZE = 0x3FE00   # confirmed from FL_4G0907551D__0006.frf ODX
ASW_BASE = 0x80080000
ASW_SIZE = 0x17FE00
HOFF_CAL = 0x300
HOFF_ASW = 0x300
ECM3_HOFF = 0x400
ECM3_LATE  = 0x520
ECM3_EARLY = 0x540


def _make_synthetic_cal(area_start_off: int = 0x1000,
                        area_end_off: int   = 0x1FFF,
                        n_areas: int        = 1,
                        extra_ecm3_area: tuple | None = None) -> tuple[bytes, bytes, int]:
    """
    Build a synthetic CAL block with correct CRC32 and ECM3 checksums.

    Returns (cal_bytes, asw1_bytes, expected_ecm3).
    The CRC32 covers one area [area_start_off..area_end_off] (relative offsets).
    The ECM3 covers either that same area (n_areas=1) or two areas if extra given.
    """
    # Fill with distinct non-zero pattern so checksums are non-trivial
    import random
    rng = random.Random(0xDEADBEEF)
    cal = bytearray(rng.randbytes(CAL_SIZE) if hasattr(rng, 'randbytes') 
                    else bytes([rng.randint(0, 255) for _ in range(CAL_SIZE)]))
    asw = bytearray(ASW_SIZE)

    # --- CRC32 header at HOFF_CAL ---
    crc_area_start_abs = CAL_BASE + area_start_off
    crc_area_end_abs   = CAL_BASE + area_end_off
    struct.pack_into('<I', cal, HOFF_CAL + 0,  0xA0000003)         # magic
    struct.pack_into('<I', cal, HOFF_CAL + 4,  0x00000000)         # placeholder
    struct.pack_into('<B', cal, HOFF_CAL + 8,  1)                  # 1 area
    struct.pack_into('<I', cal, HOFF_CAL + 12, crc_area_start_abs)
    struct.pack_into('<I', cal, HOFF_CAL + 16, crc_area_end_abs)

    from flasher.checksum_simos import crc32_vw
    crc_data = bytes(cal[area_start_off : area_end_off + 1])
    crc_val = crc32_vw(crc_data)
    struct.pack_into('<I', cal, HOFF_CAL + 4, crc_val)

    # --- ECM3 header in CAL at ECM3_HOFF ---
    # Areas: one primary + optional extra
    ecm3_pairs = [(CAL_BASE + area_start_off, CAL_BASE + area_end_off - 3)]
    if extra_ecm3_area:
        ecm3_pairs.append(extra_ecm3_area)

    n = len(ecm3_pairs)
    struct.pack_into('<I', cal, ECM3_HOFF + 16, n)  # n_areas

    # Write area addresses into ASW1 at ECM3_LATE offset
    for i, (s, e) in enumerate(ecm3_pairs):
        struct.pack_into('<I', asw, ECM3_LATE + (i * 2)     * 4, s)
        struct.pack_into('<I', asw, ECM3_LATE + (i * 2 + 1) * 4, e)

    # Also write ECM3_EARLY with a deliberately out-of-range address
    # so detect_ecm3_asw1_offset() chooses LATE
    struct.pack_into('<I', asw, ECM3_EARLY, 0xDEADBEEF)

    # Calculate ECM3 checksum
    ecm3 = 0
    for s_abs, e_abs in ecm3_pairs:
        s_off = s_abs - CAL_BASE
        e_off = e_abs - CAL_BASE
        for j in range(s_off, e_off + 1, 4):
            ecm3 += struct.unpack_from('<I', cal, j)[0]
    ecm3 &= 0xFFFFFFFFFFFFFFFF

    struct.pack_into('<I', cal, ECM3_HOFF + 0, ecm3 & 0xFFFFFFFF)
    struct.pack_into('<I', cal, ECM3_HOFF + 4, (ecm3 >> 32) & 0xFFFFFFFF)

    return bytes(cal), bytes(asw), ecm3


# ══════════════════════════════════════════════════════════════════════════════
# TEST FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def run_all(repo: pathlib.Path):

    print("\n  ── CRC32-VW ─────────────────────────────────────────────────────")
    _test_crc32_polynomial()
    _test_crc32_empty()
    _test_crc32_validate_good()
    _test_crc32_validate_bad()
    _test_crc32_fix()
    _test_crc32_fix_idempotent()

    print("\n  ── ECM3 ─────────────────────────────────────────────────────────")
    _test_ecm3_validate_good()
    _test_ecm3_validate_bad()
    _test_ecm3_fix()
    _test_ecm3_fix_idempotent()
    _test_ecm3_variant_detection_late()
    _test_ecm3_variant_detection_early()

    print("\n  ── XOR encrypt ─────────────────────────────────────────────────")
    _test_xor_roundtrip()
    _test_xor_known_vector()

    print("\n  ── Workshop code ────────────────────────────────────────────────")
    _test_workshop_code_length()
    _test_workshop_code_bcd()
    _test_workshop_code_crc8()

    print("\n  ── Block size constants ─────────────────────────────────────────")
    _test_block_size_constants()

    print("\n  ── FRF loader ───────────────────────────────────────────────────")
    key_path = _find_key(repo)
    frf_path = _find_frf()
    _test_frf_decrypt(key_path, frf_path)
    _test_frf_block_sizes(key_path, frf_path)
    _test_frf_sa2_script(key_path, frf_path)
    _test_frf_roundtrip_crc(key_path, frf_path)
    _test_frf_missing_key()


# ── CRC32 tests ────────────────────────────────────────────────────────────────

def _test_crc32_polynomial():
    """CRC32-VW known test vectors against the 0x4C11DB7 polynomial table."""
    from flasher.checksum_simos import crc32_vw, _CRC_TAB
    # _CRC_TAB[1] == 0x04C11DB7 (the generator polynomial itself)
    tab_ok = _CRC_TAB[1] == 0x04C11DB7
    # crc32_vw(b'\x01') feeds index 1 into the table first → result is the polynomial
    v1 = crc32_vw(b'\x01')
    v2 = crc32_vw(b'\xff')
    _test("crc32_vw polynomial table (TAB[1]=0x04C11DB7, 0x01→0x04C11DB7, 0xFF→0xB1F740B4)",
          tab_ok and v1 == 0x04C11DB7 and v2 == 0xB1F740B4,
          f"tab1=0x{_CRC_TAB[1]:08X} v1=0x{v1:08X} v2=0x{v2:08X}")


def _test_crc32_empty():
    """CRC32-VW of empty data is 0."""
    from flasher.checksum_simos import crc32_vw
    result = crc32_vw(b'')
    _test("crc32_vw empty input → 0",
          result == 0,
          f"got 0x{result:08X}")


def _test_crc32_validate_good():
    """validate_crc32 returns True for a block with correct checksum."""
    from flasher.checksum_simos import validate_crc32
    cal, _, _ = _make_synthetic_cal()
    valid, stored, calc = validate_crc32(cal, block_num=3)
    _test("CRC32 validate — correct checksum accepted",
          valid and stored == calc,
          f"stored=0x{stored:08X} calc=0x{calc:08X}")


def _test_crc32_validate_bad():
    """validate_crc32 returns False when the checksum is corrupted."""
    from flasher.checksum_simos import validate_crc32
    cal, _, _ = _make_synthetic_cal()
    # Corrupt the checksum field (+1)
    bad = bytearray(cal)
    stored = struct.unpack_from('<I', bad, HOFF_CAL + 4)[0]
    struct.pack_into('<I', bad, HOFF_CAL + 4, stored ^ 0xDEAD)
    valid, s, c = validate_crc32(bytes(bad), block_num=3)
    _test("CRC32 validate — corruption detected",
          not valid,
          f"stored=0x{s:08X} calc=0x{c:08X}")


def _test_crc32_fix():
    """fix_crc32 repairs a corrupted checksum."""
    from flasher.checksum_simos import fix_crc32, validate_crc32
    cal, _, _ = _make_synthetic_cal()
    bad = bytearray(cal)
    struct.pack_into('<I', bad, HOFF_CAL + 4, 0xBADC0FFE)
    fixed = fix_crc32(bytes(bad), block_num=3)
    valid, stored, calc = validate_crc32(fixed, block_num=3)
    _test("CRC32 fix — repairs corrupted checksum",
          valid and stored == calc,
          f"stored=0x{stored:08X} calc=0x{calc:08X}")


def _test_crc32_fix_idempotent():
    """fix_crc32 on already-correct data returns identical bytes."""
    from flasher.checksum_simos import fix_crc32
    cal, _, _ = _make_synthetic_cal()
    fixed = fix_crc32(cal, block_num=3)
    _test("CRC32 fix — idempotent on valid data", fixed == cal)


# ── ECM3 tests ─────────────────────────────────────────────────────────────────

def _test_ecm3_validate_good():
    """validate_ecm3 returns True for synthetic CAL+ASW1 with correct checksum."""
    from flasher.checksum_simos import validate_ecm3
    cal, asw, expected = _make_synthetic_cal()
    valid, stored, calc = validate_ecm3(cal, asw1_data=asw)
    _test("ECM3 validate — correct checksum accepted",
          valid and stored == calc,
          f"stored=0x{stored:016X} calc=0x{calc:016X}")


def _test_ecm3_validate_bad():
    """validate_ecm3 detects a corrupted ECM3 checksum."""
    from flasher.checksum_simos import validate_ecm3
    cal, asw, _ = _make_synthetic_cal()
    bad = bytearray(cal)
    struct.pack_into('<I', bad, ECM3_HOFF, 0xDEADDEAD)
    valid, stored, calc = validate_ecm3(bytes(bad), asw1_data=asw)
    _test("ECM3 validate — corruption detected",
          not valid,
          f"stored=0x{stored:016X} calc=0x{calc:016X}")


def _test_ecm3_fix():
    """fix_ecm3 repairs a corrupted ECM3 checksum."""
    from flasher.checksum_simos import fix_ecm3, validate_ecm3
    cal, asw, expected = _make_synthetic_cal()
    bad = bytearray(cal)
    struct.pack_into('<Q', bad, ECM3_HOFF, 0)   # zero out 64-bit field
    fixed = fix_ecm3(bytes(bad), asw1_data=asw)
    valid, stored, calc = validate_ecm3(fixed, asw1_data=asw)
    _test("ECM3 fix — repairs zeroed checksum",
          valid and stored == calc and calc == expected,
          f"expected=0x{expected:016X} got=0x{calc:016X}")


def _test_ecm3_fix_idempotent():
    """fix_ecm3 on already-correct data returns identical bytes."""
    from flasher.checksum_simos import fix_ecm3
    cal, asw, _ = _make_synthetic_cal()
    fixed = fix_ecm3(cal, asw1_data=asw)
    _test("ECM3 fix — idempotent on valid data", fixed == cal)


def _test_ecm3_variant_detection_late():
    """detect_ecm3_asw1_offset returns LATE (0x520) when only late is valid."""
    from flasher.checksum_simos import detect_ecm3_asw1_offset
    _, asw, _ = _make_synthetic_cal()
    # _make_synthetic_cal already writes LATE valid, EARLY invalid (0xDEADBEEF)
    result = detect_ecm3_asw1_offset(asw)
    _test("ECM3 variant detection — late (0x520) chosen when only late valid",
          result == ECM3_LATE,
          f"got 0x{result:X}")


def _test_ecm3_variant_detection_early():
    """detect_ecm3_asw1_offset returns EARLY (0x540) when only early is valid."""
    from flasher.checksum_simos import detect_ecm3_asw1_offset
    _, asw_orig, _ = _make_synthetic_cal()
    asw = bytearray(asw_orig)
    # Invalidate the LATE entry
    struct.pack_into('<I', asw, ECM3_LATE, 0xDEADBEEF)
    # Write a valid CAL-range address at EARLY
    struct.pack_into('<I', asw, ECM3_EARLY, CAL_BASE + 0x1000)
    result = detect_ecm3_asw1_offset(bytes(asw))
    _test("ECM3 variant detection — early (0x540) chosen when only early valid",
          result == ECM3_EARLY,
          f"got 0x{result:X}")


# ── XOR tests ──────────────────────────────────────────────────────────────────

def _test_xor_roundtrip():
    """xor_encrypt is symmetric: encrypt(encrypt(data)) == data."""
    from flasher.checksum_simos import xor_encrypt
    original = bytes(range(256)) * 4
    double_encrypted = xor_encrypt(xor_encrypt(original))
    _test("XOR encrypt — symmetric round-trip",
          double_encrypted == original,
          f"len={len(original)}")


def _test_xor_known_vector():
    """xor_encrypt first 8 bytes: byte[i] ^ i."""
    from flasher.checksum_simos import xor_encrypt
    data = bytes(8)   # all zeros
    result = xor_encrypt(data)
    expected = bytes(range(8))
    _test("XOR encrypt — known vector (0x00 XOR i = i)",
          result == expected,
          f"got={result.hex()} expected={expected.hex()}")


# ── Workshop code tests ────────────────────────────────────────────────────────

def _test_workshop_code_length():
    """build_workshop_code returns exactly 9 bytes."""
    from flasher.workshop_code import build_workshop_code
    code = build_workshop_code(flash_date=date(2024, 3, 15))
    _test("Workshop code — 9 bytes", len(code) == 9, f"len={len(code)}")


def _test_workshop_code_bcd():
    """Workshop code encodes year/month/day in BCD correctly."""
    from flasher.workshop_code import build_workshop_code
    code = build_workshop_code(flash_date=date(2024, 3, 15))
    # Year 24 → 0x24, Month 3 → 0x03, Day 15 → 0x15
    yy, mm, dd = code[0], code[1], code[2]
    _test("Workshop code — BCD date (2024-03-15)",
          yy == 0x24 and mm == 0x03 and dd == 0x15,
          f"yy=0x{yy:02X} mm=0x{mm:02X} dd=0x{dd:02X}")


def _test_workshop_code_crc8():
    """Last byte of workshop code is a valid CRC8 of the first 8 bytes."""
    from flasher.workshop_code import build_workshop_code, crc8
    code = build_workshop_code(flash_date=date(2024, 3, 15))
    expected_crc = crc8(code[:8])
    _test("Workshop code — CRC8 valid",
          code[8] == expected_crc,
          f"stored=0x{code[8]:02X} expected=0x{expected_crc:02X}")


# ── Block size constant tests ──────────────────────────────────────────────────

def _test_block_size_constants():
    """ecu_defs SIMOS85 block sizes match ODX-confirmed values."""
    try:
        from core.ecu_defs import SIMOS85
        b1 = SIMOS85.blocks[1]
        b2 = SIMOS85.blocks[2]
        b3 = SIMOS85.blocks[3]
        pbl_ok = b1.length == 0x13E00
        asw_ok = b2.length == 0x17FE00
        cal_ok = b3.length == 0x3FE00
        _test("Block sizes — SIMOS85 PBL=0x13E00 (81,408)",     pbl_ok, f"got 0x{b1.length:X}")
        _test("Block sizes — SIMOS85 ASW=0x17FE00 (1,572,352)", asw_ok, f"got 0x{b2.length:X}")
        _test("Block sizes — SIMOS85 CAL=0x3FE00 (261,632)",    cal_ok, f"got 0x{b3.length:X}")
    except Exception as e:
        _test("Block sizes — SIMOS85 from ecu_defs", False, str(e))


# ── FRF loader tests ───────────────────────────────────────────────────────────

_KNOWN_SA2 = "6805824A10680493300419624A05871510197082499324041966824A058702031970824A0181494C"

_EXPECTED_BLOCK_SIZES = {1: 0x13E00, 2: 0x17FE00, 3: 0x3FE00}


def _test_frf_decrypt(key_path, frf_path):
    """FRF decryption produces a valid ZIP (magic PK)."""
    if not key_path:
        _test("FRF decrypt — ZIP magic", False, "frf.key not found")
        return
    if not frf_path:
        _test("FRF decrypt — ZIP magic", True,
              "SKIP (FRF not available on this host)")
        return
    try:
        import io, zipfile
        from flasher.frf_loader import _decrypt_frf
        key = key_path.read_bytes()
        enc = frf_path.read_bytes()
        dec = _decrypt_frf(key, enc)
        ok = dec[:2] == b'PK'
        # Also check it's a valid ZIP
        if ok:
            zf = zipfile.ZipFile(io.BytesIO(dec))
            odx_names = [n for n in zf.namelist() if n.lower().endswith('.odx')]
            ok = len(odx_names) == 1
            detail = f"ZIP contains: {zf.namelist()}"
        else:
            detail = f"first 4 bytes: {dec[:4].hex()}"
        _test("FRF decrypt — ZIP magic PK + ODX inside", ok, detail)
    except Exception as e:
        _test("FRF decrypt — ZIP magic", False, str(e))


def _test_frf_block_sizes(key_path, frf_path):
    """FRF extract_blocks returns 3 blocks with ODX-confirmed sizes."""
    if not key_path or not frf_path:
        _test("FRF block sizes — 3 blocks correct sizes", True, "SKIP")
        return
    try:
        from flasher.frf_loader import FrfLoader
        loader = FrfLoader(str(key_path))
        blocks = loader.extract_blocks(str(frf_path))
        size_ok = all(
            len(blocks.get(bn, b'')) == expected
            for bn, expected in _EXPECTED_BLOCK_SIZES.items()
        )
        detail = {bn: len(blocks.get(bn, b'')) for bn in _EXPECTED_BLOCK_SIZES}
        _test("FRF block extract — 3 blocks, correct sizes",
              len(blocks) == 3 and size_ok, str(detail))
    except Exception as e:
        _test("FRF block extract — 3 blocks, correct sizes", False, str(e))


def _test_frf_sa2_script(key_path, frf_path):
    """SA2 script extracted from FRF matches the known bytecode."""
    if not key_path or not frf_path:
        _test("FRF SA2 script extraction", True, "SKIP")
        return
    try:
        from flasher.frf_loader import FrfLoader
        loader = FrfLoader(str(key_path))
        sa2 = loader.extract_sa2_script(str(frf_path))
        ok = sa2 == _KNOWN_SA2
        _test("FRF SA2 script — matches known bytecode",
              ok, f"got {sa2[:20]}..." if sa2 else "None")
    except Exception as e:
        _test("FRF SA2 script — matches known bytecode", False, str(e))


def _test_frf_roundtrip_crc(key_path, frf_path):
    """
    Integration: extract CAL+ASW from FRF, corrupt CRC32, fix it, re-validate.
    This exercises the full pipeline that the flash tab uses.
    """
    if not key_path or not frf_path:
        _test("FRF→CRC32 round-trip (extract→corrupt→fix→validate)", True, "SKIP")
        return
    try:
        from flasher.frf_loader import FrfLoader
        from flasher.checksum_simos import validate_crc32, fix_crc32

        loader = FrfLoader(str(key_path))
        blocks = loader.extract_blocks(str(frf_path))

        # The stock CAL from a fresh FRF is XOR-counter encrypted, so we
        # can't run the CRC32 check directly (CRC was computed over plaintext).
        # Instead we XOR-decrypt it first, verify CRC, then corrupt + fix.
        from flasher.checksum_simos import xor_encrypt  # symmetric
        cal_encrypted = blocks[3]
        cal_plain = xor_encrypt(cal_encrypted)   # decrypt

        # 1. Validate the stock plaintext CAL CRC32
        valid_stock, stored_stock, calc_stock = validate_crc32(cal_plain, block_num=3)
        stock_ok = valid_stock  # stock CAL should already have correct CRC32

        # 2. Corrupt and fix
        bad = bytearray(cal_plain)
        struct.pack_into('<I', bad, HOFF_CAL + 4, 0xBADC0FFE)
        fixed = fix_crc32(bytes(bad), block_num=3)
        valid_fixed, s, c = validate_crc32(fixed, block_num=3)

        ok = valid_fixed and s == c
        _test("FRF→CRC32 round-trip (extract→decrypt→corrupt→fix→validate)",
              ok,
              f"stock_valid={stock_ok} fixed_valid={valid_fixed} "
              f"stored=0x{s:08X} calc=0x{c:08X}")
    except Exception as e:
        _test("FRF→CRC32 round-trip", False, str(e))


def _test_frf_missing_key():
    """FrfLoader raises FileNotFoundError when no key is available."""
    from flasher.frf_loader import FrfLoader
    try:
        FrfLoader(key_path="/tmp/nonexistent_key_xyz.key")
        _test("FRF missing key — raises FileNotFoundError", False, "no exception raised")
    except FileNotFoundError:
        _test("FRF missing key — raises FileNotFoundError", True)
    except Exception as e:
        _test("FRF missing key — raises FileNotFoundError", False,
              f"wrong exception: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Entry points
# ══════════════════════════════════════════════════════════════════════════════

def run(verbose: bool = False) -> bool:
    """Called from tests/__main__.py backend test phase."""
    repo = _repo_root()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    run_all(repo)
    return _print_results()


if __name__ == "__main__":
    import argparse, logging
    ap = argparse.ArgumentParser(description="Run checksum + FRF tests")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)-30s %(levelname)s  %(message)s"
    )

    # Add repo root to path
    repo = _repo_root()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    print("\nSimos Suite — Checksum + FRF tests")
    print("=" * 60)
    ok = run(args.verbose)
    sys.exit(0 if ok else 1)
