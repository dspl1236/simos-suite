#!/usr/bin/env python3
"""
flasher/seat_cp_patch.py — CP limp-gate patch for the C7/4H0 memory-seat modules
J136 (driver) and J521 (passenger). Both run the same 4H0959760 V850E2 image, so
one patch applies to either dump.

Mechanism (verified against the Ghidra decompile, FUN_00036cda case 7)
──────────────────────────────────────────────────────────────────────
The CP verify does  r7 = (memcmp(computed_AES, stored_expected) == 0)  and then
FUN_0003729c(0, r7) latches the CP auth-status byte (gp-0x5048). The machine tail is

    mov 0,r7 ; cmp r0,r2 ; bnz +4 ; mov 1,r7 ; mov 0,r6 ; jarl FUN_0003729c

i.e. r7 defaults to 0 (not-authed) and only becomes 1 on a match. Flipping the
DEFAULT `mov 0,r7` -> `mov 1,r7` makes BOTH paths leave r7 = 1 => always
authenticated, module never limps. One byte.

Located by CODE SIGNATURE (not a fixed offset), so it is portable across SW levels:

    003a e011 aa05 013a 0032   = mov0,r7; cmp r0,r2; bnz; mov1,r7; mov0,r6

Patch = first byte 0x00 -> 0x01.

Integrity / delivery  (CORRECTED 2026-11-12)
────────────────────────────────────────────
Earlier notes on this patch said the seat UDS path is "RSA-1024 signed" and that it
must be written with a BDM/N-Wire bench rig. **That was wrong** — carried over from the
RSA-signed HVAC. The 4x0959760 seat is *not* RSA-signed: its ODX integrity is
`SECURITY-METHOD = CRC32` + SA2, no `SIG_*`/`FW-SIGNATURE` on any data block (confirmed
on the 4H0 `.sgo` directly and on the 4M0 FRF). So the patched image can be written over
the diagnostic port: the only integrity gate is a recomputable **CRC-32/ISO-HDLC**
(`zlib.crc32`) checked at CheckMemory. `flasher/seat_flash.py` drives that OBD flash.

These seat images also carry NO *boot-time* checksum (no internal CRC/SHA table; startup
is plain C-runtime init), so there is nothing to fix up inside the block — only the
CheckMemory CRC over the whole (uncompressed) block, which seat_flash recomputes.

Patching is per-module and reversible (write 0x00 back). It touches only the owner's own
module and transfers no credential.

CLI
───
    py -3 -m flasher.seat_cp_patch <seat_dump.bin> [-o <out.bin>]      # patch
    py -3 -m flasher.seat_cp_patch <patched.bin> --revert [-o <out>]  # undo
"""
from __future__ import annotations

import argparse
import re
from typing import Tuple

# Gate signature (10 bytes). Patch byte is at offset +0 within the match.
SIG_STOCK   = bytes.fromhex("003ae011aa05013a0032")   # mov0,r7; cmp r0,r2; bnz; mov1,r7; mov0,r6
SIG_PATCHED = bytes.fromhex("013ae011aa05013a0032")   # patched form (first byte 0x01)
PATCH_OFFSET = 0        # byte to flip inside the match
STOCK_BYTE   = 0x00
PATCHED_BYTE = 0x01


class GateNotFound(Exception):
    """The seat CP limp-gate signature was not found (or is ambiguous)."""


def find_gate(data: bytes, patched: bool = False) -> int:
    """Return the file offset of the CP limp-gate's patch byte. Raises GateNotFound
    if the signature is absent or appears more than once."""
    sig = SIG_PATCHED if patched else SIG_STOCK
    hits = [m.start() for m in re.finditer(re.escape(sig), data)]
    if not hits:
        raise GateNotFound(
            "gate signature not found — is this a 4x0959760 seat image? "
            "(SW drift can change surrounding code but not this 10-byte idiom)"
        )
    if len(hits) > 1:
        raise GateNotFound(
            f"gate signature AMBIGUOUS ({len(hits)} hits @ {[hex(h) for h in hits]}); stop."
        )
    return hits[0] + PATCH_OFFSET


def is_patched(data: bytes) -> bool:
    """True if the image already carries the limp-gate patch."""
    try:
        find_gate(data, patched=True)
        return True
    except GateNotFound:
        return False


def patch_image(data: bytes) -> Tuple[bytes, int]:
    """Apply the 1-byte limp-gate patch. Returns (patched_bytes, offset).
    Idempotent-safe: raises GateNotFound if the stock gate isn't present (e.g. the
    image is already patched — check is_patched() first)."""
    off = find_gate(data, patched=False)
    if data[off] != STOCK_BYTE:
        raise GateNotFound(f"gate byte at 0x{off:05x} is 0x{data[off]:02x}, expected 0x00")
    out = bytearray(data)
    out[off] = PATCHED_BYTE
    return bytes(out), off


def revert_image(data: bytes) -> Tuple[bytes, int]:
    """Undo the limp-gate patch. Returns (stock_bytes, offset)."""
    off = find_gate(data, patched=True)
    out = bytearray(data)
    out[off] = STOCK_BYTE
    return bytes(out), off


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="C7 seat (4x0959760) CP limp-gate patch.")
    ap.add_argument("image")
    ap.add_argument("-o", "--out")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args(argv)

    data = open(a.image, "rb").read()
    if a.revert:
        out, off = revert_image(data)
        act = f"REVERTED @0x{off:05x}: 01->00 (mov 1,r7 -> mov 0,r7)"
        default_out = a.image.replace(".bin", "_stock.bin")
    else:
        out, off = patch_image(data)
        act = f"PATCHED  @0x{off:05x}: 00->01 (mov 0,r7 -> mov 1,r7)"
        default_out = a.image.replace(".bin", "_limp_patched.bin")

    dst = a.out or default_out
    open(dst, "wb").write(out)
    print(act)
    print(f"gate 10B @0x{off:05x}: {out[off:off+10].hex()}")
    print(f"WROTE {dst}  ({len(out):,} B, 1-byte delta)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
