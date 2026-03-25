"""
ui/trans_tab.py — Transmission Live Data Tab

Polls live UDS DIDs from any of the four supported TCUs:
  ZF8HP  — 8-speed torque converter (C7 A6/A7/A8)
  DL501  — 7-speed S-Tronic DSG (C7 S6/S7)
  DQ250  — 6-speed wet DSG (MQB Golf 7/Passat)
  DQ381  — 7-speed dry DSG (MQB Golf 8)

All values are read-only in extended session — no SA2 required.
The tab handles its own connection so it doesn't interfere with
the ECU flash connection.

Layout
------
  Top bar:   TCU selector, poll interval, start/stop
  Grid:      live value cards (gear, temps, speeds, torque, pressures, flags)
  Bottom:    gear visual indicator + raw DID log
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Dict, List

from core.ecu_defs import TCUDef, TCU_REGISTRY, TCU_DISPLAY_NAMES, decode_tcu_did
from ui.interface_panel import COLORS as C

# ── DID groups for display layout ────────────────────────────────────────────
# (group_name, [did, ...])
DID_GROUPS = [
    ("gear & selector",  [0x0180, 0x0181, 0x0182]),
    ("temperatures",     [0x0115, 0x0116]),
    ("shaft speeds",     [0x01A0, 0x01A1, 0x01A2]),
    ("torque",           [0x0190, 0x0191]),
    ("clutch / pressure",[0x01B0, 0x01B1, 0x01C0]),
    ("status",           [0x01D0, 0x0205, 0x0212]),
]

# card accent colour per group
GROUP_ACCENT = {
    "gear & selector":    "#58a6ff",   # blue
    "temperatures":       "#f85149",   # red
    "shaft speeds":       "#3fb950",   # green
    "torque":             "#d29922",   # amber
    "clutch / pressure":  "#bc8cff",   # purple
    "status":             "#8b949e",   # grey
}


def lbl(parent, text, fg=None, **kw):
    return tk.Label(parent, text=text,
                    fg=fg or C["text"], bg=kw.pop("bg", C["bg"]),
                    font=kw.pop("font", ("Menlo", 10)), **kw)


def sep(parent):
    return tk.Frame(parent, bg=C["border"], height=1)


class ValueCard(tk.Frame):
    """A single live-value card with label, big value, and unit."""

    def __init__(self, parent, label: str, unit: str, accent: str = C["blue"]):
        super().__init__(parent, bg=C["surface"],
                         highlightbackground=C["border"],
                         highlightthickness=1)
        # Top accent stripe
        tk.Frame(self, bg=accent, height=2).pack(fill="x")

        inner = tk.Frame(self, bg=C["surface"], padx=10, pady=8)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text=label,
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 8), anchor="w").pack(fill="x")

        val_row = tk.Frame(inner, bg=C["surface"])
        val_row.pack(fill="x", pady=(2, 0))

        self._val_var = tk.StringVar(value="—")
        tk.Label(val_row, textvariable=self._val_var,
                 fg=C["text"], bg=C["surface"],
                 font=("Menlo", 16, "bold"),
                 anchor="w").pack(side="left")

        tk.Label(val_row, text=f" {unit}",
                 fg=C["text_dim"], bg=C["surface"],
                 font=("Menlo", 10),
                 anchor="w").pack(side="left")

    def set(self, value: str):
        self._val_var.set(value)


class GearIndicator(tk.Canvas):
    """Big graphical gear display — shows P/R/N/D/1-8."""

    GEAR_COLORS = {
        "P": "#58a6ff", "R": "#f85149", "N": "#d29922",
        "D": "#3fb950", "D/S": "#3fb950",
    }

    def __init__(self, parent, size: int = 80):
        super().__init__(parent, width=size, height=size,
                         bg=C["bg"], bd=0, highlightthickness=0)
        self._size = size
        self._gear = "—"
        self._draw()

    def set(self, gear: str):
        self._gear = gear
        self._draw()

    def _draw(self):
        s = self._size
        self.delete("all")
        # Background circle
        self.create_oval(4, 4, s-4, s-4,
                         fill=C["surface"], outline=C["border"], width=1)
        # Gear text
        color = self.GEAR_COLORS.get(self._gear, C["text"])
        try:
            g = int(self._gear)
            color = "#e3b341" if g <= 3 else "#3fb950"
        except ValueError:
            pass
        self.create_text(s//2, s//2, text=self._gear,
                         fill=color, font=("Menlo", int(s*0.38), "bold"))


class TransTab(tk.Frame):
    """Transmission live data tab."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=C["bg"])
        self._app = app
        self._tcu: Optional[TCUDef] = list(TCU_REGISTRY.values())[0]
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cards: Dict[int, ValueCard] = {}
        self._gear_indicator: Optional[GearIndicator] = None
        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self):
        body = tk.Frame(self, bg=C["bg"], padx=16, pady=10)
        body.pack(fill="both", expand=True)

        # ── Top controls ──────────────────────────────────────────────────────
        ctrl = tk.Frame(body, bg=C["bg"])
        ctrl.pack(fill="x", pady=(0, 10))

        tk.Label(ctrl, text="TCU:",
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=(0, 6))

        self._tcu_var = tk.StringVar(value=list(TCU_DISPLAY_NAMES.keys())[0])
        tcu_menu = ttk.Combobox(ctrl, textvariable=self._tcu_var,
                                 values=list(TCU_DISPLAY_NAMES.keys()),
                                 state="readonly", width=14,
                                 font=("Menlo", 10))
        tcu_menu.pack(side="left", padx=(0, 10))
        tcu_menu.bind("<<ComboboxSelected>>", self._on_tcu_change)

        tk.Label(ctrl, text="interval ms:",
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=(0, 4))
        self._interval_var = tk.StringVar(value="500")
        tk.Entry(ctrl, textvariable=self._interval_var,
                 bg=C["btn"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 10), width=5, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left", padx=(0, 10))

        self._start_btn = tk.Button(ctrl, text="start polling",
                                    fg="#0d1117", bg=C["blue"],
                                    activeforeground="#0d1117",
                                    activebackground="#79b8ff",
                                    font=("Menlo", 10, "bold"),
                                    bd=0, padx=12, pady=4, cursor="hand2",
                                    highlightbackground=C["border"],
                                    highlightthickness=1,
                                    command=self._do_start)
        self._start_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = tk.Button(ctrl, text="stop",
                                   fg=C["text"], bg=C["btn"],
                                   activeforeground=C["text"],
                                   activebackground=C["btn_hover"],
                                   font=("Menlo", 10),
                                   bd=0, padx=12, pady=4, cursor="hand2",
                                   highlightbackground=C["border"],
                                   highlightthickness=1,
                                   state="disabled",
                                   command=self._do_stop)
        self._stop_btn.pack(side="left")

        self._status_var = tk.StringVar(value="idle — connect interface and select TCU")
        tk.Label(ctrl, textvariable=self._status_var,
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=(12, 0))

        sep(body).pack(fill="x", pady=(0, 10))

        # ── Main content: gear indicator + cards ──────────────────────────────
        main = tk.Frame(body, bg=C["bg"])
        main.pack(fill="both", expand=True)

        # Left column: big gear indicator + TCU info
        left = tk.Frame(main, bg=C["bg"], width=120)
        left.pack(side="left", fill="y", padx=(0, 14))
        left.pack_propagate(False)

        tk.Label(left, text="current gear",
                 fg=C["text_dim"], bg=C["bg"],
                 font=("Menlo", 8)).pack(pady=(0, 6))
        self._gear_indicator = GearIndicator(left, size=90)
        self._gear_indicator.pack()

        sep(left).pack(fill="x", pady=10)

        self._tcu_info_var = tk.StringVar(value=self._tcu_info_text())
        tk.Label(left, textvariable=self._tcu_info_var,
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 8), justify="left",
                 wraplength=110, anchor="nw").pack(fill="x")

        # Right column: value card grid
        right = tk.Frame(main, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True)

        self._build_cards(right)

        sep(body).pack(fill="x", pady=8)

        # ── TCU Swap Probe ───────────────────────────────────────────────────
        swap_card = tk.Frame(body, bg=C["surface"],
                             highlightbackground=C["border"],
                             highlightthickness=1, padx=12, pady=8)
        swap_card.pack(fill="x", pady=(0, 8))

        swap_hdr = tk.Frame(swap_card, bg=C["surface"])
        swap_hdr.pack(fill="x")
        tk.Label(swap_hdr, text="TCU SWAP PROBE",
                 fg=C["text"], bg=C["surface"],
                 font=("Menlo", 10, "bold")).pack(side="left")
        tk.Label(swap_hdr, text="read-only",
                 fg=C["green"], bg="#1a3a1e",
                 font=("Menlo", 8, "bold"),
                 padx=6, pady=1).pack(side="left", padx=(8, 0))

        tk.Label(swap_card,
                 text="Reads VIN binding, IMMO state, SA2 capability, and marriage "
                      "status from the TCU. Use before/after a transmission swap to "
                      "understand what needs to change.",
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9), wraplength=560,
                 justify="left").pack(anchor="w", pady=(4, 6))

        swap_btn_row = tk.Frame(swap_card, bg=C["surface"])
        swap_btn_row.pack(fill="x")

        self._swap_probe_btn = tk.Button(
            swap_btn_row, text="Probe TCU Marriage State",
            fg="#0d1117", bg=C["blue"],
            activeforeground="#0d1117", activebackground="#79b8ff",
            font=("Menlo", 10, "bold"),
            bd=0, padx=12, pady=4, cursor="hand2",
            command=self._do_swap_probe)
        self._swap_probe_btn.pack(side="left")

        self._virgin_probe_btn = tk.Button(
            swap_btn_row, text="Check Virginize Support",
            fg=C["text"], bg=C["btn"],
            activeforeground=C["text"], activebackground=C["btn_hover"],
            font=("Menlo", 10),
            bd=0, padx=12, pady=4, cursor="hand2",
            command=self._do_virgin_probe)
        self._virgin_probe_btn.pack(side="left", padx=(8, 0))

        self._swap_result = tk.Text(swap_card, bg=C["bg"],
                                     fg=C["text_muted"], font=("Menlo", 9),
                                     height=12, bd=0,
                                     highlightbackground=C["border"],
                                     highlightthickness=1,
                                     state="disabled")
        self._swap_result.pack(fill="x", pady=(6, 0))
        self._swap_result.tag_config("ok",   foreground=C["green"])
        self._swap_result.tag_config("err",  foreground=C["red"])
        self._swap_result.tag_config("warn", foreground=C["amber"])
        self._swap_result.tag_config("hdr",  foreground=C["blue"])
        self._swap_result.tag_config("dim",  foreground=C["text_dim"])

        sep(body).pack(fill="x", pady=8)

        # ── Raw DID log ───────────────────────────────────────────────────────
        tk.Label(body, text="RAW LOG",
                 fg=C["text_dim"], bg=C["bg"],
                 font=("Menlo", 9)).pack(anchor="w", pady=(0, 4))

        self._log = tk.Text(body, bg=C["surface"],
                             fg=C["text_muted"], font=("Menlo", 9),
                             height=6, bd=0,
                             highlightbackground=C["border"],
                             highlightthickness=1,
                             state="disabled")
        self._log.pack(fill="x")

    def _build_cards(self, parent: tk.Frame):
        """Build value card grid from DID_GROUPS."""
        self._cards.clear()

        for row_idx, (group_name, dids) in enumerate(DID_GROUPS):
            accent = GROUP_ACCENT.get(group_name, C["blue"])

            # Section label
            tk.Label(parent, text=group_name.upper(),
                     fg=C["text_dim"], bg=C["bg"],
                     font=("Menlo", 8)).grid(
                         row=row_idx*2, column=0,
                         columnspan=max(len(dids), 1),
                         sticky="w", pady=(6, 2))

            for col_idx, did in enumerate(dids):
                if did not in self._tcu.live_dids:
                    continue
                _label, unit, *_ = self._tcu.live_dids[did]
                label = self._tcu.live_dids[did][0]
                card = ValueCard(parent, label, unit, accent=accent)
                card.grid(row=row_idx*2+1, column=col_idx,
                          padx=(0, 6), pady=(0, 4), sticky="nsew")
                parent.columnconfigure(col_idx, weight=1)
                self._cards[did] = card

    def _tcu_info_text(self) -> str:
        if not self._tcu:
            return ""
        lines = [
            self._tcu.tcu_type,
            f"Gears: {self._tcu.gear_count}",
            f"TX: 0x{self._tcu.can_tx:03X}",
            f"RX: 0x{self._tcu.can_rx:03X}",
        ]
        return "\n".join(lines)

    # ── TCU change ────────────────────────────────────────────────────────────

    def _on_tcu_change(self, _=None):
        key = self._tcu_var.get()
        self._tcu = TCU_REGISTRY[key]
        self._tcu_info_var.set(self._tcu_info_text())
        # Rebuild cards for new TCU (different live_dids subset)
        for w in self._cards.values():
            w.destroy()
        # Find the right container and rebuild
        for widget in self.winfo_children():
            self._rebuild_cards_in_widget(widget)
        self._status_var.set(f"TCU: {self._tcu.name}")

    def _rebuild_cards_in_widget(self, widget):
        """Walk widget tree to find the card grid frame and rebuild."""
        # The card grid is the 'right' frame in the main content area
        # We identify it by looking for ValueCard children
        for child in widget.winfo_children():
            if isinstance(child, ValueCard):
                # Found the card grid — rebuild
                parent = widget
                for w in list(parent.winfo_children()):
                    w.destroy()
                self._build_cards(parent)
                return
            self._rebuild_cards_in_widget(child)

    # ── Polling ───────────────────────────────────────────────────────────────

    def _do_start(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._running = True
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._status_var.set(f"polling {self._tcu.tcu_type}...")
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _do_stop(self):
        self._running = False
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._status_var.set("stopped")

    def _poll_loop(self):
        try:
            interval = max(100, int(self._interval_var.get())) / 1000.0
        except ValueError:
            interval = 0.5

        try:
            from flasher.uds_flash import _make_connection
            conn = _make_connection(
                # TCUDef doesn't subclass ECUDef, pass a minimal shim
                _ECUShim(self._tcu),
                self._app.interface,
                self._app.iface_path or None,
            )
        except Exception as e:
            self.after(0, lambda: self._log_append(f"[ERROR] connect: {e}"))
            self.after(0, self._do_stop)
            return

        import udsoncan

        class _RawCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return bytes(p)
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        dids_to_poll = list(self._tcu.live_dids.keys())

        cfg = dict(udsoncan.configs.default_client_config)
        cfg["data_identifiers"] = {did: _RawCodec for did in dids_to_poll}
        cfg["request_timeout"]  = interval * 2 + 1.0

        with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
            try:
                client.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)
                self.after(0, lambda: self._status_var.set(
                    f"polling {self._tcu.tcu_type} — extended session OK"))
            except Exception as e:
                self.after(0, lambda: self._log_append(f"[WARN] session: {e}"))

            while self._running:
                for did in dids_to_poll:
                    try:
                        raw = client.read_data_by_identifier_first(did)
                        if isinstance(raw, bytes):
                            raw_bytes = raw
                        else:
                            raw_bytes = bytes(raw) if raw else b""
                        display, unit, label = decode_tcu_did(did, raw_bytes)
                        self.after(0, lambda d=did, v=display: self._update_card(d, v))
                        self.after(0, lambda l=label, v=display, u=unit:
                                   self._log_append(f"{l}: {v} {u}"))
                    except Exception as e:
                        self.after(0, lambda d=did, e=e:
                                   self._log_append(f"DID 0x{d:04X}: {e}"))

                time.sleep(interval)

        self.after(0, self._do_stop)

    def _update_card(self, did: int, value: str):
        card = self._cards.get(did)
        if card:
            card.set(value)
        # Special: update gear indicator
        if did == 0x0180 and self._gear_indicator:
            self._gear_indicator.set(value)

    def _log_append(self, msg: str):
        self._log.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self._log.insert("end", f"[{ts}] {msg}\n")
        self._log.see("end")
        self._log.config(state="disabled")

    # ── TCU Swap Probe ─────────────────────────────────────────────────────

    # Standard VW TCU identification DIDs
    TCU_SWAP_DIDS = {
        0xF190: "VIN",
        0xF18C: "ECU Serial Number",
        0xF187: "Spare Part Number",
        0xF189: "SW Version",
        0xF191: "HW Number",
        0xF1A3: "HW Version",
        0xF197: "System Name",
        0xF17C: "FAZIT Identification",
        0xF186: "Active Session",
        0xF442: "Module Voltage",
        # Marriage / IMMO related
        0x0405: "Flash Memory State",
        0x0600: "Coding Value",
        0xF15A: "Workshop Code / Fingerprint",
        # SA2 probe — attempt seed request
    }

    # ZF factory reset routine IDs (from community research)
    # These may or may not work on VAG firmware overlay
    ZF_ROUTINES = {
        0xFF00: "Erase Memory (standard UDS)",
        0x0203: "Check Programming Preconditions",
        0x0202: "Programming Dependencies",
        0xDF01: "ZF Factory Reset (community reported)",
        0xDF00: "ZF TCU Reset",
    }

    def _swap_log(self, msg: str, tag: str = ""):
        self._swap_result.config(state="normal")
        if tag:
            self._swap_result.insert("end", msg + "\n", tag)
        else:
            self._swap_result.insert("end", msg + "\n")
        self._swap_result.see("end")
        self._swap_result.config(state="disabled")

    def _swap_clear(self):
        self._swap_result.config(state="normal")
        self._swap_result.delete("1.0", "end")
        self._swap_result.config(state="disabled")

    def _do_swap_probe(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._swap_probe_btn.config(state="disabled")
        self._swap_clear()
        self._swap_log("== TCU Marriage State Probe ==", "hdr")
        self._swap_log(f"Target: {self._tcu.name}", "dim")
        self._swap_log(f"CAN TX=0x{self._tcu.can_tx:03X}  RX=0x{self._tcu.can_rx:03X}", "dim")
        self._swap_log("")
        threading.Thread(target=self._swap_probe_task, daemon=True).start()

    def _swap_probe_task(self):
        import udsoncan
        from udsoncan.client import Client
        from udsoncan import configs

        def log(msg, tag=""):
            self.after(0, lambda: self._swap_log(msg, tag))

        class _RawCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return bytes(p)
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        try:
            from flasher.uds_flash import _make_connection
            conn = _make_connection(
                _ECUShim(self._tcu),
                self._app.interface,
                self._app.iface_path or None,
            )

            cfg = dict(configs.default_client_config)
            for did in self.TCU_SWAP_DIDS:
                cfg["data_identifiers"] = {did: _RawCodec}
            cfg["request_timeout"] = 5
            cfg["use_server_timing"] = False

            with Client(conn, request_timeout=5, config=cfg) as client:
                # Extended session
                try:
                    client.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    log("Extended session opened", "ok")
                except Exception as e:
                    log(f"Session error: {e}", "warn")

                # Read all identification DIDs
                log("")
                log("-- Identification DIDs --", "hdr")
                car_vin = ""
                tcu_serial = ""
                tcu_part = ""
                for did, label in self.TCU_SWAP_DIDS.items():
                    try:
                        cfg["data_identifiers"] = {did: _RawCodec}
                        r = client.read_data_by_identifier_first(did)
                        raw = bytes(r) if r else b""
                        # Try ASCII decode for string DIDs
                        try:
                            text = raw.decode("ascii").strip().rstrip("\x00")
                        except (UnicodeDecodeError, AttributeError):
                            text = raw.hex().upper()
                        if did == 0xF190:
                            car_vin = text
                        elif did == 0xF18C:
                            tcu_serial = text
                        elif did == 0xF187:
                            tcu_part = text
                        short = text[:50] if len(text) <= 50 else text[:50] + "..."
                        log(f"  0x{did:04X}  {label:30s}  {short}", "ok")
                    except udsoncan.exceptions.NegativeResponseException as e:
                        nrc = getattr(getattr(e, "response", None), "code", 0)
                        log(f"  0x{did:04X}  {label:30s}  NRC 0x{nrc:02X}", "warn")
                    except Exception as e:
                        log(f"  0x{did:04X}  {label:30s}  {str(e)[:40]}", "dim")

                # SA2 probe — try requesting a seed
                log("")
                log("-- SA2 Security Access Probe --", "hdr")
                for level in [0x01, 0x03, 0x11, 0x27]:
                    try:
                        resp = client.request_seed(level)
                        seed = bytes(resp.service_data.seed)
                        log(f"  Level 0x{level:02X}: seed = {seed.hex().upper()} "
                            f"({len(seed)} bytes)", "ok")
                    except udsoncan.exceptions.NegativeResponseException as e:
                        nrc = getattr(getattr(e, "response", None), "code", 0)
                        nrc_names = {
                            0x12: "subFunctionNotSupported",
                            0x22: "conditionsNotCorrect",
                            0x24: "requestSequenceError",
                            0x31: "requestOutOfRange",
                            0x35: "invalidKey",
                            0x36: "exceededNumberOfAttempts",
                            0x37: "requiredTimeDelayNotExpired",
                        }
                        name = nrc_names.get(nrc, "")
                        log(f"  Level 0x{level:02X}: NRC 0x{nrc:02X} {name}", "dim")
                    except Exception as e:
                        log(f"  Level 0x{level:02X}: {str(e)[:50]}", "dim")

                # Verdict
                log("")
                log("-- Verdict --", "hdr")
                if car_vin:
                    log(f"  TCU reports VIN: {car_vin}", "ok")
                    log("  If this doesn't match your car's VIN, the TCU is", "dim")
                    log("  married to a different vehicle.", "dim")
                else:
                    log("  Could not read VIN from TCU", "warn")

                if tcu_part:
                    log(f"  Part number: {tcu_part}", "dim")
                if tcu_serial:
                    log(f"  Serial: {tcu_serial}", "dim")

        except Exception as e:
            log(f"Connection error: {e}", "err")

        self.after(0, lambda: self._swap_probe_btn.config(state="normal"))

    def _do_virgin_probe(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._virgin_probe_btn.config(state="disabled")
        self._swap_clear()
        self._swap_log("== Virginize Support Probe ==", "hdr")
        self._swap_log(f"Target: {self._tcu.name}", "dim")
        self._swap_log("")
        self._swap_log("Checking if ZF factory reset routines are accessible...", "dim")
        self._swap_log("(read-only — no writes performed)", "dim")
        self._swap_log("")
        threading.Thread(target=self._virgin_probe_task, daemon=True).start()

    def _virgin_probe_task(self):
        import udsoncan
        from udsoncan.client import Client
        from udsoncan import configs

        def log(msg, tag=""):
            self.after(0, lambda: self._swap_log(msg, tag))

        try:
            from flasher.uds_flash import _make_connection
            conn = _make_connection(
                _ECUShim(self._tcu),
                self._app.interface,
                self._app.iface_path or None,
            )

            cfg = dict(configs.default_client_config)
            cfg["request_timeout"] = 5

            with Client(conn, request_timeout=5, config=cfg) as client:
                # Try extended session
                try:
                    client.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    log("Extended session: OK", "ok")
                except Exception as e:
                    log(f"Extended session: {e}", "warn")

                # Try programming session
                try:
                    client.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.programmingSession)
                    log("Programming session: OK", "ok")
                except udsoncan.exceptions.NegativeResponseException as e:
                    nrc = getattr(getattr(e, "response", None), "code", 0)
                    log(f"Programming session: NRC 0x{nrc:02X} "
                        f"({'needs SA2 first' if nrc == 0x22 else 'blocked'})", "warn")
                except Exception as e:
                    log(f"Programming session: {e}", "dim")

                # Back to extended for routine probing
                try:
                    client.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                except Exception:
                    pass

                # Probe each routine — request status only (0x31 0x03 = requestResults)
                # NOT start (0x31 0x01) — we don't want to trigger anything
                log("")
                log("-- Routine Availability (requestResults only) --", "hdr")
                for routine_id, desc in self.ZF_ROUTINES.items():
                    try:
                        # 0x03 = requestRoutineResults — checks if routine exists
                        # without starting it
                        resp = client.routine_control(
                            routine_id, 0x03, data=b"")
                        log(f"  0x{routine_id:04X}  {desc:40s}  RESPONDS", "ok")
                    except udsoncan.exceptions.NegativeResponseException as e:
                        nrc = getattr(getattr(e, "response", None), "code", 0)
                        nrc_names = {
                            0x11: "serviceNotSupported",
                            0x12: "subFunctionNotSupported",
                            0x22: "conditionsNotCorrect (exists, needs preconditions)",
                            0x24: "requestSequenceError (exists, needs SA2 first)",
                            0x31: "requestOutOfRange (routine not present)",
                            0x33: "securityAccessDenied (exists, needs SA2)",
                            0x72: "generalProgrammingFailure",
                        }
                        name = nrc_names.get(nrc, "")
                        exists = nrc in (0x22, 0x24, 0x33)
                        tag = "warn" if exists else "dim"
                        status = "EXISTS (locked)" if exists else "not found"
                        log(f"  0x{routine_id:04X}  {desc:40s}  {status} "
                            f"(NRC 0x{nrc:02X} {name})", tag)
                    except Exception as e:
                        log(f"  0x{routine_id:04X}  {desc:40s}  {str(e)[:40]}", "dim")

                log("")
                log("-- Interpretation --", "hdr")
                log("  Routines marked EXISTS (locked) are present in firmware", "dim")
                log("  but need SA2 unlock or programming session first.", "dim")
                log("  If ZF factory reset (0xDF01) EXISTS, virginization may", "dim")
                log("  be possible with the correct SA2 key.", "dim")
                log("")
                log("  If all routines show 'not found', VAG firmware has", "dim")
                log("  removed/disabled the ZF factory reset path.", "dim")
                log("  In that case, EEPROM direct access is the fallback.", "dim")

        except Exception as e:
            log(f"Connection error: {e}", "err")

        self.after(0, lambda: self._virgin_probe_btn.config(state="normal"))


# ── Thin shim so _make_connection() gets the CAN IDs it needs ────────────────

class _ECUShim:
    """Minimal duck-type shim: gives _make_connection() the attrs it reads."""
    def __init__(self, tcu: TCUDef):
        self.can_tx = tcu.can_tx
        self.can_rx = tcu.can_rx
        self.name   = tcu.name
