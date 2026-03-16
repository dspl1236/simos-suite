"""
transport/interfaces.py — Interface registry and auto-detection

Knows about every supported hardware interface, where to find its DLL or
serial port, and what to pass to _make_connection().

Supported interfaces
────────────────────
BLE         ESP32 ISO-TP BLE bridge (esp32-isotp-ble-bridge-c7vag)
            Wireless. Scan → connect via BLEBridge.
            No DLL needed. Works on Windows, macOS, Linux.

USBISOTP    ESP32 ISO-TP bridge over USB serial (same hardware, cable mode)
            Same A0/ESP32 hardware as BLE but plugged in via USB-C.
            Shows up as a COM port (Windows) or /dev/ttyUSB0 (Linux/Mac).
            Baud: 250000. Packet format identical to BLE.

J2534       Any J2534 PassThru DLL (Windows only — DLL is 32-bit).
            The suite is a 32-bit process when using J2534 on Windows.

    Known DLL paths
    ───────────────
    Tactrix OpenPort 2.0
        Oldest and most tested with VW_Flash. The community standard.
        ~$170 new, widely available used.
        DLL: C:/Program Files (x86)/OpenECU/OpenPort 2.0/drivers/openport 2.0/op20pt32.dll

    Mongoose J2534 (Drew Technologies / Bosch)
        Your legacy cable. Works — passthru is passthru.
        The older Mongoose Pro VAG (blue cable, ~2012–2018 era) installs
        a 32-bit DLL. Newer Mongoose units may have x64 DLL only —
        check Device Manager after install.
        DLL: C:/Program Files (x86)/Drew Technologies, Inc/Mongoose/monj2534.dll
             or: C:/Program Files (x86)/Drew Technologies/Mongoose/monj2534.dll
             or: C:/Program Files/Drew Technologies, Inc/Mongoose/monj2534.dll
        Note: Drew Technologies was acquired by Bosch in 2014. Some post-2014
        drivers install under "Bosch" instead:
             C:/Program Files (x86)/Bosch/Mongoose/monj2534.dll

    VNCI 6154A (clone ODIS cable)
        Your other cable. Works for UDS reading and probing.
        Flash reliability is lower than Tactrix — some users report
        ISO15765 timing issues under heavy load (long transfers).
        DLL: C:/Program Files (x86)/OpenShell/vcdc.dll
             or: C:/Windows/SysWOW64/RP1210/drewlinq.dll  (some variants)
             or: search registry: HKLM/SOFTWARE/WOW6432Node/PassThruSupport.04.04/

    Ross-Tech HEX-NET / HEX-V2 — NOT SUPPORTED.
        These are proprietary VCDS-only devices with no J2534 PassThru DLL.
        Ross-Tech explicitly states they cannot emulate a pass-through interface
        and are incompatible with any software other than VCDS.

SocketCAN   Linux only. Requires a SocketCAN-compatible USB-CAN adapter
            (Peak PCAN, Kvaser, or similar) and the kernel iso-tp module.
            Format: "SocketCAN_can0" (replace can0 with your interface).

────────────────────────────────────────────────────────────────────────────
Usage:
    from transport.interfaces import InterfaceRegistry, detect_j2534_dll

    reg = InterfaceRegistry()
    print(reg.available())          # lists all detected interfaces

    dll = detect_j2534_dll()        # finds first installed J2534 DLL
    conn = make_connection_for(ecu, interface="J2534", interface_path=dll)
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ── Known J2534 DLL locations (Windows, 32-bit) ───────────────────────────────

J2534_DLL_CANDIDATES = [
    # Tactrix OpenPort 2.0 — most tested, recommended for flashing
    (
        "Tactrix OpenPort 2.0",
        r"C:\Program Files (x86)\OpenECU\OpenPort 2.0\drivers\openport 2.0\op20pt32.dll",
    ),
    # Mongoose / Drew Technologies (pre-Bosch acquisition)
    (
        "Mongoose J2534 (Drew Technologies)",
        r"C:\Program Files (x86)\Drew Technologies, Inc\Mongoose\monj2534.dll",
    ),
    (
        "Mongoose J2534 (Drew Technologies alt path)",
        r"C:\Program Files (x86)\Drew Technologies\Mongoose\monj2534.dll",
    ),
    (
        "Mongoose J2534 (Program Files, 64-bit OS)",
        r"C:\Program Files\Drew Technologies, Inc\Mongoose\monj2534.dll",
    ),
    # Mongoose / Bosch (post-2014 acquisition)
    (
        "Mongoose J2534 (Bosch)",
        r"C:\Program Files (x86)\Bosch\Mongoose\monj2534.dll",
    ),
    (
        "Mongoose J2534 (Bosch alt path)",
        r"C:\Program Files\Bosch\Mongoose\monj2534.dll",
    ),
    # VNCI 6154A (OpenShell / clone)
    (
        "VNCI 6154A (OpenShell)",
        r"C:\Program Files (x86)\OpenShell\vcdc.dll",
    ),
    (
        "VNCI 6154A (drewlinq variant)",
        r"C:\Windows\SysWOW64\RP1210\drewlinq.dll",
    ),
    # Ross-Tech HEX-NET and HEX-V2 are NOT J2534 compatible.
    # They are proprietary VCDS-only devices. Ross-Tech explicitly states
    # the HEX-NET cannot emulate a PassThru interface and is unlikely to
    # work with any software other than VCDS. No DLL entry here.
]


def detect_j2534_dlls() -> List[tuple[str, str]]:
    """
    Return a list of (name, path) for all J2534 DLLs found on this system.
    Empty list on non-Windows or if none found.
    """
    if platform.system() != "Windows":
        return []

    found = []
    for name, path in J2534_DLL_CANDIDATES:
        if Path(path).exists():
            found.append((name, path))

    # Also scan the Windows registry for PassThru 04.04 entries
    # (the standard self-registration location for J2534 DLLs)
    try:
        import winreg
        for root in [winreg.HKEY_LOCAL_MACHINE]:
            for base in [
                r"SOFTWARE\WOW6432Node\PassThruSupport.04.04",
                r"SOFTWARE\PassThruSupport.04.04",
            ]:
                try:
                    key = winreg.OpenKey(root, base)
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            subkey = winreg.OpenKey(key, subkey_name)
                            try:
                                dll_path, _ = winreg.QueryValueEx(subkey, "FunctionLibrary")
                                if Path(dll_path).exists():
                                    entry = (f"Registry: {subkey_name}", dll_path)
                                    if entry not in found:
                                        found.append(entry)
                            except FileNotFoundError:
                                pass
                            winreg.CloseKey(subkey)
                            i += 1
                        except OSError:
                            break
                    winreg.CloseKey(key)
                except FileNotFoundError:
                    pass
    except ImportError:
        pass   # not Windows

    return found


def detect_j2534_dll() -> Optional[str]:
    """Return the path to the first detected J2534 DLL, or None."""
    dlls = detect_j2534_dlls()
    return dlls[0][1] if dlls else None


# ── USB-serial port detection ─────────────────────────────────────────────────

def detect_usb_isotp_ports() -> List[tuple[str, str]]:
    """
    Find serial ports that look like an ESP32 ISO-TP bridge.
    Returns list of (description, port_path).
    The ESP32 on the A0/bridge presents as a CP210x or CH340 USB-UART.
    """
    found = []
    try:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            desc = (port.description or "").lower()
            vid_pid = f"{port.vid:04X}:{port.pid:04X}" if port.vid else ""
            # CP2102 (Silicon Labs) — most ESP32 devkits
            # CH340/CH341 — cheaper clones
            # FTDI — some custom boards
            is_esp32 = (
                "cp210" in desc
                or "ch340" in desc
                or "ch341" in desc
                or port.vid == 0x10C4   # Silicon Labs
                or port.vid == 0x1A86   # QinHeng (CH340)
            )
            if is_esp32:
                label = f"{port.device}  {port.description or ''}"
                if vid_pid:
                    label += f"  [{vid_pid}]"
                found.append((label.strip(), port.device))
    except ImportError:
        pass  # pyserial not installed
    except Exception:
        pass
    return found


# ── Interface registry ────────────────────────────────────────────────────────

@dataclass
class InterfaceInfo:
    name:        str          # Display name
    interface:   str          # Interface type string for _make_connection()
    path:        str          # DLL path or serial port
    available:   bool         # Is the hardware present/detected?
    notes:       str = ""

    def __str__(self):
        status = "✓" if self.available else "✗"
        return f"[{status}] {self.name}  ({self.interface}:{self.path})"


class InterfaceRegistry:
    """
    Builds a list of all available hardware interfaces on the current system.
    Call available() to get only the ones detected as present.
    """

    def __init__(self):
        self._interfaces: List[InterfaceInfo] = []
        self._scan()

    def _scan(self):
        self._interfaces.clear()

        # BLE bridge — always listed (BLE is always possible if bleak is installed)
        try:
            import bleak  # noqa
            ble_available = True
        except ImportError:
            ble_available = False

        self._interfaces.append(InterfaceInfo(
            name      = "ESP32 BLE Bridge (BLE_TO_ISOTP20)",
            interface = "BLE",
            path      = "",
            available = ble_available,
            notes     = "Wireless. Requires bleak. Scan for device first.",
        ))

        # USB ISO-TP (ESP32 over USB serial)
        usb_ports = detect_usb_isotp_ports()
        if usb_ports:
            for label, port in usb_ports:
                self._interfaces.append(InterfaceInfo(
                    name      = f"ESP32 USB Bridge ({port})",
                    interface = "USBISOTP",
                    path      = port,
                    available = True,
                    notes     = label,
                ))
        else:
            self._interfaces.append(InterfaceInfo(
                name      = "ESP32 USB Bridge (not detected)",
                interface = "USBISOTP",
                path      = "COM3",   # placeholder
                available = False,
                notes     = "Connect ESP32 bridge via USB and check Device Manager.",
            ))

        # J2534 DLLs
        j2534_dlls = detect_j2534_dlls()
        if j2534_dlls:
            for name, path in j2534_dlls:
                self._interfaces.append(InterfaceInfo(
                    name      = name,
                    interface = "J2534",
                    path      = path,
                    available = True,
                    notes     = "Windows J2534 PassThru DLL",
                ))
        else:
            # List known hardware as unavailable so the GUI can show them
            for name, path in J2534_DLL_CANDIDATES:
                self._interfaces.append(InterfaceInfo(
                    name      = name,
                    interface = "J2534",
                    path      = path,
                    available = False,
                    notes     = "DLL not found — install drivers",
                ))

        # SocketCAN (Linux only)
        if platform.system() == "Linux":
            can_ifaces = self._scan_socketcan()
            for iface in can_ifaces:
                self._interfaces.append(InterfaceInfo(
                    name      = f"SocketCAN ({iface})",
                    interface = f"SocketCAN_{iface}",
                    path      = iface,
                    available = True,
                    notes     = "Linux SocketCAN + iso-tp kernel module required",
                ))

    def _scan_socketcan(self) -> List[str]:
        """Return names of available SocketCAN interfaces."""
        found = []
        try:
            import socket
            net_path = Path("/sys/class/net")
            if net_path.exists():
                for iface in net_path.iterdir():
                    if iface.name.startswith("can"):
                        found.append(iface.name)
        except Exception:
            pass
        return found

    def all(self) -> List[InterfaceInfo]:
        return list(self._interfaces)

    def available(self) -> List[InterfaceInfo]:
        return [i for i in self._interfaces if i.available]

    def first_available(self) -> Optional[InterfaceInfo]:
        avail = self.available()
        return avail[0] if avail else None

    def by_type(self, interface_type: str) -> List[InterfaceInfo]:
        t = interface_type.upper()
        return [i for i in self._interfaces
                if i.interface.upper().startswith(t)]

    def refresh(self):
        self._scan()


# ── Mongoose-specific notes ───────────────────────────────────────────────────

MONGOOSE_NOTES = """
Mongoose J2534 — compatibility notes
─────────────────────────────────────
The Mongoose Pro VAG (Drew Technologies, later Bosch) is a legacy cable but
works fine for the suite's needs.

Known limitations vs Tactrix OpenPort 2.0:
  - Some users report STmin timing issues during large block transfers on
    older Mongoose firmware. If you get timeout errors during flash, try
    increasing st_min_us to 500000 (0.5ms) in _make_connection().
  - The Mongoose DLL is 32-bit only. The suite must run as a 32-bit Python
    process when using J2534 on Windows (same requirement as VW_Flash).
  - Mongoose does not support the PassThru ioctl TX_IOCTL_SET_DLL_DEBUG_FLAGS
    debug flag. The J2534Connection debug=True option will silently fail —
    this is harmless.

DLL installation path (most common):
  C:/Program Files (x86)/Drew Technologies, Inc/Mongoose/monj2534.dll

If the DLL is not detected automatically, find it via Device Manager:
  Device Manager → Universal Serial Bus devices → Mongoose J2534
  → right-click → Properties → Details → Hardware Ids
  Then check: C:/Windows/SysWOW64/ for monj2534.dll

J2534 registry entry (if self-registered):
  HKLM\\SOFTWARE\\WOW6432Node\\PassThruSupport.04.04\\Mongoose
"""
