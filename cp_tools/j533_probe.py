"""
cp_tools/j533_probe.py — J533 Component Protection research tool

This is the active data capture layer. Connects to J533 via UDS, reads every
accessible DID, attempts to decode constellation-related data, and logs the
full raw exchange for offline analysis.

Designed to run ALONGSIDE an ODIS session (using the ESP32 raw sniff mode) to
capture the exact UDS byte sequence ODIS sends during a CP removal operation.

Key findings from community research and hardware teardown:
  - CP state lives in the NEC/Renesas D70F3433(A) MCU internal flash
  - External 95320 EEPROM stores coding/adaptation but NOT the cryptographic constellation
  - CP removal uses RoutineControl (0x31) with a server-signed token
  - Token validated against public key embedded in J533 MCU firmware
  - J533 CAN: TX=0x710, RX=0x77A

Usage:
    from cp_tools.j533_probe import J533Probe
    probe = J533Probe(interface="J2534")
    probe.connect()
    report = probe.full_probe()
    probe.save_report("j533_probe.json")
"""

from __future__ import annotations

import json
import logging
import struct
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import udsoncan
from udsoncan.client import Client
from udsoncan import services, configs, exceptions

log = logging.getLogger("SimosSuite.J533Probe")

# ─── J533 CAN IDs ────────────────────────────────────────────────────────────

J533_TX = 0x710   # tester → J533
J533_RX = 0x77A   # J533 → tester

# J255 Climatronic (the module we need to de-CP)
J255_TX = 0x746
J255_RX = 0x7B0

# ─── Known / suspected DIDs ──────────────────────────────────────────────────

# Standard VW DIDs — most ECUs respond to these in extended session
STD_DIDS = {
    0xF190: "VIN",
    0xF18C: "ECU Serial Number",
    0xF187: "Spare Part Number",
    0xF189: "SW Application Version",
    0xF191: "HW Number",
    0xF1A3: "HW Version",
    0xF197: "System Name",
    0xF17C: "FAZIT Identification",
    0xF19E: "ASAM File ID",
    0xF1A2: "ASAM File Version",
    0x0405: "State of Flash Memory",
    0x0600: "Coding Value",
    0xF186: "Active Diagnostic Session",
    0xF442: "Control Module Voltage",
    0x02F9: "CRC32 Checksum of FAZIT Identification String",
    0x0101: "Node Position",
}

# ── Confirmed CP / constellation DIDs on J533 ─────────────────────────────────
#
# Source: AU57X ODIS MCD Project DVR 72 (September 2022), dumpMWB extraction
# from 0.0.0@BV_GatewUDS.bv.db using ODIS-project-explorer on Linux with
# native PBL library compiled from github.com/peterGraf/pbl.
# EV_GatewPKOUDS_001 = C7 A6/A7 non-hybrid gateway variant.
#
# ALL addresses below are CONFIRMED from the MWB JSON output — not estimated.
#
# ─── Constellation DIDs ───────────────────────────────────────────────────────
#
# 0x04A3  Gateway Component List (coded)
#   Structure: END-OF-PDU-FIELD of 1-byte records, each byte = 8-bit bitfield
#   where each bit indicates whether a slot is coded (1=yes, 0=no).
#   This is the primary read/write DID for the constellation table.
#   Write requires extended session + IKA key (DID 0x00BE).
#
# 0x2A2A  Gateway Component List allocation (ECU IDs + names)
#   Structure: END-OF-PDU-FIELD of 1-byte structs {ECU_ID u8, ECU_Name u8}
#   ECU Name values (confirmed): 8=Air Conditioning (J255), 1=Engine Control
#   Module 1, 2=Transmission, 3=Brakes, 54=Seat Adjustment Driver Side,
#   6=Seat Adjustment Passenger Side, 25=Gateway (J533), etc.
#   Read to find which slot index J255 occupies in the constellation.
#
# 0x2A26  Gateway Component List present
#   Per-module online/offline bitmap (END-OF-PDU-FIELD, 1 byte per 8 modules)
#   Bit=1 means module is online (present on bus).
#
# 0x2A27  Gateway Component List sleep indication
#   Per-module sleep state bitmap.
#
# 0x2A28  Gateway Component List DTC
#   Per-module error flag bitmap (1=Error, 0=OK).
#   Reading this tells you which modules have active DTCs.
#
# 0x2A29  Gateway Component List DiagProt
#   Per-module diagnostic protocol bitmap (bit0=ISO-TP, bit1=TP2.0, bit2=TP1.6,
#   bit3=K-Line, bit4=Ethernet).
#
# 0x2A2C  Gateway Component List TP-Identifier
#   CAN TX IDs for each enrolled module (u16 per entry).
#   J255 should appear here as 0x0746.
#
# ─── Theft protection / key download DIDs ─────────────────────────────────────
#
# 0x00BE  IKA Key (J533 + J255)
#   34 bytes (272 bits), A_BYTEFIELD, IDENTICAL compu.
#   Description: "Komponentenschutzschlüssel" (Component protection key).
#   This is written by ODIS/GEKO as part of CP removal — the installation
#   key binding the module to the vehicle.
#   Confirmed present in both J533 (BV_GatewUDS) and J255 (BV_AirCondiUDS).
#
# 0x00BD  GKA Key (J255 only)
#   34 bytes, A_BYTEFIELD.
#   Description: "GFA-Schlüssel / Schreiben des GFA-Schlüssels" (device class
#   authorization key). Confirmed in BV_AirCondiUDS adaptations only.
#
# ─── CP monitoring DIDs ───────────────────────────────────────────────────────
#
# 0x0438  Stored keys for theft protection slaves — raw bytefield
# 0x0439  KS ECUs currently authenticated incorrect — raw bytefield
# 0x043A  KS ECUs formerly authenticated incorrect since last clearance
# 0x043C  Number of successful key corrections — u8 BCD
# 0x043D  Number of successful key downloads — u8 BCD
# 0x043E  Theftprotection Showroom Mode — {0:'not active', 1:'active'}
# 0x2CA9  Service key 2 sampling status (SK2)
#
# ─── Security access ──────────────────────────────────────────────────────────
#
# All CP-related write services (WriteDataByIdentTheftProteData,
# WriteDataByIdentGatewCompoList, WriteDataByIdentCalibData) show
# access_level=None in the MWB service objects, meaning they operate in
# extended diagnostic session (0x10 0x03) without an additional SA2 challenge.
# The GEKO server token provides authorization, not a seed/key exchange.
# Read operations on constellation DIDs are available in default session.
#
# ─────────────────────────────────────────────────────────────────────────────

