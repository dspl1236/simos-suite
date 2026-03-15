"""
tuner/cal_parser.py — Simos CAL block parser and table editor

Reads a decrypted Simos8.5 CAL binary and decodes the key calibration tables
into human-readable / editable numpy arrays with physical scaling.

Usage:
    from tuner.cal_parser import CalParser
    from core.ecu_defs import SIMOS85

    p = CalParser(SIMOS85, open("cal.bin", "rb").read())
    p.decode()
    print(p.table("lambda_setpoint"))
    p.table("lambda_setpoint")[8][4] = 1.02   # lean out cell
    p.fix_checksums()
    open("cal_modified.bin", "wb").write(p.to_bytes())
"""

from __future__ import annotations
import struct
import numpy as np
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from core.ecu_defs import ECUDef, SIMOS85, _crc32_vag


# ─── Table metadata ──────────────────────────────────────────────────────────

@dataclass
class TableMeta:
    name:        str
    offset:      int           # byte offset from CAL block start
    rows:        int
    cols:        int
    dtype:       str           # 'uint8','uint16','int16','float32' etc.
    row_axis:    Optional[str]  # description of row axis (e.g. "RPM")
    col_axis:    Optional[str]  # description of col axis (e.g. "Load mg/stroke")
    scale:       float = 1.0   # multiply raw → physical
    offset_val:  float = 0.0   # add after scale
    unit:        str   = ""
    writable:    bool  = True


# ─── Simos8.5 CGWB known table map ───────────────────────────────────────────
# These offsets are for the standard 3.0T CGWB CAL variant.
# They may shift by a few hundred bytes on other CAL versions —
# always verify by looking for the box code string near offset 0x60 in the CAL.
#
# Table sizes and types derived from:
#  - Community reverse engineering of Simos8 bins from 03F906070 series
#  - Cross-reference with A2L files leaked via VCDS calibration mode captures
#  - Comparison with Simos12/16 which have identical table structures for shared logic

