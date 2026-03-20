# transport/__init__.py
from .ble_bridge import BLEBridge, BLEBridgeConnection, BLEDeviceInfo, BLEBridgeSync, BLE_DEFAULT_GAP_NAME
from .interfaces import InterfaceRegistry, detect_j2534_dll, detect_j2534_dlls, detect_usb_isotp_ports

# Backwards-compat alias — FoundDevice was renamed BLEDeviceInfo
FoundDevice = BLEDeviceInfo
from .ws_bridge import WSBridge, WSBridgeConnection, ws_available, detect_funkbridge_url
