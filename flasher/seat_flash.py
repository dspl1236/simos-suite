"""
flasher/seat_flash.py — DIY UDS flasher for the C7 memory-seat modules
J136 (driver) / J521 (passenger), Renesas V850E2 (4x0959760).

Why a dedicated module (vs uds_flash.py / hvac_flash.py)
───────────────────────────────────────────────────────
The seat is simpler than either:

  * NOT RSA-signed. The 4x0959760 ODX integrity is SECURITY-METHOD = CRC32 + SA2 —
    no SIG_*/FW-SIGNATURE on any data block (confirmed on the 4H0 .sgo and the 4M0 FRF,
    and by the cross-platform flash-integrity survey). So a *modified* image can be
    written over the diagnostic port; the only gate is a recomputable CRC.
  * Integrity at CheckMemory is CRC-32/ISO-HDLC (zlib.crc32) over the whole UNCOMPRESSED
    block — the VAG ODX FW-CHECKSUM convention. VERIFIED: on plain (ENCRYPT-COMPRESS-
    METHOD 0x00) FRFs, zlib.crc32(block) reproduces the declared FW-CHECKSUM exactly
    (4K0035335B 6/6 blocks, 2G0035456 / 5N0035729A / 0D7142057D / 6C0819045A 2/2 each),
    and flasher/frf_pack.py recomputes it the same way. This is NOT the Simos internal
    VW-poly (0x4C11DB7) header CRC — the seat has no internal checksum header at all
    (plain C-runtime startup; see flasher/seat_cp_patch.py), so CheckMemory is the only
    fixup needed.
  * No LZSS, no XOR/AES, no ECM3. Blocks are plain (the 4H0 .sgo decodes to 12 plain
    blocks: 0xFFE080 descriptor + eleven 0x8000 code blocks 0x008000..0x060000).

The patch
─────────
flasher/seat_cp_patch.py flips ONE byte of the CP limp-gate (located by code signature,
so it is SW-level independent). The byte sits in the 0x038000 code block (the gate's
reconstructed file offset 0x36E66 → address 0x3EE66). We flash only the block that holds
the gate, recompute its CRC, and CheckMemory it.

SAFETY MODEL (read this)
────────────────────────
  * Every write entry point defaults to dry_run=True — it walks the whole sequence and
    logs every step WITHOUT erasing or writing. Use it to validate addressing/timing
    first.
  * read_identify_patch_flash_seat() is the recommended flow: identify the unit, read ITS
    firmware, patch THAT image (gate located by signature), and write back only the
    changed block — you patch exactly what is on your unit, sidestepping the SW-variant
    question.

ON-CAR UNKNOWNS (parameterised, confirm on the bench/parts car — like hvac_flash.py)
────────────────────────────────────────────────────────────────────────────────────
  1. CAN IDs. The convenience-bus seat-memory request/response IDs are NOT hard-coded
     here (set can_tx=/can_rx= from your own car — you have them from the Cerberus
     convenience-bus sniffs).
  2. Addressing. RequestDownload address + CheckMemory block id: the seat may address by
     physical block address (default here) or by a small block index. Parameterised via
     `address_format` and the CheckMemory builder; confirm on the unit.
  3. DFI acceptance. The stock seat flash is ENCRYPT-COMPRESS-METHOD 0x10 (BCB). We send
     uncompressed (DFI 0x00/0x00) because the on-board decompressor keys off the DFI; if
     this particular bootloader insists on 0x10 transfers we'd need the (unreversed) BCB
     codec. A clean NRC at RequestDownload for DFI 0/0 is the signal. Non-destructive.
  4. The exact CheckMemory parameter layout (RID 0x0202). Default follows the in-repo
     VW_Flash pattern [01][block_id][00 04][crc32]; fail_on_checkmemory=True surfaces a
     mismatch loudly.
"""
from __future__ import annotations

import logging
import zlib
import dataclasses
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Callable

from core.ecu_defs import ECUDef, Platform, CryptoType, STD_INFO_DIDS
from flasher import seat_cp_patch

log = logging.getLogger("SimosSuite.SeatFlash")

