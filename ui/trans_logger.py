"""
ui/trans_logger.py — Transmission Live Data Tab

Connects to any supported TCU (DQ250, DQ381, ZF 8HP, DL501) via the
currently selected hardware interface and reads live DIDs in a polling loop.

Displays:
  - Current gear + selector position (large, at-a-glance)
  - Temperature channels (ATF, clutch packs, TCU)
  - Speed channels (input, output, vehicle)
  - Pressure channels
  - Torque channels
  - Electrical + wear/adaptation values

Usage (embedded in MainWindow):
    from ui.trans_logger import TransLoggerTab
    tab = TransLoggerTab(notebook, app)
    notebook.add(tab, text="  trans  ")
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Dict, Optional, List

from core.trans_defs import TRANS_REGISTRY, TRANS_DISPLAY_NAMES, TransDef, TransDID

# Colour palette (matches rest of suite)
C = {
    "bg":         "#0d1117",
    "surface":    "#161b22",
    "border":     "#30363d",
    "text":       "#e6edf3",
    "text_muted": "#8b949e",
    "text_dim":   "#484f58",
    "green":      "#3fb950",
    "amber":      "#d29922",
    "red":        "#f85149",
    "blue":       "#58a6ff",
    "btn":        "#21262d",
    "btn_hover":  "#30363d",
}

# DID category colour accents
CAT_COLOR = {
    "gear":    "#58a6ff",   # blue
    "temp":    "#f0a050",   # warm orange
    "speed":   "#3fb950",   # green
    "pressure":"#bc8cff",   # purple
    "torque":  "#79c0ff",   # light blue
    "elec":    "#d29922",   # amber
    "adapt":   "#8b949e",   # muted
}

def _did_category(did: TransDID) -> str:
    n = did.name.lower()
    if any(w in n for w in ("gear","selector","sport","mode")):        return "gear"
    if any(w in n for w in ("temp","thermal")):                        return "temp"
    if any(w in n for w in ("speed","rpm","velocity")):                return "speed"
    if any(w in n for w in ("pressure","bar","line")):                 return "pressure"
    if any(w in n for w in ("torque","nm")):                           return "torque"
    if any(w in n for w in ("volt","voltage","supply")):               return "elec"
    return "adapt"


class TransLoggerTab(tk.Frame):
    """
    Transmission live data display tab.
    Embedded in MainWindow as the 'trans' tab.
    """

    def __init__(self, parent, app):
        super().__init__(parent, bg=C["bg"])
        self._app     = app
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._trans:  Optional[TransDef] = None
        self._cards:  Dict[int, Dict] = {}   # did → {var, unit_var, label}
        self._build()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        # Top control bar
        top = tk.Frame(self, bg=C["bg"], padx=14, pady=10)
        top.pack(fill="x")

        tk.Label(top, text="TRANSMISSION",
                 fg=C["text_dim"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left")

        # Transmission selector
        self._trans_var = tk.StringVar()
        trans_names = list(TRANS_DISPLAY_NAMES.values())
        if trans_names:
            self._trans_var.set(trans_names[0])

        self._trans_menu = ttk.Combobox(
            top, textvariable=self._trans_var,
            values=trans_names,
            state="readonly", width=45, font=("Menlo", 9),
        )
        self._trans_menu.pack(side="left", padx=(10, 10))
        self._trans_menu.bind("<<ComboboxSelected>>", self._on_trans_change)

        # Interval
        tk.Label(top, text="poll ms:", fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=(8, 4))
        self._interval_var = tk.StringVar(value="500")
        tk.Entry(top, textvariable=self._interval_var,
                 bg=C["btn"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 9), width=5, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left")

        # Buttons
        self._start_btn = tk.Button(
            top, text="start", command=self._do_start,
            fg="#0d1117", bg=C["blue"],
            activeforeground="#0d1117", activebackground="#79b8ff",
            font=("Menlo", 10, "bold"), bd=0, padx=12, pady=4,
            cursor="hand2",
            highlightbackground=C["blue"], highlightthickness=1,
        )
        self._start_btn.pack(side="left", padx=(12, 6))

        self._stop_btn = tk.Button(
            top, text="stop", command=self._do_stop,
            fg=C["text"], bg=C["btn"],
            activeforeground=C["text"], activebackground=C["btn_hover"],
            font=("Menlo", 10), bd=0, padx=12, pady=4,
            cursor="hand2",
            highlightbackground=C["border"], highlightthickness=1,
            state="disabled",
        )
        self._stop_btn.pack(side="left")

        # Status dot
        self._status_dot = tk.Label(top, text="●", fg=C["text_dim"],
                                    bg=C["bg"], font=("Menlo", 10))
        self._status_dot.pack(side="left", padx=(12, 4))
        self._status_var = tk.StringVar(value="idle")
        tk.Label(top, textvariable=self._status_var,
                 fg=C["text_muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left")

        # Separator
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Hero row — gear + ATF temp (always visible, big)
        hero = tk.Frame(self, bg=C["surface"], padx=16, pady=12)
        hero.pack(fill="x")

        self._gear_var    = tk.StringVar(value="—")
        self._sel_var     = tk.StringVar(value="—")
        self._atf_var     = tk.StringVar(value="—")
        self._vspeed_var  = tk.StringVar(value="—")

        for label_text, val_var, unit, col in [
            ("gear",      self._gear_var,   "",      CAT_COLOR["gear"]),
            ("selector",  self._sel_var,    "",      CAT_COLOR["gear"]),
            ("ATF temp",  self._atf_var,    "°C",    CAT_COLOR["temp"]),
            ("speed",     self._vspeed_var, "km/h",  CAT_COLOR["speed"]),
        ]:
            cell = tk.Frame(hero, bg=C["surface"])
            cell.pack(side="left", expand=True, fill="x", padx=8)
            tk.Label(cell, text=label_text,
                     fg=C["text_dim"], bg=C["surface"],
                     font=("Menlo", 8)).pack(anchor="w")
            tk.Label(cell, textvariable=val_var,
                     fg=col, bg=C["surface"],
                     font=("Menlo", 28, "bold")).pack(anchor="w")
            tk.Label(cell, text=unit,
                     fg=C["text_dim"], bg=C["surface"],
                     font=("Menlo", 9)).pack(anchor="w")

        # Separator
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Scrollable DID card grid
        canvas_frame = tk.Frame(self, bg=C["bg"])
        canvas_frame.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(canvas_frame, bg=C["bg"],
                                  highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical",
                                   command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._grid_inner = tk.Frame(self._canvas, bg=C["bg"])
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._grid_inner, anchor="nw"
        )
        self._grid_inner.bind("<Configure>", self._on_grid_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)

        # Log strip at bottom
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        self._log = tk.Text(self, bg=C["surface"], fg=C["text_muted"],
                             font=("Menlo", 8), height=4, bd=0,
                             highlightbackground=C["border"],
                             highlightthickness=1,
                             state="disabled")
        self._log.pack(fill="x", padx=0, pady=0)

        # Build initial card grid for first transmission
        self._on_trans_change()

    # ── Transmission change ───────────────────────────────────────────────────

    def _on_trans_change(self, _event=None):
        name = self._trans_var.get()
        self._trans = None
        for code, t in TRANS_REGISTRY.items():
            if t.name == name:
                self._trans = t
                break
        if self._trans:
            self._build_cards(self._trans)
            self._reset_hero()
            # Auto-select matching trans when ECU changes if possible
            if hasattr(self._app, "_ecu_var"):
                from core.trans_defs import ECU_DEFAULT_TRANS
                from core.ecu_defs import ECU_REGISTRY
                # find project code
                for code, ecu in ECU_REGISTRY.items():
                    if ecu.name == self._app._ecu_var.get():
                        default = ECU_DEFAULT_TRANS.get(code)
                        if default and default in TRANS_REGISTRY:
                            pass  # already selected, don't override user choice

    def _build_cards(self, trans: TransDef):
        for w in self._grid_inner.winfo_children():
            w.destroy()
        self._cards.clear()

        # Group DIDs by category
        cats: Dict[str, List[TransDID]] = {}
        for did in trans.live_dids:
            cat = _did_category(did)
            cats.setdefault(cat, []).append(did)

        cat_order = ["gear", "temp", "speed", "pressure", "torque", "elec", "adapt"]
        cat_labels = {
            "gear": "gear / selector",
            "temp": "temperatures",
            "speed": "shaft speeds",
            "pressure": "pressures",
            "torque": "torque",
            "elec": "electrical",
            "adapt": "wear / adaptation",
        }

        row = 0
        for cat in cat_order:
            dids = cats.get(cat, [])
            if not dids:
                continue

            # Section label
            tk.Label(self._grid_inner,
                     text=cat_labels[cat].upper(),
                     fg=C["text_dim"], bg=C["bg"],
                     font=("Menlo", 8)).grid(
                row=row, column=0, columnspan=4,
                sticky="w", padx=14, pady=(10, 2))
            row += 1

            # Cards — 4 per row
            for i, did in enumerate(dids):
                col = i % 4
                if col == 0 and i > 0:
                    row += 1

                card_frame = tk.Frame(
                    self._grid_inner, bg=C["surface"],
                    highlightbackground=C["border"],
                    highlightthickness=1,
                    padx=10, pady=8,
                )
                card_frame.grid(row=row, column=col,
                                padx=5, pady=3, sticky="nsew")
                self._grid_inner.columnconfigure(col, weight=1, minsize=140)

                tk.Label(card_frame,
                         text=f"0x{did.did:04X}",
                         fg=C["text_dim"], bg=C["surface"],
                         font=("Menlo", 7)).pack(anchor="w")

                tk.Label(card_frame,
                         text=did.name,
                         fg=C["text_muted"], bg=C["surface"],
                         font=("Menlo", 8)).pack(anchor="w")

                val_var = tk.StringVar(value="—")
                tk.Label(card_frame,
                         textvariable=val_var,
                         fg=CAT_COLOR.get(cat, C["text"]),
                         bg=C["surface"],
                         font=("Menlo", 15, "bold")).pack(anchor="w", pady=(2, 0))

                unit_var = tk.StringVar(value=did.unit)
                tk.Label(card_frame,
                         textvariable=unit_var,
                         fg=C["text_dim"], bg=C["surface"],
                         font=("Menlo", 7)).pack(anchor="w")

                self._cards[did.did] = {
                    "var":     val_var,
                    "unit":    unit_var,
                    "did_obj": did,
                }

            row += 1

    def _reset_hero(self):
        for v in (self._gear_var, self._sel_var,
                  self._atf_var, self._vspeed_var):
            v.set("—")

    # ── Polling ───────────────────────────────────────────────────────────────

    def _do_start(self):
        if not self._app.connected:
            messagebox.showwarning("Not connected",
                                   "Connect a hardware interface first.")
            return
        if not self._trans:
            messagebox.showwarning("No transmission",
                                   "Select a transmission first.")
            return
        self._running = True
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._set_status(C["amber"], "connecting...")
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _do_stop(self):
        self._running = False
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._set_status(C["text_dim"], "stopped")

    def _poll_loop(self):
        import time, udsoncan
        from flasher.uds_flash import _make_connection

        trans = self._trans
        if not trans:
            return

        try:
            interval = max(100, int(self._interval_var.get())) / 1000.0
        except ValueError:
            interval = 0.5

        try:
            conn = _make_connection(
                # TransDef acts as a duck-typed ECUDef here — _make_connection
                # only needs .can_tx and .can_rx
                trans,
                self._app.interface,
                self._app.iface_path or None,
            )
        except Exception as e:
            self.after(0, lambda: self._append_log(f"[ERROR] connect: {e}"))
            self.after(0, self._do_stop)
            return

        class _RawCodec(udsoncan.DidCodec):
            def __init__(self, length):
                self._length = length
            def encode(self, v): return bytes(v)
            def decode(self, p): return p
            def __len__(self): return self._length

        cfg = dict(udsoncan.configs.default_client_config)
        cfg["data_identifiers"] = {
            d.did: _RawCodec(d.length) for d in trans.live_dids
        }
        cfg["request_timeout"] = interval * 2 + 1.0

        self.after(0, lambda: self._set_status(C["green"], f"polling {trans.project}"))

        with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
            try:
                client.change_session(
                    udsoncan.services.DiagnosticSessionControl
                    .Session.extendedDiagnosticSession)
            except Exception as e:
                self.after(0, lambda: self._append_log(f"[WARN] session: {e}"))

            while self._running:
                for did_obj in trans.live_dids:
                    did = did_obj.did
                    try:
                        raw = client.read_data_by_identifier_first(did)
                        if isinstance(raw, bytes):
                            if did_obj.signed:
                                raw_int = int.from_bytes(
                                    raw[:did_obj.length], "big", signed=True)
                            else:
                                raw_int = int.from_bytes(
                                    raw[:did_obj.length], "big", signed=False)
                            physical = raw_int * did_obj.scale + did_obj.offset
                            if did_obj.unit in ("", ""):
                                display = f"{int(physical)}"
                            else:
                                display = f"{physical:.1f}"
                        else:
                            display = str(raw)

                        self.after(0, lambda d=did, v=display:
                                   self._update_card(d, v))

                    except udsoncan.exceptions.NegativeResponseException:
                        pass   # DID not supported in current session — skip silently
                    except Exception as e:
                        self.after(0, lambda d=did, e=e:
                                   self._append_log(
                                       f"0x{d:04X} err: {type(e).__name__}"))

                time.sleep(interval)

        self.after(0, self._do_stop)

    def _update_card(self, did: int, value: str):
        card = self._cards.get(did)
        if card:
            card["var"].set(value)

        # Update hero row from known DIDs
        trans = self._trans
        if not trans:
            return
        # Find what this DID represents
        for d in trans.live_dids:
            if d.did != did:
                continue
            n = d.name.lower()
            if "current gear" in n:
                self._gear_var.set(value)
            elif "selector" in n:
                self._sel_var.set(value)
            elif "atf temp" in n:
                self._atf_var.set(value)
            elif "vehicle speed" in n:
                self._vspeed_var.set(value)

    # ── Canvas resize helpers ─────────────────────────────────────────────────

    def _on_grid_resize(self, event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, color: str, msg: str):
        self._status_dot.config(fg=color)
        self._status_var.set(msg)

    def _append_log(self, msg: str):
        self._log.config(state="normal")
        self._log.insert("end", msg + "\n")
        self._log.see("end")
        self._log.config(state="disabled")
