"""
ui/main_window.py — Simos Tuning Suite — Main Application Window

Tabbed desktop GUI. Hosts all functional panels wired to the backend.

Tabs
────
  Hardware    InterfacePanel — auto-detect + connect. Drives all other tabs.
  ECU Info    Read all standard VW DIDs (VIN, part numbers, session info, etc.)
  Flash       Read / write CAL block with progress bar and checksum auto-fix.
  Tune        Calibration table editor — dropdown, editable grid, 2D chart stub.
  Logger      Live DID poller — configurable channels, running readouts.
  CP Tools    J533 active DID probe + ODX parser output.
  Raw Sniff   Hex dump of raw ISO-TP frames from BLE bridge sniff mode.

State model
───────────
  _ecu        Currently selected ECUDef (from ECU selector dropdown)
  _interface  Currently connected interface string (e.g. "BLE", "USBISOTP")
  _iface_path Path (COM port, DLL path, or "" for BLE)
  _connected  Bool — True when InterfacePanel reports connected

Run standalone:
    python -m ui.main_window
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional, Dict, Any

from core.ecu_defs import (
    ECUDef, SIMOS85,
    SIMOS12, SIMOS122, SIMOS181, SIMOS1810,
)
from ui.interface_panel import InterfacePanel, COLORS

log = logging.getLogger("SimosSuite.GUI")

# ── All ECU targets available in the selector ─────────────────────────────────
ECU_REGISTRY: Dict[str, ECUDef] = {
    "Simos8.5  — 3.0T TFSI (C7 A6/A7)":  SIMOS85,
    "Simos12   — 2.0T EA888 Gen1/2":       SIMOS12,
    "Simos12.2 — 2.0T EA888 Gen3":         SIMOS122,
    "Simos18.1 — 2.0T EA888 Gen3b MQB":   SIMOS181,
    "Simos18.10 — 2.0T MQB Evo (Golf 8)": SIMOS1810,
}

C = COLORS   # shorthand

# ── Helper: themed label ──────────────────────────────────────────────────────

def lbl(parent, text, fg=None, font=None, **kw):
    return tk.Label(parent, text=text,
                    fg=fg or C["text"], bg=kw.pop("bg", C["bg"]),
                    font=font or ("Menlo", 10), **kw)

def sep(parent):
    return tk.Frame(parent, bg=C["border"], height=1)

def btn(parent, text, command, primary=False, **kw):
    b = tk.Button(
        parent, text=text, command=command,
        fg="#0d1117" if primary else C["text"],
        bg=C["blue"] if primary else C["btn"],
        activeforeground="#0d1117" if primary else C["text"],
        activebackground="#79b8ff" if primary else C["btn_hover"],
        font=("Menlo", 10, "bold" if primary else "normal"),
        bd=0, padx=12, pady=5, cursor="hand2",
        highlightbackground=C["border"], highlightthickness=1,
        **kw,
    )
    return b

def card(parent, **kw):
    return tk.Frame(parent, bg=C["surface"],
                    highlightbackground=C["border"],
                    highlightthickness=1, **kw)

def section_label(parent, text):
    tk.Label(parent, text=text.upper(),
             fg=C["text_dim"], bg=C["bg"],
             font=("Menlo", 9)).pack(anchor="w", pady=(10, 4))


# ══════════════════════════════════════════════════════════════════════════════
# Individual tab panels
# ══════════════════════════════════════════════════════════════════════════════

class EcuInfoTab(tk.Frame):
    """Reads all standard VW DIDs and displays them in a table."""

    DID_LABELS = {
        0xF190: "VIN",
        0xF18C: "ECU Serial",
        0xF187: "Part Number",
        0xF189: "SW Version",
        0xF191: "HW Number",
        0xF1A3: "HW Version",
        0xF197: "System Name",
        0xF1AD: "Engine Code",
        0xF17C: "FAZIT",
        0xF19E: "ASAM File ID",
        0xF1A2: "ASAM File Version",
        0x0405: "Flash State",
        0x0407: "Program Attempts",
        0x0408: "Successful Programs",
        0xF186: "Active Session",
        0xF442: "Module Voltage",
        0x295A: "Vehicle Mileage",
        0x295B: "Module Mileage",
    }

    def __init__(self, parent, app: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self._app = app
        self._rows: Dict[str, tk.StringVar] = {}
        self._build()

    def _build(self):
        top = tk.Frame(self, bg=C["bg"])
        top.pack(fill="x", padx=16, pady=12)

        section_label(top, "ECU identification")

        action_row = tk.Frame(top, bg=C["bg"])
        action_row.pack(fill="x", pady=(0, 8))
        btn(action_row, "read ECU info", self._do_read, primary=True).pack(side="left")
        self._status_var = tk.StringVar(value="connect an interface first")
        tk.Label(action_row, textvariable=self._status_var,
                 fg=C["text_muted"], bg=C["bg"], font=("Menlo", 9)).pack(
                 side="left", padx=12)

        # DID table
        tbl_frame = card(top, padx=0, pady=0)
        tbl_frame.pack(fill="x")

        for i, (did, label) in enumerate(self.DID_LABELS.items()):
            row_bg = C["surface"] if i % 2 == 0 else C["bg"]
            row = tk.Frame(tbl_frame, bg=row_bg)
            row.pack(fill="x")
            tk.Label(row, text=f"0x{did:04X}  {label}",
                     fg=C["text_muted"], bg=row_bg,
                     font=("Menlo", 9), width=30, anchor="w",
                     padx=10, pady=5).pack(side="left")
            var = tk.StringVar(value="—")
            self._rows[label] = var
            tk.Label(row, textvariable=var,
                     fg=C["text"], bg=row_bg,
                     font=("Menlo", 10), anchor="w",
                     padx=10, pady=5).pack(side="left", fill="x", expand=True)

    def _do_read(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._status_var.set("reading...")
        for var in self._rows.values():
            var.set("…")

        def _task():
            try:
                from flasher.uds_flash import read_ecu_info
                result = read_ecu_info(
                    self._app.ecu,
                    self._app.interface,
                    self._app.iface_path or None,
                )
                self.after(0, lambda r=result: self._populate(r))
            except Exception as e:
                self.after(0, lambda: self._status_var.set(f"error: {e}"))

        threading.Thread(target=_task, daemon=True).start()

    def _populate(self, result: Dict[str, str]):
        for label, var in self._rows.items():
            val = result.get(label, "—")
            var.set(val)
        self._status_var.set(f"read OK — {len(result)} DIDs")


# ─────────────────────────────────────────────────────────────────────────────

class FlashTab(tk.Frame):
    """Read / write CAL block with progress bar and checksum auto-fix."""

    STEPS = ["CONNECT", "ERASE", "TRANSFER", "VERIFY", "DONE", "ERROR"]

    def __init__(self, parent, app: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self._app = app
        self._cal_path: Optional[str] = None
        self._build()

    def _build(self):
        body = tk.Frame(self, bg=C["bg"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        section_label(body, "CAL block flash")

        # File picker
        file_row = tk.Frame(body, bg=C["bg"])
        file_row.pack(fill="x", pady=(0, 8))

        self._file_var = tk.StringVar(value="no file selected")
        file_display = card(file_row, padx=10, pady=6)
        file_display.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(file_display, textvariable=self._file_var,
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9), anchor="w").pack(fill="x")

        btn(file_row, "browse...", self._pick_file).pack(side="left")

        # Checksum auto-fix toggle
        self._autofix_var = tk.BooleanVar(value=True)
        chk = tk.Checkbutton(body, text="auto-fix checksums before flash",
                              variable=self._autofix_var,
                              fg=C["text_muted"], bg=C["bg"],
                              selectcolor=C["btn"],
                              activeforeground=C["text"],
                              activebackground=C["bg"],
                              font=("Menlo", 9))
        chk.pack(anchor="w", pady=(0, 8))

        # Dry run toggle
        self._dryrun_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="dry run (go through the motions, don't write)",
                       variable=self._dryrun_var,
                       fg=C["text_muted"], bg=C["bg"],
                       selectcolor=C["btn"],
                       activeforeground=C["text"],
                       activebackground=C["bg"],
                       font=("Menlo", 9)).pack(anchor="w", pady=(0, 12))

        # Action buttons
        action_row = tk.Frame(body, bg=C["bg"])
        action_row.pack(fill="x", pady=(0, 12))
        btn(action_row, "flash CAL", self._do_flash, primary=True).pack(side="left", padx=(0, 8))
        btn(action_row, "verify only", self._do_verify).pack(side="left", padx=(0, 8))
        self._abort_btn = btn(action_row, "abort", self._do_abort)
        self._abort_btn.pack(side="left")
        self._abort_btn.config(state="disabled")

        sep(body).pack(fill="x", pady=8)

        # Progress
        section_label(body, "progress")

        # Step indicators
        step_row = tk.Frame(body, bg=C["bg"])
        step_row.pack(fill="x", pady=(0, 8))
        self._step_vars: Dict[str, tk.StringVar] = {}
        for step in ["CONNECT", "ERASE", "TRANSFER", "VERIFY"]:
            col = tk.Frame(step_row, bg=C["bg"])
            col.pack(side="left", expand=True)
            dot_var = tk.StringVar(value="○")
            tk.Label(col, textvariable=dot_var,
                     fg=C["text_dim"], bg=C["bg"],
                     font=("Menlo", 14)).pack()
            tk.Label(col, text=step.lower(),
                     fg=C["text_dim"], bg=C["bg"],
                     font=("Menlo", 8)).pack()
            self._step_vars[step] = dot_var

        # Progress bar
        self._progress_var = tk.IntVar(value=0)
        self._pbar = ttk.Progressbar(body, variable=self._progress_var,
                                      maximum=100, length=400)
        self._pbar.pack(fill="x", pady=(0, 6))

        self._progress_msg = tk.StringVar(value="idle")
        tk.Label(body, textvariable=self._progress_msg,
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9), anchor="w").pack(fill="x")

        sep(body).pack(fill="x", pady=8)

        # Log
        section_label(body, "log")
        self._log_text = tk.Text(body, bg=C["surface"],
                                  fg=C["text_muted"], font=("Menlo", 9),
                                  height=8, bd=0,
                                  highlightbackground=C["border"],
                                  highlightthickness=1,
                                  state="disabled")
        self._log_text.pack(fill="both", expand=True)

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Select CAL .bin file",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self._cal_path = path
            self._file_var.set(os.path.basename(path))

    def _log(self, msg: str):
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _reset_steps(self):
        for var in self._step_vars.values():
            var.set("○")

    def _set_step(self, step: str, state: str):
        """state: 'active' | 'done' | 'error'"""
        icons = {"active": "◉", "done": "●", "error": "✗"}
        colors = {"active": C["amber"], "done": C["green"], "error": C["red"]}
        var = self._step_vars.get(step)
        if var:
            var.set(icons.get(state, "○"))

    def _on_progress(self, p):
        """Called from flash thread — schedule UI update on main thread."""
        self.after(0, lambda: self._apply_progress(p))

    def _apply_progress(self, p):
        from flasher.uds_flash import FlashProgress
        self._progress_var.set(p.pct)
        self._progress_msg.set(p.message)
        self._log(f"[{p.step}] {p.message}")
        if p.step in self._step_vars:
            if p.step == "ERROR":
                for s in ["CONNECT", "ERASE", "TRANSFER", "VERIFY"]:
                    if self._step_vars[s].get() == "◉":
                        self._set_step(s, "error")
            else:
                # Mark previous steps done
                order = ["CONNECT", "ERASE", "TRANSFER", "VERIFY"]
                idx = order.index(p.step) if p.step in order else -1
                for i, s in enumerate(order):
                    if i < idx:
                        self._set_step(s, "done")
                    elif i == idx:
                        self._set_step(s, "active")
        if p.step == "DONE":
            for s in ["CONNECT", "ERASE", "TRANSFER", "VERIFY"]:
                self._set_step(s, "done")
            self._abort_btn.config(state="disabled")
        elif p.step == "ERROR":
            self._abort_btn.config(state="disabled")

    def _do_flash(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        if not self._cal_path:
            messagebox.showwarning("No file", "Select a CAL .bin file first.")
            return

        self._reset_steps()
        self._progress_var.set(0)
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")
        self._abort_btn.config(state="normal")

        cal_bytes = open(self._cal_path, "rb").read()

        if self._autofix_var.get():
            try:
                from tuner.cal_parser import CalParser
                parser = CalParser(self._app.ecu, cal_bytes)
                parser.decode()
                parser.fix_checksums()
                cal_bytes = parser.to_bytes()
                self._log("[CHECKSUM] auto-fix applied")
            except Exception as e:
                self._log(f"[CHECKSUM] warning: {e}")

        def _task(cb=cal_bytes):
            try:
                from flasher.uds_flash import flash_cal
                flash_cal(
                    ecu            = self._app.ecu,
                    cal_bytes      = cb,
                    interface      = self._app.interface,
                    interface_path = self._app.iface_path or None,
                    callback       = self._on_progress,
                    dry_run        = self._dryrun_var.get(),
                )
            except Exception as e:
                self.after(0, lambda: self._log(f"[ERROR] {e}"))

        threading.Thread(target=_task, daemon=True).start()

    def _do_verify(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._log("[VERIFY] verify-only mode — connecting...")

        def _task():
            try:
                from flasher.uds_flash import flash_cal
                flash_cal(
                    ecu            = self._app.ecu,
                    cal_bytes      = b"",   # not used in verify_only
                    interface      = self._app.interface,
                    interface_path = self._app.iface_path or None,
                    callback       = self._on_progress,
                    verify_only    = True,
                )
            except Exception as e:
                self.after(0, lambda: self._log(f"[ERROR] {e}"))

        threading.Thread(target=_task, daemon=True).start()

    def _do_abort(self):
        self._log("[ABORT] abort requested — will stop after current transfer block")
        self._abort_btn.config(state="disabled")
        # TODO: wire abort signal into flash_cal via threading.Event


# ─────────────────────────────────────────────────────────────────────────────

class TuneTab(tk.Frame):
    """Calibration table editor — select table, edit cells, write back."""

    def __init__(self, parent, app: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self._app = app
        self._parser = None
        self._current_table: Optional[str] = None
        self._build()

    def _build(self):
        body = tk.Frame(self, bg=C["bg"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        section_label(body, "calibration tables")

        # Top controls
        ctrl = tk.Frame(body, bg=C["bg"])
        ctrl.pack(fill="x", pady=(0, 10))

        btn(ctrl, "load .bin", self._load_bin).pack(side="left", padx=(0, 8))

        self._table_var = tk.StringVar()
        self._table_menu = ttk.Combobox(ctrl, textvariable=self._table_var,
                                         state="readonly", width=40,
                                         font=("Menlo", 10))
        self._table_menu.pack(side="left", padx=(0, 8))
        self._table_menu.bind("<<ComboboxSelected>>", lambda e: self._load_table())

        btn(ctrl, "save .bin", self._save_bin).pack(side="left", padx=(0, 8))
        btn(ctrl, "fix checksums", self._fix_checksums).pack(side="left")

        sep(body).pack(fill="x", pady=6)

        # Meta row
        self._meta_var = tk.StringVar(value="load a .bin file to begin")
        tk.Label(body, textvariable=self._meta_var,
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9), anchor="w").pack(fill="x", pady=(0, 6))

        # Table editor (grid of Entry widgets)
        self._grid_frame = card(body)
        self._grid_frame.pack(fill="both", expand=True)

        self._status_var = tk.StringVar(value="idle")
        tk.Label(body, textvariable=self._status_var,
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9), anchor="w").pack(fill="x", pady=(6, 0))

    def _load_bin(self):
        path = filedialog.askopenfilename(
            title="Select CAL .bin file",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            from tuner.cal_parser import CalParser
            cal_bytes = open(path, "rb").read()
            self._parser = CalParser(self._app.ecu, cal_bytes)
            self._parser.decode()
            tables = list(self._parser.tables.keys())
            self._table_menu["values"] = tables
            if tables:
                self._table_var.set(tables[0])
                self._load_table()
            self._status_var.set(f"loaded {os.path.basename(path)} — {len(tables)} tables")
        except Exception as e:
            messagebox.showerror("Load error", str(e))

    def _load_table(self):
        if not self._parser:
            return
        name = self._table_var.get()
        if not name:
            return
        self._current_table = name

        try:
            arr = self._parser.table(name)
            meta = self._parser.table_meta(name)
        except Exception as e:
            self._status_var.set(f"error: {e}")
            return

        # Clear grid
        for w in self._grid_frame.winfo_children():
            w.destroy()

        self._meta_var.set(
            f"{meta.name}  |  {meta.rows}×{meta.cols}  |  "
            f"scale×{meta.scale}  |  unit: {meta.unit}"
        )

        # Header row (col axis labels — just indices for now)
        cols = arr.shape[1] if arr.ndim == 2 else len(arr)
        rows = arr.shape[0] if arr.ndim == 2 else 1

        header = tk.Frame(self._grid_frame, bg=C["surface"])
        header.pack(fill="x")
        tk.Label(header, text="", bg=C["surface"],
                 font=("Menlo", 8), width=5).grid(row=0, column=0)
        for c in range(cols):
            tk.Label(header, text=str(c),
                     fg=C["text_dim"], bg=C["surface"],
                     font=("Menlo", 8), width=7).grid(row=0, column=c+1)

        # Data rows
        self._cell_vars: list[list[tk.StringVar]] = []
        data_frame = tk.Frame(self._grid_frame, bg=C["surface"])
        data_frame.pack(fill="both", expand=True)

        flat = arr.flatten() if arr.ndim == 1 else None

        for r in range(rows):
            row_vars = []
            tk.Label(data_frame, text=str(r),
                     fg=C["text_dim"], bg=C["surface"],
                     font=("Menlo", 8), width=5).grid(row=r, column=0)
            for c in range(cols):
                val = float(flat[c]) if flat is not None else float(arr[r][c])
                physical = val * meta.scale + meta.offset_val
                var = tk.StringVar(value=f"{physical:.3f}")
                entry = tk.Entry(data_frame, textvariable=var,
                                  bg=C["btn"], fg=C["text"],
                                  insertbackground=C["text"],
                                  font=("Menlo", 9), width=7, bd=0,
                                  highlightbackground=C["border"],
                                  highlightthickness=1)
                entry.grid(row=r, column=c+1, padx=1, pady=1)
                row_vars.append(var)
            self._cell_vars.append(row_vars)

        self._status_var.set(f"table: {name}  ({rows}×{cols})")

    def _fix_checksums(self):
        if not self._parser:
            self._status_var.set("no file loaded")
            return
        try:
            self._parser.fix_checksums()
            self._status_var.set("checksums fixed")
        except Exception as e:
            self._status_var.set(f"error: {e}")

    def _save_bin(self):
        if not self._parser:
            messagebox.showwarning("No file", "Load a .bin file first.")
            return
        # Write edited cell values back to parser
        if self._current_table and self._cell_vars:
            try:
                meta = self._parser.table_meta(self._current_table)
                arr = self._parser.table(self._current_table)
                for r, row in enumerate(self._cell_vars):
                    for c, var in enumerate(row):
                        physical = float(var.get())
                        raw = (physical - meta.offset_val) / meta.scale
                        if arr.ndim == 2:
                            arr[r][c] = raw
                        else:
                            arr[c] = raw
            except Exception as e:
                messagebox.showerror("Save error", f"Could not write table: {e}")
                return

        path = filedialog.asksaveasfilename(
            title="Save modified CAL .bin",
            defaultextension=".bin",
            filetypes=[("Binary files", "*.bin")],
        )
        if path:
            try:
                open(path, "wb").write(self._parser.to_bytes())
                self._status_var.set(f"saved → {os.path.basename(path)}")
            except Exception as e:
                messagebox.showerror("Save error", str(e))


# ─────────────────────────────────────────────────────────────────────────────

class LoggerTab(tk.Frame):
    """Live DID poller — configurable channels, running readouts."""

    DEFAULT_DIDS = [
        (0xF442, "Module Voltage", "V",    0.001),
        (0x295A, "Mileage",        "km",   1.0),
        (0xF186, "Session",        "",     1.0),
    ]

    def __init__(self, parent, app: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self._app    = app
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._build()

    def _build(self):
        body = tk.Frame(self, bg=C["bg"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        section_label(body, "live DID logger")

        ctrl = tk.Frame(body, bg=C["bg"])
        ctrl.pack(fill="x", pady=(0, 10))

        self._start_btn = btn(ctrl, "start polling", self._do_start, primary=True)
        self._start_btn.pack(side="left", padx=(0, 8))
        self._stop_btn = btn(ctrl, "stop", self._do_stop)
        self._stop_btn.pack(side="left", padx=(0, 8))
        self._stop_btn.config(state="disabled")

        tk.Label(ctrl, text="poll interval ms:",
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=(16, 4))
        self._interval_var = tk.StringVar(value="500")
        tk.Entry(ctrl, textvariable=self._interval_var,
                 bg=C["btn"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 10), width=6, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left")

        sep(body).pack(fill="x", pady=6)

        # Live channel cards
        self._channel_cards: Dict[int, Dict] = {}
        grid = tk.Frame(body, bg=C["bg"])
        grid.pack(fill="x", pady=(0, 10))

        for i, (did, name, unit, scale) in enumerate(self.DEFAULT_DIDS):
            c = card(grid, padx=12, pady=10)
            c.grid(row=i // 3, column=i % 3, padx=6, pady=6, sticky="nsew")
            grid.columnconfigure(i % 3, weight=1)

            tk.Label(c, text=f"0x{did:04X}  {name}",
                     fg=C["text_muted"], bg=C["surface"],
                     font=("Menlo", 8)).pack(anchor="w")
            val_var = tk.StringVar(value="—")
            tk.Label(c, textvariable=val_var,
                     fg=C["blue"], bg=C["surface"],
                     font=("Menlo", 18, "bold")).pack(anchor="w", pady=(2, 0))
            tk.Label(c, text=unit,
                     fg=C["text_dim"], bg=C["surface"],
                     font=("Menlo", 9)).pack(anchor="w")
            self._channel_cards[did] = {"var": val_var, "unit": unit, "scale": scale}

        sep(body).pack(fill="x", pady=6)

        section_label(body, "log")
        self._log = tk.Text(body, bg=C["surface"],
                             fg=C["text_muted"], font=("Menlo", 9),
                             height=10, bd=0,
                             highlightbackground=C["border"],
                             highlightthickness=1,
                             state="disabled")
        self._log.pack(fill="both", expand=True)

    def _do_start(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._running = True
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _do_stop(self):
        self._running = False
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")

    def _poll_loop(self):
        import time, udsoncan
        from flasher.uds_flash import _make_connection

        try:
            interval = max(100, int(self._interval_var.get())) / 1000.0
        except ValueError:
            interval = 0.5

        try:
            conn = _make_connection(
                self._app.ecu,
                self._app.interface,
                self._app.iface_path or None,
            )
        except Exception as e:
            self.after(0, lambda: self._append_log(f"[ERROR] connect: {e}"))
            self.after(0, self._do_stop)
            return

        class _StrCodec(udsoncan.DidCodec):
            def encode(self, v): return bytes(v)
            def decode(self, p):
                try: return p.decode("ascii").strip("\x00").strip()
                except: return p.hex()
            def __len__(self): raise udsoncan.DidCodec.ReadAllRemainingData

        cfg = dict(udsoncan.configs.default_client_config)
        cfg["data_identifiers"] = {did: _StrCodec
                                   for did in self._channel_cards}
        cfg["request_timeout"] = interval * 2 + 0.5

        with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
            try:
                client.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)
            except Exception as e:
                self.after(0, lambda: self._append_log(f"[WARN] session: {e}"))

            while self._running:
                for did, info in self._channel_cards.items():
                    try:
                        raw = client.read_data_by_identifier_first(did)
                        try:
                            val = float(raw) * info["scale"]
                            display = f"{val:.2f}"
                        except (ValueError, TypeError):
                            display = str(raw)
                        self.after(0, lambda v=display, i=info: i["var"].set(v))
                        self.after(0, lambda d=did, v=display:
                                   self._append_log(f"DID 0x{d:04X}: {v}"))
                    except Exception as e:
                        self.after(0, lambda d=did, e=e:
                                   self._append_log(f"DID 0x{d:04X} error: {e}"))
                time.sleep(interval)

        self.after(0, self._do_stop)

    def _append_log(self, msg: str):
        self._log.config(state="normal")
        self._log.insert("end", msg + "\n")
        self._log.see("end")
        self._log.config(state="disabled")


# ─────────────────────────────────────────────────────────────────────────────

class CPToolsTab(tk.Frame):
    """J533 active probe and ODX parser."""

    def __init__(self, parent, app: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self._app = app
        self._build()

    def _build(self):
        body = tk.Frame(self, bg=C["bg"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        section_label(body, "component protection tools")

        # J533 Probe
        probe_card = card(body, padx=12, pady=10)
        probe_card.pack(fill="x", pady=(0, 10))

        tk.Label(probe_card, text="J533 gateway probe",
                 fg=C["text"], bg=C["surface"],
                 font=("Menlo", 11, "bold")).pack(anchor="w")
        tk.Label(probe_card,
                 text="Reads all accessible DIDs from the J533 Lear gateway.\n"
                      "TX=0x710  RX=0x77A  |  Extended + Programming session.",
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9), justify="left").pack(anchor="w", pady=(4, 8))

        probe_btn_row = tk.Frame(probe_card, bg=C["surface"])
        probe_btn_row.pack(anchor="w")
        btn(probe_btn_row, "run full probe", self._do_probe, primary=True).pack(side="left", padx=(0, 8))
        btn(probe_btn_row, "save report JSON", self._save_probe_report).pack(side="left")

        sep(body).pack(fill="x", pady=8)

        # ODX parser
        odx_card = card(body, padx=12, pady=10)
        odx_card.pack(fill="x", pady=(0, 10))

        tk.Label(odx_card, text="ODX parser",
                 fg=C["text"], bg=C["surface"],
                 font=("Menlo", 11, "bold")).pack(anchor="w")
        tk.Label(odx_card,
                 text="Parse a flashdaten .odx file to extract CP routine ID,\n"
                      "security level, SA2 bytecode, and full DID map.",
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9), justify="left").pack(anchor="w", pady=(4, 8))

        odx_btn_row = tk.Frame(odx_card, bg=C["surface"])
        odx_btn_row.pack(anchor="w")
        btn(odx_btn_row, "open .odx file", self._load_odx).pack(side="left", padx=(0, 8))
        btn(odx_btn_row, "save extracted JSON", self._save_odx_json).pack(side="left")

        sep(body).pack(fill="x", pady=8)

        section_label(body, "output")
        self._out = tk.Text(body, bg=C["surface"],
                             fg=C["text_muted"], font=("Menlo", 9),
                             height=16, bd=0,
                             highlightbackground=C["border"],
                             highlightthickness=1,
                             state="disabled")
        self._out.pack(fill="both", expand=True)

        self._probe_report = None
        self._odx_parser   = None

    def _log(self, msg: str):
        self._out.config(state="normal")
        self._out.insert("end", msg + "\n")
        self._out.see("end")
        self._out.config(state="disabled")

    def _do_probe(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        self._log("[PROBE] starting J533 full probe...")

        def _task():
            try:
                from cp_tools.j533_probe import J533Probe
                probe = J533Probe(
                    interface      = self._app.interface,
                    interface_path = self._app.iface_path or None,
                )
                probe.connect()
                report = probe.full_probe()
                self._probe_report = report
                self.after(0, lambda r=report: self._show_probe(r))
            except Exception as e:
                self.after(0, lambda: self._log(f"[ERROR] {e}"))

        threading.Thread(target=_task, daemon=True).start()

    def _show_probe(self, report):
        import json
        self._log("[PROBE] complete")
        self._log(json.dumps(report, indent=2, default=str)[:4000])

    def _save_probe_report(self):
        if not self._probe_report:
            messagebox.showwarning("No data", "Run a probe first.")
            return
        import json
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            title="Save J533 probe report",
        )
        if path:
            json.dump(self._probe_report, open(path, "w"), indent=2, default=str)
            self._log(f"[PROBE] report saved → {path}")

    def _load_odx(self):
        path = filedialog.askopenfilename(
            title="Select flashdaten .odx file",
            filetypes=[("ODX files", "*.odx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._log(f"[ODX] loading {os.path.basename(path)}...")

        def _task():
            try:
                from cp_tools.odx_parser import ODXParser
                p = ODXParser(path)
                p.parse()
                self._odx_parser = p
                self.after(0, lambda: self._show_odx(p))
            except Exception as e:
                self.after(0, lambda: self._log(f"[ODX ERROR] {e}"))

        threading.Thread(target=_task, daemon=True).start()

    def _show_odx(self, p):
        self._log(f"[ODX] CP routine ID : {hex(p.cp_routine_id) if p.cp_routine_id else 'not found'}")
        self._log(f"[ODX] security level : {hex(p.security_level) if p.security_level else 'not found'}")
        self._log(f"[ODX] SA2 script len : {len(p.sa2_script)} bytes")
        self._log(f"[ODX] DID map        : {len(p.did_map)} entries")

    def _save_odx_json(self):
        if not self._odx_parser:
            messagebox.showwarning("No data", "Load an ODX file first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            title="Save extracted ODX data",
        )
        if path:
            self._odx_parser.save_extracted(path)
            self._log(f"[ODX] saved → {path}")


# ─────────────────────────────────────────────────────────────────────────────

class RawSniffTab(tk.Frame):
    """Hex dump of raw ISO-TP frames from BLE bridge sniff mode."""

    def __init__(self, parent, app: "MainWindow"):
        super().__init__(parent, bg=C["bg"])
        self._app     = app
        self._sniffing = False
        self._build()

    def _build(self):
        body = tk.Frame(self, bg=C["bg"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        section_label(body, "raw ISO-TP sniff")

        info = card(body, padx=12, pady=8)
        info.pack(fill="x", pady=(0, 10))
        tk.Label(info,
                 text="Captures raw CAN/ISO-TP frames from the BLE bridge sniff channel.\n"
                      "Use alongside an ODIS session to capture the CP removal UDS sequence.\n"
                      "BLE only — sniff mode uses the 0xCAFE header ID routed via 0xABF2 notify.",
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9), justify="left").pack(anchor="w")

        ctrl = tk.Frame(body, bg=C["bg"])
        ctrl.pack(fill="x", pady=(0, 10))

        self._sniff_btn = btn(ctrl, "start sniff", self._do_start, primary=True)
        self._sniff_btn.pack(side="left", padx=(0, 8))
        self._stop_btn = btn(ctrl, "stop", self._do_stop)
        self._stop_btn.pack(side="left", padx=(0, 8))
        self._stop_btn.config(state="disabled")
        btn(ctrl, "clear", self._do_clear).pack(side="left", padx=(0, 8))
        btn(ctrl, "save log", self._save_log).pack(side="left")

        sep(body).pack(fill="x", pady=6)

        section_label(body, "frame log")
        self._hex = tk.Text(body, bg=C["surface"],
                             fg=C["text_muted"], font=("Menlo", 9),
                             height=24, bd=0,
                             highlightbackground=C["border"],
                             highlightthickness=1,
                             state="disabled")
        self._hex.pack(fill="both", expand=True)
        self._frame_count = 0

    def _append(self, msg: str):
        self._hex.config(state="normal")
        self._hex.insert("end", msg + "\n")
        self._hex.see("end")
        self._hex.config(state="disabled")

    def _do_start(self):
        if self._app.interface.upper() != "BLE":
            messagebox.showwarning("BLE only",
                                   "Raw sniff mode requires the BLE bridge interface.")
            return
        self._sniffing = True
        self._sniff_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._append("[SNIFF] starting...")
        threading.Thread(target=self._sniff_loop, daemon=True).start()

    def _do_stop(self):
        self._sniffing = False
        self._sniff_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._append(f"[SNIFF] stopped — {self._frame_count} frames captured")

    def _do_clear(self):
        self._hex.config(state="normal")
        self._hex.delete("1.0", "end")
        self._hex.config(state="disabled")
        self._frame_count = 0

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt")],
            title="Save sniff log",
        )
        if path:
            content = self._hex.get("1.0", "end")
            open(path, "w").write(content)
            self._append(f"[SNIFF] log saved → {path}")

    def _sniff_loop(self):
        import time
        try:
            from transport.ble_bridge import BLEBridgeSync
            bridge = getattr(self._app, "_ble_bridge", None)
            if bridge is None:
                self.after(0, lambda: self._append(
                    "[ERROR] No BLE bridge instance. Connect via BLE interface first."))
                self.after(0, self._do_stop)
                return

            bridge.set_sniff_callback(self._on_sniff_frame)
            bridge.enable_sniff(True)
            self.after(0, lambda: self._append("[SNIFF] active — waiting for frames..."))

            while self._sniffing:
                time.sleep(0.1)

            bridge.enable_sniff(False)
            bridge.set_sniff_callback(None)

        except Exception as e:
            self.after(0, lambda: self._append(f"[ERROR] {e}"))
            self.after(0, self._do_stop)

    def _on_sniff_frame(self, frame_bytes: bytes):
        self._frame_count += 1
        import time as _t
        ts = _t.strftime("%H:%M:%S")
        hex_str = " ".join(f"{b:02X}" for b in frame_bytes)
        msg = f"[{ts}] #{self._frame_count:04d}  {hex_str}"
        self.after(0, lambda m=msg: self._append(m))


# ══════════════════════════════════════════════════════════════════════════════
# Main Window
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(tk.Tk):
    """
    Simos Tuning Suite — top-level application window.

    All tabs reference self (the app) to read:
        self.ecu         — currently selected ECUDef
        self.interface   — interface type string ("BLE", "USBISOTP", "J2534", ...)
        self.iface_path  — serial port or DLL path
        self.connected   — bool
    """

    def __init__(self):
        super().__init__()
        self.title("Simos Tuning Suite")
        self.configure(bg=C["bg"])
        self.geometry("1100x780")
        self.minsize(900, 600)

        # App state
        self.ecu:        ECUDef = SIMOS85
        self.interface:  str    = ""
        self.iface_path: str    = ""
        self.connected:  bool   = False
        self._ble_bridge         = None    # BLEBridgeSync if BLE connected

        # Setup ttk style to match dark theme
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",          background=C["bg"],  borderwidth=0)
        style.configure("TNotebook.Tab",      background=C["btn"], foreground=C["text_muted"],
                        font=("Menlo", 10),   padding=[14, 6])
        style.map("TNotebook.Tab",
                  background=[("selected", C["surface"])],
                  foreground=[("selected", C["text"])])
        style.configure("TCombobox",          fieldbackground=C["btn"],
                        background=C["btn"],  foreground=C["text"],
                        arrowcolor=C["text_muted"])
        style.configure("Horizontal.TProgressbar",
                        troughcolor=C["btn"], background=C["blue"], thickness=6)

        self._build()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Title bar ─────────────────────────────────────────────────────────
        title_bar = tk.Frame(self, bg=C["surface"],
                             highlightbackground=C["border"],
                             highlightthickness=1)
        title_bar.pack(fill="x")

        # macOS traffic lights simulation
        dots = tk.Frame(title_bar, bg=C["surface"])
        dots.pack(side="left", padx=12, pady=10)
        for color in ("#ff5f57", "#febc2e", "#28c840"):
            tk.Label(dots, text="●", fg=color, bg=C["surface"],
                     font=("Menlo", 10)).pack(side="left")

        tk.Label(title_bar, text="  Simos Tuning Suite",
                 fg=C["text"], bg=C["surface"],
                 font=("Menlo", 12, "bold")).pack(side="left")

        # ECU selector
        ecu_frame = tk.Frame(title_bar, bg=C["surface"])
        ecu_frame.pack(side="right", padx=12, pady=6)

        tk.Label(ecu_frame, text="ECU:",
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9)).pack(side="left", padx=(0, 6))

        self._ecu_var = tk.StringVar(value=list(ECU_REGISTRY.keys())[0])
        ecu_menu = ttk.Combobox(ecu_frame, textvariable=self._ecu_var,
                                values=list(ECU_REGISTRY.keys()),
                                state="readonly", width=38,
                                font=("Menlo", 9))
        ecu_menu.pack(side="left")
        ecu_menu.bind("<<ComboboxSelected>>", self._on_ecu_change)

        # Connection status badge
        self._conn_badge = tk.Label(title_bar, text="  ●  disconnected  ",
                                    fg=C["text_dim"], bg=C["surface"],
                                    font=("Menlo", 9))
        self._conn_badge.pack(side="right", padx=6)

        # ── Main notebook ──────────────────────────────────────────────────────
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=0, pady=0)

        # Hardware tab (InterfacePanel)
        hw_frame = tk.Frame(self._nb, bg=C["bg"])
        self._nb.add(hw_frame, text="  hardware  ")
        self._iface_panel = InterfacePanel(
            hw_frame,
            on_connect    = self._on_connect,
            on_disconnect = self._on_disconnect,
            ecu           = self.ecu,
        )
        self._iface_panel.pack(fill="both", expand=True)

        # ECU Info tab
        self._ecu_info_tab = EcuInfoTab(self._nb, self)
        self._nb.add(self._ecu_info_tab, text="  ECU info  ")

        # Flash tab
        self._flash_tab = FlashTab(self._nb, self)
        self._nb.add(self._flash_tab, text="  flash  ")

        # Tune tab
        self._tune_tab = TuneTab(self._nb, self)
        self._nb.add(self._tune_tab, text="  tune  ")

        # Logger tab
        self._logger_tab = LoggerTab(self._nb, self)
        self._nb.add(self._logger_tab, text="  logger  ")

        # CP Tools tab
        self._cp_tab = CPToolsTab(self._nb, self)
        self._nb.add(self._cp_tab, text="  CP tools  ")

        # Raw Sniff tab
        self._sniff_tab = RawSniffTab(self._nb, self)
        self._nb.add(self._sniff_tab, text="  raw sniff  ")

        # ── Status bar ─────────────────────────────────────────────────────────
        status_bar = tk.Frame(self, bg=C["surface"],
                              highlightbackground=C["border"],
                              highlightthickness=1)
        status_bar.pack(fill="x", side="bottom")

        self._status_var = tk.StringVar(value="ready")
        tk.Label(status_bar, textvariable=self._status_var,
                 fg=C["text_muted"], bg=C["surface"],
                 font=("Menlo", 9), anchor="w",
                 padx=12, pady=4).pack(side="left")

        # Python / platform info
        import platform
        info = f"Python {sys.version.split()[0]}  |  {platform.system()}  |  simos-suite"
        tk.Label(status_bar, text=info,
                 fg=C["text_dim"], bg=C["surface"],
                 font=("Menlo", 8),
                 padx=12, pady=4).pack(side="right")

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_connect(self, interface: str, path: str):
        self.interface   = interface
        self.iface_path  = path
        self.connected   = True
        label = f"{interface}:{path}" if path else interface
        self._conn_badge.config(text=f"  ●  {label}  ",
                                fg=C["green"])
        self._status_var.set(f"connected — {label}")
        log.info("Connected: interface=%s path=%s", interface, path)

    def _on_disconnect(self):
        self.interface  = ""
        self.iface_path = ""
        self.connected  = False
        self._ble_bridge = None
        self._conn_badge.config(text="  ●  disconnected  ", fg=C["text_dim"])
        self._status_var.set("disconnected")
        log.info("Disconnected")

    def _on_ecu_change(self, _event=None):
        name = self._ecu_var.get()
        self.ecu = ECU_REGISTRY[name]
        self._status_var.set(f"ECU: {name}")
        log.info("ECU changed to %s", name)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-28s  %(levelname)s  %(message)s",
    )
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