# The pure-logic parts (patch, CRC, plan) stay importable without a CAN stack. Only the
# live udsoncan Client functions lazily import udsoncan + the uds_flash helpers.
ProgressCallback = Callable[[object], None]


def _noop(*_a, **_k) -> None:
    return None

# SA2 seed/key bytecode — identical on the 4H0 .sgo and the 4M0 FRF.
_SEAT_SA2 = bytes.fromhex("6806814a07878a2a10e56b058493738001494c")

# The 0x8000-aligned code block that contains the CP limp-gate (gate addr ~0x3EE66).
GATE_BLOCK_ADDR = 0x038000


def _seat_ecu(name: str, project: str, compatible_hw: List[str],
              can_tx: int = 0x0, can_rx: int = 0x0, notes: str = "") -> ECUDef:
    return ECUDef(
        name=name, project_code=project, platform=Platform.PQ46,
        can_tx=can_tx, can_rx=can_rx,
        crypto=CryptoType.NONE, crypto_key=None, crypto_iv=None,
        sa2_script=_SEAT_SA2,
        blocks={},                 # loaded at runtime from the .sgo / unit read
        binfile_size=0x60000,
        compatible_hw=compatible_hw,
        info_dids=STD_INFO_DIDS,
        notes=notes,
    )


# CAN IDs intentionally 0x0 — set can_tx=/can_rx= per your car (convenience-bus seat IDs).
SEAT_J136_C7 = _seat_ecu(
    "J136 Driver memory seat — C7 (4x0959760)", "SEAT_J136",
    compatible_hw=["4H0959760A", "4H0959760D", "4H0959760E", "4H0959760F",
                   "4H0959760H", "4G8959760", "4G8959760A", "4G8959760B",
                   "4G8959760C", "4G8959760D", "4G8959760E"],
    notes="Renesas V850E2. CRC32-only (not RSA-signed) → OBD-flashable. "
          "Set CAN IDs from your car. SA2 confirmed from the 4H0 .sgo + 4M0 FRF.",
)
SEAT_J521_C7 = _seat_ecu(
    "J521 Passenger memory seat — C7 (4x0959760)", "SEAT_J521",
    compatible_hw=SEAT_J136_C7.compatible_hw,
    notes="Same image/patch as J136; different CAN address.",
)


# ─── Integrity: the CheckMemory value ────────────────────────────────────────

def seat_checkmemory_crc(block: bytes) -> int:
    """The value the seat bootloader expects at CheckMemory (RID 0x0202): CRC-32/
    ISO-HDLC over the whole uncompressed block. VERIFIED to reproduce the ODX
    FW-CHECKSUM on plain VAG FRFs (see module docstring); same as frf_pack.py."""
    return zlib.crc32(block) & 0xFFFFFFFF


def build_checkmemory_data(block_id: int, crc: int) -> bytes:
    """CheckMemory (0x31 01 0202) request payload, VW_Flash layout:
    [ALFID=0x01][block_id][len_hi=0x00][len_lo=0x04][crc32 big-endian]."""
    return bytes([0x01, block_id & 0xFF, 0x00, 0x04]) + (crc & 0xFFFFFFFF).to_bytes(4, "big")


# ─── Seat image from the .sgo (uncompressed, plain blocks) ───────────────────

@dataclass
class SeatBlock:
    addr: int
    data: bytes
    erase: Tuple[int, int]
    prog: Tuple[int, int]

    @property
    def crc(self) -> int:
        return seat_checkmemory_crc(self.data)


def load_seat_sgo(path: str) -> List[SeatBlock]:
    """Load the decoded (plain, uncompressed) flash blocks from a 4x0959760 .sgo."""
    from cp_tools.sgo_unpack import parse
    sgo = parse(open(path, "rb").read())
    out: List[SeatBlock] = []
    for b in sgo.blocks:
        if b.decoded:
            out.append(SeatBlock(addr=b.addr, data=b.data, erase=b.erase, prog=b.prog))
    return out


def locate_gate_block(blocks: List[SeatBlock]) -> SeatBlock:
    """Return the block that contains the CP limp-gate signature."""
    hits = [b for b in blocks if seat_cp_patch.is_patched(b.data)
            or _has_stock_gate(b.data)]
    if not hits:
        raise seat_cp_patch.GateNotFound(
            "no block in this image contains the seat CP limp-gate signature")
    if len(hits) > 1:
        raise seat_cp_patch.GateNotFound(
            f"gate signature in {len(hits)} blocks @ {[hex(b.addr) for b in hits]}")
    return hits[0]


