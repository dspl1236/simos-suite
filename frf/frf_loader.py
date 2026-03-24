"""
frf/frf_loader.py — FRF decryption and ODX flash block extraction
=================================================================
Implements the VAG FRF "recursive XOR" cipher and ODX parser to extract
raw flash block binaries from Flashdaten .frf files.

The FRF format:
  encrypted_payload  →  decrypt_data(frf.key)  →  ZIP archive  →  ODX XML
  ODX XML contains FLASHDATA elements with hex-encoded binary block data.

Key file: data/frf.key  (ships with VW_Flash project, 4095 bytes)

Usage:
    from frf.frf_loader import load_frf, FRFInfo
    info = load_frf("FL_4G0907551D__0006.frf", key_path="data/frf.key")
    # info.blocks: Dict[str, bytes] e.g. {"FD_0": b"...", "FD_1": b"..."}
    # info.sa2_script: bytes
    # info.block_sizes: Dict[str, int]
    # info.ecu_part: str
"""

import io
import pathlib
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Cipher
# ---------------------------------------------------------------------------

def _decrypt_frf(key_material: bytes, encrypted_data: bytes) -> bytes:
    """
    VAG FRF 'recursive XOR' stream cipher (bri3d/VW_Flash decryptfrf.py).

    State:
        first_seed  = ((first_seed + key_byte) * 3) & 0xFF
        plain_byte  = cipher_byte ^ first_seed ^ 0xFF ^ second_seed ^ key_byte
        second_seed = ((second_seed + 1) * first_seed) & 0xFF
    """
    out = bytearray(len(encrypted_data))
    key_len = len(key_material)
    key_idx = 0
    first_seed = 0
    second_seed = 1
    for i, data_byte in enumerate(encrypted_data):
        key_byte = key_material[key_idx]
        first_seed = ((first_seed + key_byte) * 3) & 0xFF
        out[i] = data_byte ^ (first_seed ^ 0xFF ^ second_seed ^ key_byte)
        second_seed = ((second_seed + 1) * first_seed) & 0xFF
        key_idx = (key_idx + 1) % key_len
    return bytes(out)


# ---------------------------------------------------------------------------
# ODX parser
# ---------------------------------------------------------------------------

@dataclass
class FRFInfo:
    """Decoded information from a flashdaten FRF container."""
    frf_path:    str
    flash_id:    str                   # e.g. FL_4G0907551D__0006
    ecu_part:    Optional[str]         # e.g. 4G0907551D
    alfid:       Optional[str]         # ALFID signature (e.g. 014101)
    sa2_script:  Optional[bytes]       # raw SA2 bytecode
    block_crc32: Dict[str, int]        # ODX CRC32 per block {name: crc}
    block_sizes: Dict[str, int]        # uncompressed size per block
    blocks:      Dict[str, bytes]      # actual binary data {name: bytes}
    layer_refs:  List[str]             # ASAM dataset refs
    encrypt_compress: Optional[str]    # e.g. "0x11"


