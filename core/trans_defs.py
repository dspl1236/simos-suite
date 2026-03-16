"""
core/trans_defs.py — Transmission definitions shim

All TCU definitions (ZF8HP, DL501, DQ250, DQ381) live in core/ecu_defs.py
alongside the ECU definitions, sharing ECUDef infrastructure.

This module re-exports the transmission API for clean imports:
    from core.trans_defs import TCU_REGISTRY, ZF8HP, DQ250, DL501, DQ381
    from core.trans_defs import TCUDef, TCU_LIVE_DIDS, decode_tcu_did
    from core.trans_defs import ECU_DEFAULT_TRANS
"""

from core.ecu_defs import (
    TCUDef,
    TCU_LIVE_DIDS,
    decode_tcu_did,
    ZF8HP,
    DL501,
    DQ250,
    DQ381,
    TCU_REGISTRY,
)

# Convenience aliases used by UI and sim_runner
TRANS_REGISTRY     = TCU_REGISTRY
TRANS_DISPLAY_NAMES = {k: v.name for k, v in TCU_REGISTRY.items()}

def get_trans(key: str):
    return TCU_REGISTRY.get(key.upper())

# Default transmission per ECU project code
# (ECU selector in ConnectTab auto-suggests the right TCU)
ECU_DEFAULT_TRANS = {
    "S85":  "ZF8HP",   # C7 3.0T TFSI — ZF 8-speed auto
    "SC8":  "DQ250",   # MQB 2.0T — Temic 6-speed DSG
    "SC1":  "DQ250",   # EA888 Gen1/2
    "SC2":  "DQ250",   # EA888 Gen3
}

__all__ = [
    "TCUDef", "TCU_LIVE_DIDS", "decode_tcu_did",
    "ZF8HP", "DL501", "DQ250", "DQ381",
    "TCU_REGISTRY", "TRANS_REGISTRY", "TRANS_DISPLAY_NAMES",
    "get_trans", "ECU_DEFAULT_TRANS",
]