def _has_stock_gate(data: bytes) -> bool:
    try:
        seat_cp_patch.find_gate(data, patched=False)
        return True
    except seat_cp_patch.GateNotFound:
        return False


# ─── The flash plan (pure, offline-testable) ─────────────────────────────────

@dataclass
class FlashStep:
    op: str                      # "session" | "sa2" | "erase" | "download" | "transfer"
                                 # | "transfer_exit" | "checkmemory" | "dependencies" | "reset"
    addr: Optional[int] = None
    length: Optional[int] = None
    block_id: Optional[int] = None
    payload: Optional[bytes] = None      # transfer data / checkmemory data
    crc: Optional[int] = None
    note: str = ""


def plan_seat_flash(blocks: List[SeatBlock], *, only_gate_block: bool = True,
                    address_format: int = 32) -> Tuple[bytes, List[FlashStep]]:
    """Build the flash plan for the limp patch (pure — no I/O, no transport).

    Returns (patched_image_of_gate_block, [FlashStep...]). By default only the block
    holding the gate is (patched and) flashed; set only_gate_block=False to flash every
    code block (the gate block patched, the rest stock).

    Each flashed block's CheckMemory step carries seat_checkmemory_crc(the exact bytes
    being transferred) — so the plan is self-consistent by construction.
    """
    gate = locate_gate_block(blocks)
    patched_data, off = seat_cp_patch.patch_image(gate.data)

    targets = [gate] if only_gate_block else list(blocks)

    steps: List[FlashStep] = [
        FlashStep("session", note="extended → 0x0203 precondition → programming"),
        FlashStep("sa2", note="SecurityAccess SA2 (seed→key from the .sgo bytecode)"),
    ]
    for b in targets:
        data = patched_data if b.addr == gate.addr else b.data
        block_id = _block_id_for(b.addr)
        crc = seat_checkmemory_crc(data)
        note = "PATCHED gate block (+1 byte @0x%05x)" % off if b.addr == gate.addr else "stock block"
        steps += [
            FlashStep("erase", addr=b.addr, length=len(data), block_id=block_id, note=note),
            FlashStep("download", addr=b.addr, length=len(data), block_id=block_id,
                      note="DFI 0x00/0x00 (uncompressed), address_format=%d" % address_format),
            FlashStep("transfer", addr=b.addr, length=len(data), payload=data),
            FlashStep("transfer_exit"),
            FlashStep("checkmemory", addr=b.addr, block_id=block_id, crc=crc,
                      payload=build_checkmemory_data(block_id, crc),
                      note="CRC-32/ISO-HDLC over the transferred block"),
        ]
    steps += [FlashStep("dependencies", note="0xFF01 CheckProgrammingDependencies"),
              FlashStep("reset", note="11 01 hardReset")]
    return patched_data, steps


def _block_id_for(addr: int) -> int:
    """Map a 0x8000-aligned code-block address to a small block id for CheckMemory.
    0x008000→1, 0x010000→2, … (confirm the unit's own numbering on the bench)."""
    if addr >= 0x8000 and (addr & 0x7FFF) == 0:
        return (addr - 0x8000) // 0x8000 + 1
    return addr & 0xFF


# ─── Live execution (udsoncan Client; dry_run by default) ────────────────────

def _client_cfg(ecu: ECUDef, timeout: int = 30) -> dict:
    from udsoncan import configs
    from flasher.uds_flash import _make_security_algo
    cfg = dict(configs.default_client_config)
    cfg["security_algo"] = _make_security_algo(ecu.sa2_script)
    cfg["security_algo_params"] = None
    cfg["data_identifiers"] = {}
    cfg["request_timeout"] = timeout
    return cfg


