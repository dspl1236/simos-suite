"""
tests/test_vin_utils.py — VIN utility tests for simos-suite.

Tests cover:
  1.  validate_vin — valid 17-char VIN passes
  2.  validate_vin — lowercase normalised to uppercase
  3.  validate_vin — short VIN raises VINError
  4.  validate_vin — long VIN raises VINError
  5.  validate_vin — illegal char I raises VINError
  6.  validate_vin — illegal char O raises VINError
  7.  validate_vin — illegal char Q raises VINError
  8.  validate_vin — spaces stripped before validation
  9.  compare_vin  — identical VINs match=True
  10. compare_vin  — different VINs match=False with position detail
  11. compare_vin  — WMI extracted correctly
  12. compare_vin  — model_year extracted correctly
  13. compare_vin  — seq_number extracted correctly
  14. compare_vin  — JHM note present on mismatch
  15. compare_vin  — no JHM note on match

Reference VIN: WAUGGAFC7DN120188
  WAU = Audi AG (Ingolstadt)
  GGA = body/model code
  FC7 = check digit / 2013 model year (D)
  D   = model year 2013
  N   = plant (Neckarsulm)
  120188 = sequence number

Run standalone:
    python -m pytest tests/test_vin_utils.py -v

Or via the simos-suite test runner:
    python -m tests
"""
from __future__ import annotations

import pathlib
import sys

# Ensure repo root is on path
_repo = pathlib.Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

import pytest
from flasher.vin_utils import VINError, validate_vin, compare_vin

# Reference values
VALID_VIN     = "WAUGGAFC7DN120188"
VALID_VIN_LC  = "wauggafc7dn120188"


# ── validate_vin ──────────────────────────────────────────────────────────────

def test_validate_vin_valid():
    """Well-formed 17-char VIN passes and returns uppercase."""
    assert validate_vin(VALID_VIN) == VALID_VIN


def test_validate_vin_lowercase_normalised():
    """Lowercase input is normalised to uppercase."""
    assert validate_vin(VALID_VIN_LC) == VALID_VIN


def test_validate_vin_short_raises():
    """VIN shorter than 17 chars raises VINError."""
    with pytest.raises(VINError, match="17"):
        validate_vin("WAUGGAFC7DN12018")      # 16 chars


def test_validate_vin_long_raises():
    """VIN longer than 17 chars raises VINError."""
    with pytest.raises(VINError, match="17"):
        validate_vin("WAUGGAFC7DN1201889")    # 18 chars


def test_validate_vin_strips_whitespace():
    """Leading/trailing whitespace is stripped before validation."""
    assert validate_vin(f"  {VALID_VIN}  ") == VALID_VIN


def test_validate_vin_illegal_char_I():
    """VIN containing 'I' raises VINError (ISO 3779 excludes I/O/Q)."""
    with pytest.raises(VINError):
        validate_vin("IAUGGAFC7DN120188")


def test_validate_vin_illegal_char_O():
    """VIN containing 'O' raises VINError."""
    with pytest.raises(VINError):
        validate_vin("OAUGGAFC7DN120188")


def test_validate_vin_illegal_char_Q():
    """VIN containing 'Q' raises VINError."""
    with pytest.raises(VINError):
        validate_vin("QAUGGAFC7DN120188")


def test_validate_vin_all_digits():
    """All-digit VIN of length 17 is valid."""
    assert validate_vin("12345678901234567") == "12345678901234567"


def test_validate_vin_mixed_valid_chars():
    """All legal alphanumeric characters (no I/O/Q) are accepted."""
    # Use A-H, J-N, P-Z, 0-9 — no I, O, Q
    assert validate_vin("ABCDEFGHJKLMNPRS0") == "ABCDEFGHJKLMNPRS0"


# ── compare_vin ───────────────────────────────────────────────────────────────

def test_compare_vin_match():
    """Identical VINs produce match=True and a success note."""
    result = compare_vin(VALID_VIN, VALID_VIN)
    assert result["match"] is True
    assert any("✓" in n for n in result["notes"])


def test_compare_vin_mismatch():
    """Different VINs produce match=False and position-level detail."""
    vin_b = VALID_VIN[:-1] + "9"    # last char differs
    result = compare_vin(VALID_VIN, vin_b)
    assert result["match"] is False
    assert any("⚠" in n for n in result["notes"])
    # Should flag position 17
    pos_notes = [n for n in result["notes"] if "position 17" in n]
    assert len(pos_notes) > 0


def test_compare_vin_mismatch_multiple_positions():
    """Multiple character differences are all reported."""
    vin_b = "XAUGGAFC7DN12018X"     # positions 1 and 17 differ
    result = compare_vin(VALID_VIN, vin_b)
    assert result["match"] is False
    pos_notes = [n for n in result["notes"] if "position" in n]
    assert len(pos_notes) >= 2


def test_compare_vin_wmi():
    """WMI (first 3 chars) is extracted from ECU VIN."""
    result = compare_vin(VALID_VIN, VALID_VIN)
    assert result["wmi"] == "WAU"   # Audi AG Ingolstadt


def test_compare_vin_model_year():
    """Model year character at position 10 is extracted."""
    result = compare_vin(VALID_VIN, VALID_VIN)
    assert result["model_year"] == "D"   # 2013


def test_compare_vin_seq_number():
    """Sequence number (last 6 chars) is extracted."""
    result = compare_vin(VALID_VIN, VALID_VIN)
    assert result["seq_number"] == "120188"


def test_compare_vin_mismatch_mentions_jhm():
    """Mismatch notes mention JHM / tuner VIN lock as a possible cause."""
    vin_b = VALID_VIN[:-1] + "9"
    result = compare_vin(VALID_VIN, vin_b)
    jhm_notes = [n for n in result["notes"] if "JHM" in n or "tuner" in n.lower()]
    assert len(jhm_notes) > 0, "Expected JHM tuner-lock note in mismatch result"


def test_compare_vin_match_no_jhm_note():
    """Matching VINs do NOT produce a JHM/tuner note."""
    result = compare_vin(VALID_VIN, VALID_VIN)
    jhm_notes = [n for n in result["notes"] if "JHM" in n]
    assert len(jhm_notes) == 0


def test_compare_vin_result_keys():
    """Result dict contains all expected keys."""
    result = compare_vin(VALID_VIN, VALID_VIN)
    for key in ("match", "ecu_vin", "chassis_vin", "wmi", "model_year", "seq_number", "notes"):
        assert key in result, f"Missing key: {key}"


# ── run() — called from tests/__main__.py ────────────────────────────────────

def run(verbose: bool = False) -> bool:
    """Run all VIN tests via pytest and return True if all passed."""
    import subprocess
    args = [sys.executable, "-m", "pytest", str(pathlib.Path(__file__)), "-q"]
    if verbose:
        args.append("-v")
    result = subprocess.run(args, cwd=str(_repo))
    return result.returncode == 0
