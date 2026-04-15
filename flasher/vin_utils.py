"""
flasher/vin_utils.py — ECU VIN read, write, and validation utilities.

VIN on Simos 8.5 / 12 / 18:
  DID 0xF190 — 17-byte ASCII VIN (ISO 3779: WMI+VDS+VIS)
  Stored in the CAL block dataset header.
  Written by tuner tooling (JHM etc.) during flash; may be VIN-locked.

  Read:  ExtendedDiagnosticSession → ReadDataByIdentifier(0xF190)
  Write: ExtendedDiagnosticSession → SA2 unlock → WriteDataByIdentifier(0xF190)

JHM VIN locking:
  JHM's server encodes your VIN into the tune file before delivery.
  This function lets you read back and verify what the ECU has stored,
  and overwrite it if needed (e.g. after re-flashing stock).

Usage:
    from flasher.vin_utils import read_vin, write_vin, validate_vin

    vin = read_vin(ecu, interface="J2534", interface_path="path/to.dll")
    print(vin)   # "WAUGGAFC7DN120188"

    # Update after reflash:
    write_vin(ecu, "WAUGGAFC7DN120188",
              interface="J2534", interface_path="path/to.dll")
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import udsoncan
from udsoncan.client import Client
from udsoncan import configs

log = logging.getLogger("SimosSuite.VIN")

# DID 0xF190 — ISO 3779 VIN (17 ASCII chars)
VIN_DID = 0xF190

# VIN format: 1 check char (I/O/Q excluded) + 16 alphanumeric (no I/O/Q in first 9)
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


class VINError(Exception):
    pass


def validate_vin(vin: str) -> str:
    """
    Validate and normalise a VIN string.

    Args:
        vin: 17-character VIN string (case-insensitive)

    Returns:
        Uppercase VIN if valid.

    Raises:
        VINError: if the VIN is not valid ISO 3779 format.
    """
    vin = vin.strip().upper()
    if len(vin) != 17:
        raise VINError(
            f"VIN must be exactly 17 characters (got {len(vin)}: {vin!r})"
        )
    if not _VIN_RE.match(vin):
        # Find the offending characters for a helpful message
        bad = [c for c in vin if c not in "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"]
        raise VINError(
            f"VIN contains invalid characters: {bad!r}. "
            "VINs use A-H, J-N, P-R, S-Z, 0-9 (I, O, Q excluded)."
        )
    return vin


def read_vin(
    ecu,
    interface: str = "J2534",
    interface_path: Optional[str] = None,
    ble_bridge=None,
) -> str:
    """
    Read the VIN stored in the ECU (DID 0xF190).

    Opens an extended diagnostic session (SA2 NOT required for read).

    Args:
        ecu:            ECUDef from core.ecu_defs
        interface:      "J2534", "BLE", or "SocketCAN_canX"
        interface_path: J2534 DLL path or SocketCAN interface name
        ble_bridge:     BLEBridgeSync instance (required for BLE)

    Returns:
        17-character VIN string.

    Raises:
        VINError: if VIN cannot be read or is malformed.
    """
    from flasher.uds_flash import _make_connection

    class _StrCodec(udsoncan.DidCodec):
        def encode(self, v): return bytes(v)
        def decode(self, p):
            try:
                return p.decode("ascii").strip("\x00 \t\r\n")
            except Exception:
                return p.hex().upper()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    conn = _make_connection(ecu, interface, interface_path, ble_bridge=ble_bridge)
    cfg = dict(configs.default_client_config)
    cfg["data_identifiers"] = {VIN_DID: _StrCodec}
    cfg["request_timeout"] = 5

    try:
        with Client(conn, request_timeout=5, config=cfg) as client:
            client.change_session(
                udsoncan.services.DiagnosticSessionControl.Session.extendedDiagnosticSession
            )
            resp = client.read_data_by_identifier([VIN_DID])
            raw_vin = resp.service_data.values[VIN_DID]
            if isinstance(raw_vin, bytes):
                vin = raw_vin.decode("ascii", errors="replace").strip("\x00 ")
            else:
                vin = str(raw_vin).strip()

    except udsoncan.exceptions.NegativeResponseException as e:
        nrc = e.response.code if hasattr(e, "response") else 0
        raise VINError(f"ECU refused VIN read — NRC 0x{nrc:02X}") from e
    except Exception as e:
        raise VINError(f"VIN read failed: {e}") from e

    if not vin or len(vin) != 17:
        raise VINError(
            f"ECU returned unexpected VIN data: {vin!r} ({len(vin)} chars)"
        )

    log.info("read_vin: %s", vin)
    return vin


def write_vin(
    ecu,
    vin: str,
    interface: str = "J2534",
    interface_path: Optional[str] = None,
    ble_bridge=None,
) -> None:
    """
    Write a VIN to the ECU (DID 0xF190).

    Requires extended diagnostic session + SA2 security access unlock.
    Use after reflashing with a tune that has a different VIN, or to
    correct a JHM VIN-locked tune on a different chassis.

    Args:
        ecu:            ECUDef from core.ecu_defs
        vin:            17-character VIN string (validated before write)
        interface:      "J2534", "BLE", or "SocketCAN_canX"
        interface_path: J2534 DLL path or SocketCAN interface name
        ble_bridge:     BLEBridgeSync instance (required for BLE)

    Raises:
        VINError: if VIN is invalid, ECU refuses the write, or verify fails.
    """
    from flasher.uds_flash import _make_connection
    from sa2_seed_key.sa2_seed_key import Sa2SeedKey

    vin = validate_vin(vin)
    vin_bytes = vin.encode("ascii")

    class _VINCodec(udsoncan.DidCodec):
        def encode(self, v): return bytes(v)
        def decode(self, p):
            try:
                return p.decode("ascii").strip("\x00 ")
            except Exception:
                return p.hex().upper()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    conn = _make_connection(ecu, interface, interface_path, ble_bridge=ble_bridge)
    cfg = dict(configs.default_client_config)
    cfg["data_identifiers"] = {VIN_DID: _VINCodec}
    cfg["request_timeout"] = 10

    try:
        with Client(conn, request_timeout=10, config=cfg) as client:
            # Extended diagnostic session
            client.change_session(
                udsoncan.services.DiagnosticSessionControl.Session.extendedDiagnosticSession
            )
            log.debug("write_vin: extended session opened")

            # SA2 security access (level 0x03/0x04)
            seed_resp = client.request_seed(0x03)
            seed = bytes(seed_resp.service_data.seed)
            log.debug("write_vin: SA2 seed = %s", seed.hex())

            sk = Sa2SeedKey(ecu.sa2_script, int.from_bytes(seed, "big"), 0)
            sk.execute()
            key = sk.key_int.to_bytes(4, "big")
            log.debug("write_vin: SA2 key  = %s", key.hex())

            client.send_key(0x04, key)
            log.debug("write_vin: SA2 unlocked")

            # Write DID 0xF190
            client.write_data_by_identifier(VIN_DID, vin_bytes)
            log.info("write_vin: wrote %s to DID 0xF190", vin)

            # Read back and verify
            resp = client.read_data_by_identifier([VIN_DID])
            readback = resp.service_data.values[VIN_DID]
            if isinstance(readback, bytes):
                readback_str = readback.decode("ascii", errors="replace").strip("\x00 ")
            else:
                readback_str = str(readback).strip()

            if readback_str != vin:
                raise VINError(
                    f"VIN write verify failed — wrote {vin!r} but "
                    f"read back {readback_str!r}"
                )
            log.info("write_vin: verified %s ✓", vin)

    except VINError:
        raise
    except udsoncan.exceptions.NegativeResponseException as e:
        nrc = e.response.code if hasattr(e, "response") else 0
        msg = {
            0x22: "conditionsNotCorrect — SA2 unlock may have failed",
            0x31: "requestOutOfRange — VIN rejected (invalid format?)",
            0x33: "securityAccessDenied — wrong SA2 key",
            0x35: "invalidKey",
        }.get(nrc, f"NRC 0x{nrc:02X}")
        raise VINError(f"ECU refused VIN write — {msg}") from e
    except Exception as e:
        raise VINError(f"VIN write failed: {e}") from e


def compare_vin(ecu_vin: str, chassis_vin: str) -> dict:
    """
    Compare ECU-stored VIN against chassis VIN.

    Args:
        ecu_vin:     VIN read from ECU (DID 0xF190)
        chassis_vin: expected VIN (from V5C / dashboard sticker / VCDS chassis)

    Returns dict with:
        match:       bool
        ecu_vin:     str
        chassis_vin: str
        wmi:         first 3 chars (World Manufacturer Identifier)
        model_year:  character at position 10 (year code)
        seq_number:  last 6 digits
        notes:       list of str — human-readable findings
    """
    ecu_vin     = ecu_vin.strip().upper()
    chassis_vin = chassis_vin.strip().upper()
    match = ecu_vin == chassis_vin

    notes = []
    if match:
        notes.append("✓ ECU VIN matches chassis VIN")
    else:
        notes.append("⚠ VIN MISMATCH — ECU and chassis VINs differ")
        # Highlight where they differ
        for i, (a, b) in enumerate(zip(ecu_vin, chassis_vin)):
            if a != b:
                notes.append(f"  position {i+1}: ECU={a!r}  chassis={b!r}")
        if len(ecu_vin) != len(chassis_vin):
            notes.append(f"  length: ECU={len(ecu_vin)}  chassis={len(chassis_vin)}")

    # Decode the ECU VIN structure
    result = {
        "match":       match,
        "ecu_vin":     ecu_vin,
        "chassis_vin": chassis_vin,
        "wmi":         ecu_vin[:3] if len(ecu_vin) >= 3 else "?",
        "model_year":  ecu_vin[9]  if len(ecu_vin) >= 10 else "?",
        "seq_number":  ecu_vin[11:] if len(ecu_vin) == 17 else "?",
        "notes":       notes,
    }

    # Annotate JHM / tuner lock
    if not match:
        notes.append(
            "If the car was tuned by JHM or another server-locked tuner, "
            "the tune file may have been issued for a different VIN. "
            "Use write_vin() to correct if needed."
        )

    return result
