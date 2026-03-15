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
}

# Suspected constellation / CP related DIDs on J533.
# Source: community DID scans on B8/C7 gateway variants, VCDS adaptation channel analysis.
# UNCONFIRMED — these need to be verified against your specific J533 firmware version.
# The ODX file (EV_GatewPkoUDS_001_AU57.odx) will have the authoritative list.
SUSPECTED_CP_DIDS = {
    0x0101: "CP Status Word (suspected)",
    0x0102: "CP Module Count (suspected)",
    0x0110: "CP Constellation Entry 0 (suspected)",
    0x0111: "CP Constellation Entry 1 (suspected)",
    0x0112: "CP Constellation Entry 2 (suspected)",
    0x0113: "CP Constellation Entry 3 (suspected)",
    0x0120: "CP Bound VIN (suspected)",
    0x0121: "CP Gateway Serial (suspected)",
    0x0130: "CP Last Operation Timestamp (suspected)",
    0x0131: "CP Last Operation Result (suspected)",
    0x0200: "Subsystem Status (suspected)",
    0x0201: "Bus Topology (suspected)",
    0x0202: "Active Module List (suspected)",
}

# Known J533 adaptation channels (from VCDS/community)
KNOWN_ADAPTATIONS = {
    0x01: "Component Protection Status",
    0x02: "CP Module Enable Bits",
    0x10: "Convenience CAN topology",
    0x11: "Powertrain CAN topology",
    0x20: "Energy management config",
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
    timestamp:    str
    vin:          str
    j533_serial:  str
    j533_part:    str
    j533_sw:      str
    j255_serial:  str   # the HVAC module we care about
    j255_vin:     str   # what VIN J255 thinks it belongs to
    std_dids:     List[DIDResult] = field(default_factory=list)
    cp_dids:      List[DIDResult] = field(default_factory=list)
    scan_dids:    List[DIDResult] = field(default_factory=list)
    raw_log:      List[str]       = field(default_factory=list)
    analysis:     str = ""


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

        cfg = dict(configs.default_client_config)
        cfg["data_identifiers"] = {did: _BytesCodec for did in
                                    list(STD_DIDS) + list(SUSPECTED_CP_DIDS)}
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
        """Best-effort decode of a DID value."""
        if not raw:
            return ""
        # Try ASCII
        try:
            s = raw.decode("ascii").strip("\x00").strip()
            if s.isprintable() and len(s) >= 4:
                return f'"{s}"'
        except Exception:
            pass
        # Known structure decodes
        if did == 0xF190 and len(raw) == 17:  # VIN
            try: return raw.decode("ascii")
            except: pass
        if did == 0xF18C:  # Serial number — often ASCII or BCD
            try: return raw.decode("ascii").strip()
            except: pass
            if len(raw) >= 7:
                return "SN:" + raw.hex().upper()
        # For suspected constellation DIDs, try structured decode
        if 0x0110 <= did <= 0x011F and len(raw) >= 8:
            # Likely: [module_addr 2B][serial 6B] or similar — speculative
            return f"addr={raw[:2].hex()} serial={raw[2:8].hex()} rest={raw[8:].hex()}"
        # Fallback
        return raw.hex(" ").upper()

    # ── Probe J533 ────────────────────────────────────────────────────────────

    def probe_j533(self) -> Tuple[Dict[int, DIDResult], Dict[int, DIDResult]]:
        """Read standard and CP-suspected DIDs from J533. Returns (std, cp) dicts."""
        self._raw_log.append("\n=== J533 PROBE ===")
        std_results = {}
        cp_results  = {}

        with self._make_client(J533_TX, J533_RX) as client:
            # Extended session
            self._raw_log.append("→ DiagSessionControl extendedDiagnosticSession")
            try:
                client.change_session(
                    services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
                client.session_timing["p2_server_max"] = 30
                client.config["request_timeout"] = 30
            except Exception as e:
                log.error("J533: failed to open extended session: %s", e)
                return std_results, cp_results

            # Standard DIDs
            self._raw_log.append("--- Standard DIDs ---")
            for did, label in STD_DIDS.items():
                raw, err = self._read_did(client, did)
                std_results[did] = DIDResult(
                    did=did, label=label,
                    raw_hex=raw.hex() if raw else "",
                    decoded=self._decode_did(did, raw) if raw else None,
                    error=err)

            # Suspected CP DIDs
            self._raw_log.append("--- Suspected CP DIDs ---")
            for did, label in SUSPECTED_CP_DIDS.items():
                raw, err = self._read_did(client, did)
                cp_results[did] = DIDResult(
                    did=did, label=label,
                    raw_hex=raw.hex() if raw else "",
                    decoded=self._decode_did(did, raw) if raw else None,
                    error=err)

        return std_results, cp_results

    # ── DID range scan ────────────────────────────────────────────────────────

    def scan_did_range(self, progress_cb=None) -> List[DIDResult]:
        """Brute-force scan DID range on J533. Returns all responsive DIDs."""
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
                    label = SUSPECTED_CP_DIDS.get(did, f"DID_{did:04X}")
                    found.append(DIDResult(
                        did=did, label=label,
                        raw_hex=raw.hex(),
                        decoded=self._decode_did(did, raw),
                        error=None))
                    log.info("  FOUND DID %#06x: %s", did, raw.hex())

        return found

    # ── Probe J255 ────────────────────────────────────────────────────────────

    def probe_j255(self) -> Tuple[str, str, str]:
        """
        Read J255 (Climatronic) serial and VIN binding.
        Returns (serial, bound_vin, cp_status).
        """
        self._raw_log.append("\n=== J255 PROBE ===")
        serial, vin, cp_status = "", "", "UNKNOWN"

        with self._make_client(J255_TX, J255_RX) as client:
            try:
                client.change_session(
                    services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
                client.session_timing["p2_server_max"] = 30
            except Exception as e:
                log.error("J255: session failed: %s", e)
                return serial, vin, cp_status

            raw_serial, _ = self._read_did(client, 0xF18C)
            if raw_serial:
                try: serial = raw_serial.decode("ascii").strip("\x00")
                except: serial = raw_serial.hex().upper()

            raw_vin, _ = self._read_did(client, 0xF190)
            if raw_vin:
                try: vin = raw_vin.decode("ascii").strip("\x00")
                except: vin = raw_vin.hex()

            # Try reading CP status DID — address varies, scan 0x0100–0x0110
            for cp_did in [0x0101, 0x0102, 0x0110, 0xF1DF]:
                raw_cp, err = self._read_did(client, cp_did)
                if raw_cp:
                    cp_status = f"DID_{cp_did:04X}={raw_cp.hex()}"
                    break

        self._raw_log.append(
            f"J255: serial={serial}  VIN={vin}  CP={cp_status}")
        return serial, vin, cp_status

    # ── Full probe ────────────────────────────────────────────────────────────

    def full_probe(self, do_scan: bool = True, progress_cb=None) -> ProbeReport:
        """Run all probes and return a complete ProbeReport."""
        import datetime
        self._raw_log.clear()

        # J533
        std_dids, cp_dids = self.probe_j533()

        # J255
        j255_serial, j255_vin, j255_cp = self.probe_j255()

        # Optional DID scan
        scan_results = []
        if do_scan:
            scan_results = self.scan_did_range(progress_cb)

        # Extract key fields
        def _get(d, did): return d[did].decoded or "" if did in d else ""

        report = ProbeReport(
            timestamp    = datetime.datetime.now().isoformat(),
            vin          = _get(std_dids, 0xF190),
            j533_serial  = _get(std_dids, 0xF18C),
            j533_part    = _get(std_dids, 0xF187),
            j533_sw      = _get(std_dids, 0xF189),
            j255_serial  = j255_serial,
            j255_vin     = j255_vin,
            std_dids     = list(std_dids.values()),
            cp_dids      = list(cp_dids.values()),
            scan_dids    = scan_results,
            raw_log      = list(self._raw_log),
            analysis     = self._analyse(std_dids, cp_dids, j255_serial, j255_vin),
        )
        return report

    def _analyse(self, std: dict, cp: dict,
                 j255_serial: str, j255_vin: str) -> str:
        """Generate a human-readable analysis of what we found."""
        lines = ["=== J533 CONSTELLATION ANALYSIS ===\n"]

        j533_serial = std[0xF18C].decoded if 0xF18C in std else "?"
        car_vin     = std[0xF190].decoded if 0xF190 in std else "?"
        lines.append(f"J533 serial:   {j533_serial}")
        lines.append(f"Vehicle VIN:   {car_vin}")
        lines.append(f"J255 serial:   {j255_serial}")
        lines.append(f"J255 bound VIN:{j255_vin}")

        lines.append("")
        if j255_vin and car_vin and j255_vin != car_vin:
            lines.append(f"⚠ VIN MISMATCH — J255 is bound to {j255_vin}, "
                          f"but this car is {car_vin}")
            lines.append("  This confirms Component Protection is active on J255.")
            lines.append("  Root cause: J533 constellation table holds the donor J255's serial.")
            lines.append("  Fix: ODIS 25.x + GRP session → CP removal routine on J255.")
        elif j255_vin and car_vin and j255_vin == car_vin:
            lines.append("✓ J255 VIN matches — CP may not be the issue, or already cleared.")
        else:
            lines.append("? Could not determine VIN binding — check DID scan results.")

        lines.append("")
        # Look for any responsive CP DIDs
        responsive = [r for r in cp.values() if not r.error]
        if responsive:
            lines.append(f"Responsive suspected CP DIDs ({len(responsive)}):")
            for r in responsive:
                lines.append(f"  {r.did:#06x}  {r.label}  →  {r.decoded or r.raw_hex}")
        else:
            lines.append("No suspected CP DIDs responded — likely requires ODX to "
                          "identify correct DID addresses for this J533 SW version.")
            lines.append("→ Provide EV_GatewPkoUDS_001_AU57.odx to unlock the DID map.")

        return "\n".join(lines)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_report(self, path: str, report: ProbeReport):
        with open(path, "w") as f:
            json.dump(asdict(report), f, indent=2)
        log.info("Report saved to %s", path)

    @staticmethod
    def load_report(path: str) -> ProbeReport:
        with open(path) as f:
            data = json.load(f)
        # Reconstruct nested dataclasses
        data["std_dids"] = [DIDResult(**d) for d in data["std_dids"]]
        data["cp_dids"]  = [DIDResult(**d) for d in data["cp_dids"]]
        data["scan_dids"]= [DIDResult(**d) for d in data["scan_dids"]]
        return ProbeReport(**data)
