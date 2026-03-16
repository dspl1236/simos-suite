# ══════════════════════════════════════════════════════════════════════════════
# TRANSMISSION CONTROL UNITS
# ══════════════════════════════════════════════════════════════════════════════
#
# All four transmissions fitted to C7 A6/A7/A8 and MQB Golf 7/Passat B8:
#
#   ZF8HP    — ZF 8-speed torque-converter auto (Bosch TCU)
#              Fitted to: C7 A6 3.0T TFSI / 3.0 TDI, A7, A8 D4
#              CAN: TX=0x7E1, RX=0x7E9  (confirmed from ESP32 fork slot 1)
#
#   DL501    — S-Tronic 7-speed dual-clutch (Borg Warner / Mechatronic)
#              Fitted to: C7 S6/S7 (4.0T V8), some A6/A7 quattro
#              CAN: TX=0x7E1, RX=0x7E9  (same physical harness as ZF8HP)
#
#   DQ250    — 6-speed wet dual-clutch (Temic)
#              Fitted to: MQB Golf 7 GTI/R, Passat B8
#              SA2 confirmed from VW_Flash (bri3d/VW_Flash lib/modules/dq250mqb.py)
#              CAN: TX=0x7E1, RX=0x7E9
#
#   DQ381    — 7-speed dry dual-clutch (Bosch)
#              Fitted to: MQB Golf 8, Tiguan II
#              SA2 confirmed from VW_Flash (bri3d/VW_Flash lib/modules/dq381.py)
#              CAN: TX=0x7E1, RX=0x7E9
#
# For now: read-only live data (basic values) via standard UDS DIDs.
# Flash support follows once ODX files are confirmed for each unit.
#
# Live DID map (TCU_LIVE_DIDS) — universal across all four units.
# All values readable in extended diagnostic session (0x10 0x03), no SA2.
# ══════════════════════════════════════════════════════════════════════════════

# ── Live DID definitions ──────────────────────────────────────────────────────
# format: did → (name, unit, scale, offset, fmt)
#   scale/offset: physical = (raw * scale) + offset
#   fmt: "int" | "float2" | "float3" | "hex" | "enum_gear"
#
# Sources:
#   - ZF8HP GA8HP45Z/70Z: community UDS scans (MHH Auto, VAG forums)
#   - DL501 0B5: community UDS scans + VCDS measurement blocks cross-ref
#   - DQ250 0D9: simos_hsl.py gear references + VW_Flash community
#   - DQ381 0GC: community scans, largely overlaps DQ250

TCU_LIVE_DIDS = {
    # ── Identity & state ─────────────────────────────────────────────────────
    0xF190: ("VIN",                    "",    1.0,  0.0, "str"),
    0xF18C: ("TCU Serial",             "",    1.0,  0.0, "str"),
    0xF187: ("Part Number",            "",    1.0,  0.0, "str"),
    0xF189: ("SW Version",             "",    1.0,  0.0, "str"),
    0xF186: ("Active Session",         "",    1.0,  0.0, "hex"),

    # ── Temperatures ─────────────────────────────────────────────────────────
    # 0x0115: transmission fluid temp — raw uint8, scale 1°C, offset -40
    0x0115: ("Trans Fluid Temp",       "°C",  1.0, -40.0, "float1"),
    # 0x0116: TCU internal temp — same encoding
    0x0116: ("TCU Temp",               "°C",  1.0, -40.0, "float1"),

    # ── Gear & selector ──────────────────────────────────────────────────────
    # 0x0180: current gear — uint8
    #   0=N, 1–8 (ZF8HP/DQ381), 1–6 (DQ250), 1–7 (DL501)
    #   0xFF=P, 0xFE=R, 0xFD=N, 0xFC=D (on some variants)
    0x0180: ("Current Gear",           "",    1.0,  0.0, "enum_gear"),
    # 0x0181: selector lever position — uint8
    #   0=P, 1=R, 2=N, 3=D/S, 4=Manual+, 5=Manual-
    0x0181: ("Selector Position",      "",    1.0,  0.0, "enum_selector"),
    # 0x0182: target gear — uint8
    0x0182: ("Target Gear",            "",    1.0,  0.0, "int"),

    # ── Torque & load ─────────────────────────────────────────────────────────
    # 0x0190: engine torque request to TCU — uint16 big-endian, 0.5 Nm/bit, -1000 offset
    0x0190: ("Engine Torque Request",  "Nm",  0.5, -1000.0, "float1"),
    # 0x0191: TCU torque limit — same encoding
    0x0191: ("TCU Torque Limit",       "Nm",  0.5, -1000.0, "float1"),

    # ── Input shaft & slip ────────────────────────────────────────────────────
    # 0x01A0: input shaft speed — uint16 BE, 1 RPM/bit
    0x01A0: ("Input Shaft Speed",      "RPM", 1.0,  0.0, "int"),
    # 0x01A1: output shaft speed — uint16 BE, 1 RPM/bit
    0x01A1: ("Output Shaft Speed",     "RPM", 1.0,  0.0, "int"),
    # 0x01A2: torque converter slip (ZF8HP / DL501) — uint16 BE, 1 RPM/bit
    0x01A2: ("TC Slip / Clutch Slip",  "RPM", 1.0,  0.0, "int"),

    # ── Clutch pressures (DQ250/DQ381 specific, ZF8HP returns 0) ─────────────
    # 0x01B0: odd clutch pack pressure — uint8, 0.1 bar/bit
    0x01B0: ("Clutch K1 Pressure",     "bar", 0.1,  0.0, "float1"),
    # 0x01B1: even clutch pack pressure
    0x01B1: ("Clutch K2 Pressure",     "bar", 0.1,  0.0, "float1"),

    # ── Solenoid & line pressure ──────────────────────────────────────────────
    # 0x01C0: line pressure — uint8, 0.1 bar/bit (ZF8HP)
    0x01C0: ("Line Pressure",          "bar", 0.1,  0.0, "float1"),

    # ── Mode flags ───────────────────────────────────────────────────────────
    # 0x01D0: TCU status flags — uint8 bitfield
    #   bit0=Sport, bit1=Winter, bit2=Manual, bit3=TipUp, bit4=TipDown
    #   bit5=TorqueReduction, bit6=SlipControl, bit7=Error
    0x01D0: ("TCU Status Flags",       "",    1.0,  0.0, "flags8"),

    # ── Fault / learning ─────────────────────────────────────────────────────
    # 0x0205: DTC count — uint8
    0x0205: ("Active DTC Count",       "",    1.0,  0.0, "int"),
    # 0x0212: adaptation status — uint8 (0=not adapted, 1=in progress, 2=complete)
    0x0212: ("Adaptation Status",      "",    1.0,  0.0, "int"),
}

