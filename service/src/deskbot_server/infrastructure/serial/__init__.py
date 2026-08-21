"""USB CDC transport for Deskbot devices.

The transport deliberately lives beside, rather than inside, the WebSocket
implementation.  Application code can continue to use the existing
``DownlinkPort`` while the device-facing link is selected at runtime.
"""

from deskbot_server.infrastructure.serial.manager import (
    SerialConnector,
    SerialDeviceManager,
)
from deskbot_server.infrastructure.serial.session import (
    DeviceSession,
    SerialDeviceSession,
    SessionDiagnostics,
)

__all__ = [
    "DeviceSession",
    "SerialConnector",
    "SerialDeviceManager",
    "SerialDeviceSession",
    "SessionDiagnostics",
]
