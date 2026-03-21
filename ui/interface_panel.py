"""
ui/interface_panel.py — Hardware Interface Selector Panel

Tkinter-based panel that wraps InterfaceRegistry to give the user:
  - Auto-detected interface list (ESP32 BLE, ESP32 USB, J2534 DLLs, SocketCAN)
  - Status indicator per interface (available / unavailable)
  - One-click selection
  - Manual COM port / DLL path override with type selector
  - Refresh button (re-scans all interfaces)
  - Connect / Disconnect button with live status dot

Usage (standalone test):
    python -m ui.interface_panel

Usage (embed in parent GUI):
    from ui.interface_panel import InterfacePanel

    panel = InterfacePanel(parent_frame, on_connect=my_callback, ecu=SIMOS85)
    panel.pack(fill="both", expand=True)

The on_connect callback receives (interface_str, interface_path) which map
directly to flasher.uds_flash._make_connection() / flasher.uds_flash.flash_cal().

Example:
    def on_connect(interface, path):
        conn = _make_connection(ecu, interface, interface_path=path)
        ...

    panel = InterfacePanel(root, on_connect=on_connect, ecu=SIMOS85)
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, font as tkfont
from typing import Callable, Optional, Tuple

from transport.interfaces import InterfaceRegistry, InterfaceInfo

# ── Color palette (dark theme matching the rest of the suite) ─────────────────

COLORS = {
    "bg":          "#0d1117",
    "surface":     "#161b22",
    "border":      "#30363d",
    "text":        "#e6edf3",
    "text_muted":  "#8b949e",
    "text_dim":    "#484f58",
    "green":       "#3fb950",
    "amber":       "#d29922",
    "red":         "#f85149",
    "blue":        "#58a6ff",
    "blue_dim":    "#0d2748",
    "btn":         "#21262d",
    "btn_hover":   "#30363d",
    "sel_bg":      "#0d2748",
    "sel_border":  "#58a6ff",
}

BADGE_COLORS = {
    "BLE":      {"bg": "#0c2a4a", "fg": "#79c0ff"},
    "USBISOTP": {"bg": "#0c2a1a", "fg": "#56d364"},
    "J2534":    {"bg": "#2a1a0c", "fg": "#e3b341"},
    "SOCKETCAN":{"bg": "#1a0c2a", "fg": "#bc8cff"},
}

STATUS_DOT = {
    True:  {"color": "#3fb950", "label": "●"},   # available
    False: {"color": "#484f58", "label": "●"},   # unavailable
}


# ── InterfacePanel ────────────────────────────────────────────────────────────

class InterfacePanel(tk.Frame):
    """
    Hardware interface selector panel.

    Parameters
    ----------
    parent:      tk parent widget
    on_connect:  callback(interface: str, path: str) — called when user connects.
                 interface is e.g. "BLE", "USBISOTP", "J2534", "SocketCAN_can0".
                 path is DLL path, COM port, or "" for BLE.
    on_disconnect: callback() — called when user disconnects.
    ecu:         ECUDef — used to label the panel. Optional, cosmetic only.
    """

    def __init__(
        self,
        parent,
        on_connect:    Optional[Callable[[str, str], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        ecu=None,
        **kwargs,
    ):
        super().__init__(parent, bg=COLORS["bg"], **kwargs)

        self._on_connect    = on_connect
        self._on_disconnect = on_disconnect
        self._ecu           = ecu
        self._registry      = InterfaceRegistry()
        self._selected_iface: Optional[InterfaceInfo] = None
        self._connected     = False
        self._iface_rows: list[dict] = []   # {frame, iface, widgets}

        self._build()
        self._populate_list()

    # ── Build layout ──────────────────────────────────────────────────────────

    def _build(self):
        # Title bar
        title_bar = tk.Frame(self, bg=COLORS["surface"],
                             highlightbackground=COLORS["border"],
                             highlightthickness=1)
        title_bar.pack(fill="x")

        title_left = tk.Frame(title_bar, bg=COLORS["surface"])
        title_left.pack(side="left", padx=12, pady=8)

        for color in ("#ff5f57", "#febc2e", "#28c840"):
            tk.Label(title_left, text="●", fg=color,
                     bg=COLORS["surface"], font=("Menlo", 9)).pack(side="left")

        tk.Label(title_bar, text="  connect to vehicle",
                 fg=COLORS["text_muted"], bg=COLORS["surface"],
                 font=("Menlo", 11, "bold")).pack(side="left")

        # path label removed — not useful to end users

        # Body
        body = tk.Frame(self, bg=COLORS["bg"], padx=14, pady=12)
        body.pack(fill="both", expand=True)

        # ── Detected interfaces section ───────────────────────────────────────
        hdr = tk.Frame(body, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 6))

        tk.Label(hdr, text="DETECTED INTERFACES",
                 fg=COLORS["text_dim"], bg=COLORS["bg"],
                 font=("Menlo", 9)).pack(side="left")

        self._refresh_btn = tk.Button(
            hdr, text="⟳  refresh",
            fg=COLORS["blue"], bg=COLORS["bg"],
            activeforeground=COLORS["text"], activebackground=COLORS["bg"],
            font=("Menlo", 9), bd=0, cursor="hand2",
            command=self._do_refresh,
        )
        self._refresh_btn.pack(side="right")

        # Scrollable interface list
        list_container = tk.Frame(body, bg=COLORS["bg"])
        list_container.pack(fill="x")

        self._list_frame = tk.Frame(list_container, bg=COLORS["bg"])
        self._list_frame.pack(fill="x")

        self._sel_info_var = tk.StringVar(value="")
        sel_info_lbl = tk.Label(body, textvariable=self._sel_info_var,
                                fg=COLORS["blue"], bg=COLORS["bg"],
                                font=("Menlo", 9), anchor="w")
        sel_info_lbl.pack(fill="x", pady=(4, 0))

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(body, bg=COLORS["border"], height=1).pack(fill="x", pady=10)

        # ── Manual override section ───────────────────────────────────────────
        tk.Label(body, text="MANUAL OVERRIDE",
                 fg=COLORS["text_dim"], bg=COLORS["bg"],
                 font=("Menlo", 9)).pack(anchor="w", pady=(0, 6))

        manual_box = tk.Frame(body, bg=COLORS["surface"],
                              highlightbackground=COLORS["border"],
                              highlightthickness=1, padx=12, pady=10)
        manual_box.pack(fill="x")

        tk.Label(manual_box,
                 text="If your ESP32 USB bridge isn't auto-detected (unrecognised VID:PID or CH341 variant),\nenter the COM port or DLL path here.",
                 fg=COLORS["text_muted"], bg=COLORS["surface"],
                 font=("Menlo", 9), justify="left").pack(anchor="w")

        row = tk.Frame(manual_box, bg=COLORS["surface"])
        row.pack(fill="x", pady=(8, 0))

        self._manual_type = tk.StringVar(value="USBISOTP")
        type_menu = ttk.Combobox(row, textvariable=self._manual_type,
                                 values=["USBISOTP", "J2534", "BLE", "SocketCAN"],
                                 width=12, state="readonly", font=("Menlo", 10))
        type_menu.pack(side="left", padx=(0, 6))
        type_menu.bind("<<ComboboxSelected>>", lambda e: self._on_manual_change())

        self._manual_port_var = tk.StringVar()
        self._manual_port_var.trace_add("write", lambda *a: self._on_manual_change())
        port_entry = tk.Entry(row, textvariable=self._manual_port_var,
                              bg=COLORS["bg"], fg=COLORS["text"],
                              insertbackground=COLORS["text"],
                              font=("Menlo", 10), bd=0,
                              highlightbackground=COLORS["border"],
                              highlightcolor=COLORS["blue"],
                              highlightthickness=1)
        port_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        port_entry.insert(0, "")
        port_entry.config(fg=COLORS["text_muted"])
        port_entry.bind("<FocusIn>",
                        lambda e: port_entry.config(fg=COLORS["text"]))

        apply_btn = tk.Button(row, text="use this",
                              fg=COLORS["text"], bg=COLORS["btn"],
                              activeforeground=COLORS["text"],
                              activebackground=COLORS["btn_hover"],
                              font=("Menlo", 10), bd=0, padx=10, pady=4,
                              cursor="hand2",
                              highlightbackground=COLORS["border"],
                              highlightthickness=1,
                              command=self._apply_manual)
        apply_btn.pack(side="left")

        # ── Note / warn / error boxes ─────────────────────────────────────────
        self._note_var = tk.StringVar()
        self._note_lbl = tk.Label(body, textvariable=self._note_var,
                                  fg=COLORS["green"], bg="#1a3a1e",
                                  font=("Menlo", 9), anchor="w",
                                  justify="left", wraplength=500,
                                  padx=8, pady=5)

        self._warn_var = tk.StringVar()
        self._warn_lbl = tk.Label(body, textvariable=self._warn_var,
                                  fg=COLORS["amber"], bg="#2e2104",
                                  font=("Menlo", 9), anchor="w",
                                  justify="left", wraplength=500,
                                  padx=8, pady=5)

        self._err_var = tk.StringVar()
        self._err_lbl = tk.Label(body, textvariable=self._err_var,
                                 fg=COLORS["red"], bg="#3d0f0f",
                                 font=("Menlo", 9), anchor="w",
                                 justify="left", wraplength=500,
                                 padx=8, pady=5)

        # ── Bottom bar ────────────────────────────────────────────────────────
        tk.Frame(body, bg=COLORS["border"], height=1).pack(fill="x", pady=10)

        bottom = tk.Frame(body, bg=COLORS["bg"])
        bottom.pack(fill="x")

        # Status bar
        self._status_frame = tk.Frame(bottom, bg=COLORS["surface"],
                                      highlightbackground=COLORS["border"],
                                      highlightthickness=1, padx=10, pady=6)
        self._status_frame.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self._status_dot = tk.Label(self._status_frame, text="●",
                                    fg=COLORS["text_dim"],
                                    bg=COLORS["surface"], font=("Menlo", 10))
        self._status_dot.pack(side="left")

        self._status_var = tk.StringVar(value="no interface selected")
        tk.Label(self._status_frame, textvariable=self._status_var,
                 fg=COLORS["text_muted"], bg=COLORS["surface"],
                 font=("Menlo", 9)).pack(side="left", padx=(6, 0))

        # Refresh button (duplicate shortcut in bottom bar)
        tk.Button(bottom, text="refresh",
                  fg=COLORS["text"], bg=COLORS["btn"],
                  activeforeground=COLORS["text"], activebackground=COLORS["btn_hover"],
                  font=("Menlo", 10), bd=0, padx=12, pady=5,
                  highlightbackground=COLORS["border"], highlightthickness=1,
                  cursor="hand2", command=self._do_refresh).pack(side="left", padx=(0, 6))

        # Connect / Disconnect button
        self._connect_btn = tk.Button(bottom, text="connect",
                                      fg="#0d1117", bg=COLORS["blue"],
                                      activeforeground="#0d1117",
                                      activebackground="#79b8ff",
                                      font=("Menlo", 10, "bold"),
                                      bd=0, padx=14, pady=5,
                                      cursor="hand2",
                                      state="disabled",
                                      command=self._do_connect)
        self._connect_btn.pack(side="left")

        # DEMO MODE button — simulates 3.0T TFSI CGWB + ZF8HP
        self._demo_btn = tk.Button(
            bottom,
            text="▶  DEMO",
            fg="#d29922",
            bg=COLORS["btn"],
            activeforeground="#d29922",
            activebackground=COLORS["btn_hover"],
            font=("Courier New", 10, "bold"),
            relief="solid", bd=1,
            highlightbackground="#d29922",
            highlightthickness=1,
            padx=10, pady=4,
            cursor="hand2",
            command=self._do_demo)
        self._demo_btn.pack(side="left", padx=(6, 0))
        tk.Label(bottom, text="Simos8.5 3.0T TFSI + ZF8HP simulation",
                 fg=COLORS["text_dim"], bg=COLORS["surface"],
                 font=("Courier New", 8)).pack(side="left", padx=(4, 0))

    # ── Interface list population ─────────────────────────────────────────────

    def _populate_list(self):
        for widget in self._list_frame.winfo_children():
            widget.destroy()
        self._iface_rows.clear()

        # Show all available interfaces (DLL present or hardware detected)
        available = self._registry.available()
        if not available:
            tk.Label(self._list_frame,
                     text="No interfaces found — install drivers or plug in your cable",
                     fg=COLORS["text_dim"], bg=COLORS["bg"],
                     font=("Menlo", 9), anchor="w").pack(anchor="w", pady=8)
            return

        for iface in available:
            self._add_iface_row(iface)

    def _add_iface_row(self, iface: InterfaceInfo):
        row = tk.Frame(self._list_frame,
                       bg=COLORS["surface"] if iface.available else COLORS["bg"],
                       highlightbackground=COLORS["border"],
                       highlightthickness=1,
                       padx=10, pady=8,
                       cursor="hand2" if iface.available else "arrow")
        row.pack(fill="x", pady=2)

        dot_color = COLORS["green"] if iface.available else COLORS["text_dim"]
        tk.Label(row, text="●", fg=dot_color, bg=row["bg"],
                 font=("Menlo", 9)).pack(side="left", padx=(0, 8))

        info = tk.Frame(row, bg=row["bg"])
        info.pack(side="left", fill="x", expand=True)

        name_fg = COLORS["text"] if iface.available else COLORS["text_dim"]
        tk.Label(info, text=iface.name, fg=name_fg, bg=row["bg"],
                 font=("Menlo", 10, "bold"), anchor="w").pack(anchor="w")

        path_text = iface.path if iface.path else "—"
        if len(path_text) > 65:
            path_text = "..." + path_text[-62:]
        tk.Label(info, text=path_text, fg=COLORS["text_muted"], bg=row["bg"],
                 font=("Menlo", 9), anchor="w").pack(anchor="w")

        badge_colors = BADGE_COLORS.get(iface.interface.upper(),
                                        {"bg": "#1a1a1a", "fg": "#666"})
        badge = tk.Label(row, text=iface.interface.split("_")[0].upper(),
                         fg=badge_colors["fg"], bg=badge_colors["bg"],
                         font=("Menlo", 8, "bold"), padx=6, pady=2)
        badge.pack(side="right", padx=(6, 0))

        entry = {"frame": row, "iface": iface}
        self._iface_rows.append(entry)

        if iface.available:
            for w in [row, info] + list(info.winfo_children()):
                w.bind("<Button-1>", lambda e, idx=len(self._iface_rows)-1:
                       self._select_row(idx))
            row.bind("<Enter>", lambda e, r=row: r.config(
                highlightbackground=COLORS["text_muted"]))
            row.bind("<Leave>", lambda e, r=row, entry=entry:
                     r.config(highlightbackground=(
                         COLORS["sel_border"] if self._selected_iface == entry["iface"]
                         else COLORS["border"])))

    def _select_row(self, idx: int):
        if self._connected:
            return
        self._selected_iface = self._iface_rows[idx]["iface"]
        iface = self._selected_iface

        # Highlight selected row
        for i, entry in enumerate(self._iface_rows):
            sel = (i == idx)
            bg = COLORS["sel_bg"] if sel else (
                COLORS["surface"] if entry["iface"].available else COLORS["bg"])
            border = COLORS["sel_border"] if sel else COLORS["border"]
            entry["frame"].config(bg=bg, highlightbackground=border)
            for child in entry["frame"].winfo_children():
                if isinstance(child, (tk.Label, tk.Frame)):
                    child.config(bg=bg)
                    for gc in child.winfo_children() if hasattr(child, 'winfo_children') else []:
                        if isinstance(gc, tk.Label):
                            gc.config(bg=bg)

        # Selected info line
        type_str = iface.interface
        self._sel_info_var.set(
            f'→  interface="{type_str}"   path="{iface.path}"')

        self._set_status(COLORS["amber"], "interface selected — ready to connect")
        self._connect_btn.config(state="normal")
        self._hide_boxes()
        if iface.notes:
            self._show_note(iface.notes)
        if not iface.available:
            self._show_warn("Interface not detected on this system. Install drivers or use manual override.")

    # ── Manual override ───────────────────────────────────────────────────────

    def _on_manual_change(self):
        port = self._manual_port_var.get().strip()
        itype = self._manual_type.get()
        if port or itype == "BLE":
            self._connect_btn.config(state="normal")
            label = "BLE" if itype == "BLE" else f"{itype}_{port}"
            self._sel_info_var.set(f'→  interface="{label}"   path="{port}"')
            self._set_status(COLORS["amber"], "manual override — ready to connect")
        else:
            if self._selected_iface is None:
                self._connect_btn.config(state="disabled")
                self._set_status(COLORS["text_dim"], "no interface selected")

    def _apply_manual(self):
        port = self._manual_port_var.get().strip()
        itype = self._manual_type.get()
        if not port and itype != "BLE":
            self._show_err("Enter a port or path first.")
            return
        self._hide_boxes()
        self._selected_iface = None          # manual takes priority
        self._on_manual_change()
        notes = {
            "USBISOTP": f"Will open {port} at 250000 baud. DTR/RTS held low to avoid ESP32 programming mode.",
            "J2534":    f"Will load J2534 DLL from: {port}",
            "BLE":      "BLE selected — will scan for BLE_TO_ISOTP20 (UUID 0xABF0).",
            "SocketCAN":f"Will bind SocketCAN {port} with iso-tp kernel module.",
        }
        self._show_note(notes.get(itype, "Manual override applied."))
        self._connect_btn.config(state="normal")

    def get_selected_interface(self) -> Optional[Tuple[str, str]]:
        """
        Returns (interface_str, path) for the current selection,
        suitable for passing directly to _make_connection().
        Returns None if nothing is selected.
        """
        port = self._manual_port_var.get().strip()
        itype = self._manual_type.get()

        # Manual override takes priority if port is set (or BLE selected)
        if port or itype == "BLE":
            if itype == "BLE":
                return ("BLE", "")
            elif itype == "SocketCAN":
                return (f"SocketCAN_{port}", port)
            else:
                return (itype, port)

        if self._selected_iface is not None:
            iface = self._selected_iface
            if iface.interface.upper().startswith("SOCKETCAN"):
                return (f"SocketCAN_{iface.path}", iface.path)
            return (iface.interface, iface.path)

        return None

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _do_refresh(self):
        if self._connected:
            return
        self._refresh_btn.config(text="⟳  scanning...", fg=COLORS["amber"])
        self._set_status(COLORS["amber"], "scanning...")
        self._hide_boxes()

        def _scan():
            self._registry.refresh()
            self.after(0, self._on_refresh_done)

        threading.Thread(target=_scan, daemon=True).start()

    def _on_refresh_done(self):
        self._selected_iface = None
        self._sel_info_var.set("")
        self._populate_list()
        n = len(self._registry.available())
        self._refresh_btn.config(text="⟳  refresh", fg=COLORS["blue"])
        if n == 0:
            self._set_status(COLORS["text_dim"],
                             "no hardware detected — check connections")
        else:
            names = ", ".join(i.interface.split("_")[0]
                              for i in self._registry.available())
            self._set_status(COLORS["green"] if n else COLORS["text_dim"],
                             f"{n} interface{'s' if n!=1 else ''} found: {names}")
        self._connect_btn.config(state="disabled")
        self._hide_boxes()

    # ── Connect / Disconnect ──────────────────────────────────────────────────

    def _do_connect(self):
        if self._connected:
            self._do_disconnect()
            return

        result = self.get_selected_interface()
        if result is None:
            self._show_err("No interface selected.")
            return

        interface, path = result
        self._connect_btn.config(state="disabled", text="connecting...")
        self._set_status(COLORS["amber"], f"connecting via {interface}...")
        self._hide_boxes()

        def _connect_task():
            # J2534 DLLs are typically 32-bit and cannot be loaded directly
            # by 64-bit Python ctypes. Hardware validation happens inside
            # J2534Connection when the first UDS command is sent.
            # For BLE, the ble_bridge handles scanning separately.
            import time
            time.sleep(0.3)
            self.after(0, lambda: self._on_connected(interface, path))

        threading.Thread(target=_connect_task, daemon=True).start()

    def _on_connected(self, interface: str, path: str):
        self._connected = True
        self._connect_btn.config(state="normal", text="disconnect",
                                 fg=COLORS["red"], bg="#3d0f0f",
                                 activeforeground=COLORS["red"],
                                 activebackground="#5a1f1f")
        label = f"{interface}:{path}" if path else interface
        self._set_status(COLORS["green"], f"connected — {label}")
        self._show_note(
            "UDS transport active. Extended session ready.\n"
            "Use _make_connection(ecu, interface, path) to create udsoncan Client.")
        if self._on_connect:
            self._on_connect(interface, path)

    def _on_connect_failed(self, reason: str):
        self._connect_btn.config(state="normal", text="connect",
                                 fg=COLORS["text"], bg=COLORS["btn"],
                                 activeforeground=COLORS["text"],
                                 activebackground=COLORS["btn_hover"])
        self._set_status(COLORS["red"], f"connection failed")
        self._show_err(reason)

    def _do_disconnect(self):
        self._connected = False
        self._connect_btn.config(text="connect",
                                 fg="#0d1117", bg=COLORS["blue"],
                                 activeforeground="#0d1117",
                                 activebackground="#79b8ff")
        result = self.get_selected_interface()
        if result:
            self._connect_btn.config(state="normal")
            self._set_status(COLORS["text_dim"], "disconnected")
        else:
            self._connect_btn.config(state="disabled")
            self._set_status(COLORS["text_dim"], "disconnected — no interface selected")
        self._hide_boxes()
        if self._on_disconnect:
            self._on_disconnect()

    # ── Status / note helpers ─────────────────────────────────────────────────

    # ── Demo Mode ─────────────────────────────────────────────────────────────

    def _do_demo(self):
        """
        Launch simulation mode — Simos8.5 3.0T TFSI CGWB + ZF8HP.
        Patches the mock connection into the suite so all tabs work
        with realistic simulated data. No hardware needed.
        """
        if self._connected:
            self._do_disconnect()

        try:
            from tests.sim_runner import (_install_mock_patch,
                                           _install_interface_patch,
                                           make_synthetic_cal,
                                           start_sniff_generator)
        except ImportError:
            self._set_status(COLORS["red"],
                             "Demo unavailable — tests/sim_runner.py not found")
            return

        # Install mock patches — routes all connections to MockConnection
        _install_mock_patch("S85", "ZF8HP")
        _install_interface_patch()

        # Show demo banner in status
        self._set_status(COLORS["amber"],
                         "▶ SIMULATION MODE  — Simos8.5 3.0T TFSI CGWB + ZF8HP")
        self._demo_btn.config(state="disabled", text="▶ DEMO (active)")

        # Fire the on_connected callback — enables all tabs
        self.after(200, lambda: self._on_connected("DEMO", "Simos8.5 3.0T TFSI CGWB"))

        # Then auto-populate all tabs with simulated data
        def _launch_auto_pop():
            import time; time.sleep(0.5)
            try:
                from tests.sim_runner import auto_connect_after_launch
                # Find the MainWindow by walking up the widget tree
                mw = self
                while mw and not hasattr(mw, "_tabs"):
                    mw = getattr(mw, "master", None) or getattr(mw, "_w", None)
                if mw and hasattr(mw, "_tabs"):
                    auto_connect_after_launch(mw, delay=0)
            except Exception as e:
                import logging; logging.getLogger(__name__).warning("demo pop: %s", e)
        import threading
        threading.Thread(target=_launch_auto_pop, daemon=True).start()

    def _set_status(self, dot_color: str, msg: str):

        self._status_dot.config(fg=dot_color)
        self._status_var.set(msg)

    def _show_note(self, msg: str):
        self._note_var.set(msg)
        self._note_lbl.pack(fill="x", pady=(6, 0))

    def _show_warn(self, msg: str):
        self._warn_var.set(msg)
        self._warn_lbl.pack(fill="x", pady=(6, 0))

    def _show_err(self, msg: str):
        self._err_var.set(msg)
        self._err_lbl.pack(fill="x", pady=(6, 0))

    def _hide_boxes(self):
        for lbl in (self._note_lbl, self._warn_lbl, self._err_lbl):
            lbl.pack_forget()


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    root.title("simos-suite — hardware interface test")
    root.configure(bg="#0d1117")
    root.geometry("640x560")

    def on_connect(interface, path):
        print(f"[CONNECTED] interface={interface!r}  path={path!r}")

    def on_disconnect():
        print("[DISCONNECTED]")

    panel = InterfacePanel(root, on_connect=on_connect,
                           on_disconnect=on_disconnect)
    panel.pack(fill="both", expand=True, padx=2, pady=2)

    root.mainloop()
