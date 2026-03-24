"""
frf_loader.py — Decrypt and parse VAG flashdaten FRF/ODX containers.

FRF files are ZIP archives encrypted with a rolling XOR cipher (key in frf.key).
The ZIP contains an ODX XML file that embeds flash block binaries as hex strings.

Cipher algorithm courtesy of bri3d/VW_Flash (GPL-3.0).
Block layout confirmed from FL_4G0907551D__0006.frf (your exact ECU).

Supported block numbers (Simos8.5, 4G0907551x):
  1 = FD_0  (PBL)   81,408 bytes
  2 = FD_1  (ASW) 1,572,352 bytes
  3 = FD_2  (CAL)   261,632 bytes

Usage:
    from flasher.frf_loader import FrfLoader
    loader = FrfLoader("path/to/frf.key")
    blocks = loader.extract_blocks("FL_4G0907551D__0006.frf")
    # blocks = {1: bytes(PBL), 2: bytes(ASW), 3: bytes(CAL)}
"""

import io
import logging
import pathlib
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, Optional

log = logging.getLogger(__name__)

# Default frf.key path — same directory as this file or provided by caller
_DEFAULT_KEY_PATHS = [
    pathlib.Path(__file__).parent.parent / "data" / "frf.key",
    pathlib.Path(__file__).parent / "frf.key",
]


def _decrypt_frf(key_material: bytes, encrypted_data: bytes) -> bytes:
    """
    Rolling XOR cipher used to encrypt VAG flashdaten FRF files.
    Algorithm: bri3d/VW_Flash frf/decryptfrf.py (GPL-3.0).

    Each output byte = input_byte XOR (first_seed XOR 0xFF XOR second_seed XOR key_byte)
    where first_seed and second_seed evolve with each byte, incorporating key and data.
    """
    output = bytearray()
    key_index = 0
    first_seed = 0
    second_seed = 1
    for data_byte in encrypted_data:
        key_byte = key_material[key_index]
        first_seed = ((first_seed + key_byte) * 3) & 0xFF
        decrypted_byte = data_byte ^ (first_seed ^ 0xFF ^ second_seed ^ key_byte)
        output.append(decrypted_byte)
        second_seed = ((second_seed + 1) * first_seed) & 0xFF
        key_index += 1
        key_index %= len(key_material)
    return bytes(output)