def _parse_odx(odx_text: str) -> FRFInfo:
    root = ET.fromstring(odx_text)
    flash_el = root.find("FLASH")
    if flash_el is None:
        raise ValueError("No <FLASH> element in ODX")

    flash_id = (flash_el.findtext("SHORT-NAME") or "").strip()

    # SA2 script + other security entries
    sa2_script = None
    alfid = None
    block_crc32: Dict[str, int] = {}
    for sec in flash_el.iter("SECURITY"):
        method = (sec.findtext("SECURITY-METHOD") or "").strip()
        sig    = (sec.findtext("FW-SIGNATURE") or "").strip()
        crc    = (sec.findtext("FW-CHECKSUM") or "").strip()
        valid  = (sec.findtext("VALIDITY-FOR") or "").strip()
        if method == "SA2" and sig:
            sa2_script = bytes.fromhex(sig)
        elif method == "ALFID" and sig:
            alfid = sig
        elif method == "CRC32" and crc and valid:
            try:
                block_crc32[valid] = int(crc, 16)
            except ValueError:
                pass

    # ENCRYPT-COMPRESS method
    enc_methods = [e.text for e in flash_el.iter("ENCRYPT-COMPRESS-METHOD") if e.text]
    encrypt_compress = enc_methods[0] if enc_methods else None

    # Block sizes from SEGMENT/UNCOMPRESSED-SIZE
    block_sizes: Dict[str, int] = {}
    for db in flash_el.iter("DATABLOCK"):
        sn = db.findtext("SHORT-NAME") or ""
        if sn.endswith("ERASE"):
            continue
        for seg in db.iter("SEGMENT"):
            unc = seg.findtext("UNCOMPRESSED-SIZE")
            if unc:
                block_sizes[sn] = int(unc)
                break

    # Binary data from FLASHDATA/DATA (hex-encoded)
    blocks: Dict[str, bytes] = {}
    for fd in flash_el.iter("FLASHDATA"):
        sn = fd.findtext("SHORT-NAME") or ""
        if sn.endswith("ERASE"):
            continue
        data_el = fd.find("DATA")
        if data_el is not None and data_el.text:
            hex_str = "".join(
                c for c in data_el.text if c in "0123456789ABCDEFabcdef"
            )
            if hex_str:
                blocks[sn] = bytes.fromhex(hex_str)

    # ECU part number from IDENT-VALUE (first 11-char entry)
    ecu_part: Optional[str] = None
    for iv in flash_el.iter("IDENT-VALUE"):
        txt = (iv.text or "").strip()
        if len(txt) >= 7 and txt[:2].isdigit():
            ecu_part = txt.strip()
            break

    # LAYER-REF (ASAM dataset references)
    layer_refs = [
        lr.get("ID-REF", "") or lr.text or ""
        for lr in flash_el.iter("LAYER-REF")
    ]

    return FRFInfo(
        frf_path="",
        flash_id=flash_id,
        ecu_part=ecu_part,
        alfid=alfid,
        sa2_script=sa2_script,
        block_crc32=block_crc32,
        block_sizes=block_sizes,
        blocks=blocks,
        layer_refs=[lr for lr in layer_refs if lr],
        encrypt_compress=encrypt_compress,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_frf(
    frf_path: str,
    key_path: Optional[str] = None,
) -> FRFInfo:
    """
    Decrypt a VAG Flashdaten .frf file and return parsed FRFInfo.

    Args:
        frf_path: Path to the .frf file.
        key_path: Path to frf.key (defaults to data/frf.key relative to
                  this module's directory, or data/frf.key in cwd).

    Returns:
        FRFInfo with blocks, SA2 script, sizes, and checksums.

    Raises:
        FileNotFoundError: if frf_path or key_path not found.
        ValueError: if decrypted data is not a valid ZIP.
    """
    frf_path = str(frf_path)
    encrypted = pathlib.Path(frf_path).read_bytes()

    # Locate key
    if key_path is None:
        candidates = [
            pathlib.Path(__file__).parent.parent / "data" / "frf.key",
            pathlib.Path("data") / "frf.key",
        ]
        for c in candidates:
            if c.exists():
                key_path = str(c)
                break
    if key_path is None or not pathlib.Path(key_path).exists():
        raise FileNotFoundError(
            "frf.key not found. Place it at data/frf.key or specify key_path."
        )
    key_material = pathlib.Path(key_path).read_bytes()

    # Decrypt
    decrypted = _decrypt_frf(key_material, encrypted)

    # Unzip
    if decrypted[:2] != b"PK":
        raise ValueError(
            f"Decrypted FRF does not start with ZIP magic (got {decrypted[:4].hex()}). "
            "Wrong key or corrupt file?"
        )
    zf = zipfile.ZipFile(io.BytesIO(decrypted))

    # Find ODX entry
    odx_entries = [n for n in zf.namelist() if n.lower().endswith(".odx")]
    if not odx_entries:
        raise ValueError(f"No .odx file found in FRF ZIP. Contents: {zf.namelist()}")

    odx_text = zf.read(odx_entries[0]).decode("utf-8", errors="replace")
    info = _parse_odx(odx_text)
    info.frf_path = frf_path
    return info


def validate_block_crc(block_name: str, data: bytes, expected_crc: int) -> bool:
    """CRC32 validation for an extracted flash block."""
    calc = zlib.crc32(data) & 0xFFFFFFFF
    return calc == expected_crc


def describe_frf(info: FRFInfo) -> str:
    """Human-readable summary of a decoded FRF."""
    lines = [
        f"Flash ID    : {info.flash_id}",
        f"ECU Part    : {info.ecu_part or 'unknown'}",
        f"ALFID       : {info.alfid or 'none'}",
        f"SA2 script  : {info.sa2_script.hex() if info.sa2_script else 'none'}",
        f"Enc/Compress: {info.encrypt_compress or 'none'}",
        f"Layer refs  : {', '.join(info.layer_refs) or 'none'}",
        "",
        "Blocks:",
    ]
    for name, data in sorted(info.blocks.items()):
        crc = zlib.crc32(data) & 0xFFFFFFFF
        expected = info.block_crc32.get(name)
        crc_status = ""
        if expected is not None:
            crc_status = f"  CRC {'OK' if crc == expected else 'MISMATCH'} (expected 0x{expected:08X})"
        lines.append(
            f"  {name:12s}: {len(data):>8,} bytes  CRC32=0x{crc:08X}{crc_status}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(
        description="Decrypt and inspect a VAG Flashdaten .frf file."
    )
    parser.add_argument("frf", help="Path to .frf file")
    parser.add_argument("--key", default=None, help="Path to frf.key (default: data/frf.key)")
    parser.add_argument("--outdir", default=None, help="Directory to write extracted .bin blocks")
    args = parser.parse_args()

    try:
        info = load_frf(args.frf, args.key)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(describe_frf(info))

    if args.outdir:
        import os
        os.makedirs(args.outdir, exist_ok=True)
        for name, data in sorted(info.blocks.items()):
            out = pathlib.Path(args.outdir) / f"{name}.bin"
            out.write_bytes(data)
            print(f"Wrote {out} ({len(data):,} bytes)")
