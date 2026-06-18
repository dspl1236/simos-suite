"""
ui/main_window.py — Simos Diagnostics Suite main application window

Tabbed desktop GUI (Tkinter + ttk) for the simos-suite diagnostics + CP/Immo workflow.
Tabs:
    1. Connect    — InterfacePanel: hardware interface selection, ECU picker
    2. ECU Info   — Read all VW identification DIDs, display live
    3. Flash      — Read CAL block / write CAL block with progress bar
    4. Logger     — Live DID poller with configurable channels
    5. CP Tools   — J533 probe, constellation capture, ODX viewer
    6. Raw Sniff  — Pass-through hex log of all ISO-TP frames

Usage:
    python -m ui.main_window
    python -m ui.main_window --ecu S85          # pre-select Simos8.5
    python -m ui.main_window --ecu SC8          # pre-select Simos18.1/6

Architecture:
    MainWindow holds shared state:
        self.ecu          — currently selected ECUDef
        self.interface    — interface string ("BLE", "USBISOTP", "J2534", ...)
        self.iface_path   — interface path (COM port, DLL path, or "")
        self.connected    — bool
        self.ble_bridge   — BLEBridgeSync instance (if BLE connected)

    Each tab receives a reference to the MainWindow and calls
    self.mw.get_connection() to get a fresh udsoncan connection.
    All backend calls run in daemon threads; UI updates use self.after().
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

# ── Suite imports (relative) ──────────────────────────────────────────────────
try:
    from core.ecu_defs import (
        ECUDef, SIMOS85, SIMOS12, SIMOS122, SIMOS18,
        ZF8HP, DL501, DQ250, DQ381,
        J533_LEAR, J255_4ZONE, J255_2ZONE,
        ECU_REGISTRY, TCU_REGISTRY,
    )
    from flasher.uds_flash import (
        flash_cal, flash_blocks, read_ecu_info, FlashProgress, _make_connection,
    )
    from transport.interfaces import InterfaceRegistry
    from ui.interface_panel import InterfacePanel
    from ui.trans_logger import TransLoggerTab
    from core.trans_defs import TRANS_REGISTRY, ECU_DEFAULT_TRANS
except ImportError as e:
    print(f"[WARN] Import error (run from repo root): {e}")
    ECUDef = SIMOS85 = SIMOS12 = SIMOS122 = SIMOS18 = None
    ZF8HP = DL501 = DQ250 = DQ381 = None
    J533_LEAR = J255_4ZONE = J255_2ZONE = None
    ECU_REGISTRY = {}; TCU_REGISTRY = {}
    InterfacePanel = None
    TransLoggerTab = None
    TRANS_REGISTRY = {}
    ECU_DEFAULT_TRANS = {}

log = logging.getLogger("SimosSuite.GUI")

# ── Palette ───────────────────────────────────────────────────────────────────
C = {
    "bg":         "#0d1117",
    "surface":    "#161b22",
    "border":     "#30363d",
    "text":       "#e6edf3",
    "muted":      "#8b949e",
    "dim":        "#484f58",
    "green":      "#3fb950",
    "amber":      "#d29922",
    "red":        "#f85149",
    "blue":       "#58a6ff",
    "blue_dim":   "#0d2748",
    "btn":        "#21262d",
    "btn_h":      "#30363d",
    "sel":        "#0d2748",
    "sel_border": "#58a6ff",
    "progress":   "#238636",
}

ECU_MAP: Dict[str, object] = {}


def _ecus():
    """Return ECU map, handling import failures gracefully."""
    if SIMOS85:
        return {
            "Simos8.5  (3.0T TFSI C7 A6/A7)":  SIMOS85,
            "Simos12   (2.0T EA888 Gen1/2)":    SIMOS12,
            "Simos12.2 (2.0T EA888 Gen3)":       SIMOS122,
            "Simos18.1/6 (MQB SC8)":            SIMOS18,
            "Simos18.10 (MQB Evo SCG)":         SIMOS18,
        }
    return {"Simos8.5 (demo)": None}


# ── Shared style helpers ───────────────────────────────────────────────────────

def _label(parent, text, size=11, color=None, **kw):
    return tk.Label(parent, text=text, fg=color or C["muted"],
                    bg=parent["bg"], font=("Menlo", size), **kw)


def _btn(parent, text, cmd, primary=False, **kw):
    fg = "#0d1117" if primary else C["text"]
    bg = C["blue"] if primary else C["btn"]
    abg = "#79b8ff" if primary else C["btn_h"]
    b = tk.Button(parent, text=text, command=cmd,
                  fg=fg, bg=bg, activeforeground=fg, activebackground=abg,
                  font=("Menlo", 11), bd=0, padx=12, pady=5, cursor="hand2",
                  highlightbackground=C["border"], highlightthickness=1, **kw)
    return b


# ─────────────────────────────────────────────────────────────────────────────
# Tooltip — lightweight hover hint for any widget
# ─────────────────────────────────────────────────────────────────────────────

class _Tooltip:
    """Shows a small popup when mouse hovers over a widget."""
    def __init__(self, widget, text, delay=600):
        self._w, self._text, self._delay = widget, text, delay
        self._id = self._win = None
        widget.bind("<Enter>",  self._enter, add="+")
        widget.bind("<Leave>",  self._leave, add="+")
        widget.bind("<Button>", self._leave, add="+")

    def _enter(self, _=None):
        self._id = self._w.after(self._delay, self._show)

    def _leave(self, _=None):
        if self._id:
            self._w.after_cancel(self._id)
            self._id = None
        if self._win:
            self._win.destroy()
            self._win = None

    def _show(self):
        if self._win:
            return
        x = self._w.winfo_rootx() + self._w.winfo_width() // 2
        y = self._w.winfo_rooty() + self._w.winfo_height() + 4
        self._win = tw = tk.Toplevel(self._w)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=C["border"])
        f = tk.Frame(tw, bg=C["surface"], padx=6, pady=3)
        f.pack(padx=1, pady=1)
        tk.Label(f, text=self._text, bg=C["surface"], fg=C["text"],
                 font=("Courier New", 8), justify="left",
                 wraplength=300).pack()


def tip(widget, text):
    """Attach a hover tooltip to any widget."""
    _Tooltip(widget, text)


def _frame(parent, bg=None, **kw):
    return tk.Frame(parent, bg=bg or C["bg"], **kw)


def _section(parent, title):
    """Titled section divider."""
    f = _frame(parent)
    f.pack(fill="x", padx=14, pady=(10, 2))
    tk.Label(f, text=title.upper(), fg=C["dim"], bg=C["bg"],
             font=("Menlo", 9)).pack(side="left")
    tk.Frame(f, bg=C["border"], height=1).pack(side="left", fill="x",
                                                expand=True, padx=(8, 0))
    return f


def _card(parent, **kw):
    return tk.Frame(parent, bg=C["surface"],
                    highlightbackground=C["border"],
                    highlightthickness=1, **kw)


def _scrolled_text(parent, height=8, **kw):
    f = _frame(parent)
    sb = tk.Scrollbar(f, bg=C["surface"])
    sb.pack(side="right", fill="y")
    t = tk.Text(f, height=height, bg=C["bg"], fg=C["text"],
                insertbackground=C["text"], font=("Menlo", 10),
                bd=0, highlightthickness=0, yscrollcommand=sb.set,
                state="disabled", **kw)
    t.pack(side="left", fill="both", expand=True)
    sb.config(command=t.yview)
    return f, t


def _log_widget(parent, height=10, **kw):
    """Create a scrolled log text widget. Returns the tk.Text widget directly."""
    frame, text = _scrolled_text(parent, height=height, **kw)
    frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
    # Tag colours for log entries
    for tag, fg in [("ok",  C["green"]),
                    ("err", C["red"]),
                    ("warn",C["amber"]),
                    ("hdr", C["blue"]),
                    ("dim", C["dim"])]:
        text.tag_config(tag, foreground=fg)
    return text


# ── Tab base ───────────────────────────────────────────────────────────────────

class _Tab(tk.Frame):
    def __init__(self, parent, mw: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self.mw = mw

    def on_connect(self):
        """Called by MainWindow when interface connects."""
        pass

    def on_disconnect(self):
        """Called by MainWindow when interface disconnects."""
        pass

    def _run(self, fn: Callable, *args, **kwargs):
        """Run fn in a daemon thread."""
        threading.Thread(target=fn, args=args, kwargs=kwargs,
                         daemon=True).start()

    def _ui(self, fn: Callable, *args):
        """Schedule fn on the UI thread."""
        self.after(0, fn, *args)

    def _append_log(self, widget: tk.Text, text: str,
                    tag: Optional[str] = None):
        widget.config(state="normal")
        widget.insert("end", text, tag or "")
        widget.see("end")
        widget.config(state="disabled")

    def _clear_log(self, widget: tk.Text):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.config(state="disabled")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Connect
# ─────────────────────────────────────────────────────────────────────────────

class ConnectTab(_Tab):
    def __init__(self, parent, mw):
        super().__init__(parent, mw)

        # ECU selector at top
        ecu_bar = _frame(self)
        ecu_bar.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(ecu_bar, text="ECU  ", fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 11)).pack(side="left")
        self._ecu_var = tk.StringVar()
        self._ecu_map = _ecus()
        names = list(self._ecu_map.keys())
        self._ecu_var.set(names[0])
        om = ttk.Combobox(ecu_bar, textvariable=self._ecu_var,
                          values=names, state="readonly",
                          font=("Menlo", 11), width=38)
        om.pack(side="left")
        om.bind("<<ComboboxSelected>>", self._on_ecu_change)
        self._on_ecu_change()

        # Interface panel fills the rest
        if InterfacePanel:
            self._panel = InterfacePanel(
                self,
                on_connect=self._on_connected,
                on_disconnect=self._on_disconnected,
                ecu=mw.ecu,
            )
            self._panel.pack(fill="both", expand=True, padx=0, pady=(6, 0))
        else:
            tk.Label(self, text="[InterfacePanel not available — check imports]",
                     fg=C["amber"], bg=C["bg"],
                     font=("Menlo", 10)).pack(pady=20)

    def _on_ecu_change(self, *_):
        name = self._ecu_var.get()
        self.mw.ecu = self._ecu_map.get(name)
        self.mw._update_ecu_label(name)

    def _on_connected(self, interface: str, path: str):
        self.mw.interface   = interface
        self.mw.iface_path  = path
        self.mw.connected   = True
        self.mw._on_connected(interface, path)

    def _on_disconnected(self):
        self.mw.connected  = False
        self.mw.interface  = None
        self.mw.iface_path = None
        self.mw._on_disconnected()


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — ECU Info
# ─────────────────────────────────────────────────────────────────────────────

class EcuInfoTab(_Tab):
    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._rows: Dict[str, tk.Label] = {}

        _section(self, "identification DIDs")

        # DID table card
        card = _card(self, padx=14, pady=10)
        card.pack(fill="x", padx=14, pady=4)

        dids = [
            "VIN", "ECU Serial", "Part Number", "SW Version",
            "HW Number", "HW Version", "System Name", "Engine Code",
            "FAZIT", "ASAM File ID", "ASAM File Version",
            "Flash State", "Program Attempts", "Successful Programs",
            "Active Session", "Module Voltage", "Vehicle Mileage",
            "Module Mileage",
        ]
        for i, name in enumerate(dids):
            row = _frame(card, bg=C["surface"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{name:<22}", fg=C["muted"],
                     bg=C["surface"], font=("Menlo", 10),
                     anchor="w").pack(side="left")
            val = tk.Label(row, text="—", fg=C["text"],
                           bg=C["surface"], font=("Menlo", 10),
                           anchor="w")
            val.pack(side="left", fill="x", expand=True)
            self._rows[name] = val

        # Bottom bar
        bot = _frame(self)
        bot.pack(fill="x", padx=14, pady=8)
        self._read_btn = _btn(bot, "read ECU info", self._do_read,
                              primary=True, state="disabled")
        self._read_btn.pack(side="left")
        tip(self._read_btn, 'Read calibration block from ECU.\nRequires extended session + SA2 unlock.')
        self._status = tk.Label(bot, text="not connected",
                                fg=C["dim"], bg=C["bg"],
                                font=("Menlo", 10))
        self._status.pack(side="left", padx=12)

        # ── VIN utility section ───────────────────────────────────────────────
        _section(self, "VIN utility")

        vin_card = _card(self, padx=14, pady=10)
        vin_card.pack(fill="x", padx=14, pady=4)

        # VIN status banner
        self._vin_status_var = tk.StringVar(value="read ECU info to populate")
        tk.Label(vin_card, textvariable=self._vin_status_var,
                 fg=C["muted"], bg=C["surface"],
                 font=("Menlo", 10), anchor="w").pack(fill="x", pady=(0, 6))

        # Row 1 — chassis VIN entry + compare
        row1 = _frame(vin_card, bg=C["surface"])
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="chassis VIN", fg=C["muted"],
                 bg=C["surface"], font=("Menlo", 10), width=14,
                 anchor="w").pack(side="left")
        self._chassis_vin_var = tk.StringVar()
        self._chassis_entry = tk.Entry(
            row1, textvariable=self._chassis_vin_var,
            bg=C["bg"], fg=C["text"], insertbackground=C["text"],
            font=("Menlo", 11), width=20, bd=0,
            highlightbackground=C["border"], highlightthickness=1)
        self._chassis_entry.pack(side="left", padx=4)
        tip(self._chassis_entry,
            "Enter the 17-char VIN from your V5C / dashboard sticker.\n"
            "Used to compare against what's stored in the ECU.")
        self._compare_btn = _btn(row1, "compare", self._do_compare_vin,
                                 state="disabled")
        self._compare_btn.pack(side="left", padx=(4, 0))
        tip(self._compare_btn,
            "Compare chassis VIN vs ECU-stored VIN.\n"
            "Flags JHM or tuner VIN-lock mismatches.")

        # Row 2 — write VIN
        row2 = _frame(vin_card, bg=C["surface"])
        row2.pack(fill="x", pady=(6, 0))
        tk.Label(row2, text="write VIN", fg=C["muted"],
                 bg=C["surface"], font=("Menlo", 10), width=14,
                 anchor="w").pack(side="left")
        self._write_vin_var = tk.StringVar()
        self._write_vin_entry = tk.Entry(
            row2, textvariable=self._write_vin_var,
            bg=C["bg"], fg=C["amber"], insertbackground=C["text"],
            font=("Menlo", 11), width=20, bd=0,
            highlightbackground=C["border"], highlightthickness=1)
        self._write_vin_entry.pack(side="left", padx=4)
        self._write_vin_btn = _btn(row2, "write to ECU", self._do_write_vin,
                                   state="disabled")
        self._write_vin_btn.pack(side="left", padx=(4, 0))
        tip(self._write_vin_btn,
            "Write VIN to ECU DID 0xF190.\n"
            "Requires extended session + SA2 unlock.\n"
            "Use after reflashing with a tune from a different VIN,\n"
            "or to correct a JHM VIN-locked tune.\n"
            "Value is read back and verified automatically.")

        # Auto-fill button — copies ECU VIN into write field
        self._autofill_btn = _btn(row2, "↑ from ECU", self._autofill_write_vin,
                                   state="disabled")
        self._autofill_btn.pack(side="left", padx=(4, 0))
        tip(self._autofill_btn, "Copy ECU VIN into write field.")

        # VIN result card
        self._vin_result_var = tk.StringVar(value="")
        self._vin_result_lbl = tk.Label(
            vin_card, textvariable=self._vin_result_var,
            fg=C["muted"], bg=C["surface"],
            font=("Menlo", 9), justify="left", anchor="w", wraplength=600)
        self._vin_result_lbl.pack(fill="x", pady=(6, 0))

    def on_connect(self):
        self._read_btn.config(state="normal")
        self._status.config(text="connected — press read", fg=C["green"])
        self._write_vin_btn.config(state="normal")
        self._compare_btn.config(state="normal")

    def on_disconnect(self):
        self._read_btn.config(state="disabled")
        self._status.config(text="not connected", fg=C["dim"])
        self._write_vin_btn.config(state="disabled")
        self._compare_btn.config(state="disabled")
        self._autofill_btn.config(state="disabled")
        for v in self._rows.values():
            v.config(text="—", fg=C["text"])
        self._vin_status_var.set("read ECU info to populate")
        self._vin_result_var.set("")

    def _do_read(self):
        if not self.mw.connected:
            return
        self._read_btn.config(state="disabled")
        self._status.config(text="reading...", fg=C["amber"])
        self._run(self._read_task)

    def _read_task(self):
        try:
            info = read_ecu_info(
                self.mw.ecu,
                interface=self.mw.interface,
                interface_path=self.mw.iface_path,
            )
            self._ui(self._show_info, info)
        except Exception as e:
            self._ui(self._show_error, str(e))

    def _show_info(self, info: Dict[str, str]):
        for name, val in info.items():
            if name in self._rows:
                err = val.startswith("<") and val.endswith(">")
                self._rows[name].config(
                    text=val,
                    fg=C["red"] if err else C["green"])
        self._read_btn.config(state="normal")
        self._status.config(text="read complete", fg=C["green"])

        # Populate VIN utility
        ecu_vin = info.get("VIN", "").strip()
        if ecu_vin and not (ecu_vin.startswith("<") and ecu_vin.endswith(">")):
            self._vin_status_var.set(f"ECU VIN: {ecu_vin}")
            self._write_vin_var.set(ecu_vin)
            self._autofill_btn.config(state="normal")
        else:
            self._vin_status_var.set("VIN not readable from ECU")

    def _show_error(self, msg: str):
        self._read_btn.config(state="normal")
        self._status.config(text=f"error: {msg}", fg=C["red"])
        import logging; logging.getLogger("SimosSuite.GUI").error("ECU info error: %s", msg)

    # ── VIN utility methods ────────────────────────────────────────────────

    def _autofill_write_vin(self):
        """Copy current ECU VIN row into the write field."""
        ecu_vin = self._rows.get("VIN")
        if ecu_vin:
            vin = ecu_vin.cget("text").strip()
            if vin and vin != "—":
                self._write_vin_var.set(vin)
                self._chassis_vin_var.set(vin)

    def _do_compare_vin(self):
        """Compare ECU VIN vs chassis VIN entered in the field."""
        ecu_vin_lbl = self._rows.get("VIN")
        ecu_vin = (ecu_vin_lbl.cget("text") if ecu_vin_lbl else "").strip()
        chassis_vin = self._chassis_vin_var.get().strip().upper()

        if not ecu_vin or ecu_vin == "—":
            self._vin_result_var.set("⚠ Read ECU info first to get ECU VIN")
            self._vin_result_lbl.config(fg=C["amber"])
            return
        if not chassis_vin:
            self._vin_result_var.set("⚠ Enter your chassis VIN first")
            self._vin_result_lbl.config(fg=C["amber"])
            return

        try:
            from flasher.vin_utils import compare_vin, validate_vin
            chassis_vin = validate_vin(chassis_vin)
            result = compare_vin(ecu_vin, chassis_vin)
            color = C["green"] if result["match"] else C["red"]
            self._vin_result_var.set("\n".join(result["notes"]))
            self._vin_result_lbl.config(fg=color)
        except Exception as e:
            self._vin_result_var.set(f"error: {e}")
            self._vin_result_lbl.config(fg=C["red"])

    def _do_write_vin(self):
        """Write VIN to ECU DID 0xF190 (SA2 required)."""
        if not self.mw.connected:
            return
        vin_raw = self._write_vin_var.get().strip()
        if not vin_raw:
            self._vin_result_var.set("⚠ Enter the VIN to write")
            self._vin_result_lbl.config(fg=C["amber"])
            return
        try:
            from flasher.vin_utils import validate_vin
            vin = validate_vin(vin_raw)
        except Exception as e:
            self._vin_result_var.set(f"⚠ {e}")
            self._vin_result_lbl.config(fg=C["amber"])
            return

        if not messagebox.askyesno(
            "Write VIN",
            f"Write VIN to ECU DID 0xF190?\n\n"
            f"  VIN: {vin}\n\n"
            "Requires SA2 security access unlock.\n"
            "The ECU must be connected and in key-on state.\n\n"
            "Continue?",
            icon="warning",
        ):
            return

        self._write_vin_btn.config(state="disabled")
        self._vin_result_var.set(f"writing {vin}...")
        self._vin_result_lbl.config(fg=C["amber"])
        self._run(self._write_vin_task, vin)

    def _write_vin_task(self, vin: str):
        try:
            from flasher.vin_utils import write_vin
            write_vin(
                self.mw.ecu,
                vin,
                interface=self.mw.interface,
                interface_path=self.mw.iface_path,
                ble_bridge=getattr(self.mw, "ble_bridge", None),
            )
            self._ui(self._write_vin_done, vin)
        except Exception as e:
            self._ui(self._write_vin_error, str(e))

    def _write_vin_done(self, vin: str):
        self._write_vin_btn.config(state="normal")
        self._vin_result_var.set(f"✓ VIN written and verified: {vin}")
        self._vin_result_lbl.config(fg=C["green"])
        self._vin_status_var.set(f"ECU VIN: {vin}  (just written)")
        # Update the VIN row in the info grid
        if "VIN" in self._rows:
            self._rows["VIN"].config(text=vin, fg=C["green"])

    def _write_vin_error(self, msg: str):
        self._write_vin_btn.config(state="normal")
        self._vin_result_var.set(f"✗ {msg}")
        self._vin_result_lbl.config(fg=C["red"])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — Flash
# ─────────────────────────────────────────────────────────────────────────────

class FlashTab(_Tab):
    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._cal_bytes: Optional[bytes] = None
        self._cal_path  = tk.StringVar(value="no file loaded")

        # Multi-block file slots  {block_num: (bytes|None, StringVar)}
        self._block_files: dict = {
            1: [None, tk.StringVar(value="not loaded")],  # CBOOT
            2: [None, tk.StringVar(value="not loaded")],  # ASW1
            3: [None, tk.StringVar(value="not loaded")],  # CAL
        }

        # ── Flash disabled banner ─────────────────────────────────────────
        flash_warn = tk.Frame(self, bg="#3d2800", padx=12, pady=8,
                              highlightbackground="#d29922",
                              highlightthickness=1)
        flash_warn.pack(fill="x", padx=14, pady=(8, 2))
        tk.Label(flash_warn,
                 text="FLASH WRITES DISABLED",
                 fg="#d29922", bg="#3d2800",
                 font=("Courier New", 11, "bold")).pack(anchor="w")
        tk.Label(flash_warn,
                 text="ECU flash writes are disabled until connection validation\n"
                      "is confirmed on real hardware. Read and verify are safe\n"
                      "and remain enabled.",
                 fg="#b08520", bg="#3d2800",
                 font=("Courier New", 9), justify="left").pack(anchor="w")

        _section(self, "CAL block")

        # File row
        file_card = _card(self, padx=12, pady=10)
        file_card.pack(fill="x", padx=14, pady=4)
        fr = _frame(file_card, bg=C["surface"])
        fr.pack(fill="x")
        _btn(fr, "open .bin", self._open_file).pack(side="left")
        _btn(fr, "open .frf", self._open_frf).pack(side="left", padx=(6, 0))
        tk.Label(fr, textvariable=self._cal_path,
                 fg=C["muted"], bg=C["surface"],
                 font=("Menlo", 10)).pack(side="left", padx=10)

        self._file_info = tk.Label(file_card, text="",
                                   fg=C["muted"], bg=C["surface"],
                                   font=("Menlo", 10), anchor="w")
        self._file_info.pack(fill="x", pady=(4, 0))

        _section(self, "operations")

        ops_card = _card(self, padx=12, pady=10)
        ops_card.pack(fill="x", padx=14, pady=4)

        op_row = _frame(ops_card, bg=C["surface"])
        op_row.pack(fill="x", pady=(0, 8))

        self._read_btn = _btn(op_row, "read CAL from ECU",
                              self._do_read_cal, state="disabled")
        self._read_btn.pack(side="left", padx=(0, 8))

        self._write_btn = _btn(op_row, "write CAL to ECU",
                               self._do_write_cal, primary=True,
                               state="disabled")
        self._write_btn.pack(side="left", padx=(0, 8))
        tip(self._write_btn, 'DISABLED — Flash writes are disabled until\nconnection validation is confirmed on hardware.\nRead operations are safe and enabled.')

        self._verify_btn = _btn(op_row, "verify checksum",
                                self._do_verify, state="disabled")
        self._verify_btn.pack(side="left")

        # Dry run toggle
        self._dry_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ops_card, text="dry run (no write)",
                       variable=self._dry_var,
                       fg=C["muted"], bg=C["surface"],
                       selectcolor=C["bg"],
                       activebackground=C["surface"],
                       activeforeground=C["text"],
                       font=("Menlo", 10)).pack(anchor="w")

        _section(self, "full flash (multi-block)")

        mb_card = _card(self, padx=12, pady=10)
        mb_card.pack(fill="x", padx=14, pady=4)

        tk.Label(mb_card, text="Load individual blocks to flash. Leave unloaded to skip.",
                 fg=C["muted"], bg=C["surface"], font=("Menlo", 9),
                 anchor="w").pack(fill="x", pady=(0, 6))

        BLOCK_LABELS = {1: "CBOOT (block 1)", 2: "ASW1  (block 2)", 3: "CAL   (block 3)"}
        for bnum, blabel in BLOCK_LABELS.items():
            row = _frame(mb_card, bg=C["surface"])
            row.pack(fill="x", pady=2)
            _btn(row, f"open {blabel.split()[0].lower()}",
                 lambda n=bnum: self._open_block_file(n)).pack(side="left")
            tk.Label(row, textvariable=self._block_files[bnum][1],
                     fg=C["muted"], bg=C["surface"],
                     font=("Menlo", 9)).pack(side="left", padx=8)

        mb_btn_row = _frame(mb_card, bg=C["surface"])
        mb_btn_row.pack(fill="x", pady=(8, 0))
        self._flash_blocks_btn = _btn(
            mb_btn_row, "flash loaded blocks",
            self._do_flash_blocks, primary=True, state="disabled")
        self._flash_blocks_btn.pack(side="left")
        tip(self._flash_blocks_btn,
            "Flash all loaded blocks in correct order (CBOOT→ASW1→CAL).\n"
            "Blocks marked 'not loaded' are skipped.\n"
            "Requires SA2 unlock. Do not interrupt once started.")

        _section(self, "progress")

        prog_card = _card(self, padx=12, pady=10)
        prog_card.pack(fill="x", padx=14, pady=4)

        self._prog_label = tk.Label(prog_card, text="idle",
                                    fg=C["muted"], bg=C["surface"],
                                    font=("Menlo", 10), anchor="w")
        self._prog_label.pack(fill="x")

        self._prog_bar_frame = tk.Frame(prog_card, bg=C["border"],
                                        height=6)
        self._prog_bar_frame.pack(fill="x", pady=(6, 0))
        self._prog_bar = tk.Frame(self._prog_bar_frame,
                                  bg=C["progress"], height=6, width=0)
        self._prog_bar.place(x=0, y=0, relheight=1.0)

        # Log
        _section(self, "log")
        log_outer, self._log = _scrolled_text(self, height=6)
        log_outer.pack(fill="both", expand=True, padx=14, pady=4)
        self._log.tag_config("ok",  foreground=C["green"])
        self._log.tag_config("err", foreground=C["red"])
        self._log.tag_config("dim", foreground=C["muted"])

    def on_connect(self):
        self._read_btn.config(state="normal")
        if self._cal_bytes:
            # Write is DISABLED until connection validation is hardware-confirmed
            # self._write_btn.config(state="normal")
            self._verify_btn.config(state="normal")
        self._refresh_flash_blocks_btn()

    def on_disconnect(self):
        for b in (self._read_btn, self._write_btn, self._verify_btn):
            b.config(state="disabled")

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open CAL .bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:
                self._cal_bytes = f.read()
            fname = os.path.basename(path)
            self._cal_path.set(fname)
            sz = len(self._cal_bytes)
            self._file_info.config(
                text=f"{sz:,} bytes  ({sz/1024:.1f} KB)",
                fg=C["green"])
            if self.mw.connected:
                # Write disabled — pending hardware validation
                self._verify_btn.config(state="normal")
            self._log_line(f"loaded {fname}  ({sz:,} bytes)\n", "ok")
        except Exception as e:
            messagebox.showerror("File error", str(e))

    def _open_frf(self):
        """Load a CAL block from a decrypted Flashdaten FRF file."""
        path = filedialog.askopenfilename(
            title="Open Flashdaten FRF",
            filetypes=[("FRF files", "*.frf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            from flasher.frf_loader import FrfLoader
            key_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "frf.key")
            loader = FrfLoader(key_path=key_path if os.path.exists(key_path) else None)
            blocks = loader.extract_blocks(path)

            # Pick the CAL block — block 3 for Simos8.5, or highest numbered block
            ecu = self.mw.ecu
            if ecu and ecu.cal_block:
                block_num = ecu.cal_block.number  # e.g. 3
            else:
                # Fallback: highest numbered block (CAL is typically last)
                block_num = max(blocks.keys())

            if block_num not in blocks:
                messagebox.showerror("FRF error",
                    f"Block {block_num} not found in FRF.\nAvailable: {list(blocks.keys())}")
                return

            self._cal_bytes = blocks[block_num]
            fname = os.path.basename(path)
            self._cal_path.set(f"{fname} [block {block_num}]")
            sz = len(self._cal_bytes)
            self._file_info.config(text=f"{sz:,} bytes", fg=C["green"])
            if self.mw.connected:
                # Write disabled — pending hardware validation
                self._verify_btn.config(state="normal")
            self._log_line(
                f"loaded {fname} [block {block_num}]  ({sz:,} bytes)\n",
                "ok")
        except FileNotFoundError as e:
            messagebox.showerror("FRF key missing",
                f"Cannot decrypt FRF: {e}\n\nPlace data/frf.key in the simos-suite directory.")
        except Exception as e:
            messagebox.showerror("FRF error", str(e))

    def _do_read_cal(self):
        """
        Read the CAL block from the ECU via UDS ReadMemoryByAddress,
        then hand the bytes to the Tune tab automatically.
        """
        if not self.mw.connected:
            messagebox.showwarning("Not connected", "Connect an interface first.")
            return
        ecu = self.mw.ecu
        if not ecu or not ecu.cal_block:
            messagebox.showwarning("No ECU", "Select an ECU with a CAL block first.")
            return

        self._set_buttons(False)
        self._log_line("reading CAL block from ECU...\n", "dim")
        self._run(self._read_task)

    def _read_task(self):
        from flasher.uds_flash import read_block, FlashProgress

        def cb(p):
            self._ui(self._update_progress, p)

        ecu = self.mw.ecu
        blk = ecu.cal_block

        try:
            # read_block() handles: extended session → programming session →
            # SA2 unlock → RequestUpload → TransferData → XOR decrypt → LZSS decompress
            cal_bytes = read_block(
                ecu            = ecu,
                block_num      = blk.number,
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                callback       = cb,
            )
            if cal_bytes is None:
                self._ui(self._flash_error, "read_block returned None — see log")
                return
            self._ui(self._read_done, cal_bytes, ecu.name)
        except Exception as e:
            self._ui(self._flash_error, str(e))

    def _read_done(self, cal_bytes: bytes, ecu_name: str):
        self._set_buttons(True)
        self._cal_bytes = cal_bytes
        sz = len(cal_bytes)
        self._cal_path.set(f"ECU read  ({sz:,} bytes)")
        self._file_info.config(
            text=f"read from {ecu_name}  ({sz/1024:.1f} KB)",
            fg=C["green"])
        if self.mw.connected:
            # Write disabled — pending hardware validation
            self._verify_btn.config(state="normal")
        self._log_line(f"read OK — {sz:,} bytes\n", "ok")
    def _do_write_cal(self):
        if not self._cal_bytes:
            messagebox.showwarning("No file", "Load a CAL .bin first.")
            return
        if not messagebox.askyesno(
                "Confirm flash",
                f"Write CAL to {self.mw.ecu.name if self.mw.ecu else 'ECU'}?\n\n"
                "This will erase and reprogram the calibration block.\n"
                "Ensure the vehicle is on a battery charger."):
            return
        self._set_buttons(False)
        self._run(self._write_task)

    def _write_task(self):
        def cb(p: "FlashProgress"):
            self._ui(self._update_progress, p)

        try:
            ok = flash_cal(
                ecu           = self.mw.ecu,
                cal_bytes     = self._cal_bytes,
                interface     = self.mw.interface,
                interface_path= self.mw.iface_path,
                callback      = cb,
                dry_run       = self._dry_var.get(),
            )
            self._ui(self._flash_done, ok)
        except Exception as e:
            self._ui(self._flash_error, str(e))

    def _do_verify(self):
        self._set_buttons(False)
        self._run(self._verify_task)

    def _verify_task(self):
        def cb(p: "FlashProgress"):
            self._ui(self._update_progress, p)
        try:
            ok = flash_cal(
                ecu           = self.mw.ecu,
                cal_bytes     = self._cal_bytes or b"",
                interface     = self.mw.interface,
                interface_path= self.mw.iface_path,
                callback      = cb,
                verify_only   = True,
            )
            self._ui(self._flash_done, ok)
        except Exception as e:
            self._ui(self._flash_error, str(e))

    def _update_progress(self, p: "FlashProgress"):
        color = {"DONE": C["green"], "ERROR": C["red"],
                 "CONNECT": C["blue"]}.get(p.step, C["amber"])
        self._prog_label.config(
            text=f"[{p.step}] {p.message}", fg=color)
        # Resize bar
        total = self._prog_bar_frame.winfo_width()
        w = max(0, int(total * p.pct / 100))
        self._prog_bar.config(width=w)
        self._log_line(f"[{p.step}] {p.message}\n",
                       "ok" if p.step == "DONE" else
                       "err" if p.step == "ERROR" else "dim")

    def _flash_done(self, ok: bool):
        self._set_buttons(True)
        if ok:
            self._prog_label.config(text="done", fg=C["green"])
            # Auto-read DTCs after a successful flash to surface any codes set during programming
            self._run(self._post_flash_dtc_task)
        else:
            self._prog_label.config(text="failed", fg=C["red"])

    def _post_flash_dtc_task(self):
        """Read DTCs after flash and log them. Non-fatal — errors are logged only."""
        try:
            from flasher.uds_flash import read_dtcs
            dtcs = read_dtcs(
                ecu            = self.mw.ecu,
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
            )
            def _show(dtcs=dtcs):
                if not dtcs or list(dtcs.keys()) == ["ERROR"]:
                    self._log_line("post-flash DTC read: none or error\n", "dim")
                else:
                    self._log_line(f"post-flash DTCs ({len(dtcs)}): " +
                                   ", ".join(dtcs.keys()) + "\n",
                                   "err" if dtcs else "ok")
            self._ui(_show)
        except Exception as e:
            self._ui(lambda: self._log_line(f"DTC read after flash: {e}\n", "dim"))

    def _flash_error(self, msg: str):
        self._set_buttons(True)
        self._log_line(f"exception: {msg}\n", "err")
        self._prog_label.config(text=f"error: {msg}", fg=C["red"])
        import logging; logging.getLogger("SimosSuite.GUI").error("Flash error: %s", msg)

    def _open_block_file(self, block_num: int):
        labels = {1: "CBOOT", 2: "ASW1", 3: "CAL"}
        path = filedialog.askopenfilename(
            title=f"Open {labels.get(block_num, 'block')} .bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            fname = os.path.basename(path)
            self._block_files[block_num][0] = data
            self._block_files[block_num][1].set(f"{fname}  ({len(data):,} B)")
            self._log_line(
                f"block {block_num} ({labels.get(block_num)}): {fname} "
                f"({len(data):,} bytes)\n", "ok")
            # Enable flash button if at least one block is loaded and connected
            self._refresh_flash_blocks_btn()
        except Exception as e:
            messagebox.showerror("File error", str(e))

    def _refresh_flash_blocks_btn(self):
        any_loaded = any(v[0] is not None for v in self._block_files.values())
        state = "normal" if (any_loaded and self.mw.connected) else "disabled"
        self._flash_blocks_btn.config(state=state)

    def _do_flash_blocks(self):
        loaded = {n: v[0] for n, v in self._block_files.items() if v[0] is not None}
        if not loaded:
            messagebox.showwarning("No files", "Load at least one block file first.")
            return
        names = {1: "CBOOT", 2: "ASW1", 3: "CAL"}
        block_list = ", ".join(names.get(n, str(n)) for n in sorted(loaded))
        if not messagebox.askyesno(
                "Confirm flash",
                f"Flash blocks: {block_list}\n\n"
                f"Target: {self.mw.ecu.name if self.mw.ecu else 'ECU'}\n\n"
                "This will erase and reprogram the selected blocks.\n"
                "Ensure the vehicle is on a battery charger.\n\n"
                "Continue?"):
            return
        self._set_buttons(False)
        self._run(self._flash_blocks_task)

    def _flash_blocks_task(self):
        def cb(p: "FlashProgress"):
            self._ui(self._update_progress, p)
        try:
            loaded = {n: v[0] for n, v in self._block_files.items() if v[0] is not None}
            ok = flash_blocks(
                ecu            = self.mw.ecu,
                blocks         = loaded,
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                callback       = cb,
                dry_run        = self._dry_var.get(),
            )
            self._ui(self._flash_done, ok)
        except Exception as e:
            self._ui(self._flash_error, str(e))

    def _set_buttons(self, enabled: bool):
        s = "normal" if enabled else "disabled"
        if self.mw.connected:
            self._read_btn.config(state=s)
        if self._cal_bytes and self.mw.connected:
            self._write_btn.config(state=s)
            self._verify_btn.config(state=s)
        self._refresh_flash_blocks_btn()

    def _log_line(self, text: str, tag: str = ""):
        self._append_log(self._log, text, tag)


class LoggerTab(_Tab):
    DIDS = [
        (0xF190, "VIN"),
        (0x295A, "Mileage km"),
        (0x295B, "Module km"),
        (0xF442, "Battery V"),
        (0x2000, "RPM"),
        (0x2001, "Boost kPa"),
        (0x2002, "MAF g/s"),
        (0x2003, "IAT °C"),
        (0x2004, "Lambda"),
        (0x2005, "Inj pw ms"),
        (0x2006, "Throttle %"),
        (0x2007, "Torque Nm"),
    ]

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._running = False
        self._values: Dict[int, tk.StringVar] = {}

        _section(self, "live channels")

        gauges = _card(self, padx=12, pady=10)
        gauges.pack(fill="x", padx=14, pady=4)

        # 3-column gauge grid — kept as an attr so the gauges can be rebuilt
        # when the preset changes (see _build_gauges / _on_preset_change).
        self._gauge_grid = _frame(gauges, bg=C["surface"])
        self._gauge_grid.pack(fill="x")

        # Build the initial gauges from the default preset's channel set so the
        # visible gauges, self._values and self._active_channels stay in sync.
        self._active_channels = self._resolve_preset_channels(
            self._preset_default())
        self._build_gauges(self._active_channels)

        _section(self, "poll settings")

        cfg_row = _frame(self)
        cfg_row.pack(fill="x", padx=14, pady=4)

        # Channel preset selector
        tk.Label(cfg_row, text="preset", fg=C["muted"],
                 bg=C["bg"], font=("Menlo", 10)).pack(side="left")
        self._preset_var = tk.StringVar(value="essential")
        preset_combo = ttk.Combobox(
            cfg_row, textvariable=self._preset_var,
            values=["essential", "fuel", "boost", "ignition", "lean diag", "full"],
            state="readonly", font=("Menlo", 10), width=12)
        preset_combo.pack(side="left", padx=(4, 16))
        preset_combo.bind("<<ComboboxSelected>>", self._on_preset_change)

        tk.Label(cfg_row, text="interval ms", fg=C["muted"],
                 bg=C["bg"], font=("Menlo", 10)).pack(side="left")
        self._interval_var = tk.IntVar(value=200)
        tk.Spinbox(cfg_row, from_=50, to=5000, increment=50,
                   textvariable=self._interval_var, width=6,
                   bg=C["surface"], fg=C["text"],
                   font=("Menlo", 10), bd=0).pack(side="left", padx=8)

        _section(self, "log output")
        log_outer, self._log = _scrolled_text(self, height=8)
        log_outer.pack(fill="both", expand=True, padx=14, pady=4)
        self._log.tag_config("val", foreground=C["blue"])
        self._log.tag_config("err", foreground=C["red"])
        self._log.tag_config("dim", foreground=C["muted"])

        # Controls
        bot = _frame(self)
        bot.pack(fill="x", padx=14, pady=6)
        self._start_btn = _btn(bot, "start logging",
                               self._toggle_log, primary=True,
                               state="disabled")
        self._start_btn.pack(side="left")
        tip(self._start_btn, 'Start live DID polling. Data displays in gauges and exports to CSV.\nECU must be in extended diagnostic session.')
        _btn(bot, "clear log", lambda: self._clear_log(self._log)
             ).pack(side="left", padx=8)
        _btn(bot, "save CSV", self._save_csv).pack(side="left")

        self._csv_rows: List[str] = []

    def on_connect(self):
        self._start_btn.config(state="normal")

    def on_disconnect(self):
        self._running = False
        self._start_btn.config(text="start logging", state="disabled",
                               fg="#0d1117", bg=C["blue"])
        for v in self._values.values():
            v.set("—")

    @staticmethod
    def _preset_default() -> str:
        """Default preset name (matches the combobox initial value)."""
        return "essential"

    def _resolve_preset_channels(self, preset: str) -> List["Channel"]:
        """
        Resolve a preset name to its list of Channel objects.

        Tries the per-preset channel tables in logger.channels_s85; if that
        module isn't present, falls back to the full SIMOS85_CHANNELS set so
        the logger still has channels to poll/display.
        """
        try:
            from logger.channels_s85 import (
                CHANNELS_ESSENTIAL, CHANNELS_FUEL, CHANNELS_BOOST,
                CHANNELS_IGNITION, CHANNELS_LEAN_DIAG, CHANNELS_FULL,
            )
            _map = {
                "essential": CHANNELS_ESSENTIAL,
                "fuel":      CHANNELS_FUEL,
                "boost":     CHANNELS_BOOST,
                "ignition":  CHANNELS_IGNITION,
                "lean diag": CHANNELS_LEAN_DIAG,
                "full":      CHANNELS_FULL,
            }
            channels = _map.get(preset, CHANNELS_ESSENTIAL)
            if channels:
                return list(channels)
        except ImportError:
            pass
        from logger import SIMOS85_CHANNELS
        return list(SIMOS85_CHANNELS)

    def _build_gauges(self, channels: List["Channel"]):
        """
        (Re)build the live-gauge grid for the given channel set.

        Tears down any existing gauge widgets and recreates one gauge per
        channel, each bound to a fresh StringVar in self._values, so the
        on-screen gauges always track the current preset's channels. The
        poll loop updates self._values[ch.did], which these labels display.
        """
        # Destroy the previous preset's gauge widgets.
        for child in self._gauge_grid.winfo_children():
            child.destroy()

        # Fresh StringVars for the new channel set.
        self._values = {}
        for i, ch in enumerate(channels):
            col_frame = _frame(self._gauge_grid, bg=C["surface"])
            col_frame.grid(row=i // 3, column=i % 3, padx=6, pady=4,
                           sticky="w")
            tk.Label(col_frame, text=f"{ch.name:<14}", fg=C["muted"],
                     bg=C["surface"], font=("Menlo", 9)).pack(side="left")
            var = tk.StringVar(value="—")
            self._values[ch.did] = var
            tk.Label(col_frame, textvariable=var, fg=C["blue"],
                     bg=C["surface"], font=("Menlo", 10, "bold"),
                     width=10, anchor="e").pack(side="left")

    def _on_preset_change(self, *_):
        """Rebuild gauge grid when preset changes."""
        preset = self._preset_var.get()
        self._active_channels = self._resolve_preset_channels(preset)
        # Rebuild the visible gauges so they track the new channel set
        # (and so the gauge count matches this preset).
        self._build_gauges(self._active_channels)

    def _toggle_log(self):
        if self._running:
            self._running = False
            self._start_btn.config(text="start logging",
                                   fg="#0d1117", bg=C["blue"])
        else:
            self._running = True
            self._start_btn.config(text="stop logging",
                                   fg=C["text"], bg=C["btn"])
            # Apply current preset if not already set
            if not hasattr(self, "_active_channels"):
                self._on_preset_change()
            self._csv_rows = ["timestamp," + ",".join(ch.name for ch in self._active_channels)]
            self._run(self._poll_loop)

    def _poll_loop(self):
        """Real LogSession-backed poll loop using logger.LogSession."""
        from logger import LogSession, SIMOS85_CHANNELS, Channel

        # Use preset channels if selected, otherwise fall back to gauge DID list
        if hasattr(self, "_active_channels") and self._active_channels:
            channels = list(self._active_channels)
        else:
            did_map = {did: name for did, name in self.DIDS}
            channels = [ch for ch in SIMOS85_CHANNELS if ch.did in did_map]
            known_dids = {ch.did for ch in channels}
            for did, name in self.DIDS:
                if did not in known_dids:
                    channels.append(Channel(did, name, "", 1.0, 0.0, 2, False, "{:.2f}"))

        session = LogSession(
            ecu         = self.mw.ecu,
            interface   = self.mw.interface,
            iface_path  = self.mw.iface_path,
            ble_bridge  = getattr(self.mw, "ble_bridge", None),
            channels    = channels,
            interval_ms = self._interval_var.get(),
        )

        self._log_session = session

        def on_row(row):
            if not self._running:
                return
            ts = row.wall_time
            parts = []
            for ch in channels:
                v = row.values.get(ch.did)
                display = ch.format(v)
                # Update gauge var if it exists
                if ch.did in self._values:
                    self._ui(self._values[ch.did].set, display)
                if v is not None and len(parts) < 6:
                    parts.append(f"{ch.name[:7]}={display}")
            line = f"{ts}  " + "  ".join(parts) + "\n"
            self._ui(self._append_log, self._log, line, "val")
            vals = ",".join(ch.format(row.values.get(ch.did)) for ch in channels)
            self._csv_rows.append(f"{ts},{vals}")

        session.start(callback=on_row)

        # Wait until stopped
        while self._running and self.mw.connected and not session.error:
            time.sleep(0.1)

        session.stop()

        if session.error:
            self._ui(self._append_log, self._log,
                     f"error: {session.error}\n", "err")

        self._ui(self._start_btn.config, text="start logging",
                 fg="#0d1117", bg=C["blue"])
        self._running = False

    def _save_csv(self):
        session = getattr(self, "_log_session", None)
        if session and session.rows():
            path = filedialog.asksaveasfilename(
                title="Save log CSV",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")],
            )
            if path:
                n = session.save_csv(path)
                messagebox.showinfo("Saved",
                    f"{n:,} rows saved\n{os.path.basename(path)}")
            return
        if not self._csv_rows:
            messagebox.showinfo("Nothing to save", "Start logging first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save log CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if path:
            with open(path, "w") as f:
                f.write("\n".join(self._csv_rows))
            messagebox.showinfo("Saved", f"Log saved: {os.path.basename(path)}")


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# TAB 6 — CP Tools
# ─────────────────────────────────────────────────────────────────────────────

# All C7 VAG modules that participate in Component Protection
# Protocol source: ODIS session log odis-cp-session-wauggafc7dn120188.md
# UDS modules: J255, J285, J234, J794 — reachable via ISO-TP on Diagnostic CAN
# KWP2000 modules: J518, J519, J136, J521, J393 — KWP on Convenience/K-Line sub-bus
#   Cannot scan KWP modules via UDS/ISO-TP — Mongoose will hang waiting for ISO-TP
#   frame that never comes (KWP uses different framing).
#   IKA key on KWP modules is written via TrainICA/TrainGVA KWP services in ODIS.
#   For our tool: read IKA from UDS modules only; KWP modules noted as "KWP2000".
# (display name,            addr,  tx_id, rx_id,  protocol)
CP_MODULES = [
    ("J533  Gateway",            "01",  0x710, 0x77A,  "UDS"),    # gateway — skip (constellation)
    ("J255  Climatronic",        "08",  0x746, 0x7B0,  "UDS"),    # UDS — scan ✓
    ("J285  Instruments",        "17",  0x714, 0x77E,  "UDS"),    # UDS — scan ✓
    ("J234  Airbag",             "15",  0x715, 0x77F,  "UDS"),    # UDS — scan ✓
    ("J794  MMI",                "5F",  0x773, 0x7DD,  "UDS"),    # UDS — scan ✓
    ("J136  Mem.Seat Driver",    "36",  0x74C, 0x7B6,  "KWP"),    # KWP2000 — skip
    ("J521  Mem.Seat Pass.",     "06",  0x74D, 0x7B7,  "KWP"),    # KWP2000 — skip
    ("J518  KESSY",              "03",  0x732, 0x79C,  "KWP"),    # KWP2000 — skip
    ("J519  Body Elect.",        "09",  0x70E, 0x778,  "KWP"),    # KWP2000 — skip
    ("J393  Central Comfort",    "46",  0x70D, 0x777,  "KWP"),    # KWP2000 — skip
]

# Known IKA key blob — Feb 2024 ODIS session, J136, VIN WAUGGA**********8
KNOWN_IKA_BLOB = bytes.fromhex(
    "E62B41D11C44AF202177FB1F274B0AC2"
    "D15BD262E4FD27AB61D123C2F15A2C93"
    "2600"
)

# Constellation DID known values
CONST_DID       = 0x04A3
IKA_DID         = 0x00BE
CP_ROUTINE_ID   = 0x0226


class CPToolsTab(_Tab):
    """
    CP Tools tab — Scan all modules for Component Protection status,
    identify CP-afflicted modules, write IKA keys, update J533 constellation.

    Workflow:
      1. ⟳ Scan All Modules  — reads DID 0x00BE from each module,
                                marks CP active (all-zeros) vs cleared
      2. Checkboxes           — select which modules to fix
      3. ✎ Write IKA Keys    — SA2 unlock + WriteDataByIdentifier(0x00BE)
                                on all selected modules
      4. ⊞ Update Constellation — rewrite J533 DID 0x04A3 to enroll
                                   all fixed modules
    """

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._scan_results: dict = {}    # mod_name → bytes or None
        self._module_vars:  dict = {}    # mod_name → BooleanVar (checkbox)
        self._module_rows:  dict = {}    # mod_name → dict of tk widgets
        self._ika_blob:     bytes = KNOWN_IKA_BLOB
        self._const_before: bytes = b""
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Action bar ────────────────────────────────────────────────────────
        act = _card(self, padx=10, pady=8)
        act.pack(fill="x", padx=14, pady=(8, 2))

        row1 = _frame(act)
        row1.pack(fill="x")

        self._scan_btn = _btn(row1, "⟳  Scan All Modules",
                              self._do_scan, primary=True, state="disabled")
        self._scan_btn.pack(side="left")
        tip(self._scan_btn, 'Reads DID 0x00BE from all CP modules.\nAuto-checks CP-active modules (all-zero key).\nRun with ignition on, ESP32 in OBD port.')

        self._write_btn = _btn(row1, "✎  Write IKA Key to Selected",
                               self._do_write, state="disabled")
        self._write_btn.pack(side="left", padx=(8, 0))

        self._const_btn = _btn(row1, "⊞  Update Constellation",
                               self._do_update_constellation, state="disabled")
        self._const_btn.pack(side="left", padx=(8, 0))
        tip(self._const_btn, 'Writes known-good constellation to J533 DID 0x04A3.\nEnrols all fixed modules in the gateway.\nSA2 unlock on J533 required.')

        self._sel_all_btn = _btn(row1, "☑ Select All CP",
                                 self._select_all_cp, state="disabled")
        self._sel_all_btn.pack(side="right")
        tip(self._sel_all_btn, "Tick all modules currently showing CP ACTIVE.\nUncheck any you don't want to fix.")

        # Row 2 — recovery options
        row2 = _frame(act)
        row2.pack(fill="x", pady=(6, 0))
        self._restore_const_btn = _btn(
            row2,
            "↺  Restore Known-Good Constellation",
            self._do_restore_constellation,
            state="disabled")
        self._restore_const_btn.pack(side="left")
        tip(self._restore_const_btn, 'Writes known-good constellation (FDA1E80C...)\nback to J533 DID 0x04A3.\nUse after module replacement or to recover.')

        # Status line
        self._status_var = tk.StringVar(value="connect to vehicle first")
        tk.Label(act, textvariable=self._status_var,
                 bg=C["surface"], fg=C["muted"],
                 font=("Courier New", 10)).pack(anchor="w", pady=(6, 0))

        # ── Verdict banner ──────────────────────────────────────────────────
        self._verdict_var = tk.StringVar(value="")
        self._verdict_lbl = tk.Label(self,
                                     textvariable=self._verdict_var,
                                     bg=C["bg"], fg=C["muted"],
                                     font=("Courier New", 13, "bold"),
                                     pady=4)
        self._verdict_lbl.pack(fill="x", padx=14, pady=(2, 0))

        # Ignition cycle button — enabled after constellation write
        self._ign_btn = _btn(self, "⚡  Cycle Ignition (guided)",
                             self._do_ignition_cycle, state="disabled")
        self._ign_btn.pack(fill="x", padx=14, pady=(2, 0))
        tip(self._ign_btn, 'Guided ignition cycle with countdown timers.\nKey OFF (12s) then key ON (10s).\nAuto-rescans after J533 reinitialises.')

        # ── Constellation banner ───────────────────────────────────────────────
        const_card = _card(self, padx=10, pady=6)
        const_card.pack(fill="x", padx=14, pady=(2, 2))
        tk.Label(const_card, text="CONSTELLATION  DID 0x04A3",
                 bg=C["surface"], fg=C["muted"],
                 font=("Courier New", 9)).pack(side="left")
        self._const_var = tk.StringVar(value="—")
        tk.Label(const_card, textvariable=self._const_var,
                 bg=C["surface"], fg=C["amber"],
                 font=("Courier New", 10)).pack(side="left", padx=(12, 0))

        # ── Module grid ───────────────────────────────────────────────────────
        grid_card = _card(self, padx=10, pady=6)
        grid_card.pack(fill="x", padx=14, pady=(2, 2))

        hdr = _frame(grid_card, bg=C["surface"])
        hdr.pack(fill="x", pady=(0, 4))
        for col, width, text in [
            (0, 2,  ""),
            (1, 22, "MODULE"),
            (2, 6,  "ADDR"),
            (3, 9,  "STATUS"),
            (4, 70, "IKA KEY (DID 0x00BE)"),
        ]:
            tk.Label(hdr, text=text, bg=C["surface"], fg=C["muted"],
                     font=("Courier New", 8), width=width,
                     anchor="w").grid(row=0, column=col, sticky="w", padx=2)

        self._grid_frame = _frame(grid_card, bg=C["surface"])
        self._grid_frame.pack(fill="x")
        self._build_module_rows()

        # ── IKA blob selector ─────────────────────────────────────────────────
        blob_card = _card(self, padx=10, pady=6)
        blob_card.pack(fill="x", padx=14, pady=(2, 2))
        tk.Label(blob_card, text="IKA BLOB TO WRITE",
                 bg=C["surface"], fg=C["muted"],
                 font=("Courier New", 9)).pack(anchor="w")
        blob_row = _frame(blob_card)
        blob_row.pack(fill="x", pady=(4, 0))
        self._blob_var = tk.StringVar(value=KNOWN_IKA_BLOB.hex().upper())
        blob_entry = tk.Entry(blob_row, textvariable=self._blob_var,
                              bg=C["bg"], fg=C["amber"],
                              insertbackground=C["green"],
                              font=("Courier New", 9),
                              relief="flat", width=72)
        blob_entry.pack(side="left")
        _btn(blob_row, "✓ Use Scanned",
             self._use_scanned_blob, state="normal").pack(
             side="left", padx=(8, 0))
        tk.Label(blob_card,
                 text="Feb 2024 session blob pre-loaded. After scan, click "
                      "'Use Scanned' to load the dominant blob from scan results.",
                 bg=C["surface"], fg=C["muted"],
                 font=("Courier New", 8), wraplength=560,
                 justify="left").pack(anchor="w", pady=(4, 0))

        # ── Constellation Map ─────────────────────────────────────────────────
        _section(self, "constellation map")
        const_map_card = _card(self, padx=10, pady=6)
        const_map_card.pack(fill="x", padx=14, pady=(2, 2))

        const_map_hdr = _frame(const_map_card, bg=C["surface"])
        const_map_hdr.pack(fill="x", pady=(0, 4))
        for col, width, text in [
            (0, 4,  "SLOT"),
            (1, 28, "MODULE"),
            (2, 7,  "CODED"),
            (3, 7,  "ONLINE"),
            (4, 8,  "CAN ID"),
            (5, 8,  "IKA"),
        ]:
            tk.Label(const_map_hdr, text=text, bg=C["surface"], fg=C["muted"],
                     font=("Courier New", 8), width=width,
                     anchor="w").grid(row=0, column=col, sticky="w", padx=2)

        self._const_map_frame = _frame(const_map_card, bg=C["surface"])
        self._const_map_frame.pack(fill="x")
        self._const_map_rows: list = []

        const_map_note = tk.Label(const_map_card,
                     text="Run scan to populate. Shows J533 constellation bitmap "
                          "vs actual bus presence.",
                     bg=C["surface"], fg=C["dim"],
                     font=("Courier New", 8))
        const_map_note.pack(anchor="w", pady=(4, 0))

        # ── Constellation Fix Actions ────────────────────────────────────────
        fix_card = _card(self, padx=10, pady=6)
        fix_card.pack(fill="x", padx=14, pady=(2, 2))

        fix_row = _frame(fix_card, bg=C["surface"])
        fix_row.pack(fill="x")

        self._deep_diag_btn = _btn(fix_row, "⊙  Deep Diagnostic",
                                    self._do_deep_diagnostic, state="disabled")
        self._deep_diag_btn.pack(side="left")
        tip(self._deep_diag_btn,
            'Read ALL constellation DIDs from J533:\n'
            '0x04A3 (coded), 0x2A2A (allocation), 0x2A26 (present),\n'
            '0x2A2C (CAN IDs). Decodes full slot map.\n'
            'Then reads IKA key from each UDS module.\n'
            'Produces a complete CP state snapshot.')

        self._fix_const_btn = _btn(fix_row,
            "⚡  Fix Constellation (match bus to bitmap)",
            self._do_fix_constellation, state="disabled")
        self._fix_const_btn.pack(side="left", padx=(8, 0))
        tip(self._fix_const_btn,
            'Reads which modules are ONLINE on the bus,\n'
            'rebuilds the constellation bitmap to match,\n'
            'and writes it to J533 DID 0x04A3.\n'
            'This re-enrolls your actual hardware.')

        tk.Label(fix_card,
                 text="Deep Diagnostic reads everything first. "
                      "Fix Constellation writes a rebuilt bitmap that "
                      "matches the modules actually on the bus.",
                 bg=C["surface"], fg=C["dim"],
                 font=("Courier New", 8), wraplength=560,
                 justify="left").pack(anchor="w", pady=(4, 0))

        # ── Log ───────────────────────────────────────────────────────────────
        _section(self, "log")
        self._log = _log_widget(self)
        self._log.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # ── Probe / Save / ODX ────────────────────────────────────────────────
        leg = _frame(self)
        leg.pack(fill="x", padx=14, pady=(0, 8))
        self._probe_btn = _btn(leg, "⊙  J533 probe (full report)",
                               self._do_probe, state="disabled")
        self._probe_btn.pack(side="left")
        self._save_btn  = _btn(leg, "↓  save report",
                               self._save_report, state="disabled")
        self._save_btn.pack(side="left", padx=(8, 0))
        self._odx_lbl   = tk.Label(leg, text="no ODX loaded",
                                   bg=C["bg"], fg=C["muted"],
                                   font=("Courier New", 9))
        self._odx_lbl.pack(side="left", padx=(16, 0))
        _btn(leg, "⊙  open ODX",
             self._open_odx, state="normal").pack(side="right")
        self._report = None

    def _build_module_rows(self):
        for mod_name, addr, tx, rx, proto in CP_MODULES:
            var = tk.BooleanVar(value=False)
            self._module_vars[mod_name] = var

            row = _frame(self._grid_frame, bg=C["surface"])
            row.pack(fill="x", pady=1)

            cb = tk.Checkbutton(row, variable=var,
                                bg=C["surface"], fg=C["green"],
                                activebackground=C["surface"],
                                selectcolor=C["bg"],
                                state="disabled")
            cb.grid(row=0, column=0, padx=2)

            name_lbl = tk.Label(row, text=mod_name,
                                bg=C["surface"], fg=C["muted"],
                                font=("Courier New", 10), width=22, anchor="w")
            name_lbl.grid(row=0, column=1, padx=2, sticky="w")

            addr_lbl = tk.Label(row, text=f"0x{addr}",
                                bg=C["surface"], fg=C["muted"],
                                font=("Courier New", 10), width=6, anchor="w")
            addr_lbl.grid(row=0, column=2, padx=2)

            status_lbl = tk.Label(row, text="—",
                                  bg=C["surface"], fg=C["muted"],
                                  font=("Courier New", 10), width=9, anchor="w")
            status_lbl.grid(row=0, column=3, padx=2)

            blob_lbl = tk.Label(row, text="—",
                                bg=C["surface"], fg=C["dim"],
                                font=("Courier New", 9), anchor="w",
                                width=70)
            blob_lbl.grid(row=0, column=4, padx=2, sticky="w")

            # KWP modules: dim the row, pre-fill status
            if proto == "KWP":
                name_lbl.config(fg=C["dim"])
                addr_lbl.config(fg=C["dim"])
                status_lbl.config(text="KWP2000", fg=C["amber"])
                blob_lbl.config(text="write via ODIS only", fg=C["dim"])

            self._module_rows[mod_name] = {
                "row": row, "cb": cb,
                "status": status_lbl, "blob": blob_lbl
            }

    # ── Connect / Disconnect ─────────────────────────────────────────────────

    def on_connect(self):
        self._scan_btn.config(state="normal")
        self._probe_btn.config(state="normal")
        self._restore_const_btn.config(state="normal")
        self._deep_diag_btn.config(state="normal")
        self._status_var.set("ready — click Scan to check all modules")
        self._status_lbl_color(C["green"])

    def on_disconnect(self):
        self._scan_btn.config(state="disabled")
        self._write_btn.config(state="disabled")
        self._const_btn.config(state="disabled")
        self._sel_all_btn.config(state="disabled")
        self._restore_const_btn.config(state="disabled")
        self._deep_diag_btn.config(state="disabled")
        self._fix_const_btn.config(state="disabled")
        self._status_var.set("connect to vehicle first")
        self._status_lbl_color(C["muted"])
        self._reset_module_rows()
        self._verdict_var.set("")
        if hasattr(self, "_ign_btn"):
            self._ign_btn.config(state="disabled")

    def _status_lbl_color(self, color):
        # find status label and recolor
        for child in self.winfo_children():
            pass  # color is via StringVar, set directly below

    # ── Scan all modules ─────────────────────────────────────────────────────

    def _do_scan(self):
        self._scan_btn.config(state="disabled")
        self._write_btn.config(state="disabled")
        self._const_btn.config(state="disabled")
        self._sel_all_btn.config(state="disabled")
        self._reset_module_rows()
        self._clear_log(self._log)
        self._append_log(self._log,
            "── CP Module Scan ─────────────────────────────────────\n", "hdr")
        self._status_var.set("scanning...")
        self._run(self._scan_task)

    def _scan_task(self):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        def set_row(mod_name, status_text, status_color, blob_text, blob_color,
                    cp_active=False):
            widgets = self._module_rows.get(mod_name, {})
            if not widgets:
                return
            self._ui(widgets["status"].config,
                     text=status_text, fg=status_color)
            self._ui(widgets["blob"].config,
                     text=blob_text,   fg=blob_color)
            if cp_active:
                self._ui(widgets["cb"].config, state="normal")
                self._ui(self._module_vars[mod_name].set, True)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        # Per-module scan — fresh connection each time with delay between.
        # The bus scan confirms individual J2534 connections work fine.
        # The shared-connection approach deadlocks due to udsoncan Client internals.
        self._scan_results = {}
        cp_count = 0
        import time as _mt

        for mod_name, addr, tx, rx, proto in CP_MODULES:
            if tx == 0x710:
                log(f"\n  {mod_name}  (gateway — skipped, constellation read separately)\n", "dim")
                continue
            if proto == "KWP":
                # KWP2000 module — cannot scan via UDS/ISO-TP.
                # Opening ISO-TP J2534 connection to a KWP module makes the
                # Mongoose hang waiting for ISO-TP frames that never arrive.
                # IKA key on these modules is written via KWP TrainICA/TrainGVA
                # (ODIS-only). We note them but skip UDS scan.
                set_row(mod_name, "KWP2000", C["amber"], "KWP module — scan via ODIS", C["amber"])
                log(f"\n  {mod_name}  TX=0x{tx:03X}  KWP2000 module — skip UDS scan\n", "warn")
                continue
            log(f"\n  {mod_name}  TX=0x{tx:03X} RX=0x{rx:03X}  probing...\n", "dim")
            _mt.sleep(1.0)

            # SSP 238: J255/J285/J234/J794 on Convenience or Drive-Train CAN via J533.
            # Multi-frame ISO-TP (34-byte DID) can deadlock PassThruReadMsgs in the
            # Mongoose DLL. Run probe in daemon thread with 15s hard timeout.
            import threading as _thr
            _probe_result = [None]
            _probe_done   = _thr.Event()

            def _run_probe(mod=mod_name, _tx=tx, _rx=rx):
                try:
                    from cp_tools.j533_probe import J533Probe
                    _cfg = dict(configs.default_client_config)
                    _cfg["data_identifiers"] = {IKA_DID: _BytesCodec}
                    _cfg["request_timeout"]   = 8
                    _cfg["p2_timeout"]        = 0.5
                    _cfg["p2_star_timeout"]   = 5.0
                    _cfg["use_server_timing"] = False
                    _conn = J533Probe(
                        interface      = self.mw.interface,
                        interface_path = self.mw.iface_path,
                        ble_bridge     = getattr(self.mw, "ble_bridge", None),
                    )._make_conn(_tx, _rx)
                    with Client(_conn, request_timeout=8, config=_cfg) as _c:
                        _c.change_session(
                            udsoncan.services.DiagnosticSessionControl
                            .Session.extendedDiagnosticSession)
                        _resp = _c.read_data_by_identifier([IKA_DID])
                        _probe_result[0] = bytes(_resp.service_data.values[IKA_DID])
                except Exception as _ex:
                    _probe_result[0] = _ex
                finally:
                    _probe_done.set()

            _thr.Thread(target=_run_probe, daemon=True).start()
            _probe_done.wait(timeout=15)

            if not _probe_done.is_set():
                # DLL deadlocked in PassThruReadMsgs — abandon daemon thread
                set_row(mod_name, "DLL hang", C["amber"],
                        "Convenience CAN multi-frame hang", C["amber"])
                log(f"    ! PassThruReadMsgs deadlock — skipped\n", "warn")
                continue

            _val = _probe_result[0]

            if isinstance(_val, udsoncan.exceptions.NegativeResponseException):
                nrc = getattr(getattr(_val, "response", None), "code", 0)
                if nrc in (0x22, 0x31):
                    set_row(mod_name, "CP ACTIVE", C["red"],
                            f"NRC 0x{nrc:02X} — CP preventing read",
                            C["red"], cp_active=True)
                    self._scan_results[mod_name] = b"\x00" * 34
                    cp_count += 1
                    log(f"    ✗ CP ACTIVE — NRC 0x{nrc:02X}\n", "err")
                else:
                    set_row(mod_name, "NRC error", C["amber"],
                            f"NRC 0x{nrc:02X}", C["amber"])
                    log(f"    ? NRC 0x{nrc:02X}\n", "warn")
                continue

            if isinstance(_val, Exception):
                _err = str(_val)[:60]
                if "timeout" in _err.lower():
                    set_row(mod_name, "not present", C["muted"], "no response", C["muted"])
                    log(f"    — not present (timeout)\n", "dim")
                else:
                    set_row(mod_name, "error", C["amber"], _err, C["amber"])
                    log(f"    ! error: {_err}\n", "warn")
                continue

            # Success — raw is the IKA key bytes
            raw = _val
            self._scan_results[mod_name] = raw
            all_zeros = all(b == 0 for b in raw)
            short     = raw[:8].hex().upper() + "..."
            log(f"  {mod_name}  TX=0x{tx:03X} RX=0x{rx:03X}\n", "hdr")
            if all_zeros:
                cp_count += 1
                set_row(mod_name, "CP ACTIVE", C["red"],
                        "all zeros — key not installed", C["red"], cp_active=True)
                log(f"    ✗ CP ACTIVE — IKA key all zeros\n", "err")
            else:
                same = (raw == KNOWN_IKA_BLOB)
                set_row(mod_name, "CP clear", C["green"],
                        short, C["green"] if same else C["amber"])
                tag = "ok" if same else "warn"
                lbl = "matches known blob" if same else "different blob"
                log(f"    ✓ CP clear  {short}  ({lbl})\n", tag)



        # Summary

        # ── Read J533 constellation LAST (after modules scanned) ──────────────
        # J533 extended session blocks J2534 forwarding to slave modules.
        # Reading constellation after module scan avoids this entirely.
        # Read J533 constellation — retry once on failure
        import time as _t
        for _attempt in range(2):
            try:
                from cp_tools.j533_probe import J533Probe
                _p533 = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )
                _cfg533 = dict(configs.default_client_config)
                _cfg533["data_identifiers"] = {CONST_DID: _BytesCodec}
                _cfg533["request_timeout"]   = 5
                _cfg533["p2_timeout"]        = 0.15
                _cfg533["use_server_timing"] = False
                _conn533 = _p533._make_conn(0x710, 0x77A)
                with Client(_conn533, request_timeout=8, config=_cfg533) as _c:
                    _c.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    _r = _c.read_data_by_identifier([CONST_DID])
                    const_bytes = bytes(_r.service_data.values[CONST_DID])
                    self._const_before = const_bytes
                    self._ui(self._const_var.set,
                             " ".join(f"{b:02X}" for b in const_bytes))
                    log(f"  J533 constellation: "
                        f"{' '.join(f'{b:02X}' for b in const_bytes)}\n", "ok")
                # Return J533 to default session — extended session blocks
                # forwarding of UDS messages to other modules via gateway
                try:
                    _c.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.defaultSession)
                    log("  J533 returned to default session\n", "dim")
                except Exception:
                    pass
                break
            except Exception as e:
                if _attempt == 0:
                    log(f"  J533 constellation retry: {e}\n", "warn")
                    _t.sleep(1.0)
                else:
                    log(f"  J533 constellation failed: {e} — continuing\n", "warn")

        # Wait for J533 to fully exit extended session (5s timeout)
        # J533 as gateway blocks forwarded UDS while in extended diagnostic session
        import time as _t
        _t.sleep(5.0)
        log(f"\n── Scan complete: {cp_count} module(s) CP active ───────────────\n",
            "ok" if cp_count == 0 else "warn")

        # Blob analysis
        valid = [v for v in self._scan_results.values()
                 if v and not all(b == 0 for b in v)]
        if valid:
            unique = set(v.hex() for v in valid)
            if len(unique) == 1:
                log("  All populated modules: IDENTICAL blob → VIN-bound key ✓\n", "ok")
                log(f"  Blob: {valid[0].hex().upper()}\n", "ok")
                self._ika_blob = valid[0]
                self._ui(self._blob_var.set, valid[0].hex().upper())
            else:
                log(f"  {len(unique)} distinct blobs found → per-module derivation\n",
                    "warn")

        if cp_count > 0:
            self._ui(self._write_btn.config, state="normal")
            self._ui(self._sel_all_btn.config, state="normal")
            self._ui(self._status_var.set,
                     f"{cp_count} module(s) CP active — select and click Write")
            self._ui(self._verdict_var.set,
                     f"⚠  {cp_count} MODULE(S) CP ACTIVE")
            self._ui(self._verdict_lbl.config, fg=C["red"])
        else:
            self._ui(self._status_var.set, "all modules clear ✓")
            self._ui(self._verdict_var.set, "✓  ALL MODULES CLEAR")
            self._ui(self._verdict_lbl.config, fg=C["green"])

        self._ui(self._scan_btn.config, state="normal")

    # ── Select all CP-active ─────────────────────────────────────────────────

    def _select_all_cp(self):
        for mod_name, row in self._module_rows.items():
            if row["cb"]["state"] == "normal":
                self._module_vars[mod_name].set(True)

    # ── Use scanned blob ─────────────────────────────────────────────────────

    def _use_scanned_blob(self):
        valid = [v for v in self._scan_results.values()
                 if v and not all(b == 0 for b in v)]
        if valid:
            self._ika_blob = valid[0]
            self._blob_var.set(valid[0].hex().upper())
            self._append_log(self._log,
                f"IKA blob updated from scan results: "
                f"{valid[0].hex().upper()[:16]}...\n", "ok")
        else:
            self._append_log(self._log,
                "No populated blobs found in scan results.\n", "warn")

    # ── Write IKA keys ───────────────────────────────────────────────────────

    def _do_write(self):
        selected = [m for m, v in self._module_vars.items() if v.get()]
        if not selected:
            self._append_log(self._log,
                "No modules selected — check boxes next to modules to fix.\n",
                "warn")
            return
        try:
            blob = bytes.fromhex(self._blob_var.get().replace(" ", ""))
        except ValueError:
            self._append_log(self._log,
                "Invalid IKA blob hex string.\n", "err")
            return
        if len(blob) != 34:
            self._append_log(self._log,
                f"IKA blob must be 34 bytes (got {len(blob)}).\n", "err")
            return

        self._write_btn.config(state="disabled")
        self._scan_btn.config(state="disabled")
        self._append_log(self._log,
            f"\n── Writing IKA key to {len(selected)} module(s) ─────────────\n",
            "hdr")
        self._run(self._write_task, selected, blob)

    def _write_task(self, selected: list, blob: bytes):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        mod_map = {m[0]: m for m in CP_MODULES}
        written = []

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        for mod_name in selected:
            if mod_name not in mod_map:
                continue
            _, addr, tx, rx, proto = mod_map[mod_name]
            log(f"\n  Writing to {mod_name}  TX=0x{tx:03X} RX=0x{rx:03X}\n",
                "hdr")
            try:
                from cp_tools.j533_probe import J533Probe
                probe = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )
                cfg = dict(configs.default_client_config)
                cfg["data_identifiers"] = {IKA_DID: _BytesCodec}
                cfg["request_timeout"]  = 10
                conn = probe._make_conn(tx, rx)
                with Client(conn, request_timeout=10, config=cfg) as c:
                    # Extended session
                    c.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    log("    extended session opened\n", "dim")

                    # SA2 seed/key — use per-module script from ecu_defs
                    try:
                        from sa2_seed_key.sa2_script import Sa2Script
                        # Map CAN TX ID to the correct ECU def SA2 script
                        _sa2_scripts = {
                            0x710: J533_LEAR.sa2_script,   # J533 Gateway
                            0x746: J255_4ZONE.sa2_script,  # J255 Climatronic
                        }
                        sa2_bytecode = _sa2_scripts.get(tx)
                        if sa2_bytecode is None:
                            log(f"    No SA2 script for TX=0x{tx:03X} — "
                                f"trying without security access\n", "warn")
                        else:
                            seed_resp = c.request_seed(0x03)
                            seed = bytes(seed_resp.service_data.seed)
                            key = Sa2Script(sa2_bytecode).execute(
                                int.from_bytes(seed, "big"))
                            c.send_key(0x04, key.to_bytes(4, "big"))
                            log(f"    SA2 unlocked ✓ (script for 0x{tx:03X})\n",
                                "ok")
                    except ImportError:
                        # Fallback to generic Sa2Algorithm if Sa2Script unavailable
                        try:
                            from sa2_seed_key.sa2_script import Sa2Algorithm
                            seed_resp = c.request_seed(0x03)
                            seed = bytes(seed_resp.service_data.seed)
                            key = Sa2Algorithm().compute_key(seed)
                            c.send_key(0x04, key)
                            log("    SA2 unlocked ✓ (generic)\n", "ok")
                        except ImportError:
                            log("    SA2 module not installed — "
                                "pip install sa2_seed_key\n", "warn")
                    except Exception as sa2_e:
                        log(f"    SA2 error: {sa2_e}\n", "err")
                        continue

                    # Write DID 0x00BE
                    c.write_data_by_identifier(IKA_DID, blob)
                    log(f"    DID 0x00BE written: "
                        f"{blob.hex().upper()[:16]}...\n", "ok")

                    # Verify
                    verify = c.read_data_by_identifier([IKA_DID])
                    readback = bytes(verify.service_data.values[IKA_DID])
                    if readback == blob:
                        log(f"    Verified ✓  readback matches\n", "ok")
                        written.append(mod_name)
                        self._ui(self._module_rows[mod_name]["status"].config,
                                 text="written ✓", fg=C["green"])
                        self._ui(self._module_rows[mod_name]["blob"].config,
                                 text=blob.hex().upper()[:36] + "...",
                                 fg=C["green"])
                    else:
                        log(f"    Verify FAILED — readback mismatch\n", "err")

            except Exception as e:
                log(f"    Error: {e}\n", "err")

        log(f"\n── Write complete: {len(written)}/{len(selected)} modules written\n",
            "ok" if len(written) == len(selected) else "warn")

        if written:
            log("\nNext step: click ⊞ Update Constellation to enroll "
                "written modules in J533.\n", "hdr")
            self._ui(self._const_btn.config, state="normal")
            self._ui(self._ign_btn.config, state="disabled")  # wait for const first

        self._ui(self._write_btn.config, state="normal")
        self._ui(self._scan_btn.config, state="normal")

    # ── Update J533 constellation ─────────────────────────────────────────────

    def _do_update_constellation(self):
        self._const_btn.config(state="disabled")
        self._append_log(self._log,
            "\n── Updating J533 constellation (DID 0x04A3) ─────────\n", "hdr")
        self._run(self._constellation_task)

    def _constellation_task(self):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        try:
            from cp_tools.j533_probe import J533Probe
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            cfg = dict(configs.default_client_config)
            cfg["data_identifiers"] = {CONST_DID: _BytesCodec}
            cfg["request_timeout"]  = 10
            conn = probe._make_conn(0x710, 0x77A)
            with Client(conn, request_timeout=10, config=cfg) as c:
                c.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                # Read current constellation
                r = c.read_data_by_identifier([CONST_DID])
                current = bytes(r.service_data.values[CONST_DID])
                log(f"  Current:  "
                    f"{' '.join(f'{b:02X}' for b in current)}\n", "dim")

                # Determine which modules were written
                written_names = [
                    m for m, row in self._module_rows.items()
                    if row["status"].cget("text") == "written ✓"
                ]
                if not written_names:
                    log("  No modules marked as written — "
                        "run Write IKA Keys first.\n", "warn")
                    self._ui(self._const_btn.config, state="normal")
                    return

                log(f"  Modules to enroll: {', '.join(written_names)}\n",
                    "hdr")

                # SA2 unlock on J533 — use confirmed script from ecu_defs
                try:
                    from sa2_seed_key.sa2_script import Sa2Script
                    seed_resp = c.request_seed(0x03)
                    seed = bytes(seed_resp.service_data.seed)
                    key = Sa2Script(J533_LEAR.sa2_script).execute(
                        int.from_bytes(seed, "big"))
                    c.send_key(0x04, key.to_bytes(4, "big"))
                    log("  SA2 unlocked on J533 ✓\n", "ok")
                except ImportError:
                    log("  SA2 module not installed — "
                        "pip install sa2_seed_key\n", "warn")
                except Exception as e:
                    log(f"  SA2 error: {e}\n", "err")
                    self._ui(self._const_btn.config, state="normal")
                    return

                # Write updated constellation
                # The Feb 2024 known-good constellation post-session:
                # FD A1 E8 0C FE 62 60 0D 00 00
                # We write this as the target — it enrolled J255, J136, and others
                # For a full re-enrol we use the post-session known value
                TARGET_CONST = bytes.fromhex("FDA1E80CFE62600D0000")

                log(f"  Writing: "
                    f"{' '.join(f'{b:02X}' for b in TARGET_CONST)}\n", "hdr")
                c.write_data_by_identifier(CONST_DID, TARGET_CONST)

                # Verify
                r2 = c.read_data_by_identifier([CONST_DID])
                readback = bytes(r2.service_data.values[CONST_DID])
                if readback == TARGET_CONST:
                    log("  Constellation written ✓  readback matches\n", "ok")
                    self._ui(self._const_var.set,
                             " ".join(f"{b:02X}" for b in readback))
                    log("\n✓ CP fix complete — click ⚡ Cycle Ignition to guide\n"
                        "through the key cycle and auto-rescan.\n",
                        "ok")
                else:
                    log(f"  Verify FAILED — readback: "
                        f"{' '.join(f'{b:02X}' for b in readback)}\n", "err")

        except Exception as e:
            log(f"  Constellation error: {e}\n", "err")

        self._ui(self._const_btn.config, state="normal")

    # ── Restore known-good constellation ─────────────────────────────────────

    def _do_restore_constellation(self):
        """Write the known-good constellation back to J533 DID 0x04A3."""
        import tkinter.messagebox as mb
        if not mb.askyesno(
            "Restore Known-Good Constellation",
            "This will write the Feb 2024 known-good constellation\n"
            "(FD A1 E8 0C FE 62 60 0D 00 00) to J533 DID 0x04A3.\n\n"
            "Use this to undo a zero-constellation experiment or\n"
            "restore CP after replacing a module.\n\n"
            "Continue?",
            icon="question"
        ):
            return
        self._restore_const_btn.config(state="disabled")
        self._append_log(self._log,
            "\n── Restoring Known-Good Constellation ──────────────────\n",
            "hdr")
        self._run(self._restore_constellation_task)

    def _restore_constellation_task(self):
        import udsoncan
        from udsoncan.client import Client
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        KNOWN_GOOD = bytes.fromhex("FDA1E80CFE62600D0000")

        try:
            from cp_tools.j533_probe import J533Probe
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            cfg = dict(configs.default_client_config)
            cfg["data_identifiers"] = {CONST_DID: _BytesCodec}
            cfg["request_timeout"]  = 10
            conn = probe._make_conn(0x710, 0x77A)

            with Client(conn, request_timeout=10, config=cfg) as c:
                c.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                # Read current
                r = c.read_data_by_identifier([CONST_DID])
                current = bytes(r.service_data.values[CONST_DID])
                log(f"  Current:    {' '.join(f'{b:02X}' for b in current)}\n",
                    "dim")
                log(f"  Restoring:  {' '.join(f'{b:02X}' for b in KNOWN_GOOD)}\n",
                    "hdr")

                if current == KNOWN_GOOD:
                    log("  Already at known-good value — no write needed ✓\n", "ok")
                    self._ui(self._restore_const_btn.config, state="normal")
                    return

                # SA2 unlock on J533 — use confirmed script from ecu_defs
                try:
                    from sa2_seed_key.sa2_script import Sa2Script
                    seed_resp = c.request_seed(0x03)
                    seed = bytes(seed_resp.service_data.seed)
                    key = Sa2Script(J533_LEAR.sa2_script).execute(
                        int.from_bytes(seed, "big"))
                    c.send_key(0x04, key.to_bytes(4, "big"))
                    log("  SA2 unlocked on J533 ✓\n", "ok")
                except ImportError:
                    log("  SA2 module not installed — "
                        "pip install sa2_seed_key\n", "warn")
                except Exception as e:
                    log(f"  SA2 error: {e}\n", "err")
                    self._ui(self._restore_const_btn.config, state="normal")
                    return

                c.write_data_by_identifier(CONST_DID, KNOWN_GOOD)

                # Verify
                r2 = c.read_data_by_identifier([CONST_DID])
                readback = bytes(r2.service_data.values[CONST_DID])
                if readback == KNOWN_GOOD:
                    log("  Restored ✓  readback matches known-good\n", "ok")
                    self._ui(self._const_var.set,
                             " ".join(f"{b:02X}" for b in readback))
                else:
                    log(f"  Verify FAILED — readback: "
                        f"{' '.join(f'{b:02X}' for b in readback)}\n", "err")

        except Exception as e:
            log(f"  Restore error: {e}\n", "err")

        self._ui(self._restore_const_btn.config, state="normal")

    # ── Deep Diagnostic ─────────────────────────────────────────────────────

    def _do_deep_diagnostic(self):
        self._deep_diag_btn.config(state="disabled")
        self._fix_const_btn.config(state="disabled")
        self._clear_log(self._log)
        self._append_log(self._log,
            "── Deep CP Diagnostic ────────────────────────────────────\n",
            "hdr")
        self._status_var.set("running deep diagnostic...")
        self._run(self._deep_diagnostic_task)

    def _deep_diagnostic_task(self):
        """
        Read ALL constellation DIDs from J533, decode the full slot map,
        then read IKA key from each accessible module. Produces a complete
        picture of what J533 expects vs what's actually on the bus.
        """
        import udsoncan
        from udsoncan.client import Client
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        DIAG_DIDS = {
            CONST_DID: "Constellation (coded bitmap)",
            0x2A2A: "Allocation table (slot→module)",
            0x2A26: "Present bitmap (online/offline)",
            0x2A2C: "TP-Identifier (CAN IDs per slot)",
            0x00BE: "IKA Key (J533)",
            0x0438: "Stored theft protection keys",
            0x043D: "Successful key downloads",
            0x043E: "Showroom mode",
        }

        raw_data = {}  # DID → bytes

        # ── Read all DIDs from J533 ──────────────────────────────────────────
        log("  Reading J533 constellation DIDs...\n", "dim")
        try:
            from cp_tools.j533_probe import J533Probe
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            cfg = dict(configs.default_client_config)
            cfg["data_identifiers"] = {did: _BytesCodec for did in DIAG_DIDS}
            cfg["request_timeout"] = 8
            cfg["use_server_timing"] = False
            conn = probe._make_conn(0x710, 0x77A)

            with Client(conn, request_timeout=8, config=cfg) as c:
                c.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                for did, label in DIAG_DIDS.items():
                    try:
                        r = c.read_data_by_identifier([did])
                        val = bytes(r.service_data.values[did])
                        raw_data[did] = val
                        short = val.hex().upper()
                        if len(short) > 40:
                            short = short[:40] + "..."
                        log(f"    0x{did:04X}  {label}\n"
                            f"           {short}\n", "ok")
                    except udsoncan.exceptions.NegativeResponseException as e:
                        nrc = getattr(getattr(e, "response", None), "code", 0)
                        log(f"    0x{did:04X}  {label}  NRC 0x{nrc:02X}\n", "warn")
                    except Exception as e:
                        log(f"    0x{did:04X}  {label}  error: {str(e)[:50]}\n", "warn")

                # Return to default session
                try:
                    c.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.defaultSession)
                except Exception:
                    pass

        except Exception as e:
            log(f"  J533 connection error: {e}\n", "err")
            self._ui(self._deep_diag_btn.config, state="normal")
            return

        # ── Decode constellation ─────────────────────────────────────────────
        log("\n── Constellation Decode ───────────────────────────────────\n",
            "hdr")

        coded_raw   = raw_data.get(CONST_DID)
        alloc_raw   = raw_data.get(0x2A2A)
        present_raw = raw_data.get(0x2A26)
        tp_id_raw   = raw_data.get(0x2A2C)

        if not coded_raw:
            log("  Cannot decode — DID 0x04A3 not read\n", "err")
            self._ui(self._deep_diag_btn.config, state="normal")
            return

        from cp_tools.j533_probe import J533Probe as _P
        constellation = _P.decode_constellation(
            coded_raw, alloc_raw, present_raw, tp_id_raw)

        # Store for Fix Constellation to use
        self._diag_constellation = constellation
        self._diag_raw = raw_data

        log(f"  {'Slot':>4}  {'Module':<28}  {'Coded':>6}  "
            f"{'Online':>6}  {'CAN ID':>8}  Status\n", "dim")
        log(f"  {'─'*4}  {'─'*28}  {'─'*6}  {'─'*6}  {'─'*8}  {'─'*12}\n", "dim")

        mismatch_count = 0
        for entry in constellation:
            slot  = entry["slot"]
            name  = entry["ecu_name_label"][:27]
            coded = "YES" if entry["coded"] else "-"
            pres  = "YES" if entry["present"] else "-"
            can   = f"0x{entry['can_id']:04X}" if entry.get("can_id") else "-"

            # Identify mismatches
            is_coded  = entry["coded"]
            is_online = entry["present"]
            if is_coded and not is_online:
                status = "ENROLLED BUT OFFLINE"
                tag = "err"
                mismatch_count += 1
            elif not is_coded and is_online:
                status = "ONLINE BUT NOT ENROLLED"
                tag = "warn"
                mismatch_count += 1
            elif is_coded and is_online:
                status = "OK"
                tag = "ok"
            else:
                status = ""
                tag = "dim"

            flag = ""
            if entry.get("ecu_name") == 8:
                flag = " ◄ J255"
            elif entry.get("ecu_name") == 54:
                flag = " ◄ J136"

            log(f"  {slot:>4}  {name:<28}  {coded:>6}  {pres:>6}  "
                f"{can:>8}  {status}{flag}\n", tag)

        # Update the constellation map display
        self._ui(self._update_const_map, constellation)

        log(f"\n  Mismatches: {mismatch_count}\n",
            "ok" if mismatch_count == 0 else "err")

        # ── IKA key analysis ─────────────────────────────────────────────────
        ika_j533 = raw_data.get(0x00BE)
        if ika_j533:
            all_zero = all(b == 0 for b in ika_j533)
            log(f"\n  J533 IKA key: {'ALL ZEROS — no key installed' if all_zero else ika_j533.hex().upper()[:32] + '...'}\n",
                "err" if all_zero else "ok")

        key_dl = raw_data.get(0x043D)
        if key_dl:
            log(f"  Key downloads: {int.from_bytes(key_dl, 'big')}\n", "dim")

        showroom = raw_data.get(0x043E)
        if showroom:
            mode = "ACTIVE" if any(b != 0 for b in showroom) else "not active"
            log(f"  Showroom mode: {mode}\n", "dim")

        # ── Verdict ──────────────────────────────────────────────────────────
        if mismatch_count > 0:
            log(f"\n  ⚠  {mismatch_count} constellation mismatch(es) found.\n"
                f"  The constellation bitmap does not match the modules on the bus.\n"
                f"  Click 'Fix Constellation' to rebuild and write a corrected bitmap.\n",
                "warn")
            self._ui(self._fix_const_btn.config, state="normal")
            self._ui(self._verdict_var.set,
                     f"⚠  {mismatch_count} CONSTELLATION MISMATCH(ES)")
            self._ui(self._verdict_lbl.config, fg=C["amber"])
        else:
            log("\n  ✓  Constellation matches bus state — no enrollment issues.\n"
                "  If CP is still active, the issue is IKA key mismatch, not enrollment.\n",
                "ok")
            self._ui(self._verdict_var.set, "✓  CONSTELLATION OK")
            self._ui(self._verdict_lbl.config, fg=C["green"])

        # Save report
        self._diag_report_json = {
            "raw_dids": {f"0x{k:04X}": v.hex().upper() for k, v in raw_data.items()},
            "constellation": constellation,
            "mismatches": mismatch_count,
        }
        self._ui(self._save_btn.config, state="normal")
        self._ui(self._deep_diag_btn.config, state="normal")

    def _update_const_map(self, constellation: list):
        """Update the constellation map grid in the UI."""
        # Clear existing rows
        for w in self._const_map_frame.winfo_children():
            w.destroy()
        self._const_map_rows.clear()

        for entry in constellation:
            if not entry["coded"] and not entry["present"]:
                continue  # skip empty slots

            row = _frame(self._const_map_frame, bg=C["surface"])
            row.pack(fill="x", pady=1)

            slot  = entry["slot"]
            name  = entry["ecu_name_label"][:27]
            coded = "YES" if entry["coded"] else "-"
            pres  = "YES" if entry["present"] else "-"
            can   = f"0x{entry['can_id']:04X}" if entry.get("can_id") else "-"

            # Mismatch coloring
            is_coded  = entry["coded"]
            is_online = entry["present"]
            if is_coded and not is_online:
                color = C["red"]
                ika = "MISSING"
            elif not is_coded and is_online:
                color = C["amber"]
                ika = "NOT ENROLLED"
            elif is_coded and is_online:
                color = C["green"]
                ika = "OK"
            else:
                color = C["dim"]
                ika = "-"

            for col, width, text, fg in [
                (0, 4,  str(slot), C["muted"]),
                (1, 28, name, color),
                (2, 7,  coded, color),
                (3, 7,  pres, color),
                (4, 8,  can, C["muted"]),
                (5, 8,  ika, color),
            ]:
                tk.Label(row, text=text, bg=C["surface"], fg=fg,
                         font=("Courier New", 9), width=width,
                         anchor="w").grid(row=0, column=col, sticky="w", padx=2)

            self._const_map_rows.append(entry)

    # ── Fix Constellation ─────────────────────────────────────────────────

    def _do_fix_constellation(self):
        """
        Rebuild constellation bitmap from the PRESENT bitmap (what's actually
        on the bus) and write it to J533 DID 0x04A3.
        """
        if not hasattr(self, '_diag_raw') or not self._diag_raw:
            self._append_log(self._log,
                "Run Deep Diagnostic first.\n", "warn")
            return

        present_raw = self._diag_raw.get(0x2A26)
        coded_raw   = self._diag_raw.get(CONST_DID)

        if not present_raw or not coded_raw:
            self._append_log(self._log,
                "Missing constellation data — run Deep Diagnostic first.\n",
                "warn")
            return

        # The fix: set coded bitmap = present bitmap
        # This enrolls every module that's currently online and unenrolls
        # any that are offline (e.g., the 4-zone J255 that's no longer installed)
        new_const = bytearray(len(coded_raw))
        # Copy present bits into coded, preserving the coded length
        for i in range(min(len(present_raw), len(new_const))):
            new_const[i] = present_raw[i]

        import tkinter.messagebox as mb
        current_hex = " ".join(f"{b:02X}" for b in coded_raw)
        new_hex     = " ".join(f"{b:02X}" for b in new_const)

        if not mb.askyesno(
            "Fix Constellation",
            f"This will rewrite the J533 constellation to match\n"
            f"the modules currently on the bus.\n\n"
            f"Current: {current_hex}\n"
            f"New:     {new_hex}\n\n"
            f"This enrolls your actual hardware and unenrolls\n"
            f"any modules that are no longer installed.\n\n"
            f"Continue?",
            icon="warning"
        ):
            return

        self._fix_const_btn.config(state="disabled")
        self._append_log(self._log,
            f"\n── Fix Constellation ──────────────────────────────────────\n"
            f"  Current: {current_hex}\n"
            f"  Writing: {new_hex}\n",
            "hdr")
        self._run(self._fix_constellation_task, bytes(new_const), coded_raw)

    def _fix_constellation_task(self, new_const: bytes, old_const: bytes):
        import udsoncan
        from udsoncan.client import Client
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        try:
            from cp_tools.j533_probe import J533Probe
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            cfg = dict(configs.default_client_config)
            cfg["data_identifiers"] = {CONST_DID: _BytesCodec}
            cfg["request_timeout"]  = 10
            conn = probe._make_conn(0x710, 0x77A)

            with Client(conn, request_timeout=10, config=cfg) as c:
                c.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                # SA2 unlock on J533 — use confirmed script from ecu_defs
                try:
                    from sa2_seed_key.sa2_script import Sa2Script
                    seed_resp = c.request_seed(0x03)
                    seed = bytes(seed_resp.service_data.seed)
                    key = Sa2Script(J533_LEAR.sa2_script).execute(
                        int.from_bytes(seed, "big"))
                    c.send_key(0x04, key.to_bytes(4, "big"))
                    log("  SA2 unlocked on J533 ✓\n", "ok")
                except ImportError:
                    log("  SA2 module not installed — "
                        "pip install sa2_seed_key\n", "warn")
                except Exception as e:
                    log(f"  SA2 error: {e}\n", "err")
                    self._ui(self._fix_const_btn.config, state="normal")
                    return

                # Write new constellation
                c.write_data_by_identifier(CONST_DID, new_const)
                log("  Write accepted ✓\n", "ok")

                # Verify
                r = c.read_data_by_identifier([CONST_DID])
                readback = bytes(r.service_data.values[CONST_DID])

                if readback == new_const:
                    log(f"  Verified ✓  readback matches\n", "ok")
                    rb_hex = " ".join(f"{b:02X}" for b in readback)
                    self._ui(self._const_var.set, rb_hex)
                    log(f"\n  ✓  Constellation fixed — matches current bus hardware.\n"
                        f"  Cycle ignition (key OFF 12s, key ON 10s) then rescan.\n",
                        "ok")
                    self._ui(self._verdict_var.set,
                             "✓  CONSTELLATION FIXED — CYCLE IGNITION")
                    self._ui(self._verdict_lbl.config, fg=C["green"])
                    self._ui(self._ign_btn.config, state="normal")
                else:
                    rb_hex = " ".join(f"{b:02X}" for b in readback)
                    log(f"  Readback differs: {rb_hex}\n", "warn")
                    log("  J533 may have modified the value. This could mean\n"
                        "  the gateway validates constellation structure.\n", "warn")

        except udsoncan.exceptions.NegativeResponseException as nre:
            nrc = getattr(getattr(nre, "response", None), "code", 0)
            log(f"  J533 rejected write — NRC 0x{nrc:02X}\n", "err")
            if nrc == 0x22:
                log("  conditionsNotCorrect — J533 may require GEKO server\n"
                    "  authorization to accept constellation changes.\n"
                    "  Next step: try routine 0x0226 before the write.\n", "warn")
            elif nrc == 0x31:
                log("  requestOutOfRange — value rejected.\n", "warn")
            elif nrc == 0x33:
                log("  securityAccessDenied — SA2 unlock may have failed.\n", "warn")
            # Log the rollback value for manual recovery
            old_hex = " ".join(f"{b:02X}" for b in old_const)
            log(f"\n  Original value preserved: {old_hex}\n"
                f"  Use 'Restore Known-Good' if needed.\n", "dim")
        except Exception as e:
            log(f"  Error: {e}\n", "err")

        self._ui(self._fix_const_btn.config, state="normal")
        self._ui(self._deep_diag_btn.config, state="normal")

    # ── Guided ignition cycle ────────────────────────────────────────────────

    def _do_ignition_cycle(self):
        """Guided key-off → wait → key-on sequence. Auto-rescans after."""
        self._ign_btn.config(state="disabled")
        self._scan_btn.config(state="disabled")
        self._write_btn.config(state="disabled")
        self._const_btn.config(state="disabled")
        self._verdict_var.set("follow ignition cycle steps...")
        self._verdict_lbl.config(fg=C["amber"])

        dlg = tk.Toplevel(self)
        dlg.title("Ignition Cycle")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.geometry("400x230")
        dlg.update_idletasks()
        px = self.winfo_rootx() + self.winfo_width()  // 2 - 200
        py = self.winfo_rooty() + self.winfo_height() // 2 - 115
        dlg.geometry(f"+{px}+{py}")

        self._ign_dlg   = dlg
        self._ign_phase = 0

        self._ign_title = tk.Label(dlg, text="STEP 1 OF 3  —  KEY OFF",
                                   bg=C["bg"], fg=C["amber"],
                                   font=("Courier New", 12, "bold"))
        self._ign_title.pack(pady=(20, 6))

        self._ign_msg = tk.Label(dlg,
            text="Turn ignition KEY OFF and remove key.\nClick Continue when done.",
            bg=C["bg"], fg=C["text"],
            font=("Courier New", 11), justify="center")
        self._ign_msg.pack(pady=6)

        self._ign_timer_lbl = tk.Label(dlg, text="",
                                       bg=C["bg"], fg=C["dim"],
                                       font=("Courier New", 10))
        self._ign_timer_lbl.pack(pady=4)

        self._ign_next_btn = tk.Button(dlg,
            text="Continue \u2192",
            command=self._ign_advance,
            bg=C["surface"], fg=C["green"],
            activebackground=C["surface"],
            activeforeground=C["green"],
            font=("Courier New", 11, "bold"),
            relief="solid", bd=1,
            highlightbackground=C["green"],
            highlightthickness=1,
            padx=20, pady=8, cursor="hand2")
        self._ign_next_btn.pack(pady=(4, 0))
        self._ign_countdown = 0

    def _ign_advance(self):
        self._ign_phase += 1
        if self._ign_phase == 1:
            # Key confirmed off — start 12s countdown
            self._ign_title.config(text="STEP 2 OF 3  —  WAITING")
            self._ign_msg.config(
                text="Waiting 12 seconds for J533 to fully power down...")
            self._ign_next_btn.config(state="disabled")
            self._ign_countdown = 12
            self._ign_tick(self._ign_off_done)
        elif self._ign_phase == 3:
            # Key confirmed on — start 10s countdown
            self._ign_title.config(text="STEP 3 OF 3  —  J533 BOOTING")
            self._ign_msg.config(
                text="Waiting 10 seconds for J533 to initialise...")
            self._ign_next_btn.config(state="disabled")
            self._ign_timer_lbl.config(fg=C["green"])
            self._ign_countdown = 10
            self._ign_tick(self._ign_on_done)

    def _ign_tick(self, callback):
        if self._ign_countdown > 0:
            self._ign_timer_lbl.config(
                text=f"{self._ign_countdown}s remaining...")
            self._ign_countdown -= 1
            self.after(1000, lambda: self._ign_tick(callback))
        else:
            callback()

    def _ign_off_done(self):
        self._ign_timer_lbl.config(
            text="\u2713 J533 powered down", fg=C["green"])
        self._ign_title.config(
            text="STEP 3 OF 3  —  KEY ON", fg=C["green"])
        self._ign_msg.config(
            text="Turn ignition KEY ON (engine off is fine).\n"
                 "Click Continue when done.")
        self._ign_next_btn.config(
            state="normal", text="Key is on \u2192")
        self._ign_phase = 2   # next click triggers phase 3

    def _ign_on_done(self):
        self._ign_timer_lbl.config(
            text="\u2713 J533 ready", fg=C["green"])
        self._ign_msg.config(text="\u2713 Rescanning now...")
        self._ign_next_btn.config(state="disabled")
        self.after(800, self._ign_finish)

    def _ign_finish(self):
        if hasattr(self, "_ign_dlg") and self._ign_dlg.winfo_exists():
            self._ign_dlg.destroy()
        self._scan_btn.config(state="normal")
        self._ign_btn.config(state="disabled")
        self._verdict_var.set("rescanning after ignition cycle...")
        self._verdict_lbl.config(fg=C["amber"])
        self.after(200, self._do_scan)

    # ── Reset module rows to default state ───────────────────────────────────

    def _reset_module_rows(self):
        for mod_name, row in self._module_rows.items():
            row["status"].config(text="—", fg=C["muted"])
            row["blob"].config(text="—", fg=C["dim"])
            row["cb"].config(state="disabled")
            self._module_vars[mod_name].set(False)
        self._const_var.set("—")

    # ── Legacy: J533 probe ───────────────────────────────────────────────────

    def _do_probe(self):
        self._probe_btn.config(state="disabled")
        self._clear_log(self._log)
        self._append_log(self._log, "connecting to J533...\n", "dim")
        self._run(self._probe_task)

    def _probe_task(self):
        probe = None
        try:
            from cp_tools.j533_probe import J533Probe
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            probe.connect()
            self._ui(self._append_log, self._log, "connected to J533\n", "ok")
            report = probe.full_probe()
            self._report = report
            self._ui(self._show_probe_report, report)
        except Exception as e:
            self._ui(self._append_log, self._log,
                     f"probe error: {e}\n", "err")
            self._ui(self._probe_btn.config, state="normal")
        finally:
            # Always release J2534 device handle — prevents channel leak
            # if full_probe() raises partway through.
            if probe is not None:
                try:
                    probe.disconnect()
                except Exception:
                    pass

    def _show_probe_report(self, report):
        self._append_log(self._log,
            "\n── probe report ─────────────────────\n", "hdr")
        data = report.__dict__ if hasattr(report, "__dict__") else report
        for k, v in data.items():
            self._append_log(self._log, f"  {k:<28}  {v}\n")
        self._probe_btn.config(state="normal")
        self._save_btn.config(state="normal")

    def _save_report(self):
        if not self._report:
            return
        path = filedialog.asksaveasfilename(
            title="Save probe report",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if path:
            import json
            from dataclasses import asdict
            try:
                data = (asdict(self._report)
                        if hasattr(self._report, "__dataclass_fields__")
                        else dict(self._report))
                with open(path, "w") as f:
                    json.dump(data, f, indent=2, default=str)
                messagebox.showinfo("Saved", os.path.basename(path))
            except Exception as e:
                messagebox.showerror("Save error", str(e))

    # ── ODX parser ────────────────────────────────────────────────────────────

    def _open_odx(self):
        path = filedialog.askopenfilename(
            title="Open ODX file",
            filetypes=[("ODX files", "*.odx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._clear_log(self._log)
        self._odx_lbl.config(text=os.path.basename(path), fg=C["amber"])
        self._run(self._parse_odx, path)

    def _parse_odx(self, path: str):
        try:
            from cp_tools.odx_parser import ODXParser
            p = ODXParser(path)
            p.parse()
            self._ui(self._show_odx, p, path)
        except Exception as e:
            self._ui(self._append_log, self._log,
                     f"ODX parse error: {e}\n", "err")
            self._ui(self._odx_lbl.config, text="parse error", fg=C["red"])

    def _show_odx(self, p, path: str):
        self._odx_lbl.config(text=os.path.basename(path), fg=C["green"])
        self._append_log(self._log,
            f"── ODX: {os.path.basename(path)} ──\n", "hdr")
        if p.cp_routine_id is not None:
            self._append_log(self._log,
                f"  CP routine ID     0x{p.cp_routine_id:04X}\n", "ok")
        if p.security_level is not None:
            self._append_log(self._log,
                f"  security level    0x{p.security_level:02X}\n", "ok")
        if p.sa2_script:
            self._append_log(self._log,
                f"  SA2 script        {len(p.sa2_script)} bytes\n", "ok")
        self._append_log(self._log,
            f"  DID map           {len(p.did_map)} entries\n")
        for did, entry in list(p.did_map.items())[:20]:
            self._append_log(self._log,
                f"    0x{did:04X}  {entry.name:<36}  {entry.byte_length}B\n",
                "dim")
        if len(p.did_map) > 20:
            self._append_log(self._log,
                f"    ... {len(p.did_map)-20} more\n", "dim")




# ─────────────────────────────────────────────────────────────────────────────
# TAB — Diagnostics (Bus Scan + DTC Read/Clear)
# ─────────────────────────────────────────────────────────────────────────────

# Full VAG C7 module list for bus scan — extends CP_MODULES with more addresses
SCAN_MODULES = [
    # (display name,            addr,  tx_id, rx_id)  — from ConnorHowell/vag-uds-ids
    ("J533  Gateway",            "01",  0x710, 0x77A),
    ("J519  Body Elect.",        "09",  0x70E, 0x778),
    ("J255  Climatronic",        "08",  0x746, 0x7B0),
    ("J285  Instruments",        "17",  0x714, 0x77E),
    ("J234  Airbag",             "15",  0x715, 0x77F),
    ("J794  MMI",                "5F",  0x773, 0x7DD),
    ("J136  Mem.Seat Driver",    "36",  0x74C, 0x7B6),
    ("J521  Mem.Seat Pass.",     "06",  0x74D, 0x7B7),
    ("J518  KESSY",              "03",  0x732, 0x79C),
    ("J525  Sound System",       "47",  0x73A, 0x7A4),
    ("J527  Steer.Column",       "16",  0x70C, 0x776),
    ("J393  Central Comfort",    "46",  0x70D, 0x777),
    ("J623  Engine (ECU)",       "01",  0x7E0, 0x7E8),
    ("J743  DSG/TCU",            "02",  0x7E1, 0x7E9),
    ("J104  ABS/ESC",            "03",  0x713, 0x77D),
    ("J428  ACC Radar",          "76",  0x757, 0x7C1),
    ("J540  PDC",                "6C",  0x74E, 0x7B8),
    ("J844  Lane Assist",        "6D",  0x756, 0x7C0),
    ("J769  Side Assist",        "3C",  0x757, 0x7C1),
    ("J587  El. Steering",       "44",  0x712, 0x77C),
]

# Standard UDS DTC status mask meanings
DTC_STATUS = {
    0x01: "testFailed",
    0x02: "testFailedThisMonitoringCycle",
    0x04: "pendingDTC",
    0x08: "confirmedDTC",
    0x10: "testNotCompletedSinceLastClear",
    0x20: "testFailedSinceLastClear",
    0x40: "testNotCompletedThisMonitoringCycle",
    0x80: "warningIndicatorRequested",
}

# VAG DTC format: 3 bytes = [byte1][byte2][byte3]
# byte1 high nibble = system prefix (P/C/B/U)
# Format them as standard OBD codes
def format_dtc(dtc_bytes: bytes) -> str:
    if len(dtc_bytes) < 2:
        return "INVALID"
    b1, b2 = dtc_bytes[0], dtc_bytes[1]
    prefix = ["P", "C", "B", "U"][(b1 >> 6) & 0x03]
    num = ((b1 & 0x3F) << 8) | b2
    return f"{prefix}{num:04X}"


class DiagTab(_Tab):
    """
    Diagnostics tab — Bus scan and DTC read/clear for all VAG modules.

    Workflow:
      1. ⟳ Bus Scan    — probes all known module addresses, shows what's present
      2. ◈ Read DTCs   — reads stored DTCs from all present modules (UDS 0x19)
      3. ✕ Clear DTCs  — clears all DTCs from selected modules (UDS 0x14)
    """

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._present:    dict = {}   # mod_name → (tx, rx)
        self._dtcs:       dict = {}   # mod_name → list of (code, status)
        self._unenrolled: list = []   # present on bus but not in constellation
        self._scan_entries: list = [] # raw entries from decode_constellation
        self._module_rows: dict = {}
        self._module_vars: dict = {}
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Action bar
        act = _card(self, padx=10, pady=8)
        act.pack(fill="x", padx=14, pady=(8, 2))

        row1 = _frame(act)
        row1.pack(fill="x")

        self._scan_btn = _btn(row1, "⟳  Bus Scan",
                              self._do_bus_scan, primary=True, state="disabled")
        self._scan_btn.pack(side="left")

        self._dtc_btn = _btn(row1, "◈  Read DTCs",
                             self._do_read_dtcs, state="disabled")
        self._dtc_btn.pack(side="left", padx=(8, 0))
        tip(self._dtc_btn, 'UDS 0x19 02 09 - reads stored and pending DTCs.\nShows P/C/B/U codes with confirmed/pending status.')

        self._clear_btn = _btn(row1, "✕  Clear DTCs (selected)",
                               self._do_clear_dtcs, state="disabled")
        self._clear_btn.pack(side="left", padx=(8, 0))
        tip(self._clear_btn, 'UDS 0x14 FFFFFF - clears all DTCs from selected modules.\nConfirmation required. Re-run Read DTCs to confirm.')

        self._sel_all_btn = _btn(row1, "☑ Select All",
                                 self._select_all, state="disabled")
        self._sel_all_btn.pack(side="right")

        # Row 2 — retrofit / adopt new modules
        row2 = _frame(act)
        row2.pack(fill="x", pady=(6, 0))
        self._adopt_btn = _btn(
            row2, "⊕  Adopt New Modules into Constellation",
            self._do_adopt, state="disabled")
        self._adopt_btn.pack(side="left")
        tip(self._adopt_btn, 'Enroll modules present on bus but not in J533 constellation.\nUse after retrofitting hardware (night vision, new ECU etc).\nWrites IKA key + sets constellation bit per slot.')
        self._adopt_lbl = tk.Label(row2, text="",
            bg=C["surface"], fg=C["amber"],
            font=("Courier New", 9))
        self._adopt_lbl.pack(side="left", padx=(10, 0))

        # Status
        self._status_var = tk.StringVar(value="connect to vehicle first")
        tk.Label(act, textvariable=self._status_var,
                 bg=C["surface"], fg=C["muted"],
                 font=("Courier New", 10)).pack(anchor="w", pady=(6, 0))

        # Summary banner
        self._summary_var = tk.StringVar(value="")
        self._summary_lbl = tk.Label(self,
                                     textvariable=self._summary_var,
                                     bg=C["bg"], fg=C["muted"],
                                     font=("Courier New", 12, "bold"),
                                     pady=3)
        self._summary_lbl.pack(fill="x", padx=14, pady=(2, 0))

        # Module grid
        grid_card = _card(self, padx=10, pady=6)
        grid_card.pack(fill="x", padx=14, pady=(2, 2))

        hdr = _frame(grid_card, bg=C["surface"])
        hdr.pack(fill="x", pady=(0, 3))
        for col, width, text in [
            (0, 2,  ""),
            (1, 22, "MODULE"),
            (2, 8,  "STATUS"),
            (3, 8,  "DTCs"),
            (4, 55, "FAULT CODES"),
        ]:
            tk.Label(hdr, text=text, bg=C["surface"], fg=C["muted"],
                     font=("Courier New", 8), width=width,
                     anchor="w").grid(row=0, column=col, sticky="w", padx=2)

        self._grid_frame = _frame(grid_card, bg=C["surface"])
        self._grid_frame.pack(fill="x")
        self._build_module_rows()

        # DTC detail log
        _section(self, "dtc detail")
        self._log = _log_widget(self)
        self._log.pack(fill="both", expand=True, padx=14, pady=(0, 8))

    def _build_module_rows(self):
        for mod_name, addr, tx, rx in SCAN_MODULES:
            var = tk.BooleanVar(value=False)
            self._module_vars[mod_name] = var

            row = _frame(self._grid_frame, bg=C["surface"])
            row.pack(fill="x", pady=1)

            cb = tk.Checkbutton(row, variable=var,
                                bg=C["surface"], fg=C["green"],
                                activebackground=C["surface"],
                                selectcolor=C["bg"],
                                state="disabled")
            cb.grid(row=0, column=0, padx=2)

            name_lbl = tk.Label(row, text=mod_name,
                                bg=C["surface"], fg=C["muted"],
                                font=("Courier New", 10), width=22, anchor="w")
            name_lbl.grid(row=0, column=1, padx=2, sticky="w")

            status_lbl = tk.Label(row, text="—",
                                  bg=C["surface"], fg=C["muted"],
                                  font=("Courier New", 10), width=8, anchor="w")
            status_lbl.grid(row=0, column=2, padx=2)

            dtc_count_lbl = tk.Label(row, text="—",
                                     bg=C["surface"], fg=C["muted"],
                                     font=("Courier New", 10), width=8, anchor="w")
            dtc_count_lbl.grid(row=0, column=3, padx=2)

            codes_lbl = tk.Label(row, text="—",
                                 bg=C["surface"], fg=C["dim"],
                                 font=("Courier New", 9), anchor="w", width=55)
            codes_lbl.grid(row=0, column=4, padx=2, sticky="w")

            self._module_rows[mod_name] = {
                "cb": cb, "status": status_lbl,
                "dtc_count": dtc_count_lbl, "codes": codes_lbl
            }

    # ── Connect / Disconnect ──────────────────────────────────────────────────

    def on_connect(self):
        self._scan_btn.config(state="normal")
        self._status_var.set("ready — click Bus Scan")

    def on_disconnect(self):
        self._scan_btn.config(state="disabled")
        self._dtc_btn.config(state="disabled")
        self._clear_btn.config(state="disabled")
        self._sel_all_btn.config(state="disabled")
        self._adopt_btn.config(state="disabled")
        self._adopt_lbl.config(text="")
        self._status_var.set("connect to vehicle first")
        self._reset_rows()


    # ── Adopt / Retrofit — enroll new modules into J533 constellation ─────────
    # Triggered when Bus Scan finds modules present but not in constellation.
    # Example: OEM Night Vision retrofit, new seat module, any hardware add.
    # Writes IKA key to each new module then updates J533 DID 0x04A3.

    def _do_adopt(self):
        if not self._unenrolled:
            self._append_log(self._log,
                "No unenrolled modules — run Bus Scan first.\n", "warn")
            return
        import tkinter.messagebox as mb
        names = "\n".join(f"  - {n}" for n,_,_,_ in self._unenrolled)
        if not mb.askyesno(
            "Adopt New Modules",
            f"Enroll {len(self._unenrolled)} new module(s) into J533:\n\n"
            + names + "\n\n"
            "Writes IKA key + updates constellation (DID 0x04A3).\n"
            "Continue?",
            icon="question"
        ):
            return
        self._adopt_btn.config(state="disabled")
        self._scan_btn.config(state="disabled")
        self._clear_log(self._log)
        self._append_log(self._log,
            f"-- Adopt {len(self._unenrolled)} New Module(s) -----------------\n",
            "hdr")
        self._run(self._adopt_task)

    def _adopt_task(self):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        # Get best IKA blob — prefer blob from CP Tools scan, fall back to known
        ika_blob = KNOWN_IKA_BLOB
        for tab in self.mw._tabs:
            if hasattr(tab, "_blob_var"):
                try:
                    b = bytes.fromhex(tab._blob_var.get().replace(" ", ""))
                    if len(b) == 34 and any(x != 0 for x in b):
                        ika_blob = b
                        log("  Using IKA blob from CP Tools tab\n", "ok")
                        break
                except Exception:
                    pass

        adopted = []   # (name, tx, rx, slot) successfully written

        # ── Write IKA key to each unenrolled module ───────────────────────────
        for mod_name, tx, rx, slot in self._unenrolled:
            log(f"\n  {mod_name}  TX=0x{tx:03X}\n", "hdr")
            try:
                from cp_tools.j533_probe import J533Probe
                probe = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )
                cfg = dict(configs.default_client_config)
                cfg["data_identifiers"] = {0x00BE: _BytesCodec}
                cfg["request_timeout"]  = 10
                conn = probe._make_conn(tx, rx)
                with Client(conn, request_timeout=10, config=cfg) as c:
                    c.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)

                    # SA2 unlock
                    try:
                        from sa2_seed_key.sa2_script import Sa2Algorithm
                        sr = c.request_seed(0x03)
                        key = Sa2Algorithm().compute_key(bytes(sr.service_data.seed))
                        c.send_key(0x04, key)
                        log("    SA2 unlocked\n", "ok")
                    except Exception as e:
                        log(f"    SA2 error: {e}\n", "err")
                        continue

                    # Check existing key — if populated skip write
                    try:
                        rv = c.read_data_by_identifier([0x00BE])
                        existing = bytes(rv.service_data.values[0x00BE])
                        if any(x != 0 for x in existing):
                            log("    Already has IKA key - skipping write\n", "ok")
                            adopted.append((mod_name, tx, rx, slot))
                            continue
                    except Exception:
                        pass

                    # Write IKA key
                    c.write_data_by_identifier(0x00BE, ika_blob)
                    # Verify readback
                    rv2 = c.read_data_by_identifier([0x00BE])
                    rb  = bytes(rv2.service_data.values[0x00BE])
                    if rb == ika_blob:
                        log(f"    IKA key written + verified\n", "ok")
                        adopted.append((mod_name, tx, rx, slot))
                    else:
                        log("    Verify mismatch\n", "err")
            except Exception as e:
                log(f"    Error: {e}\n", "err")

        if not adopted:
            log("\nNo modules written — aborting constellation update.\n", "err")
            self._ui(self._scan_btn.config, state="normal")
            return

        # ── Update J533 constellation — set bit for each adopted slot ─────────
        log(f"\n  Updating constellation for {len(adopted)} adopted module(s)\n",
            "hdr")
        try:
            from cp_tools.j533_probe import J533Probe
            p533 = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            cfg2 = dict(configs.default_client_config)
            cfg2["data_identifiers"] = {0x04A3: _BytesCodec}
            cfg2["request_timeout"]  = 10
            conn2 = p533._make_conn(0x710, 0x77A)
            with Client(conn2, request_timeout=10, config=cfg2) as c2:
                c2.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                # Read current constellation
                rc = c2.read_data_by_identifier([0x04A3])
                const = bytearray(rc.service_data.values[0x04A3])
                log(f"  Before: {const.hex().upper()}\n", "dim")

                # Set the bit for each adopted module slot
                for _, _, _, slot in adopted:
                    byte_i = slot // 8
                    bit_i  = slot % 8
                    if byte_i < len(const):
                        const[byte_i] |= (1 << bit_i)
                        log(f"  Slot {slot}: byte[{byte_i}] bit {bit_i} set\n",
                            "ok")

                # SA2 unlock J533
                from sa2_seed_key.sa2_script import Sa2Algorithm
                sr = c2.request_seed(0x03)
                key = Sa2Algorithm().compute_key(bytes(sr.service_data.seed))
                c2.send_key(0x04, key)

                # Write updated constellation
                c2.write_data_by_identifier(0x04A3, bytes(const))

                # Verify
                rv = c2.read_data_by_identifier([0x04A3])
                rb = bytearray(rv.service_data.values[0x04A3])
                log(f"  After:  {rb.hex().upper()}\n", "ok")

                if rb == const:
                    log("  Constellation updated\n", "ok")
                    log("\nAdoption complete - cycle ignition then run Bus Scan.\n",
                        "ok")
                    self._ui(self._adopt_lbl.config,
                             text=f"{len(adopted)} module(s) adopted",
                             fg=C["green"])
                else:
                    log("  Verify mismatch\n", "err")

        except Exception as e:
            log(f"  Constellation error: {e}\n", "err")

        self._ui(self._scan_btn.config, state="normal")

    def _reset_rows(self):
        for mod_name, row in self._module_rows.items():
            row["status"].config(text="—", fg=C["muted"])
            row["dtc_count"].config(text="—", fg=C["muted"])
            row["codes"].config(text="—", fg=C["dim"])
            row["cb"].config(state="disabled")
            self._module_vars[mod_name].set(False)
        self._summary_var.set("")
        self._present = {}
        self._dtcs = {}

    def _select_all(self):
        for mod_name, row in self._module_rows.items():
            if row["cb"]["state"] == "normal":
                self._module_vars[mod_name].set(True)

    # ── Bus Scan ──────────────────────────────────────────────────────────────

    def _do_bus_scan(self):
        self._scan_btn.config(state="disabled")
        self._dtc_btn.config(state="disabled")
        self._clear_btn.config(state="disabled")
        self._sel_all_btn.config(state="disabled")
        self._reset_rows()
        self._clear_log(self._log)
        self._append_log(self._log,
            "── Bus Scan ────────────────────────────────────────────\n", "hdr")
        self._append_log(self._log,
            "  Probing all known module addresses...\n", "dim")
        self._status_var.set("scanning bus...")
        self._run(self._bus_scan_task)

    def _bus_scan_task(self):
        """
        Smart bus scan — ask J533 first, fall back to hardcoded list.

        Step 1: Query J533 DIDs:
          0x2A2A  allocation table  — ECU IDs + name codes per slot
          0x2A26  present bitmap    — which slots are online right now
          0x2A2C  TP-Identifier     — TX CAN ID per slot

        Step 2: decode_constellation() builds the real module list.
          RX = TX + 8  (standard VAG UDS response offset).

        Step 3: For each discovered module, attempt a diagnostic session.
          Any response (positive or NRC) confirms presence.

        Step 4: Fall back to SCAN_MODULES hardcoded list if J533 query fails.
        """
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        # ── Step 1: Query J533 topology ──────────────────────────────────────
        gateway_modules = []   # [(display_name, tx, rx)]  from J533
        j533_query_ok   = False

        log("  Querying J533 topology (0x2A2A / 0x2A26 / 0x2A2C)...\n", "hdr")
        try:
            from cp_tools.j533_probe import J533Probe, ECU_NAME_MAP
            probe_j533 = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            cfg = dict(configs.default_client_config)
            cfg["data_identifiers"] = {
                0x2A2A: _BytesCodec,
                0x2A26: _BytesCodec,
                0x2A2C: _BytesCodec,
                0x04A3: _BytesCodec,
            }
            cfg["request_timeout"] = 5
            cfg["p2_timeout"]      = 2.0   # J533 topology DIDs need extra time
            conn = probe_j533._make_conn(0x710, 0x77A)
            with Client(conn, request_timeout=5, config=cfg) as c:
                c.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                alloc_raw   = None
                present_raw = None
                tp_id_raw   = None
                coded_raw   = bytes(10)

                for did, label in [(0x04A3, "constellation"),
                                   (0x2A2A, "allocation"),
                                   (0x2A26, "present bitmap"),
                                   (0x2A2C, "TP-IDs")]:
                    try:
                        r = c.read_data_by_identifier([did])
                        raw = bytes(r.service_data.values[did])
                        if did == 0x04A3: coded_raw   = raw
                        if did == 0x2A2A: alloc_raw   = raw
                        if did == 0x2A26: present_raw = raw
                        if did == 0x2A2C: tp_id_raw   = raw
                        log(f"    0x{did:04X} {label}: "
                            f"{raw.hex().upper()[:32]}{'...' if len(raw)>16 else ''}\n",
                            "ok")
                    except Exception as e:
                        log(f"    0x{did:04X} {label}: {e}\n", "warn")

            # Decode into module list
            from cp_tools.j533_probe import J533Probe
            entries = J533Probe.decode_constellation(
                coded_raw, alloc_raw, present_raw, tp_id_raw)

            for entry in entries:
                can_id = entry.get("can_id")
                if not can_id or can_id < 0x700:
                    continue  # no valid CAN ID
                tx = can_id
                rx = can_id + 8   # standard VAG UDS response offset
                label = entry.get("ecu_name_label", "?")
                slot  = entry.get("slot", "?")
                present = entry.get("present", False)
                name = f"{label}  [slot {slot}]"
                gateway_modules.append((name, tx, rx, present))

            if gateway_modules:
                j533_query_ok = True
                log(f"  J533 reports {len(gateway_modules)} module slot(s)\n", "ok")
            else:
                log("  J533 returned empty topology — falling back\n", "warn")

        except Exception as e:
            log(f"  J533 topology query failed: {e}\n  Falling back to known address list\n",
                "warn")

        # ── Step 2: Build scan list ───────────────────────────────────────────
        if j533_query_ok:
            # Use J533's list — these are the real modules on this car
            scan_list = [(name, tx, rx) for name, tx, rx, _ in gateway_modules]
            log(f"\n  Scanning {len(scan_list)} module(s) from J533 topology:\n",
                "hdr")
        else:
            # Fallback to hardcoded VAG C7 list
            scan_list = [(name, tx, rx) for name, _, tx, rx in SCAN_MODULES]
            log(f"\n  Scanning {len(scan_list)} known VAG C7 addresses (fallback):\n",
                "hdr")

        # ── Step 3: Probe each module ─────────────────────────────────────────
        # For J533-discovered modules we build dynamic rows, not using the
        # pre-built grid rows. Reset grid and rebuild dynamically.
        if j533_query_ok:
            self._ui(self._rebuild_grid_dynamic, gateway_modules)
            import time; time.sleep(0.1)  # let UI rebuild

        present_count = 0
        # Let J533 session close before probing individual modules
        import time as _bt
        _bt.sleep(2.0)

        seen_tx = set()
        # J533 already queried above for topology — skip to avoid J2534 channel conflict
        seen_tx.add(0x710)

        for mod_name, tx, rx in scan_list:
            if tx in seen_tx:
                continue
            seen_tx.add(tx)

            try:
                from cp_tools.j533_probe import J533Probe
                probe = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )
                cfg2 = dict(configs.default_client_config)
                cfg2["request_timeout"] = 2
                conn2 = probe._make_conn(tx, rx)
                with Client(conn2, request_timeout=2, config=cfg2) as c2:
                    c2.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.defaultSession)
                present_count += 1
                self._present[mod_name] = (tx, rx)
                self._ui(self._set_row_status, mod_name,
                         "present", C["green"], True)
                log(f"  ✓ {mod_name:<35} TX=0x{tx:03X}\n", "ok")

            except udsoncan.exceptions.NegativeResponseException:
                present_count += 1
                self._present[mod_name] = (tx, rx)
                self._ui(self._set_row_status, mod_name,
                         "present", C["green"], True)
                log(f"  ✓ {mod_name:<35} TX=0x{tx:03X} (NRC)\n", "ok")

            except Exception as e:
                if "timeout" in str(e).lower():
                    self._ui(self._set_row_status, mod_name, "absent", C["dim"])
                else:
                    self._ui(self._set_row_status, mod_name, "error", C["amber"])
                    log(f"  ? {mod_name:<35} {str(e)[:35]}\n", "warn")

        source = "J533 topology" if j533_query_ok else "known address list"
        log(f"\n── Scan complete: {present_count} present  [{source}] ──────\n",
            "ok")
        # Find unenrolled — present on bus but bit not set in constellation
        self._unenrolled = []
        self._scan_entries = entries if j533_query_ok and "entries" in dir() else []
        if j533_query_ok and self._scan_entries:
            for entry in self._scan_entries:
                if entry.get("present") and not entry.get("coded"):
                    can_id = entry.get("can_id")
                    if can_id and can_id >= 0x700:
                        name = entry.get("ecu_name_label", "unknown")
                        slot = entry.get("slot", 0)
                        self._unenrolled.append((f"{name} [s{slot}]",
                                                  can_id, can_id + 8, slot))
            if self._unenrolled:
                n = len(self._unenrolled)
                log(f"\n  ⊕ {n} module(s) on bus but NOT in constellation:\n",
                    "warn")
                for nm, tx, _, sl in self._unenrolled:
                    log(f"    {nm}  TX=0x{tx:03X}\n", "warn")
                log("  Click ⊕ Adopt to enroll in J533 constellation.\n",
                    "hdr")
                self._ui(self._adopt_btn.config, state="normal")
                self._ui(self._adopt_lbl.config,
                         text=f"{n} new module(s) not enrolled",
                         fg=C["amber"])
            else:
                self._ui(self._adopt_lbl.config,
                         text="all present modules enrolled",
                         fg=C["green"])

        self._ui(self._summary_var.set,
                 ("{} {} module(s) on bus".format(
                   "✓" if present_count else "—", present_count)))
        self._ui(self._summary_lbl.config,
                 fg=C["green"] if present_count else C["muted"])
        self._ui(self._status_var.set,
                 f"{present_count} modules found — click Read DTCs")
        self._ui(self._scan_btn.config, state="normal")
        if present_count:
            self._ui(self._dtc_btn.config, state="normal")
            self._ui(self._sel_all_btn.config, state="normal")

    def _rebuild_grid_dynamic(self, gateway_modules):
        """Rebuild module grid rows from J533 topology (dynamic — any car)."""
        # Clear existing rows
        for widget in self._grid_frame.winfo_children():
            widget.destroy()
        self._module_rows.clear()
        self._module_vars.clear()

        for name, tx, rx, present in gateway_modules:
            var = tk.BooleanVar(value=False)
            self._module_vars[name] = var
            row = _frame(self._grid_frame, bg=C["surface"])
            row.pack(fill="x", pady=1)
            cb = tk.Checkbutton(row, variable=var,
                                bg=C["surface"], fg=C["green"],
                                activebackground=C["surface"],
                                selectcolor=C["bg"], state="disabled")
            cb.grid(row=0, column=0, padx=2)
            tk.Label(row, text=name, bg=C["surface"], fg=C["muted"],
                     font=("Courier New", 10), width=35,
                     anchor="w").grid(row=0, column=1, padx=2, sticky="w")
            status_lbl = tk.Label(row,
                text="online" if present else "—",
                fg=C["green"] if present else C["dim"],
                bg=C["surface"], font=("Courier New", 10), width=8, anchor="w")
            status_lbl.grid(row=0, column=2, padx=2)
            dtc_lbl = tk.Label(row, text="—", bg=C["surface"], fg=C["muted"],
                                font=("Courier New", 10), width=8, anchor="w")
            dtc_lbl.grid(row=0, column=3, padx=2)
            codes_lbl = tk.Label(row, text="—", bg=C["surface"], fg=C["dim"],
                                  font=("Courier New", 9), anchor="w", width=55)
            codes_lbl.grid(row=0, column=4, padx=2, sticky="w")
            self._module_rows[name] = {
                "cb": cb, "status": status_lbl,
                "dtc_count": dtc_lbl, "codes": codes_lbl
            }

    def _set_row_status(self, mod_name, text, color, enable_cb=False):
        row = self._module_rows.get(mod_name)
        if not row:
            return
        row["status"].config(text=text, fg=color)
        if enable_cb:
            row["cb"].config(state="normal")
            self._module_vars[mod_name].set(True)

    # ── Read DTCs ─────────────────────────────────────────────────────────────

    def _do_read_dtcs(self):
        selected = [m for m, v in self._module_vars.items()
                    if v.get() and m in self._present]
        if not selected:
            self._append_log(self._log,
                "No modules selected.\n", "warn")
            return
        self._dtc_btn.config(state="disabled")
        self._scan_btn.config(state="disabled")
        self._clear_log(self._log)
        self._append_log(self._log,
            f"── Read DTCs — {len(selected)} module(s) ───────────────────────\n",
            "hdr")
        self._status_var.set("reading DTCs...")
        self._run(self._read_dtcs_task, selected)

    def _read_dtcs_task(self, selected: list):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        total_dtcs = 0
        self._dtcs = {}

        for mod_name in selected:
            if mod_name not in self._present:
                continue
            tx, rx = self._present[mod_name]
            log(f"\n  {mod_name}  TX=0x{tx:03X}\n", "hdr")

            try:
                from cp_tools.j533_probe import J533Probe
                probe = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )
                cfg = dict(configs.default_client_config)
                cfg["request_timeout"] = 5
                conn = probe._make_conn(tx, rx)
                with Client(conn, request_timeout=5, config=cfg) as c:
                    # Extended session for full DTC access
                    try:
                        c.change_session(
                            udsoncan.services.DiagnosticSessionControl
                            .Session.extendedDiagnosticSession)
                    except Exception:
                        pass  # some modules answer DTCs in default session

                    # UDS 0x19 02 09 — read all stored DTCs (confirmed + pending)
                    resp = c.get_dtc_by_status_mask(0x09)
                    dtcs = []
                    if hasattr(resp, 'dtcs') and resp.dtcs:
                        for dtc in resp.dtcs:
                            code = format_dtc(dtc.id.to_bytes(3, 'big')
                                              if hasattr(dtc.id, 'to_bytes')
                                              else bytes(dtc.id))
                            status = dtc.status.raw_value if hasattr(
                                dtc, 'status') else 0
                            dtcs.append((code, status))

                    self._dtcs[mod_name] = dtcs
                    count = len(dtcs)
                    total_dtcs += count

                    if dtcs:
                        codes_str = "  ".join(c for c, _ in dtcs[:5])
                        if len(dtcs) > 5:
                            codes_str += f"  +{len(dtcs)-5} more"
                        self._ui(self._module_rows[mod_name]["dtc_count"].config,
                                 text=str(count), fg=C["red"])
                        self._ui(self._module_rows[mod_name]["codes"].config,
                                 text=codes_str, fg=C["amber"])
                        log(f"    {count} DTC(s):\n", "err")
                        for code, status in dtcs:
                            active = "confirmed" if status & 0x08 else "pending"
                            log(f"      {code}  [{active}  0x{status:02X}]\n",
                                "err" if status & 0x08 else "warn")
                    else:
                        self._ui(self._module_rows[mod_name]["dtc_count"].config,
                                 text="0", fg=C["green"])
                        self._ui(self._module_rows[mod_name]["codes"].config,
                                 text="no faults", fg=C["dim"])
                        log("    no DTCs stored ✓\n", "ok")

            except udsoncan.exceptions.NegativeResponseException as nre:
                nrc = nre.response.code if hasattr(nre, "response") else 0
                self._ui(self._module_rows[mod_name]["dtc_count"].config,
                         text="—", fg=C["amber"])
                log(f"    NRC 0x{nrc:02X} — DTC service not supported\n", "warn")
            except Exception as e:
                log(f"    Error: {e}\n", "err")

        # Summary
        log(f"\n── DTC read complete: {total_dtcs} total fault(s) ──────────────\n",
            "ok" if total_dtcs == 0 else "warn")
        self._ui(self._summary_var.set,
                 f"{'✓  NO FAULTS' if total_dtcs == 0 else f'⚠  {total_dtcs} FAULT(S) STORED'}")
        self._ui(self._summary_lbl.config,
                 fg=C["green"] if total_dtcs == 0 else C["red"])
        self._ui(self._status_var.set,
                 f"{total_dtcs} DTC(s) found — select modules and click Clear DTCs to erase")
        self._ui(self._dtc_btn.config, state="normal")
        self._ui(self._scan_btn.config, state="normal")

        if total_dtcs > 0:
            self._ui(self._clear_btn.config, state="normal")

    # ── Clear DTCs ────────────────────────────────────────────────────────────

    def _do_clear_dtcs(self):
        selected = [m for m, v in self._module_vars.items()
                    if v.get() and m in self._present]
        if not selected:
            self._append_log(self._log, "No modules selected.\n", "warn")
            return
        import tkinter.messagebox as mb
        if not mb.askyesno(
            "Clear DTCs",
            f"Clear all DTCs from {len(selected)} module(s)?\n\n"
            + "\n".join(f"  • {m}" for m in selected)
            + "\n\nThis cannot be undone.",
            icon="warning"
        ):
            return
        self._clear_btn.config(state="disabled")
        self._scan_btn.config(state="disabled")
        self._dtc_btn.config(state="disabled")
        self._append_log(self._log,
            f"\n── Clear DTCs — {len(selected)} module(s) ──────────────────────\n",
            "hdr")
        self._run(self._clear_dtcs_task, selected)

    def _clear_dtcs_task(self, selected: list):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        cleared = 0
        for mod_name in selected:
            if mod_name not in self._present:
                continue
            tx, rx = self._present[mod_name]
            log(f"\n  {mod_name}\n", "hdr")
            try:
                from cp_tools.j533_probe import J533Probe
                probe = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )
                cfg = dict(configs.default_client_config)
                cfg["request_timeout"] = 10
                conn = probe._make_conn(tx, rx)
                with Client(conn, request_timeout=10, config=cfg) as c:
                    c.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    # UDS 0x14 FFFFFF — clear all DTCs
                    c.clear_dtc(0xFFFFFF)
                    cleared += 1
                    self._ui(self._module_rows[mod_name]["dtc_count"].config,
                             text="0", fg=C["green"])
                    self._ui(self._module_rows[mod_name]["codes"].config,
                             text="cleared ✓", fg=C["green"])
                    log(f"    DTCs cleared ✓\n", "ok")
            except udsoncan.exceptions.NegativeResponseException as nre:
                nrc = nre.response.code if hasattr(nre, "response") else 0
                log(f"    NRC 0x{nrc:02X} — clear rejected\n", "err")
            except Exception as e:
                log(f"    Error: {e}\n", "err")

        log(f"\n── Clear complete: {cleared}/{len(selected)} modules cleared ───\n",
            "ok" if cleared == len(selected) else "warn")
        self._ui(self._status_var.set,
                 f"DTCs cleared on {cleared} module(s) — rerun Read DTCs to confirm")
        self._ui(self._scan_btn.config, state="normal")
        self._ui(self._dtc_btn.config, state="normal")
        if self._present:
            self._ui(self._clear_btn.config, state="normal")


# TAB 7 — Raw Sniff (passive CAN bus listener)
# ─────────────────────────────────────────────────────────────────────────────

class RawSniffTab(_Tab):
    """
    Passive CAN bus sniffer using J2534 raw CAN mode.

    Opens its own J2534 channel independently of the app's connected state.
    With an OBD splitter, captures all traffic between VCDS/ODIS and the car.
    Includes software ISO-TP reassembly and UDS service decode.
    """

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._sniffing = False
        self._sniffer = None      # J2534CANSniffer instance
        self._raw_frames = []     # all captured CANFrame objects
        self._uds_messages = []   # reassembled ISOTPMessage objects

        info = _card(self, padx=12, pady=8)
        info.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(info, text=(
            "Passive CAN bus listener — opens raw CAN channel via J2534.\n"
            "Use with OBD splitter: VCDS/ODIS talks on one cable, this listens on the other.\n"
            "Captures full UDS exchange including CP removal sequences and GEKO tokens."
        ), fg=C["muted"], bg=C["surface"], font=("Menlo", 10),
            justify="left", wraplength=620).pack(anchor="w")

        # ── Controls row ──────────────────────────────────────────────────────
        ctrl = _frame(self)
        ctrl.pack(fill="x", padx=14, pady=6)
        self._sniff_btn = _btn(ctrl, "start sniff",
                               self._toggle_sniff, primary=True)
        self._sniff_btn.pack(side="left")
        _btn(ctrl, "clear", self._clear_all).pack(side="left", padx=8)
        _btn(ctrl, "save log", self._save_log).pack(side="left")
        _btn(ctrl, "save pcap", self._save_pcap).pack(side="left", padx=8)
        tip(self._sniff_btn,
            "Opens raw CAN channel on J2534 adapter.\n"
            "Does NOT require app to be connected — uses its own channel.\n"
            "Make sure no other tab has an active J2534 session.")

        # ── View mode toggle ──────────────────────────────────────────────────
        tk.Label(ctrl, text="  view:",
                 fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left", padx=(16, 4))
        self._view_mode = tk.StringVar(value="uds")
        for val, label in [("uds", "UDS decoded"), ("raw", "raw CAN")]:
            tk.Radiobutton(ctrl, text=label, variable=self._view_mode,
                           value=val, fg=C["text"], bg=C["bg"],
                           selectcolor=C["surface"],
                           activebackground=C["bg"], activeforeground=C["blue"],
                           font=("Menlo", 10),
                           command=self._refresh_view).pack(side="left", padx=2)

        # ── Filter row ────────────────────────────────────────────────────────
        filt_row = _frame(self)
        filt_row.pack(fill="x", padx=14, pady=2)
        tk.Label(filt_row, text="filter CAN ID",
                 fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left")
        self._filter_var = tk.StringVar(value="")
        tk.Entry(filt_row, textvariable=self._filter_var,
                 bg=C["surface"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 10), width=8, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left", padx=4)
        tk.Label(filt_row, text="(hex, e.g. 710 or 710,77A for multiple)",
                 fg=C["dim"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=4)

        # ── DLL path ──────────────────────────────────────────────────────────
        dll_row = _frame(self)
        dll_row.pack(fill="x", padx=14, pady=2)
        tk.Label(dll_row, text="J2534 DLL",
                 fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left")
        self._dll_var = tk.StringVar(value="")
        tk.Entry(dll_row, textvariable=self._dll_var,
                 bg=C["surface"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 10), width=50, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left", padx=4, fill="x", expand=True)
        _btn(dll_row, "browse", self._browse_dll).pack(side="left", padx=4)
        tip(self._dll_var._root,
            "Path to J2534 DLL. Auto-filled from Connect tab if available.")

        # ── Log area ──────────────────────────────────────────────────────────
        _section(self, "frame log")
        log_outer, self._hex_log = _scrolled_text(self, height=20)
        log_outer.pack(fill="both", expand=True, padx=14, pady=4)
        self._hex_log.tag_config("tx",   foreground=C["blue"])
        self._hex_log.tag_config("rx",   foreground=C["green"])
        self._hex_log.tag_config("fc",   foreground=C["dim"])
        self._hex_log.tag_config("uds",  foreground=C["amber"])
        self._hex_log.tag_config("nrc",  foreground=C["red"])
        self._hex_log.tag_config("dim",  foreground=C["dim"])
        self._hex_log.tag_config("hdr",  foreground=C["blue"])

        # Status bar
        status = _frame(self)
        status.pack(fill="x", padx=14, pady=(0, 4))
        self._frame_count = 0
        self._msg_count = 0
        self._count_lbl = tk.Label(status, text="frames: 0  |  messages: 0",
                                   fg=C["dim"], bg=C["bg"],
                                   font=("Menlo", 9))
        self._count_lbl.pack(side="left")
        self._status_lbl = tk.Label(status, text="",
                                    fg=C["dim"], bg=C["bg"],
                                    font=("Menlo", 9))
        self._status_lbl.pack(side="right")

    def on_connect(self):
        # Auto-fill DLL path from interface panel if J2534
        if self.mw.interface and self.mw.interface.upper() == "J2534":
            if self.mw.iface_path and not self._dll_var.get():
                self._dll_var.set(self.mw.iface_path)

    def on_disconnect(self):
        # Auto-fill on disconnect too (DLL path is still valid)
        pass

    def _browse_dll(self):
        path = filedialog.askopenfilename(
            title="Select J2534 DLL",
            filetypes=[("DLL files", "*.dll"), ("All files", "*.*")],
        )
        if path:
            self._dll_var.set(path)

    def _parse_filter(self):
        """Parse filter entry into a set of CAN IDs, or None for pass-all."""
        filt_str = self._filter_var.get().strip()
        if not filt_str:
            return None
        ids = set()
        for part in filt_str.replace(" ", ",").split(","):
            part = part.strip()
            if part:
                try:
                    ids.add(int(part, 16))
                except ValueError:
                    pass
        return ids if ids else None

    def _toggle_sniff(self):
        if self._sniffing:
            self._sniffing = False
            self._sniff_btn.config(text="start sniff",
                                   fg="#0d1117", bg=C["blue"])
            self._status_lbl.config(text="stopped", fg=C["dim"])
        else:
            dll = self._dll_var.get().strip()
            if not dll:
                # Try to get from interface panel
                if (self.mw.interface and self.mw.interface.upper() == "J2534"
                        and self.mw.iface_path):
                    dll = self.mw.iface_path
                    self._dll_var.set(dll)
                else:
                    messagebox.showwarning("No DLL",
                        "Enter the J2534 DLL path or select an interface on the Connect tab.")
                    return
            self._sniffing = True
            self._frame_count = 0
            self._msg_count = 0
            self._raw_frames.clear()
            self._uds_messages.clear()
            self._sniff_btn.config(text="stop sniff",
                                   fg=C["text"], bg=C["btn"])
            self._status_lbl.config(text="opening CAN channel...", fg=C["amber"])
            self._run(self._sniff_loop)

    def _sniff_loop(self):
        """
        Open raw CAN channel via J2534 and read all bus traffic.
        Reassembles ISO-TP and decodes UDS services in real time.
        """
        dll_path = self._dll_var.get().strip()

        try:
            from lib.connections.can_sniffer import (
                J2534CANSniffer, ISOTPReassembler, CANFrame,
            )
        except ImportError:
            self._ui(self._append_log, self._hex_log,
                     "ERROR: lib/connections/can_sniffer.py not found\n", "nrc")
            self._ui(self._sniff_btn.config, text="start sniff",
                     fg="#0d1117", bg=C["blue"])
            self._sniffing = False
            return

        sniffer = None
        try:
            sniffer = J2534CANSniffer(dll_path)
            sniffer.open()
            self._sniffer = sniffer
            self._ui(self._status_lbl.config,
                     text="listening on CAN bus...", fg=C["green"])
            self._ui(self._append_log, self._hex_log,
                     "── CAN sniffer active — listening for traffic ──\n", "hdr")

            reassembler = ISOTPReassembler(timeout_ms=3000)
            filt = self._parse_filter()
            t0 = None
            view_mode = self._view_mode.get()

            while self._sniffing:
                frame = sniffer.read_frame(timeout_ms=50)
                if frame is None:
                    # Check for stale partial transfers
                    if t0 is not None:
                        for msg in reassembler.flush_stale(
                                int(time.time() * 1_000_000)):
                            self._uds_messages.append(msg)
                            self._msg_count += 1
                            if view_mode == "uds":
                                self._ui(self._show_uds_msg, msg, t0)
                    continue

                # First frame sets time reference
                if t0 is None:
                    t0 = frame.timestamp_us

                # Apply CAN ID filter
                if filt is not None and frame.can_id not in filt:
                    continue

                self._raw_frames.append(frame)
                self._frame_count += 1

                # Show raw frame if in raw mode
                cur_view = self._view_mode.get()
                if cur_view != view_mode:
                    view_mode = cur_view  # user toggled mid-capture

                if view_mode == "raw":
                    self._ui(self._show_raw_frame, frame, t0)

                # Feed to ISO-TP reassembler
                msg = reassembler.feed(frame)
                if msg is not None:
                    self._uds_messages.append(msg)
                    self._msg_count += 1
                    if view_mode == "uds":
                        self._ui(self._show_uds_msg, msg, t0)

                # Update counter every 10 frames
                if self._frame_count % 10 == 0:
                    self._ui(self._count_lbl.config,
                             text=f"frames: {self._frame_count}"
                                  f"  |  messages: {self._msg_count}")

        except Exception as e:
            self._ui(self._append_log, self._hex_log,
                     f"sniffer error: {e}\n", "nrc")
        finally:
            if sniffer:
                try:
                    sniffer.close()
                except Exception:
                    pass
            self._sniffer = None
            self._sniffing = False
            self._ui(self._sniff_btn.config, text="start sniff",
                     fg="#0d1117", bg=C["blue"])
            self._ui(self._status_lbl.config, text="stopped", fg=C["dim"])
            self._ui(self._count_lbl.config,
                     text=f"frames: {self._frame_count}"
                          f"  |  messages: {self._msg_count}")

    def _show_raw_frame(self, frame, t0):
        """Display a single raw CAN frame in the log."""
        rel_ms = (frame.timestamp_us - t0) / 1000.0
        tag = "tx" if frame.direction == "TX" else "rx"
        lbl = f"  {frame.label}" if frame.label else ""
        hex_data = " ".join(f"{b:02X}" for b in frame.data)

        # Annotate ISO-TP PCI type
        pci = ""
        if frame.data:
            pci_type = (frame.data[0] >> 4) & 0x0F
            pci = {0: "SF", 1: "FF", 2: "CF", 3: "FC"}.get(pci_type, "")
            if pci:
                pci = f" [{pci}]"

        line = f"{rel_ms:10.1f}  {frame.direction}  [{frame.can_id:03X}]{lbl}  {hex_data}{pci}\n"
        self._append_log(self._hex_log, line, tag)

    def _show_uds_msg(self, msg, t0):
        """Display a reassembled UDS message in the log."""
        rel_ms = (msg.timestamp_us - t0) / 1000.0
        tag = "tx" if msg.direction == "TX" else "rx"
        lbl = f"  {msg.label}" if msg.label else ""
        uds = msg.decode_uds()

        # Color negative responses red
        if uds.startswith("NegativeResponse"):
            tag = "nrc"

        hex_short = " ".join(f"{b:02X}" for b in msg.payload[:20])
        if len(msg.payload) > 20:
            hex_short += f"... ({len(msg.payload)}B total)"

        frames_note = f"  [{msg.frame_count}F]" if msg.frame_count > 1 else ""
        line = (f"{rel_ms:10.1f}  {msg.direction}  [{msg.can_id:03X}]{lbl}"
                f"{frames_note}  {uds}\n"
                f"{'':>14}{hex_short}\n")
        self._append_log(self._hex_log, line, tag)

    def _refresh_view(self):
        """Re-render the log when user toggles between raw/UDS view."""
        if self._sniffing:
            return  # live mode handles view switch in the loop
        self._clear_log(self._hex_log)
        if not self._raw_frames:
            return
        t0 = self._raw_frames[0].timestamp_us

        if self._view_mode.get() == "raw":
            for frame in self._raw_frames:
                self._show_raw_frame(frame, t0)
        else:
            for msg in self._uds_messages:
                self._show_uds_msg(msg, t0)

    def _clear_all(self):
        self._clear_log(self._hex_log)
        self._raw_frames.clear()
        self._uds_messages.clear()
        self._frame_count = 0
        self._msg_count = 0
        self._count_lbl.config(text="frames: 0  |  messages: 0")

    def _save_log(self):
        content = self._hex_log.get("1.0", "end")
        if not content.strip():
            messagebox.showinfo("Empty", "Nothing to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save sniff log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            with open(path, "w") as f:
                f.write(content)
            messagebox.showinfo("Saved", os.path.basename(path))

    def _save_pcap(self):
        """Save raw frames as a minimal PCAP file for Wireshark analysis."""
        if not self._raw_frames:
            messagebox.showinfo("Empty", "No frames captured.")
            return
        path = filedialog.asksaveasfilename(
            title="Save PCAP",
            defaultextension=".pcap",
            filetypes=[("PCAP files", "*.pcap"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            import struct as _s
            with open(path, "wb") as f:
                # PCAP global header (link type 227 = SocketCAN)
                f.write(_s.pack("<IHHiIII",
                    0xA1B2C3D4, 2, 4, 0, 0, 128, 227))
                t0 = self._raw_frames[0].timestamp_us
                for frame in self._raw_frames:
                    ts_sec = (frame.timestamp_us - t0) // 1_000_000
                    ts_usec = (frame.timestamp_us - t0) % 1_000_000
                    # SocketCAN frame: 4-byte ID (big-endian) + 1-byte DLC + 3 pad + 8 data
                    can_id_be = frame.can_id
                    if frame.is_extended:
                        can_id_be |= 0x80000000
                    dlc = len(frame.data)
                    can_frame = _s.pack(">I", can_id_be) + bytes([dlc, 0, 0, 0])
                    can_frame += frame.data.ljust(8, b"\x00")
                    f.write(_s.pack("<IIII", ts_sec, ts_usec, len(can_frame), len(can_frame)))
                    f.write(can_frame)
            messagebox.showinfo("Saved", f"{os.path.basename(path)}\n"
                                f"{len(self._raw_frames)} frames")
        except Exception as e:
            messagebox.showerror("Save error", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class VehicleTab(_Tab):
    """Module-centric home: live bus scan (installed list) + the module firmware DB."""

    def __init__(self, parent, mw):
        super().__init__(parent, mw)

        _section(self, "Installed modules — live bus scan")
        bar = _frame(self)
        bar.pack(fill="x", padx=14, pady=(2, 4))
        self._bus = tk.StringVar(value="ALL")
        ttk.Combobox(bar, textvariable=self._bus, values=["ALL", "DRIVE", "CONV"],
                     width=8, state="readonly", font=("Menlo", 10)).pack(side="left")
        sb = _btn(bar, "Scan bus", self._scan)
        sb.pack(side="left", padx=6)
        tip(sb, "Probe every known C7 module address (needs a connection).")
        self._log = _log_widget(self, height=11)

        _section(self, "Module firmware DB")
        sbar = _frame(self)
        sbar.pack(fill="x", padx=14, pady=(2, 4))
        self._q = tk.StringVar()
        e = tk.Entry(sbar, textvariable=self._q, bg=C["bg"], fg=C["text"],
                     insertbackground=C["text"], font=("Menlo", 10), bd=0,
                     highlightbackground=C["border"], highlightthickness=1)
        e.pack(side="left", fill="x", expand=True, ipady=3)
        e.bind("<Return>", lambda _e: self._search())
        _btn(sbar, "Search", self._search).pack(side="left", padx=(6, 0))
        _btn(sbar, "CP candidates", self._candidates).pack(side="left", padx=(6, 0))
        self._dblog = _log_widget(self, height=9)
        self._ui(self._search)

    def _scan(self):
        if not self.mw.connected:
            self._append_log(self._log, "Not connected — connect an interface first.\n", "warn")
            return
        self._clear_log(self._log)
        self._append_log(self._log, "Scanning known modules…\n", "hdr")
        iface = self.mw.interface or "J2534"
        path = self.mw.iface_path
        bus = None if self._bus.get() == "ALL" else self._bus.get()

        def cb(dm):
            self._ui(self._append_log, self._log, str(dm) + "\n",
                     "ok" if dm.present else "dim")

        def work():
            try:
                from core.module_scan import scan_modules
                res = scan_modules(iface, path, only_bus=bus, timeout=1.5, callback=cb)
                n = sum(1 for d in res if d.present)
                self._ui(self._append_log, self._log,
                         f"\n{n} present / {len(res)} probed.\n", "hdr")
            except Exception as ex:
                self._ui(self._append_log, self._log, f"scan error: {ex}\n", "err")

        self._run(work)

    def _search(self):
        from core.module_db import all_modules
        self._clear_log(self._dblog)
        try:
            mods = all_modules()
        except Exception as ex:
            self._append_log(self._dblog, f"DB error: {ex}\n", "err")
            return
        q = self._q.get().strip().lower()
        if q:
            mods = [m for m in mods
                    if q in " ".join(str(v) for v in m.values()).lower()]
        self._append_log(self._dblog, f"{len(mods)} module(s)\n", "hdr")
        for m in mods:
            self._append_log(self._dblog, self._fmt(m) + "\n")

    def _candidates(self):
        from core.module_db import patch_candidates
        self._clear_log(self._dblog)
        try:
            mods = patch_candidates()
        except Exception as ex:
            self._append_log(self._dblog, f"DB error: {ex}\n", "err")
            return
        self._append_log(self._dblog, f"{len(mods)} CP-patch candidate(s)\n", "hdr")
        for m in mods:
            self._append_log(self._dblog, self._fmt(m) + "\n")

    @staticmethod
    def _fmt(m):
        part = m.get("part") or m.get("part_number") or m.get("pn") or "?"
        name = (m.get("name") or m.get("desc") or m.get("system") or "")
        arch = m.get("arch") or m.get("cpu") or "?"
        signed = m.get("signed", m.get("signing", "?"))
        fmt = m.get("format") or m.get("data_format") or "?"
        flags = []
        if m.get("cp_slave") or m.get("cp"):
            flags.append("CP")
        if m.get("have_patch") or m.get("patch"):
            flags.append("patch")
        tail = ("  [" + ",".join(flags) + "]") if flags else ""
        return "  %-13s %-24s arch=%-6s signed=%-4s fmt=%s%s" % (
            part, str(name)[:24], arch, str(signed), fmt, tail)


class FlashwareTab(_Tab):
    """Offline flashware bench — FRF/ODX/SGO decode + repack. No connection needed."""

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._frf_blocks = None
        self._frf_odx = None
        self._frf_name = None
        self._sgo_src = None

        _section(self, "FRF / ODX  —  decode + repack")
        b = _frame(self)
        b.pack(fill="x", padx=14, pady=(2, 4))
        _btn(b, "Open FRF…", self._open_frf).pack(side="left")
        _btn(b, "Replace block from BIN…", self._replace_block).pack(side="left", padx=6)
        _btn(b, "Save FRF…", self._save_frf, primary=True).pack(side="left")

        _section(self, "SGO  —  SGML Object File")
        b2 = _frame(self)
        b2.pack(fill="x", padx=14, pady=(2, 4))
        _btn(b2, "Open SGO…", self._open_sgo).pack(side="left")
        _btn(b2, "Repack SGO…", self._save_sgo, primary=True).pack(side="left", padx=6)

        self._log = _log_widget(self, height=18)
        self._append_log(
            self._log,
            "Offline flashware bench: decode and repack factory containers.\n"
            "Right-to-repair — signed modules (HVAC RSA, BCM2/DSG AES) repack\n"
            "structurally but will NOT flash. See research/bin-to-frf-sgo-packing.md.\n\n",
            "dim")

    def _key_path(self):
        import pathlib
        p = pathlib.Path(__file__).resolve().parent.parent / "data" / "frf.key"
        return str(p) if p.exists() else None

    def _open_frf(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Open FRF", filetypes=[("FRF", "*.frf"), ("All files", "*.*")])
        if not path:
            return

        def work():
            try:
                import os
                from flasher.frf_loader import FrfLoader
                from flasher.payload_codec import parse_segments, detect_codec
                ld = FrfLoader(self._key_path())
                blocks = ld.extract_blocks(path)
                odx = ld.get_odx(path)
                segs = {s["block_num"]: s for s in parse_segments(odx)
                        if s["block_num"] is not None}
                self._frf_blocks = blocks
                self._frf_odx = odx
                self._frf_name = os.path.splitext(os.path.basename(path))[0] + ".odx"
                self._ui(self._clear_log, self._log)
                self._ui(self._append_log, self._log,
                         f"Loaded {os.path.basename(path)}\n", "hdr")
                for bn in sorted(blocks):
                    s = segs.get(bn, {})
                    codec = detect_codec(s.get("dfi"), s.get("comp") is not None)
                    self._ui(self._append_log, self._log,
                             f"  block {bn}: {len(blocks[bn]):>9,} B   codec={codec}\n", "ok")
            except Exception as ex:
                self._ui(self._append_log, self._log, f"open FRF error: {ex}\n", "err")

        self._run(work)

    def _replace_block(self):
        from tkinter import filedialog, simpledialog
        if not self._frf_blocks:
            self._append_log(self._log, "Open an FRF first.\n", "warn")
            return
        loaded = ", ".join(map(str, sorted(self._frf_blocks)))
        bn = simpledialog.askinteger("Replace block",
                                     f"Block number to replace\n(loaded: {loaded})")
        if bn is None or bn not in self._frf_blocks:
            self._append_log(self._log, "Cancelled / unknown block.\n", "warn")
            return
        path = filedialog.askopenfilename(
            title="Replacement BIN", filetypes=[("BIN", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "rb") as f:
            data = f.read()
        old = len(self._frf_blocks[bn])
        self._frf_blocks[bn] = data
        self._append_log(self._log,
                         f"block {bn}: {old:,} -> {len(data):,} B  (CRC32 recomputed on save)\n", "ok")

    def _save_frf(self):
        from tkinter import filedialog
        if not self._frf_blocks:
            self._append_log(self._log, "Open an FRF first.\n", "warn")
            return
        out = filedialog.asksaveasfilename(title="Save FRF", defaultextension=".frf",
                                           filetypes=[("FRF", "*.frf")])
        if not out:
            return

        def work():
            try:
                import pathlib
                from flasher.frf_pack import frf_pack
                key = pathlib.Path(self._key_path()).read_bytes()
                frf = frf_pack(self._frf_blocks, self._frf_odx, key, self._frf_name)
                with open(out, "wb") as f:
                    f.write(frf)
                self._ui(self._append_log, self._log,
                         f"Wrote {out} ({len(frf):,} B). CRC32 recomputed.\n", "ok")
                self._ui(self._append_log, self._log,
                         "Functional pack (deflate differs cosmetically); RSA-signed "
                         "modules won't flash.\n", "dim")
            except Exception as ex:
                self._ui(self._append_log, self._log, f"save FRF error: {ex}\n", "err")

        self._run(work)

    def _open_sgo(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Open SGO", filetypes=[("SGO", "*.sgo"), ("All files", "*.*")])
        if not path:
            return

        def work():
            try:
                import os
                from cp_tools.sgo_unpack import parse
                with open(path, "rb") as f:
                    src = f.read()
                self._sgo_src = src
                sf = parse(src)
                self._ui(self._clear_log, self._log)
                self._ui(self._append_log, self._log,
                         f"Loaded {os.path.basename(path)}\n", "hdr")
                self._ui(self._append_log, self._log,
                         f"  PN={getattr(sf, 'part_number', '?')}  "
                         f"SW={getattr(sf, 'sw_version', '?')}  "
                         f"blocks={len(sf.blocks)}\n", "ok")
                for blk in sf.blocks:
                    self._ui(self._append_log, self._log,
                             f"  addr=0x{blk.addr:06X} crypt=0x{blk.crypt_byte:02X} "
                             f"mode={blk.mode.value}  {blk.note}\n")
            except Exception as ex:
                self._ui(self._append_log, self._log, f"open SGO error: {ex}\n", "err")

        self._run(work)

    def _save_sgo(self):
        from tkinter import filedialog
        if not self._sgo_src:
            self._append_log(self._log, "Open an SGO first.\n", "warn")
            return
        out = filedialog.asksaveasfilename(title="Repack SGO", defaultextension=".sgo",
                                           filetypes=[("SGO", "*.sgo")])
        if not out:
            return
        try:
            from cp_tools.sgo_pack import repack, verify_checksum
            data = repack(self._sgo_src)
            with open(out, "wb") as f:
                f.write(data)
            self._append_log(self._log,
                             f"Wrote {out} ({len(data):,} B)  byte-exact={data == self._sgo_src}  "
                             f"checksum-OK={verify_checksum(data)}\n", "ok")
        except Exception as ex:
            self._append_log(self._log, f"repack SGO error: {ex}\n", "err")


class CPLabTab(_Tab):
    """Offline Component-Protection cipher bench — gateway AES model + HVAC IKA self-test."""

    def __init__(self, parent, mw):
        super().__init__(parent, mw)

        _section(self, 'J533 gateway CP cipher  (AES-128, key "LEAR D4 Gateway.")')
        g = _frame(self)
        g.pack(fill="x", padx=14, pady=(2, 4))
        tk.Label(g, text="block (32 hex):", fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left")
        self._gw = tk.StringVar(value="01020304050607080807060504030201")
        tk.Entry(g, textvariable=self._gw, bg=C["bg"], fg=C["text"],
                 insertbackground=C["text"], font=("Menlo", 10), bd=0,
                 highlightbackground=C["border"], highlightthickness=1,
                 width=36).pack(side="left", padx=6, ipady=2)
        gb = _frame(self)
        gb.pack(fill="x", padx=14, pady=(0, 4))
        _btn(gb, "gw_enc", lambda: self._gw_op(True)).pack(side="left")
        _btn(gb, "gw_dec", lambda: self._gw_op(False)).pack(side="left", padx=6)
        _btn(gb, "Run KAT", self._gw_kat).pack(side="left")

        _section(self, "HVAC J255 IKA handshake model")
        hb = _frame(self)
        hb.pack(fill="x", padx=14, pady=(2, 4))
        _btn(hb, "Run IKA self-test", self._hvac_selftest).pack(side="left")

        self._log = _log_widget(self, height=15)
        self._append_log(
            self._log,
            "Offline CP cipher models. validate() + BDM-dump ingest need real bench\n"
            "data (CS / identity / data-flash) and stay CLI/bench-gated. See research/.\n\n",
            "dim")

    def _gw_op(self, enc):
        try:
            from cp_tools.gw_cp_cipher import gw_enc, gw_dec
            b = bytes.fromhex(self._gw.get().strip().replace(" ", ""))
            if len(b) != 16:
                raise ValueError("need exactly 16 bytes (32 hex)")
            out = (gw_enc if enc else gw_dec)(b)
            self._append_log(self._log,
                             f"{'gw_enc' if enc else 'gw_dec'}({b.hex()}) = {out.hex()}\n", "ok")
        except Exception as ex:
            self._append_log(self._log, f"error: {ex}\n", "err")

    def _gw_kat(self):
        self._run_capture("cp_tools.gw_cp_cipher", "selftest")

    def _hvac_selftest(self):
        self._run_capture("cp_tools.hvac_ika_cipher", "_selftest")

    def _run_capture(self, mod, fn):
        def work():
            import io, importlib, contextlib
            buf = io.StringIO()
            try:
                m = importlib.import_module(mod)
                f = getattr(m, fn)
                with contextlib.redirect_stdout(buf):
                    rv = f()
                self._ui(self._clear_log, self._log)
                self._ui(self._append_log, self._log, buf.getvalue() or "(no output)\n")
                self._ui(self._append_log, self._log,
                         f"\n{mod}.{fn}() -> {rv}\n",
                         "ok" if (rv or rv is None) else "warn")
            except Exception as ex:
                self._ui(self._append_log, self._log, buf.getvalue() or "")
                self._ui(self._append_log, self._log, f"error: {ex}\n", "err")

        self._run(work)


class CPCaptureTab(_Tab):
    """Live CerberusCAN passive capture + ISO-TP / VW TP 2.0 decode (CP handshake)."""

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._dev = None
        self._frames = []
        self._stop = None
        self._n = 0
        self._cur_bus = 1
        import queue
        self._q = queue.Queue()       # M2: live-logger frames (worker -> main thread)
        self._live = False            # True while the Head-2 MON live-view is running
        self._ids = set()

        _section(self, "CerberusCAN capture  (sniff + live MON)")
        bar = _frame(self)
        bar.pack(fill="x", padx=14, pady=(2, 4))
        tk.Label(bar, text="port", fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left")
        self._port = tk.StringVar()
        self._port_cb = ttk.Combobox(bar, textvariable=self._port, width=12,
                                     font=("Menlo", 10))
        self._port_cb.pack(side="left", padx=(4, 8))
        tk.Label(bar, text="bus", fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left")
        self._bus = tk.StringVar(value="1 (500k diag)")
        ttk.Combobox(bar, textvariable=self._bus, state="readonly", width=15,
                     values=["1 (500k diag, active)", "2 (500k logger, MON)"],
                     font=("Menlo", 10)).pack(side="left", padx=(4, 8))
        _btn(bar, "Detect", self._detect).pack(side="left")

        b2 = _frame(self)
        b2.pack(fill="x", padx=14, pady=(0, 4))
        _btn(b2, "Start sniff", self._start, primary=True).pack(side="left")
        _btn(b2, "Live (MON)", self._start_live, primary=True).pack(side="left", padx=6)
        _btn(b2, "Stop", self._stop_capture).pack(side="left", padx=6)
        _btn(b2, "Save CSV…", self._save).pack(side="left")
        _btn(b2, "Open CSV…", self._open_csv).pack(side="left", padx=6)
        _btn(b2, "Decode", self._decode, primary=True).pack(side="left", padx=6)

        self._status = tk.Label(self, text="idle", fg=C["dim"], bg=C["bg"],
                                font=("Menlo", 10), anchor="w")
        self._status.pack(fill="x", padx=14)
        self._log = _log_widget(self, height=15)
        self._append_log(
            self._log,
            "Passive capture via CerberusCAN, then ISO-TP + VW TP 2.0 decode.\n"
            "Bus 1 (500k, OBD 6/14) sees ALL ODIS diagnostics incl. gateway-routed\n"
            "comfort modules. Run during an ODIS CP session -> Stop -> Decode, and the\n"
            "TrainICA / 0x00BE / SecurityAccess get flagged.\n"
            "Live (MON): firmware >=0.6.0 Head-2 always-on logger — frames stream in real\n"
            "time and accumulate for Save/Decode (Head 2 = 2nd tap on the SAME 6/14 bus).\n\n", "dim")
        self._detect()

    def _set_status(self, text, color):
        self._status.config(text=text, fg=color)

    def _detect(self):
        try:
            from transport.cerberus_serial import detect_ports
            ports = [p for _l, p in detect_ports()]
        except Exception:
            ports = []
        self._port_cb["values"] = ports
        if ports and not self._port.get():
            self._port.set(ports[0])
        self._append_log(self._log, "ports: %s\n" % (", ".join(ports) or "none detected"), "dim")

    def _poll(self):
        if self._dev is not None:
            self._set_status("capturing bus %d — %d frames" % (self._cur_bus, self._n), C["green"])
            self.after(300, self._poll)

    def _start(self):
        if self._dev is not None:
            self._append_log(self._log, "already capturing.\n", "warn")
            return
        port = self._port.get().strip()
        if not port:
            self._append_log(self._log, "pick a port first (Detect).\n", "warn")
            return
        import threading
        self._cur_bus = 1 if self._bus.get().startswith("1") else 2
        self._frames = []
        self._n = 0
        self._stop = threading.Event()

        def on_frame(t, cid, data):
            self._n += 1

        def work():
            try:
                from transport.cerberus_serial import Cerberus
                self._dev = Cerberus(port)
                if not self._dev.ping():
                    self._ui(self._append_log, self._log,
                             "no PONG (check port/firmware) — capturing anyway\n", "warn")
                self._ui(self._poll)
                frames = self._dev.sniff(bus=self._cur_bus, ms=0,
                                         on_frame=on_frame, stop=self._stop.is_set)
                self._frames = frames
                self._ui(self._append_log, self._log,
                         "captured %d frames.\n" % len(frames), "ok")
            except Exception as ex:
                self._ui(self._append_log, self._log, "capture error: %s\n" % ex, "err")
            finally:
                try:
                    if self._dev:
                        self._dev.close()
                except Exception:
                    pass
                self._dev = None
                self._ui(self._set_status, "idle (%d frames)" % len(self._frames), C["dim"])

        self._append_log(self._log,
                         "starting capture on %s bus %d — trigger your event…\n"
                         % (port, self._cur_bus), "hdr")
        self._run(work)

    def _stop_capture(self):
        if self._stop is not None:
            self._stop.set()
            self._append_log(self._log, "stop requested…\n", "dim")
        else:
            self._append_log(self._log, "not capturing.\n", "warn")

    def _start_live(self):
        """Head-2 always-on background logger (MON) — live streaming view (firmware >= 0.6.0).
        Head 2 is a 2nd transceiver tapped on the SAME 6/14 bus, held listen-only, so this can
        run WHILE Head 1 drives an active UDS/CP exchange. Frames accumulate for Save / Decode."""
        if self._dev is not None:
            self._append_log(self._log, "already capturing.\n", "warn")
            return
        port = self._port.get().strip()
        if not port:
            self._append_log(self._log, "pick a port first (Detect).\n", "warn")
            return
        import threading
        self._cur_bus = 2
        self._frames = []
        self._n = 0
        self._ids = set()
        self._live = True
        self._stop = threading.Event()

        def work():
            try:
                from transport.cerberus_serial import Cerberus
                self._dev = Cerberus(port)
                if not self._dev.ping():
                    self._ui(self._append_log, self._log,
                             "no PONG (need firmware >= 0.6.0 for MON) — continuing\n", "warn")
                self._dev.set_mon_callback(lambda t, cid, data: self._q.put((t, cid, data)))
                rep = self._dev.mon_on()
                self._ui(self._append_log, self._log,
                         "MON live on Head 2 (%s) — drive Head 1 anytime; frames stream below\n"
                         % rep, "hdr")
                self._ui(self._live_poll)
                while not self._stop.is_set():
                    self._dev.pump(0.05)        # drain serial -> route M2 -> queue
            except Exception as ex:
                self._ui(self._append_log, self._log, "live error: %s\n" % ex, "err")
            finally:
                for fn in ("mon_off", "close"):
                    try:
                        if self._dev:
                            getattr(self._dev, fn)()
                    except Exception:
                        pass
                self._dev = None
                self._live = False
                self._ui(self._set_status, "idle (%d frames)" % self._n, C["dim"])

        self._append_log(self._log,
                         "starting MON live logger on %s (Head 2)…\n" % port, "hdr")
        self._run(work)

    def _live_poll(self):
        """Main-thread: drain queued M2: frames -> accumulate + stream a throttled live line."""
        import queue
        last = None
        try:
            while True:                          # drain everything queued this tick
                t, cid, data = self._q.get_nowait()
                self._frames.append((t, cid, data))
                self._n += 1
                self._ids.add(cid)
                last = (t, cid, data)
        except queue.Empty:
            pass
        if last is not None:                     # one summary line per tick (no flood)
            t, cid, data = last
            self._append_log(self._log,
                             "  live %6d fr  %3d IDs   last %03X %s\n"
                             % (self._n, len(self._ids), cid, data.hex().upper()), None)
        if self._dev is not None and self._live:
            self._set_status("live: %d frames, %d IDs (Head 2 logger)" % (self._n, len(self._ids)),
                             C["green"])
            self.after(250, self._live_poll)

    def _save(self):
        from tkinter import filedialog
        if not self._frames:
            self._append_log(self._log, "nothing captured yet.\n", "warn")
            return
        out = filedialog.asksaveasfilename(title="Save capture CSV", defaultextension=".csv",
                                           filetypes=[("CSV", "*.csv")])
        if not out:
            return
        try:
            with open(out, "w") as f:
                f.write("ms,id,data\n")
                for t, cid, data in self._frames:
                    f.write("%d,%X,%s\n" % (t, cid, data.hex().upper()))
            self._append_log(self._log, "saved %d frames -> %s\n" % (len(self._frames), out), "ok")
        except Exception as ex:
            self._append_log(self._log, "save error: %s\n" % ex, "err")

    def _open_csv(self):
        from tkinter import filedialog
        import os
        path = filedialog.askopenfilename(
            title="Open capture CSV", filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            from cp_tools.can_decode import parse_csv
            self._frames = parse_csv(path)
        except Exception as ex:
            self._append_log(self._log, "open CSV error: %s\n" % ex, "err")
            return
        self._append_log(self._log,
                         "loaded %d frames from %s\n" % (len(self._frames), os.path.basename(path)), "hdr")
        self._set_status("loaded %d frames" % len(self._frames), C["dim"])
        self._decode()

    def _decode(self):
        if not self._frames:
            self._append_log(self._log, "nothing captured yet.\n", "warn")
            return

        def work():
            try:
                from cp_tools.can_decode import decode_frames, ascii_of
                msgs = decode_frames(self._frames)
                self._ui(self._clear_log, self._log)
                self._ui(self._append_log, self._log,
                         "decoded %d UDS/KWP message(s)\n\n" % len(msgs), "hdr")
                cp = []
                for m in msgs:
                    asc = ascii_of(m.payload) if any(32 <= c < 127 for c in m.payload) else ""
                    tag = "ok" if m.cp else None
                    self._ui(self._append_log, self._log,
                             "%8d %03X %-12s %-5s %-32s %s\n"
                             % (m.t, m.can_id, (m.module or ""), m.transport, m.label, asc[:22]), tag)
                    if m.cp:
                        cp.append(m)
                if cp:
                    self._ui(self._append_log, self._log,
                             "\n=== %d CP-relevant ===\n" % len(cp), "hdr")
                    for m in cp:
                        self._ui(self._append_log, self._log,
                                 "  %03X %-12s %-20s %s\n"
                                 % (m.can_id, (m.module or ""), m.cp, m.payload.hex()), "err")
                else:
                    self._ui(self._append_log, self._log,
                             "\n(no CP-relevant services — reads/scan only)\n", "dim")
            except Exception as ex:
                self._ui(self._append_log, self._log, "decode error: %s\n" % ex, "err")

        self._run(work)


class MainWindow(tk.Tk):
    """
    Root application window.

    Shared state consumed by all tabs:
        self.ecu          — current ECUDef (set by ConnectTab)
        self.interface    — interface type string
        self.iface_path   — port or DLL path
        self.connected    — True when interface is connected
        self.ble_bridge   — BLEBridgeSync instance or None
    """

    VERSION = "0.3.3"

    def __init__(self, ecu_key: Optional[str] = None):
        super().__init__()

        # Shared state
        self.ecu:         Optional[ECUDef]  = SIMOS85
        self.interface:   Optional[str]     = None
        self.iface_path:  Optional[str]     = None
        self.connected:   bool              = False
        self.ble_bridge                     = None

        self._tabs: List[_Tab] = []

        self._setup_window()
        self._setup_style()
        self._build_titlebar()
        self._build_tabs()

        if ecu_key:
            self._select_ecu(ecu_key)

    def _setup_window(self):
        self.title(f"Simos Tuning Suite  v{self.VERSION}")
        self.geometry("860x680")
        self.minsize(760, 560)
        self.configure(bg=C["bg"])
        try:
            self.iconbitmap("")       # clear default icon
        except Exception:
            pass

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook",
                         background=C["bg"], borderwidth=0)
        style.configure("TNotebook.Tab",
                         background=C["surface"],
                         foreground=C["muted"],
                         font=("Menlo", 11),
                         padding=[14, 7],
                         borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C["bg"]),
                               ("active",  C["btn"])],
                  foreground=[("selected", C["text"]),
                               ("active",  C["text"])])
        style.configure("TCombobox",
                         fieldbackground=C["surface"],
                         background=C["surface"],
                         foreground=C["text"],
                         arrowcolor=C["muted"],
                         borderwidth=0,
                         relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", C["surface"])],
                  foreground=[("readonly", C["text"])])
        style.configure("TScrollbar",
                         background=C["surface"],
                         troughcolor=C["bg"],
                         arrowcolor=C["muted"],
                         borderwidth=0)

    def _build_titlebar(self):
        bar = tk.Frame(self, bg=C["surface"],
                       highlightbackground=C["border"],
                       highlightthickness=1)
        bar.pack(fill="x")

        # Left — dots + name
        left = tk.Frame(bar, bg=C["surface"])
        left.pack(side="left", padx=12, pady=8)
        for col in ("#ff5f57", "#febc2e", "#28c840"):
            tk.Label(left, text="●", fg=col, bg=C["surface"],
                     font=("Menlo", 9)).pack(side="left")
        tk.Label(bar, text="  simos tuning suite",
                 fg=C["text"], bg=C["surface"],
                 font=("Menlo", 12, "bold")).pack(side="left")

        # Centre — ECU + connection indicator
        centre = tk.Frame(bar, bg=C["surface"])
        centre.pack(side="left", padx=20)
        self._ecu_lbl = tk.Label(centre, text="Simos8.5  3.0T TFSI",
                                 fg=C["muted"], bg=C["surface"],
                                 font=("Menlo", 10))
        self._ecu_lbl.pack(side="left")

        # Right — status pill
        right = tk.Frame(bar, bg=C["surface"])
        right.pack(side="right", padx=12)
        self._conn_dot = tk.Label(right, text="●", fg=C["dim"],
                                  bg=C["surface"], font=("Menlo", 10))
        self._conn_dot.pack(side="left")
        self._conn_lbl = tk.Label(right, text="disconnected",
                                  fg=C["dim"], bg=C["surface"],
                                  font=("Menlo", 10))
        self._conn_lbl.pack(side="left", padx=(3, 0))
        tk.Label(right, text=f"  v{self.VERSION}",
                 fg=C["dim"], bg=C["surface"],
                 font=("Menlo", 9)).pack(side="left", padx=(10, 0))

    def _build_tabs(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, pady=(4, 0))

        tab_defs = [
            ("  connect  ",     ConnectTab),
            ("  vehicle  ",     VehicleTab),
            ("  ecu info  ",    EcuInfoTab),
            ("  flash  ",       FlashTab),
            ("  flashware  ",   FlashwareTab),
            ("  logger  ",      LoggerTab),
            ("  cp tools  ",    CPToolsTab),
            ("  cp lab  ",      CPLabTab),
            ("  cp capture  ",  CPCaptureTab),
            ("  raw sniff  ",   RawSniffTab),
            ("  diagnostics",   DiagTab),
            ("  trans  ",         TransLoggerTab),
        ]

        for title, cls in tab_defs:
            tab = cls(nb, self)
            nb.add(tab, text=title)
            self._tabs.append(tab)

    def _update_ecu_label(self, name: str):
        short = name.split("(")[0].strip()
        self._ecu_lbl.config(text=short, fg=C["muted"])

    def _select_ecu(self, key: str):
        """Pre-select ECU by short key (e.g. 'S85', 'SC8')."""
        map_ = _ecus()
        for name, ecu in map_.items():
            if key.upper() in name.upper():
                self.ecu = ecu
                self._update_ecu_label(name)
                # Auto-suggest matching transmission
                default = ECU_DEFAULT_TRANS.get(key.upper()) if ECU_DEFAULT_TRANS else None
                if default and TRANS_REGISTRY and default in TRANS_REGISTRY:
                    for tab in self._tabs:
                        if isinstance(tab, TransLoggerTab):
                            tab._set_trans_by_key(default)
                            break
                break

    def _on_connected(self, interface: str, path: str):
        label = f"{interface}:{path}" if path else interface
        self._conn_dot.config(fg=C["green"])
        self._conn_lbl.config(text=f"connected  {label}", fg=C["green"])
        for tab in self._tabs[1:]:     # all except ConnectTab itself
            tab.on_connect()
        # sim_runner may call additional setup after connect.

    def _on_disconnected(self):
        self._conn_dot.config(fg=C["dim"])
        self._conn_lbl.config(text="disconnected", fg=C["dim"])
        for tab in self._tabs[1:]:
            tab.on_disconnect()

    def get_connection(self):
        """
        Returns a fresh udsoncan connection for the current interface.
        Callers run this in a thread.
        """
        if not self.connected or not self.interface:
            raise RuntimeError("Not connected")
        return _make_connection(
            self.ecu,
            self.interface,
            interface_path = self.iface_path,
            ble_bridge     = self.ble_bridge,
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level    = logging.INFO,
        format   = "%(asctime)s  %(name)-28s  %(levelname)s  %(message)s",
        datefmt  = "%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="Simos Tuning Suite GUI")
    ap.add_argument("--ecu", default=None,
                    help="Pre-select ECU (S85, SC8, SCG, SC1, SC2)")
    ap.add_argument("--debug", action="store_true",
                    help="Enable DEBUG logging")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    app = MainWindow(ecu_key=args.ecu)
    app.mainloop()


if __name__ == "__main__":
    main()
