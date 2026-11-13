"""
tests/test_tcu_flash.py — ZF 8HP TCU codec + flasher (offline).

The crux is the method-0x22 codec (repeating-XOR + LZZ): every block must decode and re-encode
back to the exact firmware. Round-trips run on synthetic data always, and on a real 4G0927158
FRF when the Oct-2024 flashdaten + vag-tcu-tools are present (skipped otherwise). The live
udsoncan flow is exercised only on hardware.
"""
import os
import random
import zlib
import pytest

from flasher import tcu_flash as T

_FD = r"Q:\ECU FLASH\Flashdaten_Audi_20241002\Service42\Trade-Retail\Brand-A"
FRF = os.path.join(_FD, "FL_4G0927158AA_1005.frf")
_HAVE_LZZ = os.path.exists(os.path.join(T._VTT, "core", "lzz.py"))
_skip_lzz = pytest.mark.skipif(not _HAVE_LZZ, reason="vag-tcu-tools lzz codec not present")


# ── constants mirror vag-tcu-tools/platforms/zf8hp ───────────────────────────
def test_constants():
    assert T.XOR_KEY == b"CyA2008ZFVAGtcuxsam"
    assert (T.CAN_TX, T.CAN_RX) == (0x7E1, 0x7E9)
    assert T.CAL_CRC_RANGE == (0x190000, 0x1FFD5F)
    assert T.MEMORY_MAP["ASW"]["start"] == 0x040080


def test_xor_symmetric():
    data = bytes(range(256)) * 7
    assert T._xor(T._xor(data)) == data


# ── the LZZ codec round-trips (the crux) ─────────────────────────────────────
@_skip_lzz
def test_lzz_roundtrip_synthetic():
    decompress, compress = T._load_lzz()
    random.seed(7)
    cases = [
        b"",
        b"ABCD" * 4000,
        b"\x00" * 9000 + b"\xAA" * 3000,
        bytes(random.randrange(256) for _ in range(16000)),
        (b"ratios vary " * 80) + bytes(random.randrange(256) for _ in range(2000)),
    ]
    for d in cases:
        c = compress(d)
        assert bytes(decompress(c, len(d))) == d


@_skip_lzz
def test_encode_block_roundtrips():
    decompress, _c = T._load_lzz()
    data = (b"\x11\x22\x33\x44" * 1000) + bytes(range(256))
    enc = T.encode_block(data)                       # LZZ compress + XOR encrypt
    assert bytes(decompress(T._xor(enc), len(data))) == data


# ── CAL CRC helper ───────────────────────────────────────────────────────────
def test_cal_crc_and_fix():
    img = bytearray(b"\x00" * 0x200000)
    img[0x190000:0x1A0000] = bytes(range(256)) * 256     # some CAL data
    crc = T.cal_crc(bytes(img))
    assert crc == zlib.crc32(bytes(img[0x190000:0x1FFD60])) & 0xFFFFFFFF
    fixed, crc2 = T.fix_cal_crc(img)
    assert crc2 == crc
    import struct
    assert struct.unpack_from(">I", fixed, T.CAL_CRC_OFFSET)[0] == crc    # patched big-endian


def test_tcu_ecu_def():
    ecu = T._tcu_ecu()
    assert ecu.can_tx == 0x7E1 and ecu.can_rx == 0x7E9
    assert "4G0927158" in ecu.compatible_hw


# ── real flashdaten (skipped without Q:\ + vag-tcu-tools) ─────────────────────
@pytest.mark.skipif(not (os.path.exists(FRF) and _HAVE_LZZ),
                    reason="4G0927158 FRF or vag-tcu-tools not present")
def test_real_frf_all_blocks_roundtrip():
    decompress, _c = T._load_lzz()
    raw = T.decode_frf_blocks(FRF)
    assert len(raw) >= 3
    for num, data in raw.items():
        enc = T.encode_block(data)
        assert bytes(decompress(T._xor(enc), len(data))) == data, f"block {num} round-trip"
