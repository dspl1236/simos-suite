"""
cp_tools/odx_parser.py — Extract CP and UDS protocol data from flashdaten ODX files

When you provide the flashdaten ODX files, this parser extracts:
  - CP routine identifier (the 0x31 RoutineControl ID for CP removal)
  - Security access level required for CP operations
  - SA2 seed/key bytecode for J533
  - Complete DID map (every DID J533 supports, with names and byte structure)
  - CP authorization token structure

ODX (Open Diagnostic eXchange) is XML — parse with standard ElementTree.
The files are found in ODIS-S PostSetup:
  C:\\ProgramData\\ODIS-S\\diagdata\\EV_GatewPkoUDS\\*.odx

Usage:
    from cp_tools.odx_parser import ODXParser
    p = ODXParser("EV_GatewPkoUDS_001_AU57.odx")
    p.parse()
    print(p.cp_routine_id)          # e.g. 0x0203
    print(p.security_level)         # e.g. 0x11
    print(p.sa2_script.hex())
    print(p.did_map)                # {did_int: {name, description, length, ...}}
    p.save_extracted("j533_protocol.json")
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

log = logging.getLogger("SimosSuite.ODXParser")

# ODX XML namespaces — VAG ODX files use the ASAM ODX 2.0 namespace
ODX_NS = {
    "odx":   "http://www.asam.net/odx/2006",
    "xsi":   "http://www.w3.org/2001/XMLSchema-instance",
}

# Also try unnamespaced — some VAG ODX files omit the namespace declaration
_TAGS_BARE = True  # fallback to searching without namespace prefix


@dataclass
class DIDEntry:
    did:         int
    name:        str
    description: str
    byte_length: int
    data_type:   str
    read_access:  bool
    write_access: bool
    cp_related:  bool = False
    raw_xml:     str  = ""


@dataclass
class RoutineEntry:
    routine_id:    int
    name:          str
    description:   str
    service:       str    # "0x31 RoutineControl"
    subfunction:   int    # 0x01 start, 0x02 stop, 0x03 requestResults
    params_in:     List[str] = field(default_factory=list)
    params_out:    List[str] = field(default_factory=list)
    cp_related:    bool = False


@dataclass
class SecurityLevel:
    level:       int      # odd=request seed, even=send key
    name:        str
    sa2_script:  Optional[bytes] = None  # bytecode if present in ODX


@dataclass
class ODXExtract:
    source_file:     str
    ecu_name:        str
    ecu_variant:     str
    # CP-specific
    cp_routine_id:   Optional[int]
    cp_security_level: Optional[int]
    sa2_script:      Optional[str]      # hex string
    # Maps
    did_map:         List[DIDEntry]     = field(default_factory=list)
    routine_map:     List[RoutineEntry] = field(default_factory=list)
    security_levels: List[SecurityLevel] = field(default_factory=list)
    # Summary
    notes:           str = ""


class ODXParser:
    """
    Parse a VAG ASAM ODX file and extract protocol-relevant information.
    """

    def __init__(self, odx_path: str):
        self.path    = Path(odx_path)
        self._tree:  Optional[ET.ElementTree] = None
        self._root:  Optional[ET.Element] = None
        self._ns:    Dict[str, str] = {}
        self._result: Optional[ODXExtract] = None

    # ── Parsing entry point ───────────────────────────────────────────────────

    def parse(self) -> ODXExtract:
        log.info("Parsing ODX: %s", self.path)
        self._tree = ET.parse(str(self.path))
        self._root = self._tree.getroot()
        self._detect_namespace()

        ecu_name    = self._get_ecu_name()
        ecu_variant = self._get_ecu_variant()

        did_map      = self._extract_dids()
        routines     = self._extract_routines()
        sec_levels   = self._extract_security_levels()

        cp_routine, cp_level = self._identify_cp_routine(routines, sec_levels)
        sa2_script   = self._extract_sa2_script(sec_levels)

        notes = self._build_notes(cp_routine, cp_level, sa2_script, did_map)

        self._result = ODXExtract(
            source_file     = str(self.path.name),
            ecu_name        = ecu_name,
            ecu_variant     = ecu_variant,
            cp_routine_id   = cp_routine,
            cp_security_level = cp_level,
            sa2_script      = sa2_script.hex() if sa2_script else None,
            did_map         = did_map,
            routine_map     = routines,
            security_levels = sec_levels,
            notes           = notes,
        )
        log.info("Parsed: %d DIDs, %d routines, %d security levels",
                 len(did_map), len(routines), len(sec_levels))
        return self._result

    # ── Namespace detection ───────────────────────────────────────────────────

    def _detect_namespace(self):
        """Detect whether the ODX file uses the ASAM namespace or bare tags."""
        tag = self._root.tag
        if "{" in tag:
            ns_uri = tag.split("}")[0].lstrip("{")
            self._ns = {"odx": ns_uri}
            log.debug("Namespace detected: %s", ns_uri)
        else:
            self._ns = {}
            log.debug("No namespace in ODX file — using bare tag search")

    def _tag(self, name: str) -> str:
        if self._ns:
            return f"{{{self._ns.get('odx', '')}}}"+name
        return name

    def _findall(self, element: ET.Element, xpath: str) -> List[ET.Element]:
        """Find elements, trying both namespaced and bare XPath."""
        try:
            if self._ns:
                return element.findall(xpath, self._ns)
            else:
                # Convert namespaced xpath to bare
                bare = re.sub(r"odx:", "", xpath)
                return element.findall(bare)
        except Exception:
            return []

    def _find(self, element: ET.Element, xpath: str) -> Optional[ET.Element]:
        results = self._findall(element, xpath)
        return results[0] if results else None

    def _text(self, element: ET.Element, xpath: str, default: str = "") -> str:
        el = self._find(element, xpath)
        return el.text.strip() if el is not None and el.text else default

    # ── ECU identification ────────────────────────────────────────────────────

    def _get_ecu_name(self) -> str:
        for xpath in [
            ".//odx:ECU-VARIANT/odx:SHORT-NAME",
            ".//odx:BASE-VARIANT/odx:SHORT-NAME",
            ".//ECU-VARIANT/SHORT-NAME",
            ".//BASE-VARIANT/SHORT-NAME",
        ]:
            name = self._text(self._root, xpath)
            if name:
                return name
        return "UNKNOWN"

    def _get_ecu_variant(self) -> str:
        for xpath in [
            ".//odx:ECU-VARIANT/odx:LONG-NAME",
            ".//odx:BASE-VARIANT/odx:LONG-NAME",
            ".//ECU-VARIANT/LONG-NAME",
        ]:
            name = self._text(self._root, xpath)
            if name:
                return name
        return ""

    # ── DID extraction ────────────────────────────────────────────────────────

    def _extract_dids(self) -> List[DIDEntry]:
        entries = []
        # DIDs in ODX are typically DATA-IDENTIFIERS or DID-REFs inside SERVICE definitions
        # VAG ODX typically stores them in a DIAG-SERVICE for ReadDataByIdentifier (0x22)

        # Find all ReadDataByIdentifier service definitions
        for svc in self._root.iter(self._tag("DIAG-SERVICE")):
            sdg = self._text(svc, "odx:SERVICE-ID", "")
            if not sdg:
                sdg = self._text(svc, "SERVICE-ID", "")
            if "22" not in sdg.upper() and "READDATABYIDENT" not in sdg.upper():
                continue

            for req in svc.iter(self._tag("REQUEST")):
                did_val = self._extract_hex_param(req, "IDENTIFIER") or \
                           self._extract_hex_param(req, "DATA-ID")
                if did_val is None:
                    continue

                name = self._text(svc, "odx:SHORT-NAME") or \
                       self._text(svc, "SHORT-NAME") or f"DID_{did_val:04X}"
                desc = self._text(svc, "odx:LONG-NAME") or \
                       self._text(svc, "LONG-NAME") or ""

                # Determine read/write from service context
                is_write = "2E" in sdg.upper() or "WRITE" in name.upper()
                is_read  = "22" in sdg.upper() or "READ" in name.upper() or not is_write

                # Byte length — look for positive response data length
                byte_len = self._get_response_length(svc)

                cp_related = any(kw in (name + desc).upper()
                                  for kw in ["CP", "PROTECT", "KOMPONENT", "CONSTELLATION",
                                              "SERIAL", "VIN", "BINDING"])

                entries.append(DIDEntry(
                    did=did_val,
                    name=name,
                    description=desc,
                    byte_length=byte_len,
                    data_type="bytes",
                    read_access=is_read,
                    write_access=is_write,
                    cp_related=cp_related,
                ))

        # Also scan for DATA-IDENTIFIER elements directly
        for did_el in self._root.iter(self._tag("DATA-IDENTIFIER")):
            id_val = self._extract_hex_param(did_el, "ID")
            if id_val is None:
                continue
            name = self._text(did_el, "odx:SHORT-NAME") or \
                   self._text(did_el, "SHORT-NAME") or f"DID_{id_val:04X}"
            if not any(e.did == id_val for e in entries):
                entries.append(DIDEntry(
                    did=id_val, name=name, description="",
                    byte_length=0, data_type="bytes",
                    read_access=True, write_access=False,
                ))

        # Deduplicate by DID, merge info
        seen = {}
        for e in entries:
            if e.did not in seen:
                seen[e.did] = e
            else:
                # Merge: write access from either, longer description
                seen[e.did].write_access |= e.write_access
                if len(e.description) > len(seen[e.did].description):
                    seen[e.did].description = e.description

        return sorted(seen.values(), key=lambda x: x.did)

    def _extract_hex_param(self, el: ET.Element, tag: str) -> Optional[int]:
        """Find a child element by tag and parse its hex value."""
        child = self._find(el, f"odx:{tag}") or self._find(el, tag)
        if child is not None and child.text:
            try:
                txt = child.text.strip()
                if txt.startswith("0x") or txt.startswith("0X"):
                    return int(txt, 16)
                # Try pure hex without prefix
                return int(txt, 16)
            except ValueError:
                pass
        # Also check attributes
        val = el.get(tag) or el.get(tag.lower())
        if val:
            try: return int(val, 16)
            except ValueError: pass
        return None

    def _get_response_length(self, svc: ET.Element) -> int:
        """Try to determine response data length from POSITIVE-RESPONSE."""
        for resp in svc.iter(self._tag("POSITIVE-RESPONSE")):
            for param in resp.iter(self._tag("PARAM")):
                length = self._find(param, "odx:BIT-LENGTH") or \
                          self._find(param, "BIT-LENGTH")
                if length is not None and length.text:
                    try: return int(length.text.strip()) // 8
                    except: pass
        return 0

    # ── Routine extraction ────────────────────────────────────────────────────

    def _extract_routines(self) -> List[RoutineEntry]:
        entries = []
        for svc in self._root.iter(self._tag("DIAG-SERVICE")):
            sdg = self._text(svc, "odx:SERVICE-ID") or \
                   self._text(svc, "SERVICE-ID") or ""
            if "31" not in sdg.upper() and "ROUTINE" not in sdg.upper():
                continue

            routine_id = None
            subfunc    = 0x01

            for req in svc.iter(self._tag("REQUEST")):
                routine_id = routine_id or self._extract_hex_param(req, "ROUTINE-IDENTIFIER")
                routine_id = routine_id or self._extract_hex_param(req, "ROUTINE-ID")
                sf = self._extract_hex_param(req, "SUB-FUNCTION")
                if sf: subfunc = sf

            name = self._text(svc, "odx:SHORT-NAME") or \
                   self._text(svc, "SHORT-NAME") or "?"
            desc = self._text(svc, "odx:LONG-NAME") or \
                   self._text(svc, "LONG-NAME") or ""

            cp_related = any(kw in (name + desc).upper()
                              for kw in ["CP", "PROTECT", "KOMPONENT",
                                          "AUTHORIZ", "TOKEN", "GEKO", "GRP"])

            if routine_id is not None:
                entries.append(RoutineEntry(
                    routine_id=routine_id, name=name, description=desc,
                    service="0x31 RoutineControl", subfunction=subfunc,
                    cp_related=cp_related,
                ))

        return entries

    # ── Security level extraction ─────────────────────────────────────────────

    def _extract_security_levels(self) -> List[SecurityLevel]:
        levels = []
        for svc in self._root.iter(self._tag("DIAG-SERVICE")):
            sdg = self._text(svc, "odx:SERVICE-ID") or \
                   self._text(svc, "SERVICE-ID") or ""
            if "27" not in sdg.upper() and "SECURITY" not in sdg.upper():
                continue

            name  = self._text(svc, "odx:SHORT-NAME") or \
                    self._text(svc, "SHORT-NAME") or "?"
            level = None

            for req in svc.iter(self._tag("REQUEST")):
                level = self._extract_hex_param(req, "SECURITY-ACCESS-TYPE") or \
                         self._extract_hex_param(req, "ACCESS-LEVEL") or \
                         self._extract_hex_param(req, "SUB-FUNCTION")

            # Look for SA2 script in SECURITY-ACCESS or associated DIAG-CODED-TYPE
            sa2 = self._find_sa2_script(svc)

            if level is not None:
                levels.append(SecurityLevel(level=level, name=name, sa2_script=sa2))

        return levels

    def _find_sa2_script(self, element: ET.Element) -> Optional[bytes]:
        """Look for SA2 bytecode in any SA2-SCRIPT or similar element."""
        for tag in ["SA2-SCRIPT", "SEED-KEY-SCRIPT", "SECURITY-SCRIPT", "BYTECODE"]:
            el = self._find(element, f"odx:{tag}") or self._find(element, tag)
            if el is not None and el.text:
                try:
                    return bytes.fromhex(el.text.strip().replace(" ", ""))
                except Exception:
                    pass
        return None

    # ── CP identification ─────────────────────────────────────────────────────

    def _identify_cp_routine(self,
                              routines: List[RoutineEntry],
                              sec_levels: List[SecurityLevel]
                              ) -> Tuple[Optional[int], Optional[int]]:
        """Best-guess identification of the CP removal routine and its security level."""

        # Primary: any routine explicitly tagged cp_related
        cp_routines = [r for r in routines if r.cp_related]
        if cp_routines:
            routine_id = cp_routines[0].routine_id
            log.info("CP routine identified: %#06x  (%s)", routine_id, cp_routines[0].name)
        else:
            # Fallback: look for routine IDs in common VAG CP range 0x0200–0x0210
            fallback = [r for r in routines if 0x0200 <= r.routine_id <= 0x0220]
            routine_id = fallback[0].routine_id if fallback else None
            if routine_id:
                log.info("CP routine (heuristic): %#06x", routine_id)

        # Security level: look for level used alongside CP routine, or default 0x11
        cp_level = None
        if sec_levels:
            # Programming-level security access is typically odd level 0x11 or 0x03
            for sl in sec_levels:
                if sl.level in (0x11, 0x03, 0x01):
                    cp_level = sl.level
                    break
            if cp_level is None:
                cp_level = sec_levels[0].level

        return routine_id, cp_level

    def _extract_sa2_script(self, sec_levels: List[SecurityLevel]) -> Optional[bytes]:
        for sl in sec_levels:
            if sl.sa2_script:
                return sl.sa2_script
        return None

    # ── Summary ───────────────────────────────────────────────────────────────

    def _build_notes(self, cp_routine, cp_level, sa2_script, did_map) -> str:
        lines = []
        if cp_routine:
            lines.append(f"✓ CP routine found: RoutineControl 0x31 0x01 {cp_routine:#06x}")
        else:
            lines.append("? CP routine not found — check routine_map manually")

        if cp_level:
            lines.append(f"✓ Security level for CP: 0x{cp_level:02X}")
        else:
            lines.append("? Security level not identified")

        if sa2_script:
            lines.append(f"✓ SA2 script found: {sa2_script.hex()}")
        else:
            lines.append("? SA2 script not found in ODX — may be in a linked file")

        cp_dids = [d for d in did_map if d.cp_related]
        lines.append(f"\n{len(did_map)} total DIDs, {len(cp_dids)} CP-related:")
        for d in cp_dids[:10]:
            lines.append(f"  {d.did:#06x}  {d.name}  ({d.byte_length}B)")

        return "\n".join(lines)

    # ── Output ────────────────────────────────────────────────────────────────

    def save_extracted(self, output_path: str):
        if not self._result:
            raise RuntimeError("Call parse() first")
        data = asdict(self._result)
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        log.info("Extracted protocol data saved to %s", output_path)
        return output_path

    def print_summary(self):
        if not self._result:
            raise RuntimeError("Call parse() first")
        r = self._result
        print(f"\n{'='*60}")
        print(f"ODX: {r.source_file}")
        print(f"ECU: {r.ecu_name}  ({r.ecu_variant})")
        print(f"CP routine ID:     {r.cp_routine_id:#06x}" if r.cp_routine_id else "CP routine: NOT FOUND")
        print(f"CP security level: {r.cp_security_level:#04x}" if r.cp_security_level else "Security level: NOT FOUND")
        print(f"SA2 script:        {r.sa2_script}" if r.sa2_script else "SA2 script: NOT FOUND")
        print(f"\nDIDs: {len(r.did_map)} total")
        print(f"Routines: {len(r.routine_map)} total")
        print(f"Security levels: {len(r.security_levels)} total")
        print(f"\n{r.notes}")
        print("="*60)


# ── CLI usage ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python odx_parser.py <path_to.odx> [output.json]")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO)
    parser = ODXParser(sys.argv[1])
    result = parser.parse()
    parser.print_summary()

    if len(sys.argv) >= 3:
        parser.save_extracted(sys.argv[2])
        print(f"\nSaved to {sys.argv[2]}")
