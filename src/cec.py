"""Minimal bindings for the Linux kernel CEC API (linux/cec.h).

Talks to /dev/cecN directly via ioctl, so neither libcec nor cec-utils is
needed at runtime. Struct layouts and ioctl numbers are identical on arm64
and x86_64.
"""

import errno
import fcntl
import os
import struct
from dataclasses import dataclass
from typing import Optional


# ioctl request numbers
CEC_ADAP_G_PHYS_ADDR = 0x80026101
CEC_ADAP_G_LOG_ADDRS = 0x805C6103
CEC_ADAP_S_LOG_ADDRS = 0xC05C6104
CEC_TRANSMIT = 0xC0386105
CEC_RECEIVE = 0xC0386106
CEC_S_MODE = 0x40046109

CEC_MODE_INITIATOR = 0x01
CEC_MODE_FOLLOWER = 0x10

CEC_LOG_ADDRS_FL_ALLOW_UNREG_FALLBACK = 0x1
CEC_LOG_ADDRS_FL_ALLOW_RC_PASSTHRU = 0x2

CEC_TX_STATUS_OK = 0x01
CEC_RX_STATUS_OK = 0x01
CEC_RX_STATUS_FEATURE_ABORT = 0x04

CEC_VERSION_1_4 = 0x05
CEC_VENDOR_ID_NONE = 0xFFFFFFFF
CEC_PHYS_ADDR_INVALID = 0xFFFF
CEC_LOG_ADDR_INVALID = 0xFF

LA_TV = 0
LA_BROADCAST = 15  # also "Unregistered" when used as initiator

PRIM_DEVTYPE_PLAYBACK = 4
LOG_ADDR_TYPE_PLAYBACK = 3
ALL_DEVTYPE_PLAYBACK = 0x10

# Opcodes
FEATURE_ABORT = 0x00
IMAGE_VIEW_ON = 0x04
STANDBY = 0x36
USER_CONTROL_PRESSED = 0x44
USER_CONTROL_RELEASED = 0x45
ROUTING_CHANGE = 0x80
ROUTING_INFORMATION = 0x81
ACTIVE_SOURCE = 0x82
REQUEST_ACTIVE_SOURCE = 0x85
SET_STREAM_PATH = 0x86
GIVE_DEVICE_POWER_STATUS = 0x8F
REPORT_POWER_STATUS = 0x90

ABORT_UNRECOGNIZED_OPCODE = 0x00

# REPORT_POWER_STATUS operand: on, standby, standby->on, on->standby
POWER_STATUS_NAMES = {0: "ON", 1: "OFF", 2: "ON", 3: "OFF"}

# struct cec_msg (56 bytes)
_MSG = struct.Struct("=QQIIII16sBBBBBBBx")
# struct cec_log_addrs (92 bytes)
_LOG_ADDRS = struct.Struct("=4sHBBII15s4s4s4s48sx")


def format_phys_addr(addr: int) -> str:
    """Format a 16-bit physical address as a.b.c.d."""
    return ".".join(str((addr >> shift) & 0xF) for shift in (12, 8, 4, 0))


@dataclass
class CECMessage:
    """A received CEC message, or the result of a transmit."""

    initiator: int
    destination: int
    opcode: Optional[int]
    operands: bytes
    tx_status: int = 0
    rx_status: int = 0

    @property
    def is_broadcast(self) -> bool:
        return self.destination == LA_BROADCAST

    def phys_addr(self, offset: int = 0) -> int:
        """Decode a physical address operand starting at the given offset."""
        return (self.operands[offset] << 8) | self.operands[offset + 1]

    def __str__(self) -> str:
        opcode = "poll" if self.opcode is None else f"0x{self.opcode:02x}"
        operands = self.operands.hex(":") or "-"
        return f"{self.initiator:x}->{self.destination:x} {opcode} [{operands}]"


