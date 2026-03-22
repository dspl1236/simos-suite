"""
data/dtc_lookup.py — VAG DTC decoder

DTC database sourced from VW_Flash (bri3d/VW_Flash).
658 VAG-specific fault codes with P-codes, descriptions, and symbols.
"""
from __future__ import annotations
import csv, pathlib

_DTC_DB: dict[int, dict] = {}

def _load():
    if _DTC_DB:
        return
    csv_path = pathlib.Path(__file__).parent / "dtcs.csv"
    if not csv_path.exists():
        return
    with open(csv_path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                _DTC_DB[int(row["code"])] = {
                    "pcode":  row.get("pcode", ""),
                    "name":   row.get("name", ""),
                    "symbol": row.get("symbol", ""),
                }
            except (ValueError, KeyError):
                pass

def dtc_to_human(dtc_id: int, status_byte: int = 0) -> str:
    _load()
    entry = _DTC_DB.get(dtc_id)
    if not entry:
        return f"Unknown DTC 0x{dtc_id:06X}"
    parts = []
    if status_byte & 0x08: parts.append("Confirmed")
    if status_byte & 0x01: parts.append("Test Failed")
    if status_byte & 0x04: parts.append("Pending")
    if status_byte & 0x80: parts.append("MIL On")
    status = ", ".join(parts) if parts else "Stored"
    return f"{entry['pcode']} : {entry['name']} [{status}]"

def load_dtcs() -> dict[int, dict]:
    _load()
    return dict(_DTC_DB)