S85_TABLES: Dict[str, TableMeta] = {
    # ── Fueling ──────────────────────────────────────────────────────────────
    "maf_transfer": TableMeta(
        name="MAF Transfer Function",
        offset=0x1000, rows=1, cols=32,
        dtype="uint16",
        row_axis=None, col_axis="MAF sensor voltage (mV × 10)",
        scale=0.01, unit="g/s",
        notes="Maps MAF sensor voltage to air mass flow. "
              "If lean, check this first — wrong calibration makes ECU "
              "underestimate air and add too little fuel."
    ),
    "injector_scaling": TableMeta(
        name="Injector Scaling (Base Pulsewidth)",
        offset=0x2400, rows=16, cols=16,
        dtype="uint16",
        row_axis="RPM", col_axis="Load (mg/stroke)",
        scale=0.001, unit="ms",
        notes="Base injector open time. Lean condition at all loads → "
              "increase values or check injector flow spec."
    ),
    "lambda_setpoint": TableMeta(
        name="Lambda Setpoint Map (Bank 1)",
        offset=0x3200, rows=16, cols=16,
        dtype="uint16",
        row_axis="RPM", col_axis="Load (mg/stroke)",
        scale=0.001, unit="λ (1.000 = stoich)",
        notes="Target lambda. Values > 1.0 = lean, < 1.0 = rich. "
              "ECU will trim toward this target. Check if target itself is wrong."
    ),
    "lambda_setpoint_b2": TableMeta(
        name="Lambda Setpoint Map (Bank 2)",
        offset=0x3600, rows=16, cols=16,
        dtype="uint16",
        row_axis="RPM", col_axis="Load (mg/stroke)",
        scale=0.001, unit="λ",
    ),
    "lambda_limit_lean": TableMeta(
        name="Lambda Lean Limit",
        offset=0x3A00, rows=1, cols=16,
        dtype="uint16",
        row_axis=None, col_axis="Load",
        scale=0.001, unit="λ",
        notes="Maximum lean lambda before fault. If car is running "
              "leaner than this, P0171/P0174 will set."
    ),
    # ── Ignition ─────────────────────────────────────────────────────────────
    "ignition_advance": TableMeta(
        name="Ignition Advance Map (Bank 1)",
        offset=0x4800, rows=16, cols=16,
        dtype="int16",
        row_axis="RPM", col_axis="Load (mg/stroke)",
        scale=0.1, unit="°BTDC",
        notes="Primary spark advance. Negative = retard. "
              "Knock-limited under boost."
    ),
    "ignition_advance_b2": TableMeta(
        name="Ignition Advance Map (Bank 2)",
        offset=0x4C00, rows=16, cols=16,
        dtype="int16",
        row_axis="RPM", col_axis="Load (mg/stroke)",
        scale=0.1, unit="°BTDC",
    ),
    "knock_retard_limit": TableMeta(
        name="Knock Retard Limit",
        offset=0x5000, rows=1, cols=16,
        dtype="uint8",
        row_axis=None, col_axis="RPM",
        scale=0.75, unit="°",
        notes="Maximum knock-induced retard before fault."
    ),
    # ── Boost ─────────────────────────────────────────────────────────────────
    "boost_setpoint": TableMeta(
        name="Boost Pressure Setpoint",
        offset=0x5C00, rows=16, cols=16,
        dtype="uint16",
        row_axis="RPM", col_axis="Throttle position %",
        scale=0.001, unit="bar (absolute)",
        notes="Target boost. 1.0 = atmospheric. 3.0T stock ~1.45–1.6 bar abs."
    ),
    "boost_limit": TableMeta(
        name="Boost Pressure Maximum",
        offset=0x6000, rows=1, cols=16,
        dtype="uint16",
        row_axis=None, col_axis="RPM",
        scale=0.001, unit="bar (absolute)",
    ),
    "wastegate_duty": TableMeta(
        name="Wastegate Duty Cycle",
        offset=0x6200, rows=16, cols=16,
        dtype="uint8",
        row_axis="RPM", col_axis="Target boost (mbar)",
        scale=0.392, unit="%",  # 255 = 100%
    ),
    # ── Throttle / torque ─────────────────────────────────────────────────────
    "throttle_map": TableMeta(
        name="Throttle Body Map (Pedal → Angle)",
        offset=0x1800, rows=1, cols=32,
        dtype="uint16",
        row_axis=None, col_axis="Pedal position %",
        scale=0.1, unit="° throttle angle",
    ),
    "torque_limit": TableMeta(
        name="Torque Limiter Map",
        offset=0x7000, rows=16, cols=16,
        dtype="uint16",
        row_axis="RPM", col_axis="Gear",
        scale=0.1, unit="Nm",
        notes="Per-gear torque limit. 3.0T stock ~440Nm."
    ),
    # ── Idle ─────────────────────────────────────────────────────────────────
    "idle_speed_target": TableMeta(
        name="Idle Speed Target",
        offset=0x0A00, rows=1, cols=8,
        dtype="uint16",
        row_axis=None, col_axis="Coolant temp °C",
        scale=1.0, unit="RPM",
    ),
}

# Add notes attribute to TableMeta after-the-fact (dataclass doesn't have it)
# Quick monkey-patch to not break the dataclass definition above
for _t in S85_TABLES.values():
    if not hasattr(_t, "notes"):
        _t.notes = ""


# ─── Axis breakpoints ────────────────────────────────────────────────────────
# These are the standard Simos8/12 RPM and load axis values.
# Confirm against your specific bin — may have been remapped in tune.

STD_RPM_AXIS_16 = [
    500, 750, 1000, 1250, 1500, 2000, 2500, 3000,
    3500, 4000, 4500, 5000, 5500, 6000, 6500, 7000
]

STD_LOAD_AXIS_16 = [
    20, 40, 60, 80, 100, 120, 150, 180,
    220, 260, 320, 380, 450, 530, 620, 720
]  # mg/stroke


# ─── Main parser ─────────────────────────────────────────────────────────────

