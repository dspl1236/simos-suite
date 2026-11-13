"""
tests/test_seat_flash.py — offline tests for the C7 seat CP limp-patch + OBD flasher.

No hardware and no CAN stack required: the patch/CRC/plan core is transport-free. The
live udsoncan Client functions (identify_seat/flash_seat_block) are exercised only on a
real unit and are not covered here. Tests that need the Oct-2024 flashdaten (the real
.sgo / FRF on the Q: drive) are skipped automatically when those files are absent.
"""
import os
import re
import zlib
import pytest

from flasher import seat_cp_patch as scp
from flasher import seat_flash as sf

# ── Real flashdaten (optional — tests skip if not present) ───────────────────
_FD = r"Q:\ECU FLASH\Flashdaten_Audi_20241002\Service42\Trade-Retail\Brand-A"
SEAT_SGO = os.path.join(_FD, "4H0959760A__0114.sgo")   # C7 driver seat
GT_FRF   = os.path.join(_FD, "FL_4K0035335B_0533.frf")  # plain CRC32 FRF (ground truth)


# ── Synthetic block (self-contained, CI-safe) ────────────────────────────────

def _make_block(gate_off: int = 0x1000, size: int = 0x8000, addr: int = 0x038000):
    """A 0x8000 code block of 0xFF filler with the stock gate idiom embedded once."""
    buf = bytearray(b"\xFF" * size)
    buf[gate_off:gate_off + len(scp.SIG_STOCK)] = scp.SIG_STOCK
    return sf.SeatBlock(addr=addr, data=bytes(buf), erase=(addr, addr + size), prog=(addr, addr + size))


# ── seat_cp_patch ────────────────────────────────────────────────────────────

def test_patch_flips_exactly_one_byte():
    blk = _make_block()
    patched, off = scp.patch_image(blk.data)
    assert sum(a != b for a, b in zip(blk.data, patched)) == 1
    assert blk.data[off] == scp.STOCK_BYTE and patched[off] == scp.PATCHED_BYTE


def test_patch_revert_roundtrip():
    blk = _make_block()
    patched, _ = scp.patch_image(blk.data)
    assert scp.is_patched(patched) and not scp.is_patched(blk.data)
    stock, _ = scp.revert_image(patched)
    assert stock == blk.data


def test_patch_refuses_non_seat_image():
    with pytest.raises(scp.GateNotFound):
        scp.patch_image(b"\x00" * 0x1000)


def test_patch_refuses_ambiguous_gate():
    buf = bytearray(b"\xFF" * 0x8000)
    buf[0x100:0x100 + 10] = scp.SIG_STOCK
    buf[0x2000:0x2000 + 10] = scp.SIG_STOCK
    with pytest.raises(scp.GateNotFound):
        scp.patch_image(bytes(buf))


# ── CheckMemory CRC + request layout ─────────────────────────────────────────

def test_checkmemory_crc_is_iso_hdlc():
    data = bytes(range(256)) * 7
    assert sf.seat_checkmemory_crc(data) == (zlib.crc32(data) & 0xFFFFFFFF)


def test_checkmemory_request_layout():
    # [ALFID=01][block_id][00 04][crc32 big-endian]
    out = sf.build_checkmemory_data(7, 0xA0D8780C)
    assert out == bytes.fromhex("01070004a0d8780c")


# ── plan_seat_flash ──────────────────────────────────────────────────────────

def test_plan_only_gate_block_is_self_consistent():
    # Only the 0x038000 block carries the gate; the others are plain 0xFF filler.
    blocks = [
        sf.SeatBlock(0x008000, b"\xFF" * 0x8000, (0x8000, 0x10000), (0x8000, 0x10000)),
        _make_block(gate_off=0x1000, addr=0x038000),
        sf.SeatBlock(0x010000, b"\xFF" * 0x8000, (0x10000, 0x18000), (0x10000, 0x18000)),
    ]
    patched, steps = sf.plan_seat_flash(blocks, only_gate_block=True)
    ops = [s.op for s in steps]
    assert ops == ["session", "sa2", "erase", "download", "transfer",
                   "transfer_exit", "checkmemory", "dependencies", "reset"]
    transfer = next(s for s in steps if s.op == "transfer")
    checkmem = next(s for s in steps if s.op == "checkmemory")
    # exactly the patched bytes are transferred, and CheckMemory carries their CRC
    assert transfer.payload == patched
    assert checkmem.crc == sf.seat_checkmemory_crc(patched)
    assert checkmem.payload == sf.build_checkmemory_data(checkmem.block_id, checkmem.crc)


def test_plan_block_id_mapping():
    assert sf._block_id_for(0x008000) == 1
    assert sf._block_id_for(0x038000) == 7


def test_locate_gate_block_unique():
    g = _make_block(addr=0x038000)
    plain = sf.SeatBlock(0x008000, b"\xFF" * 0x8000, (0, 0), (0, 0))
    assert sf.locate_gate_block([plain, g]).addr == 0x038000
    with pytest.raises(scp.GateNotFound):
        sf.locate_gate_block([plain, plain])


# ── Real flashdaten (skipped when Q:\ is not mounted) ─────────────────────────

@pytest.mark.skipif(not os.path.exists(SEAT_SGO), reason="C7 seat .sgo not present")
def test_real_seat_sgo_gate_in_0x038000_block():
    blocks = sf.load_seat_sgo(SEAT_SGO)
    gate = sf.locate_gate_block(blocks)
    assert gate.addr == 0x038000
    patched, steps = sf.plan_seat_flash(blocks, only_gate_block=True)
    assert sum(a != b for a, b in zip(gate.data, patched)) == 1
    checkmem = next(s for s in steps if s.op == "checkmemory")
    assert checkmem.crc == (zlib.crc32(patched) & 0xFFFFFFFF)


@pytest.mark.skipif(not os.path.exists(GT_FRF), reason="ground-truth FRF not present")
def test_crc_convention_matches_real_frf_checksum():
    from flasher.frf_loader import FrfLoader
    L = FrfLoader()
    odx = L.get_odx(GT_FRF).decode("latin-1")
    declared = re.findall(
        r"<SECURITY-METHOD[^>]*>CRC32</SECURITY-METHOD>\s*"
        r"<FW-CHECKSUM[^>]*>([0-9A-Fa-f]+)</FW-CHECKSUM>", odx)
    blocks = [v for _, v in sorted(L.extract_blocks(GT_FRF).items())]
    assert blocks and len(declared) >= len(blocks)
    for i, b in enumerate(blocks):
        assert f"{sf.seat_checkmemory_crc(b):08X}" == declared[i].upper()
