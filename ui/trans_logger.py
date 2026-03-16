"""
ui/trans_logger.py — Transmission live data tab

Plugs into MainWindow as a _Tab subclass. Reads live DIDs from any supported
TCU (ZF 8HP, DL501, DQ250, DQ381) via the already-connected interface.

Layout
──────
  Top bar     transmission selector, poll interval, start/stop, status dot
  Hero strip  Current Gear · Selector · ATF Temp · Vehicle Speed  (large)
  Card grid   all other DIDs grouped by category (temps / speeds / pressures /
              torque / electrical / wear), 4 columns, scrollable
  Log strip   brief error/event log at the bottom (4 lines)

Integration
───────────
  on_connect()    → enables start button
  on_disconnect() → stops polling if running
  Uses self.mw.get_connection() which returns a fresh udsoncan connection
  routed through whichever interface is connected (BLE, USB, J2534, SocketCAN)

Transmission selector is independent of the ECU selector — lets you talk to
the TCU directly without having to switch the ECU definition.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Dict, List, Optional

from core.trans_defs import (
    TRANS_REGISTRY, TRANS_DISPLAY_NAMES, ECU_DEFAULT_TRANS,
)
from core.ecu_defs import TCUDef, TCU_LIVE_DIDS, decode_tcu_did

# ── Palette (matches main_window.py) ─────────────────────────────────────────
C = {
    "bg":      "#0d1117",
    "surface": "#161b22",
    "border":  "#30363d",
    "text":    "#e6edf3",
    "muted":   "#8b949e",
    "dim":     "#484f58",
    "green":   "#3fb950",
    "amber":   "#d29922",
    "red":     "#f85149",
    "blue":    "#58a6ff",
    "btn":     "#21262d",
    "btn_h":   "#30363d",
}

CAT_COLOR = {
    "gear":     "#58a6ff",
    "temp":     "#f0a050",
    "speed":    "#3fb950",
    "pressure": "#bc8cff",
    "torque":   "#79c0ff",
    "elec":     "#d29922",
    "adapt":    "#8b949e",
}

CAT_LABEL = {
    "gear":     "gear / selector",
    "temp":     "temperatures",
    "speed":    "shaft speeds",
    "pressure": "pressures",
    "torque":   "torque",
    "elec":     "electrical",
    "adapt":    "wear / adaptation",
}

CAT_ORDER = ["gear", "temp", "speed", "pressure", "torque", "elec", "adapt"]


def _did_cat(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ("gear", "selector", "sport", "mode", "lever", "session")):
        return "gear"
    if any(w in n for w in ("temp", "thermal")):
        return "temp"
    if any(w in n for w in ("speed", "rpm", "shaft", "slip")):
        return "speed"
    if any(w in n for w in ("pressure", "bar", "line")):
        return "pressure"
    if any(w in n for w in ("torque", " nm")):
        return "torque"
    if any(w in n for w in ("volt", "voltage", "supply")):
        return "elec"
    return "adapt"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lbl(parent, text, size=9, color=None, **kw):
    return tk.Label(parent, text=text,
                    fg=color or C["muted"], bg=parent["bg"],
                    font=("Menlo", size), **kw)


def _btn(parent, text, cmd, primary=False, **kw):
    return tk.Button(
        parent, text=text, command=cmd,
        fg="#0d1117" if primary else C["text"],
        bg=C["blue"] if primary else C["btn"],
        activeforeground="#0d1117" if primary else C["text"],
        activebackground="#79b8ff" if primary else C["btn_h"],
        font=("Menlo", 10), bd=0, padx=11, pady=4,
        cursor="hand2",
        highlightbackground=C["blue"] if primary else C["border"],
        highlightthickness=1, **kw,
    )


def _sep(parent):
    return tk.Frame(parent, bg=C["border"], height=1)


# ══════════════════════════════════════════════════════════════════════════════

class TransLoggerTab(tk.Frame):
    """
    Transmission live data tab.
    Inherits from tk.Frame rather than _Tab so it can be used standalone
    or embedded — MainWindow adds it to the notebook directly.
    """

    def __init__(self, parent, mw):
        super().__init__(parent, bg=C["bg"])
        self.mw       = mw
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._trans:  Optional[TransDef] = None
        self._cards:  Dict[int, tk.StringVar] = {}
        self._build()

    # ── Called by MainWindow ──────────────────────────────────────────────────

    def on_connect(self):
        self._start_btn.config(state="normal")
        # Auto-suggest trans based on current ECU
        if self.mw.ecu:
            for code, ecu in __import__("core.ecu_defs",
                                        fromlist=["ECU_REGISTRY"]).ECU_REGISTRY.items():
                if ecu is self.mw.ecu:
                    default = ECU_DEFAULT_TRANS.get(code)
                    if default and default in TRANS_REGISTRY:
                        self._set_trans_by_key(default)
                    break

    def on_disconnect(self):
        if self._running:
            self._do_stop()
        self._start_btn.config(state="disabled")

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Top control bar ───────────────────────────────────────────────────
        top = tk.Frame(self, bg=C["bg"], padx=14, pady=8)
        top.pack(fill="x")

        _lbl(top, "TRANS", size=9).pack(side="left")

        # Transmission selector
        self._trans_var = tk.StringVar()
        trans_names = list(TRANS_DISPLAY_NAMES.values())
        self._trans_var.set(trans_names[0] if trans_names else "")

        self._trans_menu = ttk.Combobox(
            top, textvariable=self._trans_var,
            values=trans_names, state="readonly",
            font=("Menlo", 9), width=46,
        )
        self._trans_menu.pack(side="left", padx=(8, 10))
        self._trans_menu.bind("<<ComboboxSelected>>",
                               lambda _: self._on_trans_change())

        _lbl(top, "poll ms").pack(side="left")
        self._interval_var = tk.StringVar(value="500")
        tk.Entry(top, textvariable=self._interval_var,
                 bg=C["btn"], fg=C["text"],
                 insertbackground=C["text"],
                 font=("Menlo", 9), width=5, bd=0,
                 highlightbackground=C["border"],
                 highlightthickness=1).pack(side="left", padx=(4, 10))

        self._start_btn = _btn(top, "start", self._do_start, primary=True)
        self._start_btn.pack(side="left", padx=(0, 6))
        self._start_btn.config(state="disabled")

        self._stop_btn = _btn(top, "stop", self._do_stop)
        self._stop_btn.pack(side="left")
        self._stop_btn.config(state="disabled")

        self._dot = tk.Label(top, text="●", fg=C["dim"],
                              bg=C["bg"], font=("Menlo", 10))
        self._dot.pack(side="left", padx=(10, 4))
        self._status_var = tk.StringVar(value="idle")
        tk.Label(top, textvariable=self._status_var,
                 fg=C["muted"], bg=C["bg"],
                 font=("Menlo", 9)).pack(side="left")

        _sep(self).pack(fill="x")

        # ── Hero strip — big at-a-glance values ───────────────────────────────
        hero = tk.Frame(self, bg=C["surface"], padx=16, pady=10)
        hero.pack(fill="x")

        self._hero_vars: Dict[str, tk.StringVar] = {}
        for label, unit, color in [
            ("gear",    "",      CAT_COLOR["gear"]),
            ("select",  "",      CAT_COLOR["gear"]),
            ("ATF",     "°C",    CAT_COLOR["temp"]),
            ("speed",   "km/h",  CAT_COLOR["speed"]),
            ("in RPM",  "rpm",   CAT_COLOR["speed"]),
        ]:
            cell = tk.Frame(hero, bg=C["surface"])
            cell.pack(side="left", expand=True, fill="x", padx=6)
            _lbl(cell, label, size=8, color=C["dim"]).pack(anchor="w")
            var = tk.StringVar(value="—")
            self._hero_vars[label] = var
            tk.Label(cell, textvariable=var,
                     fg=color, bg=C["surface"],
                     font=("Menlo", 26, "bold")).pack(anchor="w")
            _lbl(cell, unit, size=8, color=C["dim"]).pack(anchor="w")

        _sep(self).pack(fill="x")

        # ── Scrollable card grid ───────────────────────────────────────────────
        wrap = tk.Frame(self, bg=C["bg"])
        wrap.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(wrap, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical",
                           command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = tk.Frame(self._canvas, bg=C["bg"])
        self._win_id = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
                          lambda e: self._canvas.configure(
                              scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                           lambda e: self._canvas.itemconfig(
                               self._win_id, width=e.width))

        # Mousewheel scroll
        self._canvas.bind_all("<MouseWheel>",
                               lambda e: self._canvas.yview_scroll(
                                   -1 * (e.delta // 120), "units"))

        # ── Log strip ─────────────────────────────────────────────────────────
        _sep(self).pack(fill="x")
        self._log = tk.Text(self, bg=C["surface"], fg=C["muted"],
                             font=("Menlo", 8), height=3, bd=0,
                             highlightbackground=C["border"],
                             highlightthickness=1, state="disabled")
        self._log.pack(fill="x")

        # ── Tag colours for log ───────────────────────────────────────────────
        self._log.tag_configure("err",  foreground=C["red"])
        self._log.tag_configure("warn", foreground=C["amber"])
        self._log.tag_configure("ok",   foreground=C["green"])

        # Build initial card grid
        self._on_trans_change()

    # ── Transmission change ───────────────────────────────────────────────────

    def _on_trans_change(self):
        name = self._trans_var.get()
        self._trans = None
        for key, t in TRANS_REGISTRY.items():
            if t.name == name or key == name:
                self._trans = t
                break
        if not self._trans and TRANS_REGISTRY:
            self._trans = next(iter(TRANS_REGISTRY.values()))
        if self._trans:
            self._build_cards(self._trans)
        self._reset_hero()

    def _set_trans_by_key(self, key: str):
        t = TRANS_REGISTRY.get(key)
        if t:
            self._trans_var.set(t.name)
            self._trans = t
            self._build_cards(t)
            self._reset_hero()

    # ── Card grid ─────────────────────────────────────────────────────────────

    def _build_cards(self, trans: TCUDef):
        for w in self._inner.winfo_children():
            w.destroy()
        self._cards.clear()

        # Group DIDs by category — live_dids is Dict[int, (name,unit,scale,offset,fmt)]
        cats: Dict[str, List[tuple]] = {}   # cat → [(did, name, unit, scale, offset, fmt)]
        for did, spec in trans.live_dids.items():
            name = spec[0]
            cats.setdefault(_did_cat(name), []).append((did,) + spec)

        grid_row = 0
        COLS = 4

        for cat in CAT_ORDER:
            dids = cats.get(cat, [])
            if not dids:
                continue

            # Section header
            tk.Label(self._inner,
                     text=CAT_LABEL[cat].upper(),
                     fg=C["dim"], bg=C["bg"],
                     font=("Menlo", 8)).grid(
                row=grid_row, column=0, columnspan=COLS,
                sticky="w", padx=14, pady=(10, 3))
            grid_row += 1

            for i, spec in enumerate(dids):
                did_int, name, unit, scale, offset, fmt = spec
                col = i % COLS
                if i > 0 and col == 0:
                    grid_row += 1

                card = tk.Frame(self._inner, bg=C["surface"],
                                highlightbackground=C["border"],
                                highlightthickness=1,
                                padx=10, pady=8)
                card.grid(row=grid_row, column=col,
                          padx=4, pady=2, sticky="nsew")
                self._inner.columnconfigure(col, weight=1, minsize=130)

                # DID address
                tk.Label(card, text=f"0x{did_int:04X}",
                         fg=C["dim"], bg=C["surface"],
                         font=("Menlo", 7)).pack(anchor="w")
                # Name
                tk.Label(card, text=name,
                         fg=C["muted"], bg=C["surface"],
                         font=("Menlo", 8)).pack(anchor="w")
                # Value (large)
                var = tk.StringVar(value="—")
                tk.Label(card, textvariable=var,
                         fg=CAT_COLOR.get(cat, C["text"]),
                         bg=C["surface"],
                         font=("Menlo", 14, "bold")).pack(
                             anchor="w", pady=(2, 0))
                # Unit
                tk.Label(card, text=unit,
                         fg=C["dim"], bg=C["surface"],
                         font=("Menlo", 7)).pack(anchor="w")

                self._cards[did_int] = var

            grid_row += 1

    def _reset_hero(self):
        for v in self._hero_vars.values():
            v.set("—")

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _do_start(self):
        if not self.mw.connected:
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
        self._set_status(C["amber"], "connecting…")
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True)
        self._thread.start()

    def _do_stop(self):
        self._running = False
        self._start_btn.config(state="normal" if self.mw.connected
                                else "disabled")
        self._stop_btn.config(state="disabled")
        self._set_status(C["dim"], "stopped")

    def _poll_loop(self):
        import time as _time
        import udsoncan

        trans = self._trans

        try:
            interval = max(100, int(self._interval_var.get())) / 1000.0
        except ValueError:
            interval = 0.5

        # Build a duck-typed namespace _make_connection can use
        class _TCUProxy:
            can_tx = trans.can_tx
            can_rx = trans.can_rx
            sa2_script = trans.sa2_script

        try:
            from flasher.uds_flash import _make_connection
            conn = _make_connection(
                _TCUProxy(),
                self.mw.interface,
                interface_path=self.mw.iface_path,
                ble_bridge=getattr(self.mw, "ble_bridge", None),
            )
        except Exception as e:
            self.after(0, lambda: self._log_msg(f"connect error: {e}", "err"))
            self.after(0, self._do_stop)
            return

        class _RawCodec(udsoncan.DidCodec):
            def __init__(self, length):
                self._len = length
            def encode(self, v):
                return bytes(v)
            def decode(self, p):
                return bytes(p[:self._len])
            def __len__(self):
                return self._len

        cfg = dict(udsoncan.configs.default_client_config)
        # Use a 4-byte raw codec for all TCU DIDs — decode_tcu_did handles parsing
        cfg["data_identifiers"] = {
            did: _RawCodec(4) for did in trans.live_dids
        }
        cfg["request_timeout"] = max(2.0, interval * 3)

        self.after(0, lambda: self._set_status(
            C["green"], f"polling  {trans.project}  {trans.can_tx:#05x}→{trans.can_rx:#05x}"))

        try:
            with udsoncan.Client(conn, request_timeout=5, config=cfg) as client:
                try:
                    client.change_session(
                        udsoncan.services.DiagnosticSessionControl
                        .Session.extendedDiagnosticSession)
                    self.after(0, lambda: self._log_msg(
                        f"extended session opened — {trans.name}", "ok"))
                except Exception as e:
                    self.after(0, lambda: self._log_msg(
                        f"session warn: {e}", "warn"))

                while self._running:
                    for did_obj in trans.live_dids:
                        if not self._running:
                            break
                        try:
                            raw = client.read_data_by_identifier_first(did_obj)
                            if isinstance(raw, (bytes, bytearray)):
                                display, _, _ = decode_tcu_did(did_obj, raw)
                            else:
                                display = str(raw)
                            self.after(0, lambda d=did_obj,
                                       v=display: self._update(d, v))
                        except udsoncan.exceptions.NegativeResponseException:
                            pass  # DID not supported — silently skip
                        except Exception as e:
                            self.after(0, lambda d=did_obj, e=e:
                                       self._log_msg(
                                           f"0x{d:04X}: {type(e).__name__}",
                                           "warn"))
                    _time.sleep(interval)

        except Exception as e:
            self.after(0, lambda: self._log_msg(f"poll error: {e}", "err"))

        self.after(0, self._do_stop)

    # ── Card + hero update ────────────────────────────────────────────────────

    def _update(self, did: int, value: str):
        var = self._cards.get(did)
        if var:
            var.set(value)
        # Mirror to hero strip
        trans = self._trans
        if not trans or did not in trans.live_dids:
            return
        name = trans.live_dids[did][0].lower()
        if "current gear" in name or "gear" == name:
            self._hero_vars["gear"].set(value)
        elif "selector" in name or "lever" in name:
            self._hero_vars["select"].set(value)
        elif "fluid temp" in name or "atf" in name:
            self._hero_vars["ATF"].set(value)
        elif "vehicle speed" in name:
            self._hero_vars["speed"].set(value)
        elif "input" in name and ("speed" in name or "rpm" in name):
            self._hero_vars["in RPM"].set(value)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, color: str, msg: str):
        self._dot.config(fg=color)
        self._status_var.set(msg)

    def _log_msg(self, msg: str, tag: str = ""):
        self._log.config(state="normal")
        self._log.insert("end", msg + "\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")