def _with_can(ecu: ECUDef, can_tx: Optional[int], can_rx: Optional[int]) -> ECUDef:
    if can_tx is None and can_rx is None:
        if not ecu.can_tx or not ecu.can_rx:
            raise ValueError("seat CAN IDs not set — pass can_tx=/can_rx= (from your car)")
        return ecu
    return dataclasses.replace(ecu, can_tx=can_tx or ecu.can_tx, can_rx=can_rx or ecu.can_rx)


def identify_seat(ecu: ECUDef = SEAT_J136_C7, *, can_tx: Optional[int] = None,
                  can_rx: Optional[int] = None, interface: str = "J2534",
                  interface_path: Optional[str] = None) -> Dict[str, str]:
    """Read identification DIDs (extended session, read-only, no SA2). Run this FIRST to
    confirm the unit's part number / SW before any flash."""
    import udsoncan
    from udsoncan.client import Client
    from udsoncan import services, configs
    from flasher.uds_flash import _make_connection

    ecu = _with_can(ecu, can_tx, can_rx)
    conn = _make_connection(ecu, interface, interface_path)

    class _Str(udsoncan.DidCodec):
        def encode(self, v): return bytes(v)
        def decode(self, p):
            try:
                s = p.decode("ascii").strip("\x00 \t\r\n")
                if s and all(32 <= ord(c) < 127 for c in s):
                    return s
            except Exception:
                pass
            return p.hex().upper()
        def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

    cfg = dict(configs.default_client_config)
    cfg["data_identifiers"] = {d: _Str for d in ecu.info_dids}
    cfg["request_timeout"] = 8
    out: Dict[str, str] = {}
    with Client(conn, request_timeout=8, config=cfg) as client:
        client.change_session(services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
        for did in ecu.info_dids:
            try:
                out[f"{did:#06x}"] = str(client.read_data_by_identifier_first(did))
            except Exception as e:
                out[f"{did:#06x}"] = f"<{type(e).__name__}>"
    log.info("identify_seat: %s", out)
    return out


def flash_seat_block(ecu: ECUDef, addr: int, data: bytes, *,
                     can_tx: Optional[int] = None, can_rx: Optional[int] = None,
                     interface: str = "J2534", interface_path: Optional[str] = None,
                     sa2_level: int = 0x11, erase_routine: int = 0xFF00,
                     checkmemory_routine: int = 0x0202, address_format: int = 32,
                     fail_on_checkmemory: bool = True, dry_run: bool = True,
                     callback: ProgressCallback = _noop) -> bool:
    """Flash ONE plain block to the seat (DFI 0x00/0x00) and CheckMemory it with the
    recomputed CRC-32. `data` is the exact (patched) uncompressed block.

    dry_run=True (default) walks the sequence and logs every step WITHOUT erasing or
    writing. On a real run, CheckMemory sends seat_checkmemory_crc(data); a NRC there is
    fatal when fail_on_checkmemory=True (the integrity gate rejected the image or the
    param layout is wrong — do NOT trust the flash)."""
    import udsoncan
    from udsoncan.client import Client
    from udsoncan import services, exceptions
    from flasher.uds_flash import _make_connection, FlashProgress

    ecu = _with_can(ecu, can_tx, can_rx)
    conn = _make_connection(ecu, interface, interface_path)
    cfg = _client_cfg(ecu)
    block_id = _block_id_for(addr)
    crc = seat_checkmemory_crc(data)
    tag = "DRY-RUN " if dry_run else ""
    callback(FlashProgress("CONNECT", f"{tag}Flashing seat block @0x{addr:06X} ({len(data):,} B)…", 0))

    with Client(conn, request_timeout=30, config=cfg) as client:
        def _st(t=30):
            try: client.session_timing["p2_server_max"] = t
            except TypeError: client.session_timing.p2_server_max = t
            client.config["request_timeout"] = t

        try:
            client.change_session(services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
            _st(30)
            try: client.start_routine(0x0203)
            except Exception as e: log.debug("precondition 0x0203: %s", e)
            client.tester_present()
            callback(FlashProgress("CONNECT", "Programming session…", 8))
            client.change_session(services.DiagnosticSessionControl.Session.programmingSession)
            _st(30); client.tester_present()
            callback(FlashProgress("CONNECT", "SA2 security access…", 12))
            client.unlock_security_access(sa2_level)
            client.tester_present()
        except exceptions.NegativeResponseException as e:
            callback(FlashProgress("ERROR", f"Session/SA2 failed: {e}", 0)); return False

        callback(FlashProgress("ERASE", f"{tag}Erase @0x{addr:06X}…", 15))
        if not dry_run:
            try:
                client.start_routine(erase_routine, data=bytes([0x01, block_id]))
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"Erase failed: {e}", 0)); return False
        client.tester_present()

        callback(FlashProgress("TRANSFER", f"{tag}RequestDownload ({len(data):,} B)…", 18))
        max_block = 0xFFD
        if not dry_run:
            try:
                dfi = udsoncan.DataFormatIdentifier(compression=0x0, encryption=0x0)
                mem = udsoncan.MemoryLocation(address=addr, memorysize=len(data),
                                              address_format=address_format, memorysize_format=32)
                resp = client.request_download(mem, dfi)
                max_block = resp.service_data.max_length
            except Exception as e:
                callback(FlashProgress("ERROR", f"RequestDownload failed (DFI 0/0 — see note 3): {e}", 0))
                return False

        chunk_size = max_block - 2
        counter, offset, n = 1, 0, len(data)
        while offset < n:
            chunk = data[offset:offset + chunk_size]
            callback(FlashProgress("TRANSFER", f"{tag}{offset:#08x}/{n:#08x}", 18 + int(70 * offset / n)))
            if not dry_run:
                try:
                    client.transfer_data(counter, chunk)
                except exceptions.NegativeResponseException as e:
                    callback(FlashProgress("ERROR", f"TransferData failed at {offset:#x}: {e}", 0))
                    return False
            offset += len(chunk)
            counter = (counter + 1) & 0xFF
            if counter == 0: counter = 1
            if (offset // chunk_size) % 50 == 0 and not dry_run:
                try: client.tester_present()
                except Exception: pass

        if not dry_run:
            try: client.request_transfer_exit()
            except exceptions.NegativeResponseException as e:
                callback(FlashProgress("ERROR", f"TransferExit failed: {e}", 0)); return False
            client.tester_present()

        callback(FlashProgress("VERIFY", f"CheckMemory CRC32=0x{crc:08X}…", 95))
        if not dry_run:
            try:
                client.start_routine(checkmemory_routine, data=build_checkmemory_data(block_id, crc))
            except exceptions.NegativeResponseException as e:
                msg = f"CheckMemory rejected (CRC=0x{crc:08X}): {e}"
                if fail_on_checkmemory:
                    callback(FlashProgress("ERROR", msg + " — image NOT confirmed", 0)); return False
                log.warning(msg + " (continuing — verify on the unit)")

        if not dry_run:
            try: client.start_routine(0xFF01)        # CheckProgrammingDependencies
            except Exception as e: log.debug("CheckProgrammingDependencies: %s", e)
            client.tester_present()
            callback(FlashProgress("DONE", "Resetting module…", 99))
            try: client.ecu_reset(services.ECUReset.ResetType.hardReset)
            except Exception as e: log.debug("ECU reset: %s", e)

    callback(FlashProgress("DONE", f"{tag}seat block @0x{addr:06X} {'simulated' if dry_run else 'flashed'}", 100))
    return True


def patch_and_flash_seat(sgo_path: str, ecu: ECUDef = SEAT_J136_C7, *,
                         can_tx: Optional[int] = None, can_rx: Optional[int] = None,
                         interface: str = "J2534", interface_path: Optional[str] = None,
                         dry_run: bool = True, callback: ProgressCallback = _noop) -> bool:
    """Orchestrator: load a 4x0959760 .sgo, patch the gate block, flash ONLY that block.
    dry_run=True by default."""
    blocks = load_seat_sgo(sgo_path)
    patched, steps = plan_seat_flash(blocks, only_gate_block=True)
    gate = locate_gate_block(blocks)
    log.info("seat flash plan: %d steps, gate block @0x%06X, CRC32=0x%08X",
             len(steps), gate.addr, seat_checkmemory_crc(patched))
    return flash_seat_block(ecu, gate.addr, patched, can_tx=can_tx, can_rx=can_rx,
                            interface=interface, interface_path=interface_path,
                            dry_run=dry_run, callback=callback)