class CECAdapter:
    """One open file handle on a /dev/cecN device."""

    def __init__(self, path: str = "/dev/cec0"):
        self.path = path
        self.fd: Optional[int] = None

    def open(self) -> None:
        self.fd = os.open(self.path, os.O_RDWR)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def set_mode(self, mode: int) -> None:
        fcntl.ioctl(self.fd, CEC_S_MODE, struct.pack("=I", mode))

    def get_phys_addr(self) -> int:
        buf = bytearray(2)
        fcntl.ioctl(self.fd, CEC_ADAP_G_PHYS_ADDR, buf)
        return struct.unpack("=H", buf)[0]

    def get_log_addr(self) -> int:
        """Return our claimed logical address, or 15 if unconfigured."""
        buf = bytearray(_LOG_ADDRS.size)
        fcntl.ioctl(self.fd, CEC_ADAP_G_LOG_ADDRS, buf)
        log_addrs, _, _, num_log_addrs = _LOG_ADDRS.unpack(buf)[:4]
        if num_log_addrs == 0 or log_addrs[0] == CEC_LOG_ADDR_INVALID:
            return LA_BROADCAST
        return log_addrs[0]

    def configure_playback(self, osd_name: str, rc_passthrough: bool) -> None:
        """
        Claim a Playback Device logical address with the given OSD name.

        Any existing configuration (e.g. one left behind by libcec) is
        cleared first, since the kernel refuses to reconfigure a configured
        adapter.

        Args:
            osd_name: Name the TV shows for this input (max 14 characters)
            rc_passthrough: Turn remote-control keys forwarded by the TV
                into keystrokes on the kernel's CEC input device
        """
        empty = b"\xff" * 4
        clear = _LOG_ADDRS.pack(empty, 0, CEC_VERSION_1_4, 0, CEC_VENDOR_ID_NONE,
                                0, b"", b"", b"", b"", b"")
        fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, bytearray(clear))

        flags = CEC_LOG_ADDRS_FL_ALLOW_UNREG_FALLBACK
        if rc_passthrough:
            flags |= CEC_LOG_ADDRS_FL_ALLOW_RC_PASSTHRU
        config = _LOG_ADDRS.pack(
            empty, 0, CEC_VERSION_1_4, 1, CEC_VENDOR_ID_NONE, flags,
            osd_name.encode("ascii", "replace")[:14],
            bytes([PRIM_DEVTYPE_PLAYBACK]),
            bytes([LOG_ADDR_TYPE_PLAYBACK]),
            bytes([ALL_DEVTYPE_PLAYBACK]),
            b"",
        )
        fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, bytearray(config))

    def transmit(self, destination: int, payload: bytes,
                 reply: int = 0, timeout_ms: int = 0) -> CECMessage:
        """
        Transmit a message and block until it is sent.

        Args:
            destination: Logical address to send to (15 = broadcast)
            payload: Opcode followed by operands
            reply: If non-zero, also wait for a reply with this opcode
            timeout_ms: How long to wait for the reply

        Returns:
            CECMessage: The reply if one was requested and received,
            otherwise the transmitted message. Check tx_status/rx_status.
        """
        header = (self.get_log_addr() << 4) | destination
        data = bytes([header]) + payload
        buf = bytearray(_MSG.pack(0, 0, len(data), timeout_ms, 0, 0, data,
                                  reply, 0, 0, 0, 0, 0, 0))
        fcntl.ioctl(self.fd, CEC_TRANSMIT, buf)
        return self._unpack(buf)

    def receive(self, timeout_ms: int) -> Optional[CECMessage]:
        """Block until a message arrives, returning None on timeout."""
        buf = bytearray(_MSG.pack(0, 0, 0, timeout_ms, 0, 0, b"",
                                  0, 0, 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(self.fd, CEC_RECEIVE, buf)
        except OSError as e:
            if e.errno == errno.ETIMEDOUT:
                return None
            raise
        return self._unpack(buf)

    @staticmethod
    def _unpack(buf: bytearray) -> CECMessage:
        fields = _MSG.unpack(buf)
        length, data = fields[2], fields[6]
        rx_status, tx_status = fields[8], fields[9]
        return CECMessage(
            initiator=data[0] >> 4,
            destination=data[0] & 0xF,
            opcode=data[1] if length > 1 else None,
            operands=bytes(data[2:length]),
            tx_status=tx_status,
            rx_status=rx_status,
        )
