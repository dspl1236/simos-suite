"""
cp_tools/mwb_extract.py — Extract CP routine ID from ES_LIBCompoProteGen3V12.sd.db

This is the final missing piece for Component Protection research.
All other CP data has been confirmed from the AU57X string database.
This script extracts the exact 2-byte RoutineControl ID for the CP
authentication routine from the binary .sd.db file.

Background
──────────
The AU57X ODIS MCD project stores diagnostic service definitions in
binary PBL (Persistent Block Library) format as .bv.db / .sd.db files.
The CP authentication routine lives in:

    ES_LIBCompoProteGen3V12.sd.db

The routine identifier for:

    DC_ES_LIBCompoProteGen3V12_DiagnServi_RoutiContrStartRoutiCompoProte

...is the 2-byte value sent as:

    31 01 [HI] [LO]   ← RoutineControl Start + routine ID

Once we have this, j533_probe.py can attempt a full CP auth sequence.

Method
──────
Uses the open-source PBL library (github.com/peterGraf/pbl) compiled as
a native Linux .so — no Windows, no ODIS installation required.

Alternatively, if pbl is not available, this script attempts a raw binary
search for the routine ID by scanning for known byte patterns around the
service identifier string.

Usage
─────
    # With pbl native library (recommended):
    python -m cp_tools.mwb_extract --db /path/to/ES_LIBCompoProteGen3V12.sd.db

    # With full ODIS project folder (runs dumpMWB.py):
    python -m cp_tools.mwb_extract --project /path/to/AU57X

    # Raw binary search only (no pbl required):
    python -m cp_tools.mwb_extract --db /path/to/ES_LIBCompoProteGen3V12.sd.db --raw

Output
──────
    Prints the discovered routine ID and writes cp_tools/cp_routine_id.json:
    {
        "routine_id_hex": "0x????",
        "routine_id_bytes": [??, ??],
        "uds_sequence": "31 01 ?? ??",
        "source": "ES_LIBCompoProteGen3V12.sd.db",
        "confirmed": false
    }

    Once confirmed against a live J533, "confirmed" is set to true and
    j533_probe.py starts the CP auth routine automatically.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("SimosSuite.MWBExtract")

# ── Known strings to locate in the binary ────────────────────────────────────
# These appear in ES_LIBCompoProteGen3V12.sd.db adjacent to the routine ID bytes

TARGET_SERVICE = "RoutiContrStartRoutiCompoProte"
TARGET_LIB     = "ES_LIBCompoProteGen3V12"
TARGET_STOP    = "RoutiContrStopRoutiCompoProte"
TARGET_REQUEST = "RoutiContrRequeRoutiResulCompoProte"

# Known CP routine ID candidates from community research
# (narrowed down from AU57X service layer analysis)
CANDIDATE_IDS = [
    0x0203,  # CP Gen 1 (older platforms)
    0x0205,  # CP Gen 2
    0x0210,  # Seen in some C7 captures
    0x0215,
    0x0260,
    0x0261,
    0x0270,
    0x4001,  # CP Gen 3 candidate (from ES_LIBCompoProteGen3V12 naming)
    0x4010,
    0x4100,
]


# ── Raw binary search ─────────────────────────────────────────────────────────

def search_raw(db_path: Path) -> Optional[int]:
    """
    Search the raw .sd.db binary for patterns that suggest a routine ID.

    Strategy:
    1. Find the offset of TARGET_SERVICE string in the binary
    2. Scan ±512 bytes around it for 2-byte values that match CANDIDATE_IDS
    3. Also scan for the UDS RoutineControl pattern: 31 01 ?? ??
    4. Return the most plausible candidate

    This is a heuristic. Confirmation against a live J533 is required.
    """
    log.info("Raw binary search in %s  (%d bytes)", db_path.name,
             db_path.stat().st_size)

    data = db_path.read_bytes()
    results = []

    # Search for target string
    needle = TARGET_SERVICE.encode("utf-8")
    offset = data.find(needle)
    if offset == -1:
        # Try UTF-16
        needle16 = TARGET_SERVICE.encode("utf-16-le")
        offset = data.find(needle16)
        if offset != -1:
            log.info("Found target string (UTF-16) at offset 0x%X", offset)
    else:
        log.info("Found target string (UTF-8) at offset 0x%X", offset)

    if offset == -1:
        log.warning("Target string not found in binary")
        log.info("Trying candidate ID scan across full binary...")
        # Fall back: scan entire file for candidate IDs in RoutineControl context
        for i in range(0, len(data) - 4):
            if data[i] == 0x31 and data[i+1] == 0x01:
                rid = (data[i+2] << 8) | data[i+3]
                if rid in CANDIDATE_IDS:
                    log.info("Found candidate 0x%04X at offset 0x%X (31 01 pattern)",
                             rid, i)
                    results.append((rid, i, "31-01-pattern"))
    else:
        # Scan window around the string
        window_start = max(0, offset - 512)
        window_end   = min(len(data), offset + len(needle) + 512)
        window       = data[window_start:window_end]

        log.info("Scanning window 0x%X–0x%X (%d bytes)",
                 window_start, window_end, len(window))

        # Look for 2-byte values matching candidates
        for i in range(len(window) - 1):
            val = (window[i] << 8) | window[i+1]
            if val in CANDIDATE_IDS:
                abs_off = window_start + i
                log.info("Candidate 0x%04X at offset 0x%X (±window)", val, abs_off)
                results.append((val, abs_off, "window-candidate"))

        # Look for 31 01 pattern in window
        for i in range(len(window) - 3):
            if window[i] == 0x31 and window[i+1] == 0x01:
                rid = (window[i+2] << 8) | window[i+3]
                abs_off = window_start + i
                log.info("31 01 pattern → routine 0x%04X at 0x%X", rid, abs_off)
                results.append((rid, abs_off, "31-01-window"))

    if not results:
        log.warning("No candidates found in raw search")
        return None

    # Prefer results closest to the target string
    if offset != -1:
        results.sort(key=lambda r: abs(r[1] - offset))

    best = results[0]
    log.info("Best candidate: 0x%04X  (method: %s)", best[0], best[2])
    return best[0]


# ── PBL-based extraction ───────────────────────────────────────────────────────

def find_pbl_so() -> Optional[Path]:
    """Look for compiled pbl_linux.so in standard locations."""
    candidates = [
        Path(__file__).parent / "pbl_linux.so",
        Path(__file__).parent.parent / "lib" / "pbl_linux.so",
        Path("/usr/local/lib/pbl_linux.so"),
        Path("/tmp/pbl_linux.so"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def compile_pbl() -> Optional[Path]:
    """
    Attempt to compile pbl from source if git is available.
    Returns path to compiled .so or None.
    """
    try:
        import shutil
        if not shutil.which("git") or not shutil.which("gcc"):
            return None

        tmpdir = Path(tempfile.mkdtemp())
        log.info("Cloning peterGraf/pbl into %s ...", tmpdir)
        subprocess.run(
            ["git", "clone", "--depth=1",
             "https://github.com/peterGraf/pbl.git", str(tmpdir / "pbl")],
            check=True, capture_output=True, timeout=60)

        src_dir = tmpdir / "pbl" / "src" / "src"
        so_path = tmpdir / "pbl_linux.so"
        src_files = list(src_dir.glob("*.c"))
        if not src_files:
            return None

        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2",
             "-o", str(so_path)] + [str(f) for f in src_files] + ["-lm"],
            check=True, capture_output=True, timeout=120)

        if so_path.exists():
            # Copy to cp_tools/ for future use
            dest = Path(__file__).parent / "pbl_linux.so"
            import shutil
            shutil.copy(so_path, dest)
            log.info("pbl compiled → %s", dest)
            return dest
    except Exception as e:
        log.warning("pbl compile failed: %s", e)
    return None


def extract_with_pbl(db_path: Path, pbl_so: Path) -> Optional[int]:
    """
    Use the compiled PBL .so to decode the .sd.db file and extract
    the routine ID structurally.

    This mirrors what dumpMWB.py does for the .bv.db files but targets
    the service definition (.sd.db) format.
    """
    log.info("PBL extraction: %s → %s", pbl_so.name, db_path.name)
    try:
        pbl = ctypes.CDLL(str(pbl_so))
        log.info("PBL library loaded: %s", pbl_so)

        # PBL key-file operations
        pbl.pblKfOpen.restype  = ctypes.c_void_p
        pbl.pblKfOpen.argtypes = [ctypes.c_char_p, ctypes.c_int]
        pbl.pblKfClose.argtypes = [ctypes.c_void_p]
        pbl.pblKfFind.restype  = ctypes.c_long
        pbl.pblKfFind.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_char_p, ctypes.c_size_t,
                                   ctypes.c_char_p, ctypes.c_size_t]

        kf = pbl.pblKfOpen(str(db_path).encode(), 0)
        if not kf:
            log.warning("pblKfOpen returned NULL — not a valid PBL key file")
            return None

        key_buf  = ctypes.create_string_buffer(4096)
        data_buf = ctypes.create_string_buffer(65536)

        # Scan all keys for routine-related entries
        routine_id = None
        rc = pbl.pblKfFind(kf, 4, b"", 0, key_buf, 65536)  # 4 = PBLFIRST
        while rc >= 0:
            key = key_buf.value
            key_str = key.decode("utf-8", errors="replace")

            if "RoutiContrStart" in key_str and "CompoProte" in key_str:
                # The value bytes should contain the routine ID
                data = bytes(data_buf)[:max(0, rc)]
                log.info("Found key: %s  (%d bytes)", key_str[:80], len(data))

                # Scan the value for a plausible 2-byte routine ID
                for i in range(len(data) - 1):
                    val = (data[i] << 8) | data[i+1]
                    if 0x0100 <= val <= 0x8FFF and val not in (0xFFFF, 0x0000):
                        log.info("  Candidate routine ID: 0x%04X at value[%d]",
                                 val, i)
                        if routine_id is None:
                            routine_id = val

            rc = pbl.pblKfFind(kf, 5, b"", 0, key_buf, 65536)  # 5 = PBLNEXT

        pbl.pblKfClose(kf)
        return routine_id

    except Exception as e:
        log.error("PBL extraction error: %s", e)
        return None


# ── dumpMWB.py integration ────────────────────────────────────────────────────

def extract_with_dumpMWB(project_path: Path) -> Optional[int]:
    """
    Run kartoffelpflanze's dumpMWB.py against the AU57X project folder
    and parse the output JSON for the CP routine ID.

    Expects dumpMWB.py to be in the PATH or in lib/.
    """
    dump_script = None
    for candidate in [
        Path("lib/dumpMWB.py"),
        Path(__file__).parent.parent / "lib" / "dumpMWB.py",
        Path("dumpMWB.py"),
    ]:
        if candidate.exists():
            dump_script = candidate
            break

    if not dump_script:
        log.warning("dumpMWB.py not found — place it in lib/ or current dir")
        return None

    sd_db = project_path / "ES_LIBCompoProteGen3V12.sd.db"
    if not sd_db.exists():
        # Search recursively
        matches = list(project_path.rglob("ES_LIBCompoProteGen3V12.sd.db"))
        if matches:
            sd_db = matches[0]
        else:
            log.error("ES_LIBCompoProteGen3V12.sd.db not found under %s", project_path)
            return None

    log.info("Running dumpMWB.py on %s", sd_db)
    out_dir = Path(tempfile.mkdtemp())
    try:
        result = subprocess.run(
            [sys.executable, str(dump_script), "service", str(sd_db), str(out_dir)],
            capture_output=True, text=True, timeout=120)
        log.info("dumpMWB stdout: %s", result.stdout[:500])
        if result.returncode != 0:
            log.warning("dumpMWB stderr: %s", result.stderr[:500])

        # Search output JSON files for routine ID
        for json_file in out_dir.rglob("*.json"):
            try:
                data = json.loads(json_file.read_text(errors="replace"))
                text = json.dumps(data)
                if "RoutiContrStartRoutiCompoProte" in text:
                    log.info("Found CP routine reference in %s", json_file.name)
                    # Extract ID from JSON structure
                    m = re.search(r'"routineIdentifier"\s*:\s*"?0x?([0-9A-Fa-f]{2,4})"?',
                                  text)
                    if m:
                        rid = int(m.group(1), 16)
                        log.info("Routine ID from JSON: 0x%04X", rid)
                        return rid
            except Exception:
                continue
    except subprocess.TimeoutExpired:
        log.error("dumpMWB timed out")
    except Exception as e:
        log.error("dumpMWB error: %s", e)

    return None


# ── Save result ───────────────────────────────────────────────────────────────

def save_result(routine_id: Optional[int], source: str,
                confirmed: bool = False) -> Path:
    """Write the discovered routine ID to cp_tools/cp_routine_id.json."""
    out = {
        "routine_id_hex":   f"0x{routine_id:04X}" if routine_id else None,
        "routine_id_bytes": [routine_id >> 8, routine_id & 0xFF] if routine_id else None,
        "uds_sequence":     (f"31 01 {routine_id>>8:02X} {routine_id&0xFF:02X}"
                             if routine_id else None),
        "source":           source,
        "confirmed":        confirmed,
        "notes": (
            "This is the RoutineControl ID for RoutiContrStartRoutiCompoProte "
            "in ES_LIBCompoProteGen3V12. Once confirmed against a live J533, "
            "set confirmed=true. j533_probe.py will use this automatically."
        ),
    }
    out_path = Path(__file__).parent / "cp_routine_id.json"
    out_path.write_text(json.dumps(out, indent=2))
    return out_path


def load_confirmed_routine_id() -> Optional[int]:
    """
    Load the confirmed CP routine ID from cp_routine_id.json.
    Returns None if not yet confirmed.
    Called by j533_probe.py.
    """
    try:
        p = Path(__file__).parent / "cp_routine_id.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        if data.get("confirmed") and data.get("routine_id_hex"):
            return int(data["routine_id_hex"], 16)
    except Exception:
        pass
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-30s  %(levelname)s  %(message)s")

    ap = argparse.ArgumentParser(
        description="Extract CP routine ID from ES_LIBCompoProteGen3V12.sd.db")
    ap.add_argument("--db",      type=Path,
                    help="Direct path to ES_LIBCompoProteGen3V12.sd.db")
    ap.add_argument("--project", type=Path,
                    help="Path to AU57X ODIS project folder")
    ap.add_argument("--raw",     action="store_true",
                    help="Raw binary search only (no PBL library)")
    ap.add_argument("--pbl",     type=Path,
                    help="Path to pbl_linux.so (compiled PBL library)")
    ap.add_argument("--confirm", type=str, metavar="0x????",
                    help="Manually confirm a routine ID (e.g. --confirm 0x0261)")
    args = ap.parse_args()

    # Manual confirmation
    if args.confirm:
        rid = int(args.confirm, 16)
        out = save_result(rid, "manual", confirmed=True)
        print(f"\n[CONFIRMED] Routine ID: 0x{rid:04X}")
        print(f"            UDS: 31 01 {rid>>8:02X} {rid&0xFF:02X}")
        print(f"            Written to: {out}")
        print(f"\nj533_probe.py will now use this ID automatically.")
        return

    routine_id = None
    source     = "unknown"

    # 1. Try dumpMWB.py with project folder
    if args.project:
        routine_id = extract_with_dumpMWB(args.project)
        if routine_id:
            source = f"dumpMWB: {args.project.name}"

    # 2. Try PBL-based extraction
    if routine_id is None and args.db and not args.raw:
        pbl_so = args.pbl or find_pbl_so()
        if pbl_so is None:
            print("pbl_linux.so not found. Attempting compile...")
            pbl_so = compile_pbl()
        if pbl_so:
            routine_id = extract_with_pbl(args.db, pbl_so)
            if routine_id:
                source = f"PBL: {args.db.name}"

    # 3. Raw binary search
    if routine_id is None and args.db:
        routine_id = search_raw(args.db)
        if routine_id:
            source = f"raw-search: {args.db.name}"

    # Results
    print("\n" + "=" * 60)
    if routine_id:
        print(f"  CP Routine ID found:  0x{routine_id:04X}")
        print(f"  UDS sequence:         31 01 {routine_id>>8:02X} {routine_id&0xFF:02X}")
        print(f"  Source:               {source}")
        print()
        print("  This is a CANDIDATE — confirm against a live J533 before")
        print("  trusting it. Run with --confirm 0x???? once verified.")
        out = save_result(routine_id, source, confirmed=False)
        print(f"\n  Written to: {out}")
    else:
        print("  No routine ID found.")
        print()
        print("  Provide the .sd.db file directly:")
        print("    python -m cp_tools.mwb_extract --db ES_LIBCompoProteGen3V12.sd.db")
        print()
        print("  Or the full AU57X project folder:")
        print("    python -m cp_tools.mwb_extract --project /path/to/AU57X")
    print("=" * 60)


if __name__ == "__main__":
    main()
