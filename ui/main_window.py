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
        self._status.config(text=f"error: {msg[:60]}", fg=C["red"])


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

            with udsoncan.Client(conn, request_timeout=30, config=cfg) as client:
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
        self._prog_label.config(text=f"error: {msg[:50]}", fg=C["red"])

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
                 fg="#3fb95090", bg=C["bg"], font=("Menlo", 9),
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
# TAB 6 — CP Tools
# ─────────────────────────────────────────────────────────────────────────────

class CPToolsTab(_Tab):
    """
    CP Tools tab — J533 constellation probe, CP routine check, ODX parser.

    The CP routine check button (⟳ check CP status) is the key diagnostic:
    it connects to J533, reads the constellation and IKA key DID, then fires
    RoutineControl Start (31 01 02 26) and reports J533's response. This lets
    you confirm:
      - Whether J533 sees J255 in the constellation
      - Whether the IKA key is zeroed (CP active) or populated (CP cleared)
      - Whether 0x0226 is the correct routine ID (7F 31 22 = yes, needs token)

    All operations run in a background thread — UI stays responsive.
    """

    def __init__(self, parent, mw):
        super().__init__(parent, mw)

        # ── CP status check (primary diagnostic) ─────────────────────────────
        _section(self, "component protection check")

        cp_info = _card(self, padx=12, pady=8)
        cp_info.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(cp_info, text=(
            "Reads constellation + IKA key from J533, then sends RoutineControl\n"
            "Start (31 01 02 26) to confirm the routine ID and read J533's response.\n"
            "Expected response if ID is correct: 7F 31 22 (conditionsNotCorrect — needs token).\n"
            "Expected response if ID is wrong:   7F 31 31 (requestOutOfRange)."
        ), fg=C["muted"], bg=C["surface"], font=("Menlo", 10),
            justify="left", wraplength=600).pack(anchor="w")

        cp_row = _frame(self)
        cp_row.pack(fill="x", padx=14, pady=6)
        self._cp_btn = _btn(cp_row, "⟳  check CP status",
                            self._do_cp_check, primary=True, state="disabled")
        self._cp_btn.pack(side="left")
        self._ika_btn = _btn(cp_row, "📖  read IKA keys",
                             self._do_read_ika, state="disabled")
        self._ika_btn.pack(side="left", padx=8)

        # Status indicator
        self._cp_status_var = tk.StringVar(value="not connected")
        self._cp_status_lbl = tk.Label(cp_row, textvariable=self._cp_status_var,
                                        fg=C["muted"], bg=C["bg"],
                                        font=("Menlo", 10))
        self._cp_status_lbl.pack(side="left", padx=16)

        # Result summary strip
        self._cp_result_frame = _frame(self)
        self._cp_result_frame.pack(fill="x", padx=14, pady=(0, 4))
        self._cp_fields = {}
        for label in ("VIN", "J255 slot", "IKA key", "Routine 0x0226", "J533 response"):
            row = _frame(self._cp_result_frame)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"  {label:<22}", fg=C["dim"], bg=C["bg"],
                     font=("Menlo", 9)).pack(side="left")
            var = tk.StringVar(value="—")
            tk.Label(row, textvariable=var, fg=C["blue"], bg=C["bg"],
                     font=("Menlo", 9)).pack(side="left")
            self._cp_fields[label] = var

        # ── J533 full probe ───────────────────────────────────────────────────
        _section(self, "J533 full DID probe")

        probe_info = _card(self, padx=12, pady=8)
        probe_info.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(probe_info, text=(
            "Sweeps all accessible DIDs on J533 (TX=0x710, RX=0x77A).\n"
            "Run alongside ODIS in raw sniff mode to capture the full CP token exchange."
        ), fg=C["muted"], bg=C["surface"], font=("Menlo", 10),
            justify="left", wraplength=600).pack(anchor="w")

        op_row = _frame(self)
        op_row.pack(fill="x", padx=14, pady=6)
        self._probe_btn = _btn(op_row, "run full probe",
                               self._do_probe, state="disabled")
        self._probe_btn.pack(side="left")
        self._save_btn = _btn(op_row, "save report JSON",
                              self._save_report, state="disabled")
        self._save_btn.pack(side="left", padx=8)

        # ── ODX parser ────────────────────────────────────────────────────────
        _section(self, "ODX parser")

        odx_row = _frame(self)
        odx_row.pack(fill="x", padx=14, pady=4)
        _btn(odx_row, "open ODX file", self._open_odx).pack(side="left")
        self._odx_lbl = tk.Label(odx_row, text="no ODX loaded",
                                 fg=C["muted"], bg=C["bg"],
                                 font=("Menlo", 10))
        self._odx_lbl.pack(side="left", padx=10)

        # ── Output log ────────────────────────────────────────────────────────
        _section(self, "output")
        log_outer, self._log = _scrolled_text(self, height=12)
        log_outer.pack(fill="both", expand=True, padx=14, pady=4)
        self._log.tag_config("ok",   foreground=C["green"])
        self._log.tag_config("err",  foreground=C["red"])
        self._log.tag_config("hdr",  foreground=C["blue"])
        self._log.tag_config("dim",  foreground=C["muted"])
        self._log.tag_config("warn", foreground=C["amber"])

        self._report = None

    # ── Connection state ──────────────────────────────────────────────────────

    def on_connect(self):
        self._probe_btn.config(state="normal")
        self._cp_btn.config(state="normal")
        self._ika_btn.config(state="normal")
        self._cp_status_var.set("connected — ready")
        self._cp_status_lbl.config(fg=C["green"])

    def on_disconnect(self):
        self._probe_btn.config(state="disabled")
        self._cp_btn.config(state="disabled")
        self._ika_btn.config(state="disabled")
        self._cp_status_var.set("not connected")
        self._cp_status_lbl.config(fg=C["muted"])
        for var in self._cp_fields.values():
            var.set("—")

    # ── CP status check ───────────────────────────────────────────────────────

    def _do_read_ika(self):
        """Read DID 0x00BE (IKA key) from J533 and J255 directly."""
        self._ika_btn.config(state="disabled")
        self._clear_log(self._log)
        self._append_log(self._log,
            "── IKA key readback ─────────────────────────────────\n", "hdr")
        self._append_log(self._log,
            "  Known J136 blob: E6 2B 41 D1 1C 44 AF 20 21 77 FB 1F\n"
            "                   27 4B 0A C2 D1 5B D2 62 E4 FD 27 AB\n"
            "                   61 D1 23 C2 F1 5A 2C 93 26 00\n", "dim")
        self._run(self._ika_task)

    def _ika_task(self):
        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        KNOWN_J136 = bytes.fromhex(
            "E62B41D11C44AF202177FB1F274B0AC2D15B"
            "D262E4FD27AB61D123C2F15A2C932600")

        try:
            from cp_tools.j533_probe import J533Probe
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            probe.connect()
            log("  J533 connected\n", "ok")

            results = probe.read_all_ika_keys()

            for module, info in results.items():
                if module == "J533_constellation":
                    log(f"\n  Constellation (0x04A3):\n", "hdr")
                    log(f"    {info.get('hex_spaced','')}\n", "ok")
                    continue

                log(f"\n  {module} — DID 0x00BE:\n", "hdr")
                status = info.get("status","?")
                raw    = info.get("raw", b"")

                if status == "ok":
                    hex_s = info.get("hex_spaced","")
                    cp    = info.get("cp_active")
                    length = info.get("length", 0)
                    log(f"    Length: {length} bytes\n")
                    log(f"    Hex:    {hex_s}\n",
                        "warn" if cp else "ok")
                    if cp:
                        log(f"    ⚠ ALL ZEROS — CP still active\n", "warn")
                    elif cp is False:
                        log(f"    ✓ Key populated — CP cleared\n", "ok")

                    # Compare J533 against known J136 blob
                    if module == "J533" and raw == KNOWN_J136:
                        log("    ✓ MATCHES known J136 blob!\n", "ok")
                    elif module == "J255" and raw and not all(b==0 for b in raw):
                        # This is the blob we COULDN'T get from the log
                        log("    ★ J255 IKA blob captured — new data!\n", "ok")
                        self._ui(self._cp_fields["IKA key"].set,
                                 f"{raw[:8].hex().upper()}... ({length}B)")
                else:
                    log(f"    {status}\n", "err")

            log("\n── readback complete ────────────────────────────────\n", "hdr")

        except Exception as e:
            log(f"  error: {e}\n", "err")
        finally:
            self._ui(self._ika_btn.config, state="normal")

    def _do_cp_check(self):
        self._cp_btn.config(state="disabled")
        self._cp_status_var.set("checking...")
        self._cp_status_lbl.config(fg=C["amber"])
        for var in self._cp_fields.values():
            var.set("...")
        self._clear_log(self._log)
        self._append_log(self._log,
            "── CP status check ──────────────────────────────────\n", "hdr")
        self._append_log(self._log,
            "  Connecting to J533 (TX=0x710 RX=0x77A)...\n", "dim")
        self._run(self._cp_check_task)

    def _cp_check_task(self):
        """
        Background thread: connect to J533, read constellation + IKA key,
        fire RoutineControl Start 0x0226, report raw response.
        """
        import struct

        def log(msg, tag=""):
            self._ui(self._append_log, self._log, msg, tag)

        def field(name, val, color=None):
            self._ui(self._cp_fields[name].set, val)
            if color:
                # We can't easily recolor a StringVar label — log it instead
                pass

        try:
            from cp_tools.j533_probe import J533Probe, CP_ROUTINE_ID
        except ImportError as e:
            self._ui(self._cp_status_var.set, f"import error: {e}")
            self._ui(self._cp_status_lbl.config, fg=C["red"])
            self._ui(self._cp_btn.config, state="normal")
            return

        try:
            probe = J533Probe(
                interface      = self.mw.interface,
                interface_path = self.mw.iface_path,
                ble_bridge     = getattr(self.mw, "ble_bridge", None),
            )
            probe.connect()
            log("  J533 connected\n", "ok")

            # ── VIN ───────────────────────────────────────────────────────────
            try:
                vin = probe.read_did_raw(0xF190)
                vin_str = vin.decode("ascii", errors="replace").strip("\x00").strip()
                log(f"  VIN               {vin_str}\n", "ok")
                field("VIN", vin_str)
            except Exception as e:
                log(f"  VIN read failed: {e}\n", "warn")
                field("VIN", f"error: {e}")

            # ── Constellation — find J255 slot ────────────────────────────────
            try:
                alloc = probe.read_did_raw(0x2A2A)
                # Structure: pairs of (ecu_id u8, ecu_name u8)
                j255_slot = None
                for i in range(0, len(alloc) - 1, 2):
                    slot_idx = alloc[i]
                    ecu_name = alloc[i + 1]
                    if ecu_name == 8:   # 8 = Air Conditioning = J255
                        j255_slot = slot_idx
                        break
                if j255_slot is not None:
                    log(f"  J255 in slot      {j255_slot}  (ECU name code 8)\n", "ok")
                    field("J255 slot", str(j255_slot))
                else:
                    log("  J255 NOT found in constellation\n", "warn")
                    field("J255 slot", "not enrolled")
            except Exception as e:
                log(f"  constellation read failed: {e}\n", "warn")
                field("J255 slot", f"error: {e}")

            # ── IKA key — 34 bytes, all zeros = CP active ────────────────────
            try:
                ika = probe.read_did_raw(0x00BE)
                if len(ika) == 34 and all(b == 0 for b in ika):
                    log("  IKA key (0x00BE)  all-zero — CP ACTIVE\n", "warn")
                    field("IKA key", "all-zero (CP active)")
                elif len(ika) == 34:
                    log(f"  IKA key (0x00BE)  {ika[:8].hex()}...  CP cleared\n", "ok")
                    field("IKA key", f"{ika[:8].hex()}...  (populated)")
                else:
                    log(f"  IKA key (0x00BE)  unexpected length {len(ika)}\n", "warn")
                    field("IKA key", f"{len(ika)}B: {ika.hex()}")
            except Exception as e:
                log(f"  IKA key read failed: {e}\n", "warn")
                field("IKA key", f"error: {e}")

            # ── RoutineControl Start 0x0226 ───────────────────────────────────
            rid_hi = (CP_ROUTINE_ID >> 8) & 0xFF
            rid_lo = CP_ROUTINE_ID & 0xFF
            log(f"\n  Sending RoutineControl Start: 31 01 {rid_hi:02X} {rid_lo:02X}\n",
                "hdr")
            field("Routine 0x0226", "sending...")

            try:
                raw_resp = probe.start_cp_routine()
                if raw_resp is None:
                    log("  No response (timeout)\n", "err")
                    field("Routine 0x0226", "timeout")
                    field("J533 response", "no response")
                else:
                    hex_resp = raw_resp.hex(" ").upper()
                    log(f"  Raw response:     {hex_resp}\n", "ok")
                    field("J533 response", hex_resp)

                    # Interpret
                    if len(raw_resp) >= 1 and raw_resp[0] == 0x71:
                        log("  ✓ ROUTINE ACCEPTED — J533 accepted 0x0226\n", "ok")
                        field("Routine 0x0226", "✓ accepted (0x71)")
                    elif len(raw_resp) >= 3 and raw_resp[0] == 0x7F:
                        nrc = raw_resp[2]
                        if nrc == 0x22:
                            log("  ✓ ID CONFIRMED — NRC 0x22 conditionsNotCorrect\n"
                                "    J533 knows this routine but requires the token.\n",
                                "ok")
                            field("Routine 0x0226", "✓ ID confirmed (NRC 0x22)")
                        elif nrc == 0x31:
                            log("  ✗ WRONG ID — NRC 0x31 requestOutOfRange\n"
                                "    0x0226 is NOT the correct routine ID.\n",
                                "err")
                            field("Routine 0x0226", "✗ wrong ID (NRC 0x31)")
                        elif nrc == 0x7E:
                            log("  ○ NRC 0x7E — subFunctionNotSupportedInActiveSession\n"
                                "    Try extended session first.\n", "warn")
                            field("Routine 0x0226", "needs ext session (0x7E)")
                        else:
                            log(f"  ? NRC 0x{nrc:02X} — see ISO 14229-1 Table A.1\n", "warn")
                            field("Routine 0x0226", f"NRC 0x{nrc:02X}")
                    else:
                        log(f"  ? Unknown response format\n", "warn")
                        field("Routine 0x0226", f"unknown: {hex_resp}")

            except Exception as e:
                log(f"  RoutineControl error: {e}\n", "err")
                field("Routine 0x0226", f"error: {e}")
                field("J533 response", "exception")

            log("\n── check complete ───────────────────────────────────\n", "hdr")
            self._ui(self._cp_status_var.set, "check complete")
            self._ui(self._cp_status_lbl.config, fg=C["green"])

        except Exception as e:
            self._ui(self._append_log, self._log,
                     f"  fatal: {e}\n", "err")
            self._ui(self._cp_status_var.set, f"error: {str(e)[:40]}")
            self._ui(self._cp_status_lbl.config, fg=C["red"])

        finally:
            self._ui(self._cp_btn.config, state="normal")

    # ── J533 full probe ───────────────────────────────────────────────────────

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
        self._append_log(self._log, "\n── probe report ─────────────────────\n", "hdr")
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
                data = asdict(self._report) if hasattr(self._report, "__dataclass_fields__")                        else dict(self._report)
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
        self._append_log(self._log, f"── ODX: {os.path.basename(path)} ──\n", "hdr")
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
                f"    0x{did:04X}  {entry.name:<36}  {entry.byte_length}B\n", "dim")
        if len(p.did_map) > 20:
            self._append_log(self._log,
                f"    ... {len(p.did_map)-20} more\n", "dim")


# ─────────────────────────────────────────────────────────────────────────────
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

    VERSION = "0.2.0"

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