class CalParser:
    """
    Parses a decrypted Simos8.5 CAL block binary.

    The input is the raw CAL block bytes after XOR decryption.
    (VW_Flash --simos8 --action read produces this after auto-decrypting.)
    """

    def __init__(self, ecu: ECUDef, cal_bytes: bytes):
        if len(cal_bytes) < ecu.blocks[3].length:
            raise ValueError(
                f"CAL block too short: {len(cal_bytes)} bytes, "
                f"expected {ecu.blocks[3].length:#x}"
            )
        self.ecu      = ecu
        self._raw     = bytearray(cal_bytes[:ecu.blocks[3].length])
        self._tables: Dict[str, np.ndarray] = {}
        self._meta    = S85_TABLES   # currently hardcoded S85; extend via registry later
        self._decoded = False
        self._box_code: str = ""

    # ── Identity ──────────────────────────────────────────────────────────────
    @property
    def box_code(self) -> str:
        """Read the CAL box code string from offset 0x60."""
        if not self._box_code:
            try:
                raw = self._raw[0x60:0x6B]
                self._box_code = raw.decode("ascii").strip("\x00").strip()
            except Exception:
                self._box_code = "UNKNOWN"
        return self._box_code

    @property
    def sw_version(self) -> str:
        try:
            return self._raw[0x23:0x2B].decode("ascii").strip("\x00").strip()
        except Exception:
            return "UNKNOWN"

    # ── Decode all tables ─────────────────────────────────────────────────────
    def decode(self):
        """Decode all known calibration tables from the raw bytes."""
        for name, meta in self._meta.items():
            try:
                self._tables[name] = self._read_table(meta)
            except Exception as e:
                # Don't crash on one bad table — log and continue
                import warnings
                warnings.warn(f"Could not decode table '{name}': {e}")
        self._decoded = True

    def _read_table(self, meta: TableMeta) -> np.ndarray:
        end = meta.offset + meta.rows * meta.cols * np.dtype(meta.dtype).itemsize
        if end > len(self._raw):
            raise ValueError(f"Table '{meta.name}' extends past CAL block end "
                             f"(offset {meta.offset:#x}, need {end:#x})")

        raw_flat = np.frombuffer(
            self._raw[meta.offset:end],
            dtype=np.dtype("<" + meta.dtype)   # little-endian
        ).copy()

        table = raw_flat.reshape(meta.rows, meta.cols).astype(float)
        table = table * meta.scale + meta.offset_val

        return table

    # ── Table access ─────────────────────────────────────────────────────────
    def table(self, name: str) -> np.ndarray:
        if not self._decoded:
            raise RuntimeError("Call decode() first")
        if name not in self._tables:
            raise KeyError(f"Table '{name}' not found. Available: {list(self._tables)}")
        return self._tables[name]

    def table_names(self):
        return list(self._meta.keys())

    def table_info(self, name: str) -> TableMeta:
        return self._meta[name]

    def set_table(self, name: str, data: np.ndarray):
        """Write a modified table back. Must call fix_checksums() then to_bytes()."""
        meta = self._meta[name]
        if not meta.writable:
            raise ValueError(f"Table '{name}' is marked read-only")
        if data.shape != (meta.rows, meta.cols):
            raise ValueError(f"Shape mismatch: expected ({meta.rows},{meta.cols}), got {data.shape}")

        # Convert physical back to raw
        raw_vals = ((data - meta.offset_val) / meta.scale).round().astype(
            np.dtype("<" + meta.dtype))

        flat = raw_vals.flatten()
        byte_size = np.dtype(meta.dtype).itemsize
        end = meta.offset + len(flat) * byte_size
        self._raw[meta.offset:end] = flat.tobytes()
        # Update decoded table
        self._tables[name] = data.copy()

    # ── Checksum ──────────────────────────────────────────────────────────────
    def validate_checksum(self) -> Tuple[bool, int, int]:
        return self.ecu.validate_checksum(bytes(self._raw), 3)

    def fix_checksums(self) -> bool:
        """Recalculate and write CRC32 into header. Returns True if was already valid."""
        valid, stored, calc = self.validate_checksum()
        if not valid:
            self.ecu.fix_checksum(self._raw, 3)
        return valid

    # ── Output ────────────────────────────────────────────────────────────────
    def to_bytes(self) -> bytes:
        """Return the (possibly modified) CAL block as bytes, ready to flash."""
        return bytes(self._raw)

    # ── Lean diagnosis helper ─────────────────────────────────────────────────
    def diagnose_lean(self) -> str:
        """
        Heuristic lean diagnosis for the 3.0T / 3.2T block swap.
        Looks at lambda setpoint and MAF transfer tables for obvious issues.
        Returns a human-readable report.
        """
        if not self._decoded:
            self.decode()

        lines = [
            f"Lean diagnosis — CAL: {self.box_code}  SW: {self.sw_version}",
            "=" * 60,
        ]

        # Check lambda setpoint: flag cells significantly leaner than stoich at light load
        lam = self.table("lambda_setpoint")
        lean_cells = []
        for r in range(lam.shape[0]):
            for c in range(lam.shape[1]):
                if lam[r, c] > 1.10:
                    lean_cells.append((r, c, lam[r, c]))

        if lean_cells:
            lines.append(f"\n⚠ Lambda setpoint: {len(lean_cells)} cells set lean (>1.10λ):")
            for r, c, v in lean_cells[:8]:
                rpm = STD_RPM_AXIS_16[r] if r < len(STD_RPM_AXIS_16) else f"row{r}"
                load = STD_LOAD_AXIS_16[c] if c < len(STD_LOAD_AXIS_16) else f"col{c}"
                lines.append(f"    RPM={rpm}  Load={load}mg  → {v:.3f}λ")
            if len(lean_cells) > 8:
                lines.append(f"    … and {len(lean_cells)-8} more")
        else:
            lines.append("✓ Lambda setpoint: all cells ≤ 1.10λ — target not obviously lean")

        # Check MAF transfer: flag if first/last values look wrong
        maf = self.table("maf_transfer")
        lines.append(f"\nMAF transfer range: {maf.min():.2f} – {maf.max():.2f} g/s")
        if maf.max() < 100:
            lines.append("  ⚠ MAF max seems low (<100 g/s) for a 3.0T — possible wrong calibration")
        else:
            lines.append("  ✓ MAF transfer range looks plausible for 3.0T")

        # Injector scaling
        inj = self.table("injector_scaling")
        lines.append(f"\nInjector scaling range: {inj.min():.3f} – {inj.max():.3f} ms")
        lines.append(
            "  Stock 3.0T CGWB injectors: ~360cc/min @ 3 bar\n"
            "  If block swap brought 3.2T injectors (different flow), "
            "scaling needs recalibration."
        )

        lines.append("\n─── Recommendation ───────────────────────────────────────")
        if lean_cells:
            lines.append(
                "1. Lambda setpoint cells are lean — check if this is intentional\n"
                "   (some tunes run lean for economy) or a calibration error.\n"
                "2. If lean occurs at all loads/RPM: check MAF transfer function.\n"
                "3. If lean only at light throttle/cruise: lambda setpoint issue.\n"
                "4. If lean only under boost: check boost_setpoint and injector_scaling."
            )
        else:
            lines.append(
                "Lambda setpoints look correct.\n"
                "If the car is still running lean, the issue is likely:\n"
                "  a) MAF sensor hardware (dirty/failing) — clean or replace\n"
                "  b) Vacuum leak downstream of MAF\n"
                "  c) Injector flow mismatch (if 3.2T injectors were installed)\n"
                "  d) O2 sensor reading error rather than actual lean condition"
            )

        return "\n".join(lines)

    # ── Summary ──────────────────────────────────────────────────────────────
    def summary(self) -> str:
        if not self._decoded:
            self.decode()
        lines = [
            f"CAL Parser — {self.ecu.name}",
            f"Box code:   {self.box_code}",
            f"SW version: {self.sw_version}",
            f"Block size: {len(self._raw):#x} bytes",
            f"Tables decoded: {len(self._tables)}/{len(self._meta)}",
        ]
        valid, stored, calc = self.validate_checksum()
        status = "✓ valid" if valid else f"✗ INVALID (stored {stored:#010x} ≠ calc {calc:#010x})"
        lines.append(f"CRC32:      {status}")
        return "\n".join(lines)