CONFIRMED_CP_DIDS = {
    # Constellation — read these to map the module layout
    0x04A3: "Gateway Component List (coded bitmap)",
    0x2A2A: "Gateway Component List allocation (ECU IDs + names)",
    0x2A26: "Gateway Component List present (online/offline bitmap)",
    0x2A27: "Gateway Component List sleep indication",
    0x2A28: "Gateway Component List DTC bitmap",
    0x2A29: "Gateway Component List DiagProt",
    0x2A2C: "Gateway Component List TP-Identifier (CAN IDs)",
    # Theft protection monitoring
    0x0438: "Stored keys for theft protection slaves",
    0x0439: "KS ECUs currently authenticated incorrect",
    0x043A: "KS ECUs formerly authenticated incorrect since last clearance",
    0x043C: "Number of successful key corrections",
    0x043D: "Number of successful key downloads",
    0x043E: "Theftprotection Showroom Mode",
    0x2CA9: "Service key 2 sampling status",
    # IKA key — write to deliver CP authorization
    0x00BE: "IKA Key (34 bytes — Komponentenschutzschlüssel)",
}

# J255-specific writable CP DIDs (confirmed from BV_AirCondiUDS adaptations)
J255_CP_WRITE_DIDS = {
    0x00BE: "IKA-Key (34 bytes) — installation key binding module to vehicle",
    0x00BD: "GKA-Key (34 bytes) — device class authorization key",
}

# ─── CP Routine ID — extracted from ES_LIBCompoProteGen3V12.sd.db ────────────
#
# The RoutineControl identifier for RoutiContrStartRoutiCompoProte.
# Extracted via binary analysis of ES_LIBCompoProteGen3V12.sd.db:
#   - 772-byte master index (unique to V12): header bytes[4:6] BE = 0x0226,
#     repeated at bytes[8:10] — characteristic double-key storage
#   - 268-byte service definition (new in V12): 3 sub-functions confirming
#     RoutineControl start/stop/requestResult pattern
#   - New 62-byte range index covering 0x0500–0xFFFF service group
#
# UDS sequence to start CP routine:
#   31 01 02 26  [payload...]
#
# Once confirmed on live J533, call:
#   python -m cp_tools.mwb_extract --confirm 0x0226
# and set confirmed=True in cp_routine_id.json.
#
CP_ROUTINE_ID: int = 0x0226   # pending hardware confirmation

def _load_cp_routine_id() -> int:
    """
    Load CP routine ID from cp_routine_id.json if confirmed, else use default.
    Allows hardware confirmation to auto-wire into probe without code changes.
    """
    try:
        import json, pathlib
        p = pathlib.Path(__file__).parent / "cp_routine_id.json"
        if p.exists():
            data = json.loads(p.read_text())
            if data.get("routine_id_hex"):
                return int(data["routine_id_hex"], 16)
    except Exception:
        pass
    return CP_ROUTINE_ID


# ECU name → slot value mapping (confirmed from DID 0x2A2A structure)
ECU_NAME_MAP = {
    1: "Engine Control Module 1",
    2: "Transmission Control Module",
    3: "Brakes 1",
    4: "Steering Angle",
    5: "Kessy",
    6: "Seat Adjustment Passenger Side",
    7: "Display Control Unit",
    8: "Air Conditioning",          # ← J255 HVAC
    9: "Central Electrics",
    17: "Engine Control Module 2",
    19: "Adaptive Cruise Control",
    21: "Airbag",
    23: "Dash Board",
    25: "Gateway",                  # ← J533 (self)
    37: "Immobilizer",
    54: "Seat Adjustment Driver Side",  # ← J136
    68: "Steering Assistance",
    71: "Sound System",
    95: "Information Control Unit 1",
    117: "Telematics",
}

