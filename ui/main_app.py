"""
ui/main_app.py — Simos Suite main window

Tabbed desktop GUI (tkinter + ttk). Boots the Hardware tab now;
Flash / Tune / Logger / CP Tools tabs load as Phase 3 progresses.

Run:
    python -m ui.main_app
or:
    python ui/main_app.py
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from transport.interfaces import InterfaceInfo


def main():
    root = tk.Tk()
    root.title("Simos Tuning Suite")
    root.geometry("1040x720")
    root.configure(bg="#0a0c0f")
    root.minsize(860, 580)

    # ── Style ────────────────────────────────────────────────────────────────
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TNotebook",       background="#0f1215", borderwidth=0)
    style.configure("TNotebook.Tab",
                    background="#0f1215", foreground="#3d5060",
                    font=("Courier New", 9, "bold"),
                    padding=[14, 6])
    style.map("TNotebook.Tab",
              background=[("selected","#0a0c0f")],
              foreground=[("selected","#00d4ff")])
    style.configure("TFrame", background="#0a0c0f")

    # ── Title bar area ───────────────────────────────────────────────────────
    topbar = tk.Frame(root, bg="#0f1215", height=42)
    topbar.pack(fill="x", side="top")
    topbar.pack_propagate(False)

    tk.Label(topbar, text="SIMOS", bg="#0f1215", fg="#d4dde6",
             font=("Courier New", 16, "bold")).pack(side="left", padx=(16,0), pady=8)
    tk.Label(topbar, text="SUITE", bg="#0f1215", fg="#00d4ff",
             font=("Courier New", 16, "bold")).pack(side="left", pady=8)
    tk.Label(topbar, text="  ·  3.0T TFSI / Simos8.5  ·  EA888", bg="#0f1215",
             fg="#3d5060", font=("Courier New", 9)).pack(side="left", pady=8)

    # Global connection status (updated by hardware tab)
    _conn_dot = tk.Label(topbar, text="●", bg="#0f1215", fg="#333",
                         font=("Courier New", 10))
    _conn_dot.pack(side="right", padx=(0,6))
    _conn_label = tk.Label(topbar, text="NOT CONNECTED", bg="#0f1215",
                           fg="#3d5060", font=("Courier New", 8, "bold"))
    _conn_label.pack(side="right", padx=(0,4))

    tk.Frame(root, bg="#2a3540", height=1).pack(fill="x")

    # ── Notebook ─────────────────────────────────────────────────────────────
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    def on_connected(iface: InterfaceInfo):
        _conn_dot.config(fg="#00ff88")
        _conn_label.config(fg="#00ff88", text=iface.name.upper())

    # Hardware tab (live)
    from ui.hardware_tab import HardwareTab
    hw = HardwareTab(nb, on_connect=on_connected)
    nb.add(hw, text="  Hardware  ")

    # Placeholder tabs (Phase 3)
    for name in ["ECU Info", "Flash", "Tune", "Logger", "CP Tools", "Sniff"]:
        ph = tk.Frame(nb, bg="#0a0c0f")
        tk.Label(ph, text=f"{name} — coming in Phase 3",
                 bg="#0a0c0f", fg="#3d5060",
                 font=("Courier New", 11)).place(relx=0.5, rely=0.5, anchor="center")
        nb.add(ph, text=f"  {name}  ")

    # ── Status bar ───────────────────────────────────────────────────────────
    tk.Frame(root, bg="#2a3540", height=1).pack(fill="x", side="bottom")
    sbar = tk.Frame(root, bg="#0f1215", height=24)
    sbar.pack(fill="x", side="bottom")
    sbar.pack_propagate(False)
    tk.Label(sbar, text="  Simos Tuning Suite  ·  github.com/dspl1236/simos-suite",
             bg="#0f1215", fg="#3d5060", font=("Courier New", 8)).pack(side="left", pady=3)

    root.mainloop()


if __name__ == "__main__":
    main()
