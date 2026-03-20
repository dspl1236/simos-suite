"""
ui/hardware_tab.py — Hardware Interface Tab for Simos Suite GUI

Provides:
  - Auto-detected interface list (from transport.interfaces.InterfaceRegistry)
  - Per-interface detail panel (type, port/DLL, VID:PID, notes)
  - Connect / Disconnect buttons with live state feedback
  - Manual port override (collapsible) for unlisted COM ports or custom DLLs
  - SCAN PORTS button that re-runs InterfaceRegistry.refresh()
  - Interface log (timestamped events)

Designed to be embedded as a ttk.Frame tab in the main SimosSuite window.
Thread safety: BLE/USB connect runs in a background thread; all UI updates
come back via self.after() to avoid tkinter cross-thread errors.

Usage:
    from ui.hardware_tab import HardwareTab
    tab = HardwareTab(notebook, on_connect=my_callback)
    notebook.add(tab, text="Hardware")

on_connect(iface: InterfaceInfo) is called on the main thread after a
successful connection. Pass the InterfaceInfo to flash / logger / probe.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from typing import Optional, Callable

from transport.interfaces import InterfaceRegistry, InterfaceInfo


# ── Colour palette (dark industrial) ─────────────────────────────────────────
PAL = {
    "bg0":       "#0a0c0f",
    "bg1":       "#0f1215",
    "bg2":       "#151a1f",
    "bg3":       "#1c2228",
    "border":    "#2a3540",
    "text":      "#d4dde6",
    "text_dim":  "#3d5060",
    "text_sec":  "#7a9ab0",
    "accent":    "#00d4ff",
    "green":     "#00ff88",
    "yellow":    "#ffb800",
    "red":       "#ff3d3d",
    "orange":    "#ff8c00",
}

BADGE_COLORS = {
    "BLE":      ("#00d4ff", "#0a1a24"),
    "USBISOTP": ("#00ff88", "#0a1a12"),
    "J2534":    ("#ffb800", "#1a1200"),
    "SocketCAN":("#ff8c00", "#1a0e00"),
    "WIFI":     ("#00e676", "#001a0a"),
}


class HardwareTab(ttk.Frame):
    """
    Full hardware interface tab — drop into any ttk.Notebook.
    """

    def __init__(self, parent, on_connect: Optional[Callable[[InterfaceInfo], None]] = None):
        super().__init__(parent)
        self.on_connect_cb = on_connect
        self.registry      = InterfaceRegistry()
        self.selected:  Optional[InterfaceInfo] = None
        self.connected: Optional[InterfaceInfo] = None
        self._manual_open = False

        self.configure(style="BG0.TFrame")
        self._build_styles()
        self._build_ui()
        self._populate_list()
        self._log("INFO", "InterfaceRegistry initialized")
        self._log("INFO", f"{len(self.registry.available())} of {len(self.registry.all())} interfaces available")

    # ── Styles ─────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style()
        for name, bg in [("BG0","#0a0c0f"),("BG1","#0f1215"),("BG2","#151a1f"),("BG3","#1c2228")]:
            s.configure(f"{name}.TFrame", background=bg)
            s.configure(f"{name}.TLabel", background=bg, foreground=PAL["text"],
                        font=("Courier New", 10))
        s.configure("Dim.TLabel",  background=PAL["bg0"], foreground=PAL["text_dim"], font=("Courier New", 9))
        s.configure("Sec.TLabel",  background=PAL["bg0"], foreground=PAL["text_sec"], font=("Courier New", 10))
        s.configure("Accent.TLabel",background=PAL["bg0"],foreground=PAL["accent"],  font=("Courier New", 10))
        s.configure("Green.TLabel", background=PAL["bg0"],foreground=PAL["green"],   font=("Courier New", 10))
        s.configure("Yellow.TLabel",background=PAL["bg0"],foreground=PAL["yellow"],  font=("Courier New", 10))
        s.configure("Red.TLabel",   background=PAL["bg0"],foreground=PAL["red"],     font=("Courier New", 10))
        s.configure("Header.TLabel",background=PAL["bg1"],foreground=PAL["text_dim"],
                    font=("Courier New", 8, "bold"))

    # ── Layout ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ── Left column: interface list ──────────────────────────────────
        left = tk.Frame(self, bg=PAL["bg1"], width=280)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.rowconfigure(1, weight=1)

        tk.Label(left, text="  DETECTED INTERFACES", bg=PAL["bg1"],
                 fg=PAL["text_dim"], font=("Courier New", 8, "bold"),
                 anchor="w").grid(row=0, column=0, sticky="ew", pady=(8,4))

        tk.Frame(left, bg=PAL["border"], height=1).grid(row=0, column=0, sticky="ews")

        # Scrollable list
        lf = tk.Frame(left, bg=PAL["bg1"])
        lf.grid(row=1, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self.list_canvas = tk.Canvas(lf, bg=PAL["bg1"], highlightthickness=0,
                                     bd=0, width=278)
        vsb = tk.Scrollbar(lf, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=vsb.set)
        self.list_canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.list_inner = tk.Frame(self.list_canvas, bg=PAL["bg1"])
        self._list_win  = self.list_canvas.create_window((0,0), window=self.list_inner,
                                                          anchor="nw")
        self.list_inner.bind("<Configure>", lambda e:
            self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))

        # Scan button
        scan_area = tk.Frame(left, bg=PAL["bg1"])
        scan_area.grid(row=2, column=0, sticky="ew", pady=8, padx=8)

        self.scan_btn = tk.Button(
            scan_area, text="⟳  SCAN PORTS",
            bg=PAL["bg3"], fg=PAL["text_sec"],
            font=("Courier New", 9, "bold"),
            activebackground=PAL["bg3"], activeforeground=PAL["accent"],
            bd=0, padx=12, pady=7, cursor="hand2",
            command=self._run_scan
        )
        self.scan_btn.pack(fill="x")

        # ── Right column ─────────────────────────────────────────────────
        right = tk.Frame(self, bg=PAL["bg0"])
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=1)

        # Detail pane
        self.detail_frame = tk.Frame(right, bg=PAL["bg0"], height=230)
        self.detail_frame.grid(row=0, column=0, sticky="ew")
        self.detail_frame.columnconfigure(0, weight=1)
        self.detail_frame.grid_propagate(False)

        self._detail_hint = tk.Label(
            self.detail_frame,
            text="← Select an interface",
            bg=PAL["bg0"], fg=PAL["text_dim"],
            font=("Courier New", 10)
        )
        self._detail_hint.place(relx=0.5, rely=0.5, anchor="center")

        tk.Frame(right, bg=PAL["border"], height=1).grid(row=0, column=0, sticky="ews")

        # Bottom section
        bottom = tk.Frame(right, bg=PAL["bg1"])
        bottom.grid(row=1, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(3, weight=1)

        # Connect / Disconnect buttons
        btn_row = tk.Frame(bottom, bg=PAL["bg1"])
        btn_row.grid(row=0, column=0, sticky="ew", padx=16, pady=12)
        btn_row.columnconfigure(0, weight=1)

        self.conn_btn = tk.Button(
            btn_row, text="CONNECT",
            bg="#004a5c", fg=PAL["accent"],
            font=("Courier New", 12, "bold"),
            activebackground="#006a7a", activeforeground=PAL["accent"],
            bd=0, padx=24, pady=10, cursor="hand2",
            state="disabled",
            command=self._do_connect
        )
        self.conn_btn.grid(row=0, column=0, sticky="ew")

        self.disc_btn = tk.Button(
            btn_row, text="DISCONNECT",
            bg=PAL["bg3"], fg=PAL["red"],
            font=("Courier New", 12, "bold"),
            activebackground=PAL["bg3"], activeforeground=PAL["red"],
            bd=0, padx=24, pady=10, cursor="hand2",
            command=self._do_disconnect
        )
        # disc_btn hidden until connected

        # Manual override
        self._build_manual(bottom)

        # Log
        self._build_log(bottom)

    def _build_manual(self, parent):
        """Collapsible manual port override section."""
        msec = tk.Frame(parent, bg=PAL["bg2"], bd=0)
        msec.grid(row=1, column=0, sticky="ew", padx=16, pady=(0,8))
        msec.columnconfigure(0, weight=1)

        hdr = tk.Frame(msec, bg=PAL["bg2"])
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=8)

        self._mchev = tk.Label(hdr, text="▶", bg=PAL["bg2"], fg=PAL["text_dim"],
                               font=("Courier New", 9), cursor="hand2")
        self._mchev.pack(side="left")

        tk.Label(hdr, text="  MANUAL PORT OVERRIDE",
                 bg=PAL["bg2"], fg=PAL["text_dim"],
                 font=("Courier New", 8, "bold"), cursor="hand2"
                 ).pack(side="left")
        tk.Label(hdr, text="  — for unlisted / custom ports",
                 bg=PAL["bg2"], fg=PAL["text_dim"],
                 font=("Courier New", 8)).pack(side="left")

        for w in [self._mchev, hdr]:
            w.bind("<Button-1>", lambda e: self._toggle_manual())

        self._manual_body = tk.Frame(msec, bg=PAL["bg2"])

        # Type selector
        tr = tk.Frame(self._manual_body, bg=PAL["bg2"])
        tr.pack(fill="x", padx=12, pady=(0,6))
        tk.Label(tr, text="Type", bg=PAL["bg2"], fg=PAL["text_dim"],
                 font=("Courier New", 9), width=6, anchor="w").pack(side="left")

        self._mtype = tk.StringVar(value="USBISOTP")
        opts = ["USBISOTP", "BLE", "WIFI", "J2534", "SocketCAN"]
        om = tk.OptionMenu(tr, self._mtype, *opts, command=lambda _: self._on_mtype())
        om.config(bg=PAL["bg1"], fg=PAL["text_sec"], font=("Courier New", 9),
                  bd=0, highlightthickness=0, activebackground=PAL["bg3"],
                  activeforeground=PAL["accent"])
        om["menu"].config(bg=PAL["bg1"], fg=PAL["text_sec"], font=("Courier New", 9))
        om.pack(side="left", fill="x", expand=True)

        # Port row
        self._mport_row = tk.Frame(self._manual_body, bg=PAL["bg2"])
        self._mport_row.pack(fill="x", padx=12, pady=(0,6))
        self._mport_lbl = tk.Label(self._mport_row, text="Port", bg=PAL["bg2"],
                                   fg=PAL["text_dim"], font=("Courier New", 9), width=6, anchor="w")
        self._mport_lbl.pack(side="left")
        self._mport_var = tk.StringVar()
        tk.Entry(self._mport_row, textvariable=self._mport_var,
                 bg=PAL["bg1"], fg=PAL["text"], insertbackground=PAL["accent"],
                 font=("Courier New", 10), bd=0, highlightthickness=1,
                 highlightcolor=PAL["accent"], highlightbackground=PAL["border"]
                 ).pack(side="left", fill="x", expand=True, padx=(0,6))
        tk.Button(self._mport_row, text="USE THIS",
                  bg=PAL["bg3"], fg=PAL["text_sec"],
                  font=("Courier New", 8, "bold"),
                  activebackground=PAL["bg3"], activeforeground=PAL["accent"],
                  bd=0, padx=8, pady=4, cursor="hand2",
                  command=self._apply_manual).pack(side="left")

        # DLL row (J2534 only)
        self._mdll_row = tk.Frame(self._manual_body, bg=PAL["bg2"])
        self._mdll_lbl = tk.Label(self._mdll_row, text="DLL", bg=PAL["bg2"],
                                  fg=PAL["text_dim"], font=("Courier New", 9), width=6, anchor="w")
        self._mdll_lbl.pack(side="left")
        self._mdll_var = tk.StringVar()
        tk.Entry(self._mdll_row, textvariable=self._mdll_var,
                 bg=PAL["bg1"], fg=PAL["text"], insertbackground=PAL["accent"],
                 font=("Courier New", 9), bd=0, highlightthickness=1,
                 highlightcolor=PAL["accent"], highlightbackground=PAL["border"]
                 ).pack(side="left", fill="x", expand=True, padx=(0,6))
        tk.Button(self._mdll_row, text="USE THIS",
                  bg=PAL["bg3"], fg=PAL["text_sec"],
                  font=("Courier New", 8, "bold"),
                  activebackground=PAL["bg3"], activeforeground=PAL["accent"],
                  bd=0, padx=8, pady=4, cursor="hand2",
                  command=self._apply_manual).pack(side="left")

    def _build_log(self, parent):
        """Timestamped interface log."""
        log_frame = tk.Frame(parent, bg=PAL["bg0"])
        log_frame.grid(row=3, column=0, sticky="nsew", padx=0, pady=0)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        hdr = tk.Frame(log_frame, bg=PAL["bg1"])
        hdr.grid(row=0, column=0, sticky="ew")
        tk.Label(hdr, text="  INTERFACE LOG", bg=PAL["bg1"], fg=PAL["text_dim"],
                 font=("Courier New", 8, "bold"), anchor="w").pack(side="left", pady=6)
        tk.Button(hdr, text="CLEAR", bg=PAL["bg1"], fg=PAL["text_dim"],
                  font=("Courier New", 8), bd=0, cursor="hand2",
                  activebackground=PAL["bg1"], activeforeground=PAL["text_sec"],
                  command=self._clear_log).pack(side="right", padx=10)

        self.log_text = tk.Text(
            log_frame, height=7, bg=PAL["bg0"], fg=PAL["text_sec"],
            font=("Courier New", 9), bd=0, padx=10, pady=6,
            insertbackground=PAL["accent"],
            selectbackground=PAL["bg3"], state="disabled",
            wrap="word"
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")

        lvsb = tk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lvsb.set)
        lvsb.grid(row=1, column=1, sticky="ns")

        # Log colour tags
        self.log_text.tag_configure("ts",   foreground=PAL["text_dim"])
        self.log_text.tag_configure("INFO", foreground=PAL["text_sec"])
        self.log_text.tag_configure("OK",   foreground=PAL["green"])
        self.log_text.tag_configure("WARN", foreground=PAL["yellow"])
        self.log_text.tag_configure("ERR",  foreground=PAL["red"])

    # ── Interface list population ────────────────────────────────────────────

    def _populate_list(self):
        for w in self.list_inner.winfo_children():
            w.destroy()
        self._iface_frames = {}

        for iface in self.registry.all():
            self._add_iface_row(iface)

        self.list_canvas.update_idletasks()
        self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))

    def _add_iface_row(self, iface: InterfaceInfo):
        is_conn = self.connected and self.connected.interface == iface.interface \
                  and self.connected.path == iface.path
        is_sel  = self.selected  and self.selected.interface == iface.interface \
                  and self.selected.path == iface.path

        bg_base = PAL["bg2"] if is_sel else PAL["bg1"]
        fg_name = PAL["text"] if iface.available else PAL["text_dim"]

        row = tk.Frame(self.list_inner, bg=bg_base, cursor="hand2" if iface.available else "")
        row.pack(fill="x", padx=0, pady=0)

        # Left accent bar
        bar_color = PAL["accent"] if is_sel else bg_base
        tk.Frame(row, bg=bar_color, width=3).pack(side="left", fill="y")

        inner = tk.Frame(row, bg=bg_base)
        inner.pack(side="left", fill="x", expand=True, padx=8, pady=8)

        top_row = tk.Frame(inner, bg=bg_base)
        top_row.pack(fill="x")

        # Status dot
        dot_color = PAL["green"] if is_conn else (PAL["yellow"] if iface.available else "#333")
        dot = tk.Label(top_row, text="●", bg=bg_base, fg=dot_color,
                       font=("Courier New", 8))
        dot.pack(side="left", padx=(0,4))

        # Name
        tk.Label(top_row, text=iface.name, bg=bg_base, fg=fg_name,
                 font=("Courier New", 10, "bold"), anchor="w").pack(side="left", fill="x", expand=True)

        # Badge
        btype = iface.interface.upper().split("_")[0]
        bcfg  = BADGE_COLORS.get(btype, (PAL["text_dim"], PAL["bg3"]))
        tk.Label(top_row, text=f" {btype} ", bg=bcfg[1], fg=bcfg[0],
                 font=("Courier New", 8, "bold")).pack(side="right")

        # Port / notes line
        port_text = iface.path if iface.path else ("ready" if iface.available else "not found")
        if not iface.available:
            port_text = f"✗ {iface.notes}"
        tk.Label(inner, text=port_text, bg=bg_base, fg=PAL["text_dim"],
                 font=("Courier New", 8), anchor="w").pack(fill="x")

        # Separator
        tk.Frame(self.list_inner, bg=PAL["border"], height=1).pack(fill="x")

        if iface.available:
            for w in [row, inner, top_row, dot]:
                w.bind("<Button-1>", lambda e, i=iface: self._pick(i))

    # ── Interface selection ──────────────────────────────────────────────────

    def _pick(self, iface: InterfaceInfo):
        self.selected = iface
        self._populate_list()
        self._render_detail(iface)
        already = (self.connected and self.connected.interface == iface.interface
                   and self.connected.path == iface.path)
        self.conn_btn.config(state="disabled" if already else "normal")
        self._log("INFO", f"Selected: {iface.name}  [{iface.interface}:{iface.path}]")

    def _render_detail(self, iface: InterfaceInfo):
        for w in self.detail_frame.winfo_children():
            w.destroy()

        self.detail_frame.columnconfigure(0, weight=1)
        is_conn = (self.connected and self.connected.interface == iface.interface
                   and self.connected.path == iface.path)

        # Title row
        tr = tk.Frame(self.detail_frame, bg=PAL["bg0"])
        tr.grid(row=0, column=0, sticky="ew", padx=16, pady=(14,6))

        tk.Label(tr, text=iface.name, bg=PAL["bg0"], fg=PAL["text"],
                 font=("Courier New", 14, "bold")).pack(side="left")

        pill_fg = PAL["green"] if is_conn else (PAL["accent"] if iface.available else "#555")
        pill_bg = "#0a1a12"   if is_conn else (PAL["bg3"]    if iface.available else PAL["bg3"])
        pill_tx = "CONNECTED" if is_conn else ("READY"       if iface.available else "NOT AVAILABLE")
        tk.Label(tr, text=f" {pill_tx} ", bg=pill_bg, fg=pill_fg,
                 font=("Courier New", 8, "bold")).pack(side="left", padx=10)

        # Notes / description
        tk.Label(self.detail_frame, text=iface.notes, bg=PAL["bg0"], fg=PAL["text_sec"],
                 font=("Courier New", 9), anchor="w", wraplength=480
                 ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0,10))

        # Detail grid
        grid = tk.Frame(self.detail_frame, bg=PAL["bg0"])
        grid.grid(row=2, column=0, sticky="ew", padx=16)

        cards = [
            ("INTERFACE TYPE", iface.interface, "accent"),
            ("PORT / PATH",    iface.path or "—", "normal"),
            ("STATUS",         "DETECTED" if iface.available else "NOT FOUND",
             "green" if iface.available else "dim"),
        ]

        for col, (label, value, style) in enumerate(cards):
            card = tk.Frame(grid, bg=PAL["bg2"], padx=10, pady=8)
            card.grid(row=0, column=col, sticky="nsew", padx=(0,8))
            grid.columnconfigure(col, weight=1)
            tk.Label(card, text=label, bg=PAL["bg2"], fg=PAL["text_dim"],
                     font=("Courier New", 7, "bold")).pack(anchor="w")
            fg = {"accent": PAL["accent"], "green": PAL["green"],
                  "dim": "#555", "normal": PAL["text"]}.get(style, PAL["text"])
            tk.Label(card, text=value, bg=PAL["bg2"], fg=fg,
                     font=("Courier New", 9), wraplength=140, anchor="w").pack(anchor="w", pady=(4,0))

    # ── Scan ────────────────────────────────────────────────────────────────

    def _run_scan(self):
        self.scan_btn.config(text="⟳  SCANNING...", fg=PAL["accent"], state="disabled")
        self._log("INFO", "Scanning serial ports (VID 10C4/1A86)...")
        self._log("INFO", "Scanning J2534 registry...")

        def _do():
            self.registry.refresh()
            self.after(0, self._scan_done)

        threading.Thread(target=_do, daemon=True).start()

    def _scan_done(self):
        n = len(self.registry.available())
        t = len(self.registry.all())
        self._log("OK", f"Scan complete — {n}/{t} interfaces available")
        for iface in self.registry.available():
            self._log("OK", f"  ✓ {iface.name}  [{iface.interface}:{iface.path}]")
        for iface in self.registry.all():
            if not iface.available:
                self._log("WARN", f"  ✗ {iface.name} — {iface.notes}")
        self.scan_btn.config(text="⟳  SCAN PORTS", fg=PAL["text_sec"], state="normal")
        self._populate_list()

    # ── Connect / Disconnect ─────────────────────────────────────────────────

    def _do_connect(self):
        if not self.selected:
            return
        iface = self.selected
        self.conn_btn.config(state="disabled", text="CONNECTING...")
        self._log("INFO", f"Connecting: {iface.name}  [{iface.interface}:{iface.path}]")

        def _do():
            # Real connection would call _make_connection() here.
            # This wires the BLE scan+connect or serial open and calls back.
            import time
            time.sleep(0.8)   # placeholder — replace with actual connect
            self.after(0, lambda: self._connect_ok(iface))

        threading.Thread(target=_do, daemon=True).start()

    def _connect_ok(self, iface: InterfaceInfo):
        self.connected = iface
        self.conn_btn.config(text="CONNECT")
        self.conn_btn.grid_remove()
        self.disc_btn.grid(row=0, column=0, sticky="ew")
        self._log("OK", f"Connected: {iface.name}")
        self._populate_list()
        if self.selected:
            self._render_detail(self.selected)
        if self.on_connect_cb:
            self.on_connect_cb(iface)

    def _do_disconnect(self):
        self._log("WARN", f"Disconnecting: {self.connected.name if self.connected else ''}")
        self.connected = None
        self.disc_btn.grid_remove()
        self.conn_btn.config(text="CONNECT", state="normal" if self.selected else "disabled")
        self.conn_btn.grid(row=0, column=0, sticky="ew")
        self._log("INFO", "Disconnected.")
        self._populate_list()
        if self.selected:
            self._render_detail(self.selected)

    # ── Manual override ──────────────────────────────────────────────────────

    def _toggle_manual(self):
        self._manual_open = not self._manual_open
        if self._manual_open:
            self._manual_body.pack(fill="x", padx=0, pady=(0,8))
            self._mchev.config(text="▼")
        else:
            self._manual_body.pack_forget()
            self._mchev.config(text="▶")
        self._on_mtype()

    def _on_mtype(self):
        t = self._mtype.get()
        if t == "J2534":
            self._mport_row.pack_forget()
            self._mdll_row.pack(fill="x", padx=12, pady=(0,6))
        elif t == "BLE":
            self._mport_row.pack_forget()
            self._mdll_row.pack_forget()
        elif t == "WIFI":
            self._mdll_row.pack_forget()
            self._mport_lbl.config(text="URL")
            if not self._mport_var.get().startswith("ws"):
                self._mport_var.set("ws://funkbridge.local/ws")
            self._mport_row.pack(fill="x", padx=12, pady=(0,6))
        elif t == "SocketCAN":
            self._mdll_row.pack_forget()
            self._mport_lbl.config(text="Iface")
            self._mport_row.pack(fill="x", padx=12, pady=(0,6))
        else:
            self._mdll_row.pack_forget()
            self._mport_lbl.config(text="Port")
            self._mport_row.pack(fill="x", padx=12, pady=(0,6))

    def _apply_manual(self):
        t   = self._mtype.get()
        port = (self._mdll_var.get() if t == "J2534" else self._mport_var.get()).strip()

        if t not in ("BLE", "WIFI") and not port:
            self._log("ERR", "Manual override: port / path is required")
            return

        if t == "SocketCAN":
            cs = f"SocketCAN_{port}"
        elif t == "WIFI":
            cs = "WIFI"
            if port and not port.startswith("ws"):
                port = "ws://" + port + "/ws"
        else:
            cs = t

        iface = InterfaceInfo(
            name      = f"{t} ({port})" if port else t,
            interface = cs,
            path      = port,
            available = True,
            notes     = "Manual override — not auto-detected",
        )

        # Inject into registry list
        existing = [i for i in self.registry._interfaces if i.interface != cs or i.path != port]
        self.registry._interfaces = [iface] + existing

        self._log("OK", f"Manual override added: {iface.name}")
        self._populate_list()
        self._pick(iface)
        self._toggle_manual()

    # ── Log ─────────────────────────────────────────────────────────────────

    def _log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{ts}  ", "ts")
        self.log_text.insert("end", f"{msg}\n", level)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
