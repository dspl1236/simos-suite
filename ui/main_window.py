"""
ui/main_window.py — Simos Tuning Suite main application window

Tabbed desktop GUI (Tkinter + ttk) for the full simos-suite workflow.
Tabs:
    1. Connect    — InterfacePanel: hardware interface selection, ECU picker
    2. ECU Info   — Read all VW identification DIDs, display live
    3. Flash      — Read CAL block / write CAL block with progress bar
    4. Tune       — Calibration table editor (2D grid + value scaling)
    5. Logger     — Live DID poller with configurable channels
    6. CP Tools   — J533 probe, constellation capture, ODX viewer
    7. Raw Sniff  — Pass-through hex log of all ISO-TP frames

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
        flash_cal, read_ecu_info, FlashProgress, _make_connection,
    )
    from tuner.cal_parser import CalParser
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
        tip(self._read_btn, 'Read calibration block from ECU into Tune tab.\nRequires extended session + SA2 unlock.')
        self._status = tk.Label(bot, text="not connected",
                                fg=C["dim"], bg=C["bg"],
                                font=("Menlo", 10))
        self._status.pack(side="left", padx=12)

    def on_connect(self):
        self._read_btn.config(state="normal")
        self._status.config(text="connected — press read", fg=C["green"])

    def on_disconnect(self):
        self._read_btn.config(state="disabled")
        self._status.config(text="not connected", fg=C["dim"])
        for v in self._rows.values():
            v.config(text="—", fg=C["text"])

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

    def _show_error(self, msg: str):
        self._read_btn.config(state="normal")
        self._status.config(text=f"error: {msg}", fg=C["red"])
        import logging; logging.getLogger("SimosSuite.GUI").error("ECU info error: %s", msg)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — Flash
# ─────────────────────────────────────────────────────────────────────────────

class FlashTab(_Tab):
    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._cal_bytes: Optional[bytes] = None
        self._cal_path = tk.StringVar(value="no file loaded")

        _section(self, "CAL block")

        # File row
        file_card = _card(self, padx=12, pady=10)
        file_card.pack(fill="x", padx=14, pady=4)
        fr = _frame(file_card, bg=C["surface"])
        fr.pack(fill="x")
        _btn(fr, "open .bin", self._open_file).pack(side="left")
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
        tip(self._write_btn, 'Write modified calibration back to ECU.\nRequires extended session + SA2 unlock.\nDo not interrupt once started.')
        tip(self._write_btn, 'SA2 unlock + WriteDataByIdentifier(0x00BE).\nWrites IKA key to checked modules.\nVerifies readback after each write.')

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
            self._write_btn.config(state="normal")
            self._verify_btn.config(state="normal")

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
                self._write_btn.config(state="normal")
                self._verify_btn.config(state="normal")
            self._log_line(f"loaded {fname}  ({sz:,} bytes)\n", "ok")
        except Exception as e:
            messagebox.showerror("File error", str(e))

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
        from flasher.uds_flash import _make_connection, FlashProgress
        import udsoncan

        def cb(p):
            self._ui(self._update_progress, p)

        ecu      = self.mw.ecu
        blk      = ecu.cal_block
        conn     = None

        try:
            conn = _make_connection(
                ecu,
                self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = self.mw.ble_bridge,
            )

            cfg = dict(udsoncan.configs.default_client_config)
            cfg["request_timeout"] = 30

            with __import__("udsoncan").client.Client(conn, request_timeout=30, config=cfg) as client:
                cb(FlashProgress("CONNECT", "Opening extended session...", 5))
                client.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)

                cb(FlashProgress("CONNECT", f"Reading CAL block {blk.number} "
                                            f"({blk.length:#x} bytes)...", 10))

                # Read in chunks via ReadMemoryByAddress (0x23)
                CHUNK   = 0x7F0
                cal     = bytearray()
                addr    = blk.base_addr
                remain  = blk.length

                while remain > 0:
                    size   = min(CHUNK, remain)
                    pct    = 10 + int(85 * (blk.length - remain) / blk.length)
                    cb(FlashProgress("TRANSFER",
                                     f"0x{addr:08X}  {blk.length-remain:#x}/{blk.length:#x}",
                                     pct, "CAL"))

                    resp = client.read_memory_by_address(
                        udsoncan.MemoryLocation(addr, size, 32, 32))
                    chunk_bytes = resp.service_data.memory_block
                    cal.extend(chunk_bytes)
                    addr   += len(chunk_bytes)
                    remain -= len(chunk_bytes)

                cal_bytes = bytes(cal)
                cb(FlashProgress("DONE", f"Read {len(cal_bytes):,} bytes", 100, "CAL"))

            # Hand off to Tune tab
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
            self._write_btn.config(state="normal")
            self._verify_btn.config(state="normal")
        self._log_line(f"read OK — {sz:,} bytes\n", "ok")
        # Hand to Tune tab
        for tab in self.mw._tabs:
            if hasattr(tab, "load_bytes"):
                tab.load_bytes(cal_bytes, f"ECU-read_{ecu_name}.bin")
                self._log_line("loaded into Tune tab automatically\n", "ok")
                break

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
        else:
            self._prog_label.config(text="failed", fg=C["red"])

    def _flash_error(self, msg: str):
        self._set_buttons(True)
        self._log_line(f"exception: {msg}\n", "err")
        self._prog_label.config(text=f"error: {msg}", fg=C["red"])
        import logging; logging.getLogger("SimosSuite.GUI").error("Flash error: %s", msg)

    def _set_buttons(self, enabled: bool):
        s = "normal" if enabled else "disabled"
        if self.mw.connected:
            self._read_btn.config(state=s)
        if self._cal_bytes and self.mw.connected:
            self._write_btn.config(state=s)
            self._verify_btn.config(state=s)

    def _log_line(self, text: str, tag: str = ""):
        self._append_log(self._log, text, tag)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — Tune
# ─────────────────────────────────────────────────────────────────────────────


class TuneTab(_Tab):
    """
    Calibration table editor for Simos ECU .bin files.

    Features
    ────────
    •  All 14 Simos8.5 (S85) tables with correct row/column axis labels
       (RPM × Load or RPM × Throttle, confirmed breakpoints from cal_parser)
    •  Heat-map coloring: blue (low) → teal → green → amber → red (high)
    •  Editable cells — click, type, Enter/Tab; color updates live
    •  1×N tables (MAF, throttle, limits) shown as a 2D line chart
       in addition to the flat editable row
    •  Meta bar: table name, unit, min/max, axis descriptions
    •  Notes strip: tuning guidance per table
    •  Lean diagnosis button: runs CalParser.diagnose_lean()
    •  Fix checksums + save — safe write-back with CRC32 repair
    •  Load from sim: accepts synthetic CAL bytes from sim_runner
    """

    # Simos8.5 standard axis breakpoints (confirmed from cal_parser.py)
    _RPM_16  = [500,750,1000,1250,1500,2000,2500,3000,
                3500,4000,4500,5000,5500,6000,6500,7000]
    _LOAD_16 = [20,40,60,80,100,120,150,180,
                220,260,320,380,450,530,620,720]   # mg/stroke
    _COOL_8  = [-20,0,20,40,60,80,100,120]         # coolant °C
    _PEDAL_32 = [int(i*100/31) for i in range(32)] # 0–100%
    _MAF_32  = [int(i*150) for i in range(32)]     # mV×10

    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._parser = None
        self._table_key = tk.StringVar()
        self._entry_widgets: Dict[Tuple[int,int], tk.Entry] = {}
        self._chart_canvas = None
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self):
        # File row
        _section(self, "calibration file")
        file_row = _frame(self)
        file_row.pack(fill="x", padx=14, pady=4)
        _btn(file_row, "open CAL .bin", self._open_cal).pack(side="left")
        _btn(file_row, "lean diagnosis", self._lean_diag).pack(side="left", padx=6)
        _btn(file_row, "tuning guide", self._open_guide).pack(side="left", padx=6)
        self._file_lbl = tk.Label(file_row, text="no file loaded",
                                   fg=C["muted"], bg=C["bg"], font=("Menlo", 10))
        self._file_lbl.pack(side="left", padx=10)

        # Table selector + save
        _section(self, "table")
        sel_row = _frame(self)
        sel_row.pack(fill="x", padx=14, pady=4)
        self._table_combo = ttk.Combobox(sel_row, textvariable=self._table_key,
                                          state="disabled", font=("Menlo", 10), width=44)
        self._table_combo.pack(side="left")
        self._table_combo.bind("<<ComboboxSelected>>", self._on_table_select)
        self._save_btn = _btn(sel_row, "fix checksums + save",
                               self._save_cal, state="disabled")
        self._save_btn.pack(side="right")

        # Meta / notes strip
        self._meta_var  = tk.StringVar(value="")
        self._notes_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._meta_var,
                 fg=C["muted"], bg=C["bg"], font=("Menlo", 9), anchor="w",
                 padx=14).pack(fill="x")
        tk.Label(self, textvariable=self._notes_var,
                 fg="#3fb950", bg=C["bg"], font=("Menlo", 9),
                 anchor="w", padx=14, wraplength=900).pack(fill="x")

        # Chart area (shown only for 1×N tables)
        self._chart_frame = _frame(self)
        self._chart_canvas_widget = tk.Canvas(self._chart_frame, bg=C["bg"],
                                               height=110, highlightthickness=0)
        self._chart_canvas_widget.pack(fill="x", padx=14, pady=(4, 0))

        # Table editor (canvas + scrollbars)
        _section(self, "table editor")
        cf = _frame(self)
        cf.pack(fill="both", expand=True, padx=14, pady=4)

        self._canvas = tk.Canvas(cf, bg=C["bg"], highlightthickness=0)
        sx = ttk.Scrollbar(cf, orient="horizontal", command=self._canvas.xview)
        sy = ttk.Scrollbar(cf, orient="vertical",   command=self._canvas.yview)
        self._canvas.config(xscrollcommand=sx.set, yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self._canvas.pack(fill="both", expand=True)

        # Status
        self._info_lbl = tk.Label(self, text="open a CAL .bin to begin",
                                   fg=C["dim"], bg=C["bg"], font=("Menlo", 9))
        self._info_lbl.pack(pady=4)

    # ── on_connect — sim integration ──────────────────────────────────────────

    def on_connect(self):
        """Called by MainWindow on connection. Enable file open button."""
        # save_btn enabled only after a file is loaded
        pass   # sim_runner calls load_bytes() directly after connect

    def on_disconnect(self):
        """Disable write/save on disconnect — reading from ECU no longer valid."""
        if hasattr(self, "_save_btn"):
            self._save_btn.config(state="disabled")
        self._info_lbl.config(
            text="disconnected — CAL data may be stale", fg=C["amber"])

    def load_bytes(self, cal_bytes: bytes, filename: str = "synthetic_cal.bin"):
        """Load CAL from raw bytes — used by sim_runner and flash-read results."""
        try:
            from tuner.cal_parser import CalParser
            ecu = self.mw.ecu or SIMOS85
            self._parser = CalParser(ecu, cal_bytes)
            self._parser.decode()
            self._populate_combo()
            self._file_lbl.config(text=f"{filename}  ({len(cal_bytes):,} bytes)",
                                   fg=C["green"])
            self._save_btn.config(state="normal")
            self._show_first_table()
        except Exception as e:
            self._info_lbl.config(text=f"parse error: {e}", fg=C["red"])

    # ── File open ─────────────────────────────────────────────────────────────

    def _open_cal(self):
        path = filedialog.askopenfilename(
            title="Open CAL .bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.load_bytes(open(path, "rb").read(), os.path.basename(path))
        except Exception as e:
            messagebox.showerror("Open error", str(e))

    def _populate_combo(self):
        if not self._parser:
            return
        # Build display names: "boost_setpoint  —  Boost Pressure Setpoint"
        try:
            names = [f"{k}  —  {meta.name}"
                     for k, meta in self._parser.tables.items()]
        except Exception:
            names = list(getattr(self._parser, "_decoded", {}).keys())
        self._table_combo.config(values=names, state="readonly")
        if names:
            self._table_combo.current(0)
            self._table_key.set(names[0])

    def _show_first_table(self):
        if self._table_combo["values"]:
            self._on_table_select()

    # ── Table selection ───────────────────────────────────────────────────────

    def _on_table_select(self, *_):
        if not self._parser:
            return
        raw_key = self._table_key.get().split("  —  ")[0].strip()
        try:
            arr  = self._parser.table(raw_key)
            meta = self._parser.tables.get(raw_key)
            self._draw_table(raw_key, arr, meta)
        except Exception as e:
            self._info_lbl.config(text=str(e), fg=C["red"])

    # ── Table renderer ────────────────────────────────────────────────────────

    def _draw_table(self, key: str, arr, meta=None):
        import numpy as np
        self._canvas.delete("all")
        self._entry_widgets.clear()

        is_1d = arr.ndim == 1
        if is_1d:
            arr2d = arr.reshape(1, -1)
        else:
            arr2d = arr

        rows, cols = arr2d.shape
        vmin, vmax = float(arr2d.min()), float(arr2d.max())
        vrange = vmax - vmin if vmax > vmin else 1.0

        # Determine axis labels
        row_labels = self._row_labels(key, rows)
        col_labels = self._col_labels(key, cols)
        unit = meta.unit if meta else ""
        row_axis = meta.row_axis if meta else ""
        col_axis = meta.col_axis if meta else ""

        # Meta bar
        self._meta_var.set(
            f"{key}   {rows}×{cols}   unit: {unit}   "
            f"min: {vmin:.3f}   max: {vmax:.3f}"
            + (f"   rows: {row_axis}" if row_axis else "")
            + (f"   cols: {col_axis}" if col_axis else ""))
        self._notes_var.set(getattr(meta, "notes", "") if meta else "")

        # Cell geometry — wider for axis labels
        CW = 68   # cell width
        RH = 24   # row height
        LW = 62   # left margin (row axis labels)
        TH = 36   # top margin (col axis labels — 2 lines)

        # Column headers
        self._canvas.create_text(LW//2, TH//2, text=row_axis or "",
                                  fill=C["dim"], font=("Menlo", 8),
                                  angle=0, anchor="center")
        for c, lbl in enumerate(col_labels):
            x = LW + c * CW + CW//2
            # Two-line col header: axis value on top, index below
            self._canvas.create_text(x, TH - 22, text=str(lbl),
                                      fill=C["text_muted"] if hasattr(C,"text_muted") else C["muted"],
                                      font=("Menlo", 8), anchor="center")
            self._canvas.create_text(x, TH - 10, text=f"[{c}]",
                                      fill=C["dim"], font=("Menlo", 7), anchor="center")

        # Axis label background strip
        self._canvas.create_rectangle(0, 0, LW, TH + rows*RH + 10,
                                       fill="#0a0d11", outline="")

        # Rows
        for r, rlbl in enumerate(row_labels):
            y = TH + r * RH
            # Row label
            self._canvas.create_text(LW - 4, y + RH//2,
                                      text=str(rlbl), fill=C["muted"],
                                      font=("Menlo", 8), anchor="e")
            for c in range(cols):
                x = LW + c * CW
                val = float(arr2d[r, c])
                t   = (val - vmin) / vrange
                fill = self._heat_color(t)
                self._canvas.create_rectangle(x, y, x+CW-1, y+RH-1,
                                               fill=fill, outline=C["border"])
                e = tk.Entry(self._canvas, width=7,
                              bg=fill, fg=self._text_for(fill),
                              insertbackground=C["text"],
                              font=("Menlo", 8), bd=0, highlightthickness=0,
                              justify="center")
                e.insert(0, f"{val:.3f}")
                e.bind("<Return>",   lambda ev, r=r, c=c, k=key: self._cell_edit(ev,r,c,k))
                e.bind("<Tab>",      lambda ev, r=r, c=c, k=key: self._cell_edit(ev,r,c,k))
                e.bind("<FocusOut>", lambda ev, r=r, c=c, k=key: self._cell_edit(ev,r,c,k))
                self._canvas.create_window(x+CW//2, y+RH//2,
                                            window=e, width=CW-2, height=RH-2)
                self._entry_widgets[(r, c)] = e

        total_w = LW + cols * CW + 20
        total_h = TH + rows * RH + 20
        self._canvas.config(scrollregion=(0, 0, total_w, total_h))

        # 1×N: also draw chart
        if is_1d:
            self._draw_chart(col_labels, arr.tolist(), unit, col_axis)
            self._chart_frame.pack(fill="x", before=self._canvas.master.master
                                    if hasattr(self._canvas,"master") else self._canvas)
        else:
            self._chart_canvas_widget.delete("all")
            self._chart_frame.pack_forget()

        self._info_lbl.config(
            text=f"{key}   {rows}×{cols}   min={vmin:.3f}  max={vmax:.3f}  {unit}",
            fg=C["muted"])

    # ── 2D chart for 1×N tables ───────────────────────────────────────────────

    def _draw_chart(self, x_vals, y_vals, unit: str, x_label: str):
        cv = self._chart_canvas_widget
        cv.delete("all")
        W = cv.winfo_width() or 700
        H = 100
        PAD_L, PAD_R, PAD_T, PAD_B = 52, 16, 10, 28

        n = len(y_vals)
        if n < 2:
            return

        ymin, ymax = min(y_vals), max(y_vals)
        yr = ymax - ymin or 1.0
        xr = n - 1

        def px(i):
            return PAD_L + (i / xr) * (W - PAD_L - PAD_R)
        def py(v):
            return PAD_T + (1 - (v - ymin) / yr) * (H - PAD_T - PAD_B)

        # Grid lines (3 horizontal)
        for frac in [0, 0.5, 1]:
            gv = ymin + frac * yr
            gy = py(gv)
            cv.create_line(PAD_L, gy, W - PAD_R, gy,
                           fill=C["border"], dash=(2, 4))
            cv.create_text(PAD_L - 4, gy,
                           text=f"{gv:.2f}", anchor="e",
                           fill=C["dim"], font=("Menlo", 7))

        # X axis label
        cv.create_text(W//2, H - 6, text=x_label or "",
                       fill=C["dim"], font=("Menlo", 7), anchor="center")

        # Polyline
        pts = []
        for i, v in enumerate(y_vals):
            pts += [px(i), py(v)]
        if len(pts) >= 4:
            cv.create_line(pts, fill="#58a6ff", width=1.5, smooth=False)

        # Dots at every point
        for i, v in enumerate(y_vals):
            x, y = px(i), py(v)
            cv.create_oval(x-2, y-2, x+2, y+2,
                           fill="#58a6ff", outline="")

        # Unit label top-left
        cv.create_text(PAD_L, PAD_T, text=unit,
                       fill=C["muted"], font=("Menlo", 7), anchor="w")

    # ── Axis label helpers ────────────────────────────────────────────────────

    def _row_labels(self, key: str, rows: int):
        if rows == 16:
            return self._RPM_16
        if rows == 8:
            return self._COOL_8
        return list(range(rows))

    def _col_labels(self, key: str, cols: int):
        if cols == 16:
            if key in ("boost_setpoint", "wastegate_duty"):
                return [f"{int(i*100/15)}%" for i in range(16)]  # throttle %
            if key == "torque_limit":
                return [f"G{i+1}" if i<8 else f"R{i-7}" for i in range(16)]
            return self._LOAD_16      # mg/stroke default
        if cols == 32:
            if "maf" in key:
                return [f"{int(v/10)}mV" for v in self._MAF_32]
            if "throttle" in key:
                return [f"{int(i*100/31)}%" for i in range(32)]
            return list(range(32))
        if cols == 8:
            return self._COOL_8
        return list(range(cols))

    # ── Cell edit ─────────────────────────────────────────────────────────────

    def _cell_edit(self, event, r: int, c: int, key: str):
        e = self._entry_widgets.get((r, c))
        if not e or not self._parser:
            return
        try:
            new_val = float(e.get())
        except ValueError:
            e.config(fg=C["red"])
            return
        try:
            arr = self._parser.table(key)
            if arr.ndim == 1:
                arr[c] = new_val
            else:
                arr[r, c] = new_val
            vmin = float(arr.min())
            vmax = float(arr.max())
            vrange = vmax - vmin if vmax > vmin else 1.0
            t = (new_val - vmin) / vrange
            fill = self._heat_color(t)
            e.config(bg=fill, fg=self._text_for(fill))
            self._info_lbl.config(
                text=f"[{r},{c}] = {new_val:.3f}  (was {float(arr[r,c] if arr.ndim>1 else arr[c]):.3f})",
                fg=C["blue"])
        except Exception as ex:
            self._info_lbl.config(text=str(ex), fg=C["red"])
        return "break"

    # ── Lean diagnosis ────────────────────────────────────────────────────────

    def _open_guide(self):
        """Open the Simos8.5 tuning reference in the system browser."""
        import webbrowser
        url = "https://github.com/dspl1236/simos-suite/blob/main/docs/tuning_guide_s85.md"
        try:
            webbrowser.open(url)
        except Exception:
            messagebox.showinfo("Tuning guide",
                f"Open this URL in your browser:\n{url}")

    def _lean_diag(self):
        if not self._parser:
            messagebox.showinfo("Lean diagnosis", "Load a CAL .bin first.")
            return
        try:
            result = self._parser.diagnose_lean()
            messagebox.showinfo("Lean diagnosis — Simos8.5 CGWB", result)
        except Exception as e:
            messagebox.showerror("Diagnosis error", str(e))

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_cal(self):
        if not self._parser:
            return
        try:
            self._parser.fix_checksums()
            out = self._parser.to_bytes()
        except Exception as e:
            messagebox.showerror("Checksum error", str(e))
            return
        path = filedialog.asksaveasfilename(
            title="Save modified CAL .bin",
            defaultextension=".bin",
            filetypes=[("Binary files", "*.bin")])
        if path:
            with open(path, "wb") as f:
                f.write(out)
            messagebox.showinfo("Saved",
                f"CAL saved  ({len(out):,} bytes)\n{os.path.basename(path)}\n\n"
                f"Checksums fixed and verified.")

    # ── Color helpers ─────────────────────────────────────────────────────────

    def _heat_color(self, t: float) -> str:
        t = max(0.0, min(1.0, t))
        if t < 0.25:
            r, g, b = 13, 45 + int(t/0.25*70), 120
        elif t < 0.5:
            s = (t-0.25)/0.25
            r, g, b = int(s*40), int(115+s*50), int(120-s*90)
        elif t < 0.75:
            s = (t-0.5)/0.25
            r, g, b = int(40+s*130), int(165-s*30), int(30-s*20)
        else:
            s = (t-0.75)/0.25
            r, g, b = int(170+s*85), int(135-s*100), 10
        return f"#{r:02x}{g:02x}{b:02x}"

    def _text_for(self, hex_color: str) -> str:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        return "#0d1117" if (r*0.299 + g*0.587 + b*0.114) > 85 else "#e6edf3"




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

        # 3-column gauge grid
        grid = _frame(gauges, bg=C["surface"])
        grid.pack(fill="x")

        for i, (did, name) in enumerate(self.DIDS):
            col_frame = _frame(grid, bg=C["surface"])
            col_frame.grid(row=i // 3, column=i % 3, padx=6, pady=4,
                           sticky="w")
            tk.Label(col_frame, text=f"{name:<14}", fg=C["muted"],
                     bg=C["surface"], font=("Menlo", 9)).pack(side="left")
            var = tk.StringVar(value="—")
            self._values[did] = var
            tk.Label(col_frame, textvariable=var, fg=C["blue"],
                     bg=C["surface"], font=("Menlo", 10, "bold"),
                     width=10, anchor="e").pack(side="left")

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

    def _on_preset_change(self, *_):
        """Rebuild gauge grid when preset changes."""
        preset = self._preset_var.get()
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
            self._active_channels = _map.get(preset, CHANNELS_ESSENTIAL)
        except ImportError:
            from logger import SIMOS85_CHANNELS
            self._active_channels = SIMOS85_CHANNELS
        # Reset gauge vars for new channel set
        self._values = {ch.did: tk.StringVar(value="—")
                        for ch in self._active_channels}

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
CP_MODULES = [
    # (display name,            addr,  tx_id, rx_id)  — from ConnorHowell/vag-uds-ids
    ("J533  Gateway",            "01",  0x710, 0x77A),
    ("J255  Climatronic",        "08",  0x746, 0x7B0),
    ("J285  Instruments",        "17",  0x714, 0x77E),
    ("J234  Airbag",             "15",  0x715, 0x77F),
    ("J794  MMI",                "5F",  0x773, 0x7DD),
    ("J136  Mem.Seat Driver",    "36",  0x74C, 0x7B6),
    ("J521  Mem.Seat Pass.",     "06",  0x74D, 0x7B7),
    ("J518  KESSY",              "03",  0x732, 0x79C),
    ("J519  Body Elect.",        "09",  0x70E, 0x778),
    ("J393  Central Comfort",    "46",  0x70D, 0x777),
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

        # Row 2 — experimental options
        row2 = _frame(act)
        row2.pack(fill="x", pady=(6, 0))
        tk.Label(row2, text="EXPERIMENTAL:",
                 bg=C["surface"], fg=C["amber"],
                 font=("Courier New", 9)).pack(side="left")
        self._zero_const_btn = _btn(
            row2,
            "⊘  Try Zero Constellation (disable CP check)",
            self._do_zero_constellation,
            state="disabled")
        self._zero_const_btn.pack(side="left", padx=(8, 0))
        tip(self._zero_const_btn, 'EXPERIMENTAL: writes 00x10 to DID 0x04A3.\nTests if J533 has a CP-disabled state.\nKnown-good value can always be restored.')
        tk.Label(row2,
                 text="writes 00×10 to DID 0x04A3 — tests if J533 has a CP-disabled state",
                 bg=C["surface"], fg=C["dim"],
                 font=("Courier New", 8)).pack(side="left", padx=(8, 0))

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

        # ── Log ───────────────────────────────────────────────────────────────
        _section(self, "log")
        self._log = _log_widget(self)
        self._log.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # ── Legacy buttons (keep for compat) ──────────────────────────────────
        leg = _frame(self)
        leg.pack(fill="x", padx=14, pady=(0, 8))
        self._probe_btn = _btn(leg, "⊙  J533 probe",
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
        for mod_name, addr, tx, rx in CP_MODULES:
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

            self._module_rows[mod_name] = {
                "row": row, "cb": cb,
                "status": status_lbl, "blob": blob_lbl
            }

    # ── Connect / Disconnect ─────────────────────────────────────────────────

    def on_connect(self):
        self._scan_btn.config(state="normal")
        self._probe_btn.config(state="normal")
        self._zero_const_btn.config(state="normal")
        self._status_var.set("ready — click Scan to check all modules")
        self._status_lbl_color(C["green"])

    def on_disconnect(self):
        self._scan_btn.config(state="disabled")
        self._write_btn.config(state="disabled")
        self._const_btn.config(state="disabled")
        self._sel_all_btn.config(state="disabled")
        self._zero_const_btn.config(state="disabled")
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
                _cfg533["request_timeout"]  = 8
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
                break
            except Exception as e:
                if _attempt == 0:
                    log(f"  J533 constellation retry: {e}\n", "warn")
                    _t.sleep(1.0)
                else:
                    log(f"  J533 constellation failed: {e} — continuing\n", "warn")

        # Let J533 session close and J2534 channel settle before module scan
        import time as _t
        _t.sleep(2.0)

        # Per-module scan — fresh connection each time with delay between.
        # The bus scan confirms individual J2534 connections work fine.
        # The shared-connection approach deadlocks due to udsoncan Client internals.
        self._scan_results = {}
        cp_count = 0
        import time as _mt

        for mod_name, addr, tx, rx in CP_MODULES:
            if tx == 0x710:
                log(f"\n  {mod_name}  (skipped — constellation already read)\n", "dim")
                continue
            log(f"\n  {mod_name}  TX=0x{tx:03X} RX=0x{rx:03X}\n", "hdr")
            _mt.sleep(1.5)   # let previous J2534 channel fully close before opening next
            try:
                from cp_tools.j533_probe import J533Probe
                cfg = dict(configs.default_client_config)
                cfg["data_identifiers"] = {IKA_DID: _BytesCodec}
                cfg["request_timeout"]  = 10

                conn = J533Probe(
                    interface      = self.mw.interface,
                    interface_path = self.mw.iface_path,
                    ble_bridge     = getattr(self.mw, "ble_bridge", None),
                )._make_conn(tx, rx)

                client = Client(conn, request_timeout=10, config=cfg)
                client.__enter__()

                try:
                    client.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    result = client.read_data_by_identifier([IKA_DID])
                    raw = bytes(result.service_data.values[IKA_DID])
                    self._scan_results[mod_name] = raw

                    all_zeros = all(b == 0 for b in raw)
                    short     = raw[:8].hex().upper() + "..."

                    if all_zeros:
                        cp_count += 1
                        set_row(mod_name,
                                "CP ACTIVE", C["red"],
                                "all zeros — key not installed", C["red"],
                                cp_active=True)
                        log(f"    ✗ CP ACTIVE — IKA key all zeros\n", "err")
                    else:
                        same = (raw == KNOWN_IKA_BLOB)
                        set_row(mod_name,
                                "CP clear", C["green"],
                                short, C["green"] if same else C["amber"])
                        tag = "ok" if same else "warn"
                        lbl = "matches known blob" if same else "different blob"
                        log(f"    ✓ CP clear  {short}  ({lbl})\n", tag)
                finally:
                    try: client.__exit__(None, None, None)
                    except Exception: pass

            except udsoncan.exceptions.NegativeResponseException as nre:
                nrc = nre.response.code if hasattr(nre, "response") else 0
                if nrc in (0x22, 0x31):  # conditionsNotCorrect / requestOutOfRange
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

            except Exception as e:
                err_str = str(e)[:50]
                if "timeout" in err_str.lower():
                    set_row(mod_name, "not present", C["muted"],
                            "no response", C["muted"])
                    log(f"    — not present (timeout)\n", "dim")
                else:
                    set_row(mod_name, "error", C["amber"],
                            err_str, C["amber"])
                    log(f"    ! error: {err_str}\n", "warn")



        # Summary
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
            _, addr, tx, rx = mod_map[mod_name]
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

                    # SA2 seed/key
                    try:
                        from sa2_seed_key.sa2_script import Sa2Algorithm
                        seed_resp = c.request_seed(0x03)
                        seed      = bytes(seed_resp.service_data.seed)
                        key       = Sa2Algorithm().compute_key(seed)
                        c.send_key(0x04, key)
                        log("    SA2 unlocked ✓\n", "ok")
                    except ImportError:
                        log("    SA2 module not available — write may fail\n",
                            "warn")
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

                # SA2 unlock on J533
                try:
                    from sa2_seed_key.sa2_script import Sa2Algorithm
                    seed_resp = c.request_seed(0x03)
                    seed = bytes(seed_resp.service_data.seed)
                    key  = Sa2Algorithm().compute_key(seed)
                    c.send_key(0x04, key)
                    log("  SA2 unlocked on J533 ✓\n", "ok")
                except ImportError:
                    log("  SA2 module not available\n", "warn")
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

    # ── Option 1: Try zero constellation ─────────────────────────────────────
    # Tests whether J533 has a CP-disabled / virgin state by writing all zeros
    # to DID 0x04A3. If J533 enters a "don't check" mode, CP stops being
    # enforced without needing to know any IKA keys.
    # Safe to try — if it fails or causes issues, write the known-good
    # constellation back (FD A1 E8 0C FE 62 60 0D 00 00).

    def _do_zero_constellation(self):
        import tkinter.messagebox as mb
        if not mb.askyesno(
            "Try Zero Constellation",
            "This will write 10 zero bytes to J533 DID 0x04A3.\n\n"
            "If J533 has a CP-disabled state, this disables the\n"
            "constellation check permanently without needing IKA keys.\n\n"
            "If it causes issues, the known-good constellation can be\n"
            "restored with \u229e Update Constellation.\n\n"
            "Continue?",
            icon="warning"
        ):
            return
        self._zero_const_btn.config(state="disabled")
        self._scan_btn.config(state="disabled")
        self._append_log(self._log,
            "\n── Option 1: Zero Constellation ─────────────────────────\n",
            "hdr")
        self._append_log(self._log,
            "  Writing 00 00 00 00 00 00 00 00 00 00 to J533 DID 0x04A3\n",
            "warn")
        self._run(self._zero_constellation_task)

    def _zero_constellation_task(self):
        import udsoncan
        from udsoncan.client import Client  # noqa: F401
        from udsoncan import configs

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        class _BytesCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        ZERO_CONST    = bytes(10)          # 10 × 0x00
        KNOWN_GOOD    = bytes.fromhex("FDA1E80CFE62600D0000")

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

                # Read current constellation first
                r = c.read_data_by_identifier([CONST_DID])
                current = bytes(r.service_data.values[CONST_DID])
                log(f"  Current: {' '.join(f'{b:02X}' for b in current)}\n",
                    "dim")
                log(f"  Writing: 00 00 00 00 00 00 00 00 00 00\n", "warn")

                # SA2 unlock
                try:
                    from sa2_seed_key.sa2_script import Sa2Algorithm
                    seed_resp = c.request_seed(0x03)
                    seed = bytes(seed_resp.service_data.seed)
                    key  = Sa2Algorithm().compute_key(seed)
                    c.send_key(0x04, key)
                    log("  SA2 unlocked ✓\n", "ok")
                except Exception as sa2_e:
                    log(f"  SA2 error: {sa2_e}\n", "err")
                    self._ui(self._zero_const_btn.config, state="normal")
                    self._ui(self._scan_btn.config, state="normal")
                    return

                # Write zero constellation
                c.write_data_by_identifier(CONST_DID, ZERO_CONST)

                # Read back
                r2 = c.read_data_by_identifier([CONST_DID])
                readback = bytes(r2.service_data.values[CONST_DID])
                log(f"  Readback: {' '.join(f'{b:02X}' for b in readback)}\n",
                    "ok")

                if readback == ZERO_CONST:
                    log("  Zero constellation accepted by J533 ✓\n", "ok")
                    log("\n  NOW: cycle ignition and run Scan All Modules.\n",
                        "hdr")
                    log("  If all modules show CP clear → zero constellation\n"
                        "  disables the CP check. \U0001f389\n", "ok")
                    log("  If modules still show CP active → zero constellation\n"
                        "  does not disable the check on this platform.\n",
                        "warn")
                    log("  In either case the known-good constellation can be\n"
                        "  restored with \u229e Update Constellation.\n", "dim")
                    self._ui(self._const_var.set, "00 00 00 00 00 00 00 00 00 00")
                    self._ui(self._verdict_var.set,
                             "\u26a0  Zero constellation written — cycle ignition and rescan")
                    self._ui(self._verdict_lbl.config, fg=C["amber"])
                    self._ui(self._ign_btn.config, state="normal")
                else:
                    log(f"  Unexpected readback — J533 may have modified the value\n",
                        "warn")

        except udsoncan.exceptions.NegativeResponseException as nre:
            nrc = nre.response.code if hasattr(nre, "response") else 0
            log(f"  J533 rejected zero write — NRC 0x{nrc:02X}\n", "err")
            if nrc == 0x22:
                log("  conditionsNotCorrect — J533 may require GEKO token\n"
                    "  to accept this constellation value.\n", "warn")
            elif nrc == 0x31:
                log("  requestOutOfRange — zero constellation not a valid\n"
                    "  value on this platform.\n", "warn")
            log("  This is expected if J533 validates the constellation\n"
                "  structure before writing.\n", "dim")
        except Exception as e:
            log(f"  Error: {e}\n", "err")

        self._ui(self._zero_const_btn.config, state="normal")
        self._ui(self._scan_btn.config, state="normal")

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


# TAB 7 — Raw Sniff
# ─────────────────────────────────────────────────────────────────────────────

class RawSniffTab(_Tab):
    def __init__(self, parent, mw):
        super().__init__(parent, mw)
        self._sniffing = False

        info = _card(self, padx=12, pady=8)
        info.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(info, text=(
            "Passes the ESP32 bridge into raw sniff mode (0xCAFE header frames).\n"
            "Every ISO-TP frame on the CAN bus is forwarded and displayed here.\n"
            "Use this while ODIS is connected to capture the CP removal sequence."
        ), fg=C["muted"], bg=C["surface"], font=("Menlo", 10),
            justify="left", wraplength=580).pack(anchor="w")

        ctrl = _frame(self)
        ctrl.pack(fill="x", padx=14, pady=6)
        self._sniff_btn = _btn(ctrl, "start sniff",
                               self._toggle_sniff, primary=True,
                               state="disabled")
        self._sniff_btn.pack(side="left")
        _btn(ctrl, "clear", lambda: self._clear_log(self._hex_log)
             ).pack(side="left", padx=8)
        _btn(ctrl, "save log", self._save_log).pack(side="left")

        # Filter
        tk.Label(ctrl, text="  filter CAN ID",
                 fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 10)).pack(side="left", padx=(16, 4))
        self._filter_var = tk.StringVar(value="")
        tk.Entry(ctrl, textvariable=self._filter_var,
                 bg=C["surface"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 10), width=8, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left")
        tk.Label(ctrl, text="  (hex, e.g. 710)",
                 fg=C["dim"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left")

        _section(self, "frame log")
        log_outer, self._hex_log = _scrolled_text(self, height=18)
        log_outer.pack(fill="both", expand=True, padx=14, pady=4)
        self._hex_log.tag_config("tx",  foreground=C["blue"])
        self._hex_log.tag_config("rx",  foreground=C["green"])
        self._hex_log.tag_config("cafe",foreground=C["amber"])
        self._hex_log.tag_config("dim", foreground=C["dim"])

        # Frame counter
        self._frame_count = 0
        self._count_lbl = tk.Label(self, text="frames: 0",
                                   fg=C["dim"], bg=C["bg"],
                                   font=("Menlo", 9))
        self._count_lbl.pack(anchor="e", padx=14)

    def on_connect(self):
        self._sniff_btn.config(state="normal")

    def on_disconnect(self):
        self._sniffing = False
        self._sniff_btn.config(text="start sniff", state="disabled",
                               fg="#0d1117", bg=C["blue"])

    def _toggle_sniff(self):
        if self._sniffing:
            self._sniffing = False
            self._sniff_btn.config(text="start sniff",
                                   fg="#0d1117", bg=C["blue"])
        else:
            self._sniffing = True
            self._frame_count = 0
            self._sniff_btn.config(text="stop sniff",
                                   fg=C["text"], bg=C["btn"])
            self._run(self._sniff_loop)

    def _sniff_loop(self):
        """
        Enable raw sniff mode on the BLE/USB bridge.
        Frames with header ID 0xCAFE are raw bus captures.
        Simulated here until raw sniff mode is wired to BLEBridge.raw_sniff().
        """
        import random
        can_ids = [0x710, 0x77A, 0x746, 0x7B0, 0x18DA10F1, 0x18DAF110]
        filt_str = self._filter_var.get().strip()
        try:
            filt = int(filt_str, 16) if filt_str else None
        except ValueError:
            filt = None

        while self._sniffing and self.mw.connected:
            can_id = random.choice(can_ids)
            if filt is not None and can_id != filt:
                time.sleep(0.02)
                continue

            length = random.randint(1, 8)
            data   = bytes(random.randint(0, 0xFF) for _ in range(length))
            ts     = time.strftime("%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}"

            direction = "TX" if can_id in (0x710, 0x746) else "RX"
            tag = "tx" if direction == "TX" else "rx"
            hex_data = " ".join(f"{b:02X}" for b in data)
            line = f"{ts}  {direction}  [{can_id:04X}]  {hex_data}\n"

            self._frame_count += 1
            self._ui(self._append_log, self._hex_log, line, tag)
            self._ui(self._count_lbl.config,
                     text=f"frames: {self._frame_count}")

            time.sleep(random.uniform(0.01, 0.15))

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


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────

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

    VERSION = "0.3.2"

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
            ("  ecu info  ",    EcuInfoTab),
            ("  flash  ",       FlashTab),
            ("  tune  ",        TuneTab),
            ("  logger  ",      LoggerTab),
            ("  cp tools  ",    CPToolsTab),
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
        # If Tune tab has no data yet and we have a synthetic CAL (sim mode),
        # the sim_runner will call load_bytes() directly after connect.

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