# Legacy dict for backward compat — replaced by CONFIRMED_CP_DIDS above
SUSPECTED_CP_DIDS = CONFIRMED_CP_DIDS

# Known J533 adaptation channels
KNOWN_ADAPTATIONS = {
    0x00BE: "IKA Key (CP key — extended session write)",
    0x043E: "Theftprotection Showroom Mode",
    0x2CA8: "Service key 2 settings",
}


@dataclass
class DIDResult:
    did:      int
    label:    str
    raw_hex:  str
    decoded:  Optional[str]
    error:    Optional[str]


@dataclass
class ProbeReport:
    timestamp:      str
    vin:            str
    j533_serial:    str
    j533_part:      str
    j533_sw:        str
    j255_serial:    str         # the HVAC module serial
    j255_vin:       str         # what VIN J255 reports it belongs to
    j255_cp_status: str = ""    # IKA key state on J255
    std_dids:       List[DIDResult] = field(default_factory=list)
    cp_dids:        List[DIDResult] = field(default_factory=list)
    j255_dids:      List[DIDResult] = field(default_factory=list)
    scan_dids:      List[DIDResult] = field(default_factory=list)
    raw_log:        List[str]       = field(default_factory=list)
    analysis:       str = ""


class J533Probe:
    """
    UDS probe for J533 (and J255) — extracts all accessible data for CP research.
    """

    def __init__(self,
                 interface:      str = "J2534",
                 interface_path: Optional[str] = None,
                 scan_range:     Tuple[int, int] = (0x0100, 0x0300)):
        self.interface      = interface
        self.interface_path = interface_path
        self.scan_start, self.scan_end = scan_range
        self._client_j533:  Optional[Client] = None
        self._client_j255:  Optional[Client] = None
        self._raw_log:      List[str] = []

    # ── Connection ────────────────────────────────────────────────────────────

    def _make_conn(self, tx: int, rx: int):
        params = {"tx_padding": 0x55}
        if self.interface.upper() == "J2534":
            from lib.connections.j2534_connection import J2534Connection
            return J2534Connection(
                windll=self.interface_path,
                rxid=rx,
                txid=tx,
                st_min=0x19,   # ~1ms between frames
            )
        else:
            from udsoncan.connections import IsoTPSocketConnection
            iface = self.interface_path or self.interface.split("_", 1)[-1]
            return IsoTPSocketConnection(iface, rxid=rx, txid=tx, params=params)

    def _make_client(self, tx: int, rx: int) -> Client:
        conn = self._make_conn(tx, rx)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        all_dids = list(STD_DIDS) + list(CONFIRMED_CP_DIDS)
        cfg = dict(configs.default_client_config)
        cfg["data_identifiers"] = {did: _BytesCodec for did in all_dids}
        cfg["request_timeout"] = 5
        return Client(conn, request_timeout=5, config=cfg)

    # ── Low-level read ────────────────────────────────────────────────────────

    def _read_did(self, client: Client, did: int) -> Tuple[Optional[bytes], Optional[str]]:
        try:
            resp = client.read_data_by_identifier_first(did)
            raw = bytes(resp) if isinstance(resp, (bytes, bytearray)) else resp
            self._raw_log.append(f"  DID {did:#06x} → {raw.hex()}")
            return raw, None
        except exceptions.NegativeResponseException as e:
            nrc = e.response.code if hasattr(e.response, "code") else "?"
            self._raw_log.append(f"  DID {did:#06x} → NRC {nrc:#04x}")
            return None, f"NRC {nrc:#04x}"
        except exceptions.TimeoutException:
            self._raw_log.append(f"  DID {did:#06x} → TIMEOUT")
            return None, "TIMEOUT"
        except Exception as e:
            self._raw_log.append(f"  DID {did:#06x} → {type(e).__name__}: {e}")
            return None, str(e)

    def _decode_did(self, did: int, raw: bytes) -> str:
        """
        Decode a DID response using confirmed structure from AU57X MWB dump.

        All constellation DIDs (0x04A3, 0x2A2x) are END-OF-PDU-FIELDs of
        1-byte records — each byte is a bitfield of 8 module slots.
        Module slot index maps to (byte_offset * 8 + bit_position).
        DID 0x2A2A is different: pairs of {ECU_ID u8, ECU_Name u8}.
        DID 0x2A2C: u16 TP-CAN-Identifier per module (big-endian).
        DID 0x2CA9: bit 0=SK2 active, bit 1=request normal, bit 2=request
                    immediate, then 20-byte exception list × 2.
        """
        if not raw:
            return ""

        # ── ASCII identity fields ──────────────────────────────────────────────
        if did in (0xF190,):            # VIN — 17 bytes ASCII
            try: return raw[:17].decode("ascii").strip("\x00")
            except: pass

        if did in (0xF18C, 0xF187, 0xF189, 0xF191, 0xF1A3,
                   0xF197, 0xF17C, 0xF19E, 0xF1A2):
            try:
                s = raw.decode("ascii").strip("\x00 ")
                if s: return f'"{s}"'
            except: pass

        # ── 0x04A3 — Gateway Component List (coded bitmap) ────────────────────
        # END-OF-PDU-FIELD of 1-byte bitfield records.
        # Each byte covers 8 sequential module slots.
        # Slot N is coded if (raw[N//8] >> (N%8)) & 1.
        if did == 0x04A3:
            coded = []
            for byte_i, b in enumerate(raw):
                for bit_i in range(8):
                    if (b >> bit_i) & 1:
                        slot = byte_i * 8 + bit_i
                        coded.append(slot)
            return f"coded_slots={coded}  ({len(coded)} modules coded)"

        # ── 0x2A26 — Component List present (online bitmap) ───────────────────
        if did == 0x2A26:
            online = []
            for byte_i, b in enumerate(raw):
                for bit_i in range(8):
                    if (b >> bit_i) & 1:
                        online.append(byte_i * 8 + bit_i)
            return f"online_slots={online}"

        # ── 0x2A27 — Sleep indication bitmap ─────────────────────────────────
        if did == 0x2A27:
            sleeping = []
            for byte_i, b in enumerate(raw):
                for bit_i in range(8):
                    if (b >> bit_i) & 1:
                        sleeping.append(byte_i * 8 + bit_i)
            return f"sleeping_slots={sleeping}"

        # ── 0x2A28 — DTC bitmap ───────────────────────────────────────────────
        if did == 0x2A28:
            errors = []
            for byte_i, b in enumerate(raw):
                for bit_i in range(8):
                    if (b >> bit_i) & 1:
                        errors.append(byte_i * 8 + bit_i)
            return f"dtc_slots={errors}" if errors else "no_errors"

        # ── 0x2A29 — DiagProt per module ──────────────────────────────────────
        # Each byte = protocol flags for one module slot (not 8 per byte here,
        # one full byte per module). bit0=ISO-TP bit1=TP2.0 bit2=TP1.6 bit3=K-Line bit4=Ethernet
        if did == 0x2A29:
            PROT = {0: "ISO-TP", 1: "TP2.0", 2: "TP1.6", 3: "K-Line", 4: "Ethernet"}
            parts = []
            for slot, b in enumerate(raw):
                protos = [PROT[i] for i in range(5) if (b >> i) & 1]
                if protos:
                    parts.append(f"slot{slot}=[{','.join(protos)}]")
            return "  ".join(parts) if parts else raw.hex()

        # ── 0x2A2A — ECU IDs and Names (allocation table) ────────────────────
        # END-OF-PDU-FIELD of {ECU_ID u8, ECU_Name u8} pairs, byte_size=1 each.
        # ECU Name 8 = Air Conditioning (J255).
        if did == 0x2A2A:
            parts = []
            for i in range(0, len(raw) - 1, 2):
                ecu_id   = raw[i]
                ecu_name = raw[i + 1]
                label    = ECU_NAME_MAP.get(ecu_name, f"ECU_NAME_{ecu_name}")
                slot     = i // 2
                parts.append(f"slot{slot}:id={ecu_id:#04x},name={ecu_name}({label})")
            return "  ".join(parts)

        # ── 0x2A2C — TP-Identifier (CAN IDs per module) ──────────────────────
        # END-OF-PDU-FIELD of u16 big-endian CAN TX IDs, one per module slot.
        if did == 0x2A2C:
            parts = []
            for i in range(0, len(raw) - 1, 2):
                can_id = (raw[i] << 8) | raw[i + 1]
                slot   = i // 2
                parts.append(f"slot{slot}:{can_id:#06x}")
            return "  ".join(parts)

        # ── 0x2CA9 — Service key 2 sampling status ────────────────────────────
        # Byte 0 bits: [0]=SK2 active, [1]=request normal, [2]=request immediate
        # Bytes 1–20: ECU exception list 1 (20 bytes)
        # Bytes 21–40: ECU exception list 2 (20 bytes)
        if did == 0x2CA9 and len(raw) >= 1:
            flags = raw[0]
            active   = bool(flags & 0x01)
            req_norm = bool(flags & 0x02)
            req_imm  = bool(flags & 0x04)
            ex1 = raw[1:21].hex().upper()  if len(raw) > 20 else ""
            ex2 = raw[21:41].hex().upper() if len(raw) > 40 else ""
            return (f"SK2_active={active} req_normal={req_norm} req_immediate={req_imm}"
                    f"  exception1={ex1}  exception2={ex2}")

        # ── 0x043E — Showroom Mode ────────────────────────────────────────────
        if did == 0x043E and raw:
            return "active" if raw[0] else "not active"

        # ── BCD counters ──────────────────────────────────────────────────────
        if did in (0x043C, 0x043D) and len(raw) == 1:
            return f"{raw[0]}"   # BCD-P single byte

        # ── IKA / GKA key — 34 bytes bytefield ───────────────────────────────
        if did in (0x00BE, 0x00BD) and len(raw) >= 34:
            zeroed = all(b == 0 for b in raw[:34])
            return f"{'ZEROED (no key)' if zeroed else 'KEY_PRESENT'}  [{raw[:34].hex().upper()}]"

        # ── Raw bytefield fallback ─────────────────────────────────────────────
        try:
            s = raw.decode("ascii").strip("\x00 ")
            if s.isprintable() and len(s) >= 3:
                return f'"{s}"'
        except: pass
        return raw.hex(" ").upper()

    # ── Constellation helpers ─────────────────────────────────────────────────

    @staticmethod
    def find_j255_slot(allocation_raw: bytes) -> Optional[int]:
        """
        Parse DID 0x2A2A response to find J255's slot index.
        Returns slot index (0-based) or None if not found.
        J255 ECU Name value = 8 (Air Conditioning, confirmed from MWB dump).
        """
        for i in range(0, len(allocation_raw) - 1, 2):
            ecu_name = allocation_raw[i + 1]
            if ecu_name == 8:   # Air Conditioning
                return i // 2
        return None

    @staticmethod
    def decode_constellation(coded_raw: bytes,
                              allocation_raw: Optional[bytes] = None,
                              present_raw:    Optional[bytes] = None,
                              tp_id_raw:      Optional[bytes] = None,
                              ) -> List[dict]:
        """
        Decode a full constellation from raw DID responses.

        Returns list of dicts, one per active slot:
          {slot, coded, ecu_id, ecu_name, ecu_name_label, present, can_id}

        The J255 entry is the one with ecu_name==8.
        """
        # Build slot map from allocation table (DID 0x2A2A)
        alloc: dict = {}   # slot → {ecu_id, ecu_name}
        if allocation_raw:
            for i in range(0, len(allocation_raw) - 1, 2):
                slot     = i // 2
                ecu_id   = allocation_raw[i]
                ecu_name = allocation_raw[i + 1]
                alloc[slot] = {"ecu_id": ecu_id, "ecu_name": ecu_name,
                               "ecu_name_label": ECU_NAME_MAP.get(ecu_name,
                                                  f"ECU_NAME_{ecu_name}")}

        # Decode coded bitmap (DID 0x04A3)
        coded_slots: set = set()
        for byte_i, b in enumerate(coded_raw):
            for bit_i in range(8):
                if (b >> bit_i) & 1:
                    coded_slots.add(byte_i * 8 + bit_i)

        # Decode present bitmap (DID 0x2A26)
        present_slots: set = set()
        if present_raw:
            for byte_i, b in enumerate(present_raw):
                for bit_i in range(8):
                    if (b >> bit_i) & 1:
                        present_slots.add(byte_i * 8 + bit_i)

        # Decode TP IDs (DID 0x2A2C)
        tp_ids: dict = {}   # slot → can_id
        if tp_id_raw:
            for i in range(0, len(tp_id_raw) - 1, 2):
                slot   = i // 2
                can_id = (tp_id_raw[i] << 8) | tp_id_raw[i + 1]
                tp_ids[slot] = can_id

        # Merge into entries
        all_slots = coded_slots | set(alloc.keys()) | set(tp_ids.keys())
        entries = []
        for slot in sorted(all_slots):
            entry = {
                "slot":           slot,
                "coded":          slot in coded_slots,
                "present":        slot in present_slots,
                "ecu_id":         alloc.get(slot, {}).get("ecu_id"),
                "ecu_name":       alloc.get(slot, {}).get("ecu_name"),
                "ecu_name_label": alloc.get(slot, {}).get("ecu_name_label", "?"),
                "can_id":         tp_ids.get(slot),
            }
            entries.append(entry)
        return entries

    # ── Probe J533 ────────────────────────────────────────────────────────────

    def probe_j533(self) -> Tuple[Dict[int, DIDResult], Dict[int, DIDResult]]:
        """
        Read standard and confirmed CP DIDs from J533.
        Returns (std_results, cp_results) dicts keyed by DID.

        Confirmed DID addresses from AU57X EV_GatewPKOUDS_001 MWB dump:
          0x04A3  Gateway Component List (coded bitmap) — primary constellation
          0x2A2A  Allocation table (ECU IDs + name codes)
          0x2A26  Present bitmap
          0x2A27  Sleep bitmap
          0x2A28  DTC bitmap
          0x2A29  DiagProt per module
          0x2A2C  TP-Identifier (CAN IDs)
          0x0438  Stored keys for theft protection slaves
          0x0439  KS ECUs currently authenticated incorrect
          0x043D  Number of successful key downloads
          0x043E  Theftprotection Showroom Mode
          0x2CA9  Service key 2 sampling status
          0x00BE  IKA Key (readable in extended session)
        All in extended diagnostic session (0x10 0x03), no SA2 required.
        """
        self._raw_log.append("\n=== J533 PROBE ===")
        std_results: Dict[int, DIDResult] = {}
        cp_results:  Dict[int, DIDResult] = {}

        with self._make_client(J533_TX, J533_RX) as client:
            self._raw_log.append("→ DiagSessionControl extendedDiagnosticSession")
            try:
                client.change_session(
                    services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
                client.session_timing["p2_server_max"] = 30
                client.config["request_timeout"] = 30
            except Exception as e:
                log.error("J533: failed to open extended session: %s", e)
                return std_results, cp_results

            # Standard identity DIDs
            self._raw_log.append("--- Standard DIDs ---")
            for did, label in STD_DIDS.items():
                raw, err = self._read_did(client, did)
                std_results[did] = DIDResult(
                    did=did, label=label,
                    raw_hex=raw.hex() if raw else "",
                    decoded=self._decode_did(did, raw) if raw else None,
                    error=err)

            # Confirmed CP / constellation DIDs
            self._raw_log.append("--- Confirmed CP/constellation DIDs ---")
            for did, label in CONFIRMED_CP_DIDS.items():
                raw, err = self._read_did(client, did)
                cp_results[did] = DIDResult(
                    did=did, label=label,
                    raw_hex=raw.hex() if raw else "",
                    decoded=self._decode_did(did, raw) if raw else None,
                    error=err)

        return std_results, cp_results

    # ── DID range scan ────────────────────────────────────────────────────────

    def scan_did_range(self, progress_cb=None) -> List[DIDResult]:
        """
        Brute-force scan DID range on J533. Returns all responsive DIDs.
        Default range covers the 0x04A3/0x2A2x area from the confirmed map.
        Set scan_range=(0x0000, 0xFFFF) for a full sweep (takes ~45 min).
        """
        self._raw_log.append(
            f"\n=== DID SCAN {self.scan_start:#06x}–{self.scan_end:#06x} ===")
        found = []
        total = self.scan_end - self.scan_start

        with self._make_client(J533_TX, J533_RX) as client:
            try:
                client.change_session(
                    services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
                client.session_timing["p2_server_max"] = 30
                client.config["request_timeout"] = 0.8
            except Exception as e:
                log.error("DID scan: session failed: %s", e)
                return found

            for i, did in enumerate(range(self.scan_start, self.scan_end)):
                if progress_cb:
                    progress_cb(i, total, did)
                raw, err = self._read_did(client, did)
                if raw is not None:
                    label = CONFIRMED_CP_DIDS.get(did, f"DID_{did:04X}")
                    found.append(DIDResult(
                        did=did, label=label,
                        raw_hex=raw.hex(),
                        decoded=self._decode_did(did, raw),
                        error=None))
                    log.info("  FOUND DID %#06x: %s", did, raw.hex())

        return found

    # ── Probe J255 ────────────────────────────────────────────────────────────

    def probe_j255(self) -> Tuple[str, str, str, Dict[int, DIDResult]]:
        """
        Read J255 (Climatronic HVAC) identity and CP key state.
        Returns (serial, bound_vin, cp_status, did_results).

        Confirmed DIDs from AU57X BV_AirCondiUDS / EV_AirCondiComfoUDS_002:
          0xF18C  ECU Serial Number
          0xF190  VIN
          0xF187  Spare Part Number
          0xF189  SW Version
          0xF1DF  ECU Programming Information
          0x00BD  GKA-Key (34 bytes)
          0x00BE  IKA-Key (34 bytes)
        """
        self._raw_log.append("\n=== J255 PROBE ===")
        serial, vin, cp_status = "", "", "UNKNOWN"
        did_results: Dict[int, DIDResult] = {}

        j255_dids = {
            0xF18C: "ECU Serial Number",
            0xF190: "VIN",
            0xF187: "Spare Part Number",
            0xF189: "SW Version",
            0xF1A3: "HW Version",
            0xF197: "System Name",
            0xF1DF: "ECU Programming Information",
            0x00BD: "GKA-Key (34 bytes)",
            0x00BE: "IKA-Key (34 bytes)",
        }

        with self._make_client(J255_TX, J255_RX) as client:
            try:
                client.change_session(
                    services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
                client.session_timing["p2_server_max"] = 30
            except Exception as e:
                log.error("J255: session failed: %s", e)
                return serial, vin, cp_status, did_results

            for did, label in j255_dids.items():
                raw, err = self._read_did(client, did)
                did_results[did] = DIDResult(
                    did=did, label=label,
                    raw_hex=raw.hex() if raw else "",
                    decoded=self._decode_did(did, raw) if raw else None,
                    error=err)

            # Extract key fields
            if did_results[0xF18C].raw_hex:
                raw = bytes.fromhex(did_results[0xF18C].raw_hex)
                try: serial = raw.decode("ascii").strip("\x00 ")
                except: serial = raw.hex().upper()

            if did_results[0xF190].raw_hex:
                raw = bytes.fromhex(did_results[0xF190].raw_hex)
                try: vin = raw[:17].decode("ascii").strip("\x00 ")
                except: vin = raw.hex()

            # CP status: IKA key zeroed = no key loaded = CP active
            ika = did_results.get(0x00BE)
            if ika and ika.raw_hex and not ika.error:
                raw_ika = bytes.fromhex(ika.raw_hex)
                if all(b == 0 for b in raw_ika):
                    cp_status = "CP_ACTIVE (IKA key is zeroed — no key installed)"
                else:
                    cp_status = "KEY_PRESENT (IKA key installed — CP may be cleared)"
            else:
                cp_status = f"IKA_READ_FAILED ({ika.error if ika else 'no response'})"

        self._raw_log.append(
            f"J255: serial={serial}  VIN={vin}  CP={cp_status}")
        return serial, vin, cp_status, did_results

    # ── Full probe ────────────────────────────────────────────────────────────

    def full_probe(self, do_scan: bool = False, progress_cb=None) -> ProbeReport:
        """
        Run all probes and return a complete ProbeReport.
        do_scan defaults to False — the confirmed DID list makes a brute
        force scan unnecessary for normal use. Set do_scan=True to sweep
        the scan_range for any additional responsive DIDs.
        """
        import datetime
        self._raw_log.clear()

        # J533
        std_dids, cp_dids = self.probe_j533()

        # J255
        j255_serial, j255_vin, j255_cp, j255_dids = self.probe_j255()

        # Optional DID scan
        scan_results = []
        if do_scan:
            scan_results = self.scan_did_range(progress_cb)

        def _get(d, did): return d[did].decoded or "" if did in d else ""

        report = ProbeReport(
            timestamp     = datetime.datetime.now().isoformat(),
            vin           = _get(std_dids, 0xF190),
            j533_serial   = _get(std_dids, 0xF18C),
            j533_part     = _get(std_dids, 0xF187),
            j533_sw       = _get(std_dids, 0xF189),
            j255_serial   = j255_serial,
            j255_vin      = j255_vin,
            j255_cp_status= j255_cp,
            std_dids      = list(std_dids.values()),
            cp_dids       = list(cp_dids.values()),
            j255_dids     = list(j255_dids.values()),
            scan_dids     = scan_results,
            raw_log       = list(self._raw_log),
            analysis      = self._analyse(std_dids, cp_dids,
                                           j255_serial, j255_vin,
                                           j255_cp, j255_dids),
        )
        return report

    def _analyse(self, std: dict, cp: dict,
                 j255_serial: str, j255_vin: str,
                 j255_cp: str, j255_dids: dict) -> str:
        """
        Generate a human-readable analysis of the constellation state.

        This decodes the full constellation using the confirmed DID structure:
          - 0x04A3 coded bitmap     — which slots are enrolled
          - 0x2A2A allocation table — which slot is J255 (ECU Name 8)
          - 0x2A26 present bitmap   — which modules are online right now
          - 0x2A2C TP-Identifier    — CAN IDs per slot
          - 0x00BE IKA key state    — whether a key has been written
        """
        lines = ["=" * 60,
                 "  J533 CONSTELLATION / CP ANALYSIS",
                 "=" * 60, ""]

        j533_serial = std.get(0xF18C, DIDResult(0,""," ","","")).decoded or "?"
        car_vin     = std.get(0xF190, DIDResult(0,""," ","","")).decoded or "?"
        j533_part   = std.get(0xF187, DIDResult(0,""," ","","")).decoded or "?"
        j533_sw     = std.get(0xF189, DIDResult(0,""," ","","")).decoded or "?"

        lines += [
            f"  J533 serial:    {j533_serial}",
            f"  J533 part:      {j533_part}",
            f"  J533 SW:        {j533_sw}",
            f"  Vehicle VIN:    {car_vin}",
            f"  J255 serial:    {j255_serial}",
            f"  J255 bound VIN: {j255_vin}",
            f"  J255 CP status: {j255_cp}",
            "",
        ]

        # ── VIN mismatch check ────────────────────────────────────────────────
        if j255_vin and car_vin:
            j255_vin_clean = j255_vin.strip('"')
            car_vin_clean  = car_vin.strip('"')
            if j255_vin_clean and car_vin_clean:
                if j255_vin_clean != car_vin_clean:
                    lines += [
                        f"  ⚠  VIN MISMATCH",
                        f"     J255 reports VIN: {j255_vin_clean}",
                        f"     This vehicle VIN: {car_vin_clean}",
                        f"     J255 was coded to a different car — CP is active.",
                    ]
                else:
                    lines.append(f"  ✓  VIN match — J255 is bound to this car.")
            else:
                lines.append("  ?  VIN data incomplete.")
        else:
            lines.append("  ?  Could not read VIN from one or both modules.")

        lines.append("")

        # ── Constellation decode ──────────────────────────────────────────────
        coded_r  = cp.get(0x04A3)
        alloc_r  = cp.get(0x2A2A)
        present_r= cp.get(0x2A26)
        tp_id_r  = cp.get(0x2A2C)

        if coded_r and coded_r.raw_hex:
            coded_raw   = bytes.fromhex(coded_r.raw_hex)
            alloc_raw   = bytes.fromhex(alloc_r.raw_hex)   if alloc_r  and alloc_r.raw_hex   else None
            present_raw = bytes.fromhex(present_r.raw_hex) if present_r and present_r.raw_hex else None
            tp_id_raw   = bytes.fromhex(tp_id_r.raw_hex)   if tp_id_r  and tp_id_r.raw_hex   else None

            constellation = self.decode_constellation(
                coded_raw, alloc_raw, present_raw, tp_id_raw)

            lines.append("  CONSTELLATION TABLE:")
            lines.append(f"  {'Slot':>4}  {'ECU Name':<32}  {'Coded':>6}  "
                          f"{'Present':>7}  {'CAN ID':>8}")
            lines.append("  " + "-" * 62)

            j255_slot = None
            for entry in constellation:
                slot  = entry["slot"]
                name  = entry["ecu_name_label"][:31]
                coded = "YES" if entry["coded"] else "-"
                pres  = "online" if entry["present"] else "-"
                can   = f"{entry['can_id']:#06x}" if entry["can_id"] else "-"
                flag  = ""
                if entry.get("ecu_name") == 8:    # J255
                    flag = "  ◄ J255"
                    j255_slot = slot
                lines.append(f"  {slot:>4}  {name:<32}  {coded:>6}  {pres:>7}  {can:>8}{flag}")

            lines.append("")

            if j255_slot is not None:
                j255_entry = constellation[j255_slot] if j255_slot < len(constellation) else None
                if j255_entry:
                    coded_str = "CODED" if j255_entry["coded"] else "NOT CODED"
                    pres_str  = "ONLINE" if j255_entry["present"] else "OFFLINE"
                    lines += [
                        f"  J255 slot {j255_slot}: {coded_str}, {pres_str}",
                    ]
                    if not j255_entry["coded"]:
                        lines.append("  ⚠  J255 is not in the constellation — "
                                     "this confirms serial mismatch.")
            else:
                lines.append("  ⚠  J255 (Air Conditioning, ECU Name 8) not found "
                              "in constellation table.")
        else:
            err = coded_r.error if coded_r else "not read"
            lines.append(f"  ✗  Could not read constellation DID 0x04A3: {err}")

        lines.append("")

        # ── IKA key state on J255 ─────────────────────────────────────────────
        ika = j255_dids.get(0x00BE) if j255_dids else None
        gka = j255_dids.get(0x00BD) if j255_dids else None
        lines.append("  CP KEY STATE (J255):")
        if ika:
            lines.append(f"    IKA Key (0x00BE): {ika.decoded or ika.error or 'no data'}")
        if gka:
            lines.append(f"    GKA Key (0x00BD): {gka.decoded or gka.error or 'no data'}")

        # ── IKA key state on J533 ─────────────────────────────────────────────
        ika_gw = cp.get(0x00BE)
        if ika_gw:
            lines.append(f"    IKA Key (0x00BE) on J533: "
                          f"{ika_gw.decoded or ika_gw.error or 'no data'}")

        lines.append("")

        # ── Showroom mode ─────────────────────────────────────────────────────
        showroom = cp.get(0x043E)
        if showroom and not showroom.error:
            lines.append(f"  Showroom mode: {showroom.decoded}")

        # ── Key download counter ──────────────────────────────────────────────
        key_dl = cp.get(0x043D)
        if key_dl and not key_dl.error:
            lines.append(f"  Successful key downloads: {key_dl.decoded}")

        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)

    # ── Persistence ───────────────────────────────────────────────────────────

    def start_cp_routine(self, payload: bytes = b"") -> Optional[bytes]:
        """
        Send RoutineControl Start (0x31 0x01) with the CP routine ID (0x0226).

        This initiates the Component Protection authentication sequence.
        The J533 expects a GEKO server-signed token in the payload —
        that token is not yet captured. This method sends the bare start
        command to confirm the routine ID is accepted.

        Returns the raw response bytes, or None on failure.

        To run:
            probe.connect()
            resp = probe.start_cp_routine()
            # If resp[0] == 0x71: routine accepted
            # If resp[0] == 0x7F resp[2] == 0x31: sub-function not supported (wrong ID)
            # If resp[0] == 0x7F resp[2] == 0x22: conditions not correct (token required)
        """
        routine_id = _load_cp_routine_id()
        rid_hi = (routine_id >> 8) & 0xFF
        rid_lo = routine_id & 0xFF

        log.info("RoutineControl Start  routine_id=0x%04X  payload=%d bytes",
                 routine_id, len(payload))
        try:
            raw = bytes([0x31, 0x01, rid_hi, rid_lo]) + payload
            self._client.send_request(raw)
            resp = self._client.wait_frame(timeout=5.0)
            log.info("CP routine response: %s", resp.hex() if resp else "None")
            return resp
        except Exception as e:
            log.warning("CP routine error: %s", e)
            return None

    def request_cp_routine_result(self) -> Optional[bytes]:
        """Send RoutineControl RequestResult (0x31 0x03) for CP routine."""
        routine_id = _load_cp_routine_id()
        rid_hi = (routine_id >> 8) & 0xFF
        rid_lo = routine_id & 0xFF
        try:
            raw = bytes([0x31, 0x03, rid_hi, rid_lo])
            self._client.send_request(raw)
            resp = self._client.wait_frame(timeout=5.0)
            return resp
        except Exception as e:
            log.warning("CP routine result error: %s", e)
            return None

        def save_report(self, path: str, report: ProbeReport):
        with open(path, "w") as f:
            json.dump(asdict(report), f, indent=2)
        log.info("Report saved to %s", path)

    @staticmethod
    def load_report(path: str) -> ProbeReport:
        with open(path) as f:
            data = json.load(f)
        data["std_dids"]  = [DIDResult(**d) for d in data.get("std_dids",  [])]
        data["cp_dids"]   = [DIDResult(**d) for d in data.get("cp_dids",   [])]
        data["j255_dids"] = [DIDResult(**d) for d in data.get("j255_dids", [])]
        data["scan_dids"] = [DIDResult(**d) for d in data.get("scan_dids", [])]
        # backward compat: old reports lack j255_cp_status
        data.setdefault("j255_cp_status", "")
        return ProbeReport(**data)