# Human-readable enum decoders
GEAR_ENUM = {
    0x00: "N", 0xFF: "P", 0xFE: "R", 0xFD: "N", 0xFC: "D",
    **{i: str(i) for i in range(1, 9)},
}

SELECTOR_ENUM = {
    0: "P", 1: "R", 2: "N", 3: "D/S", 4: "M+", 5: "M-",
}

TCU_STATUS_FLAGS = [
    "Sport", "Winter", "Manual", "Tip+", "Tip-",
    "Torque Reduction", "Slip Ctrl", "Error",
]


def decode_tcu_did(did: int, raw_bytes: bytes):
    """
    Decode a raw TCU DID response to (value, unit, label).
    Returns (display_string, unit, label) for display in the UI.
    """
    if did not in TCU_LIVE_DIDS:
        return raw_bytes.hex(), "", f"DID 0x{did:04X}"

    label, unit, scale, offset, fmt = TCU_LIVE_DIDS[did]

    if fmt == "str":
        try:
            return raw_bytes.decode("ascii").strip("\x00").strip(), unit, label
        except Exception:
            return raw_bytes.hex(), unit, label

    if fmt == "hex":
        return raw_bytes.hex(), unit, label

    if fmt == "enum_gear":
        raw = raw_bytes[0] if raw_bytes else 0
        return GEAR_ENUM.get(raw, f"0x{raw:02X}"), unit, label

    if fmt == "enum_selector":
        raw = raw_bytes[0] if raw_bytes else 0
        return SELECTOR_ENUM.get(raw, f"0x{raw:02X}"), unit, label

    if fmt == "flags8":
        raw = raw_bytes[0] if raw_bytes else 0
        active = [TCU_STATUS_FLAGS[i] for i in range(8) if raw & (1 << i)]
        return ", ".join(active) if active else "OK", unit, label

    # Numeric: try to decode as big-endian int
    try:
        if len(raw_bytes) == 1:
            raw_int = raw_bytes[0]
        elif len(raw_bytes) == 2:
            raw_int = int.from_bytes(raw_bytes, "big")
        elif len(raw_bytes) == 4:
            raw_int = int.from_bytes(raw_bytes, "big")
        else:
            raw_int = int.from_bytes(raw_bytes[:2], "big")
        physical = raw_int * scale + offset
        if fmt == "int":
            return str(int(physical)), unit, label
        elif fmt == "float1":
            return f"{physical:.1f}", unit, label
        elif fmt == "float2":
            return f"{physical:.2f}", unit, label
        elif fmt == "float3":
            return f"{physical:.3f}", unit, label
        else:
            return f"{physical:.1f}", unit, label
    except Exception:
        return raw_bytes.hex(), unit, label



# ── TCUDef — thin wrapper for transmission modules ───────────────────────────

@dataclass
class TCUDef(ECUDef):
    """
    Transmission Control Unit definition.
    Inherits ECUDef but adds:
      live_dids:   dict of DID → (name, unit, scale, offset, fmt)
      tcu_type:    human-readable type string
      gear_count:  number of forward gears
    The blocks dict is sparse for now (read-only / live data target).
    Flash block layout and SA2 to be populated from ODX when available.
    """
    live_dids:  dict = field(default_factory=dict)
    tcu_type:   str  = ""
    gear_count: int  = 0

    def decode_live_did(self, did: int, raw: bytes):
        return decode_tcu_did(did, raw)