class FrfLoader:
    """Decrypt and parse VAG flashdaten FRF containers into flash block binaries."""

    def __init__(self, key_path: Optional[str] = None):
        """
        Args:
            key_path: Path to frf.key.  If None, searches default locations.
        """
        if key_path:
            self._key = pathlib.Path(key_path).read_bytes()
        else:
            for p in _DEFAULT_KEY_PATHS:
                if p.exists():
                    self._key = p.read_bytes()
                    log.debug("frf.key loaded from %s", p)
                    break
            else:
                raise FileNotFoundError(
                    "frf.key not found. Download VW_Flash and copy data/frf.key "
                    "to simos-suite/data/frf.key, or pass key_path explicitly."
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_blocks(self, frf_path: str) -> Dict[int, bytes]:
        """
        Decrypt an FRF file and return its flash blocks as raw bytes.

        Returns:
            {block_number: raw_bytes} where block_number matches the Simos8.5
            block numbering (1=PBL, 2=ASW, 3=CAL).

        Raises:
            ValueError: if the file is not a valid decrypted ZIP or ODX.
        """
        encrypted = pathlib.Path(frf_path).read_bytes()
        log.info("Decrypting %s (%d bytes)", frf_path, len(encrypted))
        decrypted = _decrypt_frf(self._key, encrypted)

        if decrypted[:2] != b"PK":
            raise ValueError(
                f"{frf_path}: decryption did not produce a ZIP file "
                f"(got {decrypted[:4].hex()}). Wrong frf.key?"
            )

        with zipfile.ZipFile(io.BytesIO(decrypted), "r") as zf:
            names = zf.namelist()
            odx_names = [n for n in names if n.lower().endswith(".odx")]
            if not odx_names:
                raise ValueError(f"{frf_path}: ZIP contains no ODX file: {names}")
            odx_data = zf.read(odx_names[0])
            log.debug("Opened ODX: %s (%d bytes)", odx_names[0], len(odx_data))

        return self._parse_odx_blocks(odx_data)

    def extract_sa2_script(self, frf_path: str) -> Optional[str]:
        """
        Return the SA2 seed/key bytecode hex string from the FRF's ODX.
        Returns None if not found (non-Simos ECU or DIAG-only ODX).
        """
        encrypted = pathlib.Path(frf_path).read_bytes()
        decrypted = _decrypt_frf(self._key, encrypted)
        if decrypted[:2] != b"PK":
            return None
        with zipfile.ZipFile(io.BytesIO(decrypted), "r") as zf:
            odx_names = [n for n in zf.namelist() if n.lower().endswith(".odx")]
            if not odx_names:
                return None
            odx_data = zf.read(odx_names[0])
        return self._parse_sa2_script(odx_data)

    def get_odx(self, frf_path: str) -> bytes:
        """Return raw ODX XML bytes from an FRF file."""
        encrypted = pathlib.Path(frf_path).read_bytes()
        decrypted = _decrypt_frf(self._key, encrypted)
        if decrypted[:2] != b"PK":
            raise ValueError(f"{frf_path}: not a valid FRF")
        with zipfile.ZipFile(io.BytesIO(decrypted), "r") as zf:
            odx_names = [n for n in zf.namelist() if n.lower().endswith(".odx")]
            if not odx_names:
                raise ValueError(f"{frf_path}: no ODX in ZIP")
            return zf.read(odx_names[0])

    # ------------------------------------------------------------------
    # ODX parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_odx_blocks(odx_data: bytes) -> Dict[int, bytes]:
        """
        Parse an ODX flash container and return {block_number: raw_bytes}.

        The ODX FLASH schema stores:
          - FLASHDATA elements with <DATA> holding hex-encoded binary
          - DATABLOCK elements referencing FLASHDATAs and containing SEGMENTS
            with SOURCE-START-ADDRESS (= block number for Simos ECUs)

        Block numbers are stored as decimal integers in SOURCE-START-ADDRESS.
        ERASE pseudo-blocks (DB_xERASE) are skipped.
        """
        try:
            root = ET.fromstring(odx_data)
        except ET.ParseError as e:
            raise ValueError(f"ODX XML parse error: {e}")

        # Map FLASHDATA ID → binary
        flashdata_map: Dict[str, bytes] = {}
        for fd in root.iter("FLASHDATA"):
            fd_id = fd.get("ID", "")
            data_el = fd.find("DATA")
            if data_el is None or not (data_el.text or "").strip():
                continue
            hex_str = "".join(
                c for c in data_el.text if c in "0123456789ABCDEFabcdef"
            )
            if hex_str:
                flashdata_map[fd_id] = bytes.fromhex(hex_str)

        if not flashdata_map:
            raise ValueError("ODX contains no FLASHDATA with binary content")

        log.debug("Found %d FLASHDATA entries", len(flashdata_map))

        # Map block_number → binary via DATABLOCK → FLASHDATA-REF → SEGMENT address
        blocks: Dict[int, bytes] = {}

        for db in root.iter("DATABLOCK"):
            sn_el = db.find("SHORT-NAME")
            if sn_el is None:
                continue
            name = sn_el.text or ""
            if name.endswith("ERASE"):
                continue  # skip erase-only pseudo-blocks

            # Get block number from first SEGMENT's SOURCE-START-ADDRESS
            block_num: Optional[int] = None
            segs = db.find("SEGMENTS")
            if segs is not None:
                for seg in segs:
                    addr_el = seg.find("SOURCE-START-ADDRESS")
                    if addr_el is not None and addr_el.text:
                        try:
                            block_num = int(addr_el.text)
                        except ValueError:
                            block_num = int(addr_el.text, 16)
                        break

            if block_num is None:
                log.warning("DATABLOCK %s has no segment address, skipping", name)
                continue

            # Find the FLASHDATA-REF for this DATABLOCK
            fd_ref = db.find(".//FLASHDATA-REF")
            if fd_ref is None:
                continue
            fd_id_ref = fd_ref.get("ID-REF", "")
            if fd_id_ref not in flashdata_map:
                log.warning(
                    "DATABLOCK %s references unknown FLASHDATA %s", name, fd_id_ref
                )
                continue

            blocks[block_num] = flashdata_map[fd_id_ref]
            log.debug(
                "Block %d (%s): %d bytes from %s",
                block_num, name, len(blocks[block_num]), fd_id_ref,
            )

        if not blocks:
            raise ValueError("ODX parsed but no flash blocks extracted")

        log.info(
            "Extracted %d blocks: %s",
            len(blocks),
            {k: len(v) for k, v in sorted(blocks.items())},
        )
        return blocks

    @staticmethod
    def _parse_sa2_script(odx_data: bytes) -> Optional[str]:
        """Extract SA2 FW-SIGNATURE hex string from ODX."""
        try:
            root = ET.fromstring(odx_data)
        except ET.ParseError:
            return None
        for sec in root.iter("SECURITY"):
            sm = sec.find("SECURITY-METHOD")
            fs = sec.find("FW-SIGNATURE")
            if sm is not None and sm.text == "SA2" and fs is not None:
                return (fs.text or "").strip()
        return None


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(
        description="Decrypt an FRF file and dump flash blocks to disk."
    )
    parser.add_argument("frf", help="Path to .frf file")
    parser.add_argument("--key", help="Path to frf.key (default: data/frf.key)")
    parser.add_argument("--outdir", default=".", help="Output directory for .bin files")
    parser.add_argument("--sa2", action="store_true", help="Print SA2 script and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    loader = FrfLoader(args.key)

    if args.sa2:
        script = loader.extract_sa2_script(args.frf)
        print(f"SA2 script: {script or 'NOT FOUND'}")
        sys.exit(0)

    blocks = loader.extract_blocks(args.frf)
    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for block_num, data in sorted(blocks.items()):
        out = outdir / f"block_{block_num}.bin"
        out.write_bytes(data)
        print(f"Block {block_num}: {len(data):,} bytes → {out}")
