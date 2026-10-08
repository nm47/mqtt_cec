"""HDMI-CEC controller using the kernel CEC API directly."""

import errno
import logging
import threading
import time
from typing import Callable, Optional

from . import cec
from .cec import CECAdapter, CECMessage


# HDMI physical address mapping for input switching
HDMI_PHYSICAL_ADDRESSES = {
    "HDMI1": 0x1000,
    "HDMI2": 0x2000,
    "HDMI3": 0x3000,
    "HDMI4": 0x4000,
}

# How long after a power command to ignore polled power states that
# contradict it. The TV keeps reporting "on" for ~3s after <Standby>.
POWER_SETTLE_SECONDS = 15

# How long to wait for the TV to answer a power status query
POWER_QUERY_TIMEOUT_MS = 1000

StateCallback = Callable[[str, str], None]


def input_name(phys_addr: int) -> str:
    """Map a physical address to the TV input it is behind."""
    if phys_addr == 0:
        return "TV"  # the TV's own sources (home screen, apps, tuner)
    return f"HDMI{phys_addr >> 12}"


class CECController:
    """
    Controls the TV over HDMI-CEC and tracks its real power and input state.

    Claims a Playback Device logical address on the CEC adapter and runs two
    background threads: one follows bus traffic (the TV announces input
    changes and standby), the other polls the TV's power status.
    """

    def __init__(self, adapter_path: str = "/dev/cec0", osd_name: str = "Raspberry Pi",
                 rc_passthrough: bool = True, poll_interval: int = 30):
        """
        Initialize CEC controller.

        Args:
            adapter_path: CEC device node
            osd_name: Name the TV shows for this device
            rc_passthrough: Forward TV remote keys to the Pi as keystrokes
            poll_interval: Seconds between power status polls (0 polls only
                at startup and when the TV looks like it woke up)
        """
        self.adapter_path = adapter_path
        self.osd_name = osd_name
        self.rc_passthrough = rc_passthrough
        self.poll_interval = poll_interval
        self.logger = logging.getLogger(__name__)

        self.power: str = "UNKNOWN"
        self.input: str = "UNKNOWN"
        self.on_state_change: Optional[StateCallback] = None

        self._tx: Optional[CECAdapter] = None
        self._rx: Optional[CECAdapter] = None
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._stop = threading.Event()
        self._poll_now = threading.Event()
        self._threads: list = []
        self._active_source = False
        self._expected_power: Optional[str] = None
        self._expected_power_until = 0.0

    def start(self) -> None:
        """Configure the CEC adapter and start the background threads."""
        self.logger.info(f"Opening CEC adapter {self.adapter_path}")
        self._stop.clear()
        self._poll_now.set()  # learn the TV's power state right away

        self._tx = CECAdapter(self.adapter_path)
        self._tx.open()
        self._tx.configure_playback(self.osd_name, self.rc_passthrough)

        # Separate handle for following bus traffic so blocking receives
        # never delay transmits
        self._rx = CECAdapter(self.adapter_path)
        self._rx.open()
        self._rx.set_mode(cec.CEC_MODE_INITIATOR | cec.CEC_MODE_FOLLOWER)

        self.logger.info(
            f"CEC configured as '{self.osd_name}' "
            f"(logical address {self._tx.get_log_addr()}, "
            f"physical address {cec.format_phys_addr(self._tx.get_phys_addr())})"
        )

        self._threads = [
            threading.Thread(target=self._receive_loop, name="cec-rx", daemon=True),
            threading.Thread(target=self._poll_loop, name="cec-poll", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Stop the background threads and close the adapter."""
        self.logger.info("Stopping CEC controller")
        self._stop.set()
        self._poll_now.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []
        for adapter in (self._rx, self._tx):
            if adapter:
                adapter.close()
        self._rx = self._tx = None

    def is_alive(self) -> bool:
        """Check if the background threads are still running."""
        return bool(self._threads) and all(t.is_alive() for t in self._threads)

    # Commands

    def power_on(self) -> bool:
        """Turn the TV on (no-op if it already is)."""
        return self._set_power("ON")

    def power_off(self) -> bool:
        """Put the TV in standby (no-op if it already is)."""
        return self._set_power("OFF")

    def toggle_power(self) -> bool:
        """Toggle a pending power target, or query the TV for its current state."""
        return self._set_power(None)

    def switch_input(self, hdmi_input: str) -> bool:
        """
        Switch TV to specified HDMI input.

        Broadcasts <Active Source> for the input's physical address, which
        this TV honours even though the message nominally comes from us.
        For the Pi's own input this is a proper One Touch Play.

        Args:
            hdmi_input: HDMI input name (HDMI1, HDMI2, HDMI3, HDMI4)

        Returns:
            bool: True if the command was sent successfully, False otherwise
        """
        if hdmi_input not in HDMI_PHYSICAL_ADDRESSES:
            self.logger.error(f"Invalid HDMI input: {hdmi_input}")
            return False

        phys_addr = HDMI_PHYSICAL_ADDRESSES[hdmi_input]
        is_self = phys_addr == self._tx.get_phys_addr()
        self.logger.info(
            f"Switching to {hdmi_input} (phys-addr={cec.format_phys_addr(phys_addr)})"
        )

        if is_self and not self._send(cec.LA_TV, cec.IMAGE_VIEW_ON):
            return False
        if not self._send(cec.LA_BROADCAST, cec.ACTIVE_SOURCE,
                          phys_addr >> 8, phys_addr & 0xFF):
            return False

        with self._lock:
            self._active_source = is_self
        # The TV confirms with <Routing Change>, but can take several seconds
        self._update_state(input=hdmi_input)
        return True

    def query_power(self) -> Optional[str]:
        """
        Ask the TV for its power status.

        Returns:
            "ON", "OFF", or None if the TV did not answer
        """
        try:
            reply = self._tx.transmit(
                cec.LA_TV, bytes([cec.GIVE_DEVICE_POWER_STATUS]),
                reply=cec.REPORT_POWER_STATUS, timeout_ms=POWER_QUERY_TIMEOUT_MS,
            )
        except OSError as e:
            self.logger.warning(f"Power status query failed: {e}")
            return None

        if not (reply.tx_status & cec.CEC_TX_STATUS_OK):
            self.logger.debug(f"Power status query not acknowledged (tx_status=0x{reply.tx_status:02x})")
            return None
        if (reply.rx_status & cec.CEC_RX_STATUS_OK and reply.opcode == cec.REPORT_POWER_STATUS
                and reply.operands):
            return cec.POWER_STATUS_NAMES.get(reply.operands[0])
        self.logger.debug(f"No power status reply (rx_status=0x{reply.rx_status:02x})")
        return None

    def _set_power(self, target: Optional[str]) -> bool:
        # Serialize command decisions, but let the receiver and poller keep
        # processing messages while a power query blocks.
        with self._command_lock:
            with self._lock:
                pending = (self._expected_power
                           if time.monotonic() < self._expected_power_until else None)

            current = self.query_power()
            if target is None:
                # A second toggle reverses the first even while the TV still
                # reports its old state. Otherwise use a fresh observation.
                basis = pending or current
                if basis not in ("ON", "OFF"):
                    self.logger.warning("Cannot toggle: TV power status is unknown")
                    return False
                target = "OFF" if basis == "ON" else "ON"

            if current == target and pending in (None, target):
                self.logger.info(f"TV is already {target}, not sending command")
                with self._lock:
                    self._expected_power = None
                    self._expected_power_until = 0.0
                self._update_state(power=target)
                return True

            opcode = cec.IMAGE_VIEW_ON if target == "ON" else cec.STANDBY
            if not self._send(cec.LA_TV, opcode):
                return False

            with self._lock:
                self._expected_power = target
                self._expected_power_until = time.monotonic() + POWER_SETTLE_SECONDS
                if target == "OFF":
                    self._active_source = False
            self._update_state(power=target)
            return True

    def _send(self, destination: int, opcode: int, *operands: int) -> bool:
        """Transmit a message, returning True if it was acknowledged."""
        payload = bytes([opcode, *operands])
        try:
            result = self._tx.transmit(destination, payload)
        except OSError as e:
            if e.errno == errno.ENONET:
                self.logger.error("CEC adapter is not configured (is the TV connected?)")
            else:
                self.logger.error(f"Failed to send CEC message: {e}")
            return False

        if not (result.tx_status & cec.CEC_TX_STATUS_OK):
            self.logger.error(f"CEC message {result} not acknowledged (tx_status=0x{result.tx_status:02x})")
            return False
        self.logger.debug(f"Sent {result}")
        return True

    # State tracking

    def _update_state(self, power: Optional[str] = None, input: Optional[str] = None) -> None:
        with self._lock:
            changed = False
            if power is not None and power != self.power:
                self.logger.info(f"TV power: {self.power} -> {power}")
                self.power = power
                changed = True
            if input is not None and input != self.input:
                self.logger.info(f"TV input: {self.input} -> {input}")
                self.input = input
                changed = True
            snapshot = (self.power, self.input)

        if changed and self.on_state_change:
            try:
                self.on_state_change(*snapshot)
            except Exception as e:
                self.logger.error(f"State change callback failed: {e}")

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            # With poll_interval 0 we still poll on demand (startup, TV wake hints)
            self._poll_now.wait(timeout=self.poll_interval or None)
            self._poll_now.clear()
            if self._stop.is_set():
                continue

            power = self.query_power()
            if power is not None:
                self._apply_reported_power(power)

    def _apply_reported_power(self, power: str) -> None:
        """Record a power state reported by the TV, unless it contradicts a recent command."""
        with self._lock:
            settling = (self._expected_power is not None
                        and time.monotonic() < self._expected_power_until)
            if settling and power != self._expected_power:
                self.logger.debug(f"Ignoring reported power {power} while TV settles")
                return
            self._expected_power = None
        self._update_state(power=power)

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._rx.receive(timeout_ms=500)
            except OSError as e:
                self.logger.error(f"CEC receive failed: {e}")
                return  # is_alive() turns false and main restarts us
            if msg is None or msg.opcode is None:
                continue

            self.logger.debug(f"Received {msg}")
            try:
                self._handle_message(msg)
            except Exception as e:
                self.logger.error(f"Error handling CEC message {msg}: {e}")

    def _handle_message(self, msg: CECMessage) -> None:
        opcode = msg.opcode
        own_addr = self._tx.get_phys_addr()

        # The TV sends <Request Active Source> and routing messages when it
        # wakes, so double-check power if we think it is off
        # Power replies (including duplicates) must never trigger another poll.
        if (msg.initiator == cec.LA_TV and self.power != "ON" and opcode in (
                cec.REQUEST_ACTIVE_SOURCE, cec.ROUTING_CHANGE,
                cec.ROUTING_INFORMATION, cec.SET_STREAM_PATH, cec.ACTIVE_SOURCE)):
            self._poll_now.set()

        if opcode == cec.ROUTING_CHANGE and len(msg.operands) >= 4:
            new_addr = msg.phys_addr(2)
            with self._lock:
                # Don't re-assert <Active Source> here: the TV can announce a
                # route to us seconds late, after it was asked to go elsewhere
                self._active_source = new_addr == own_addr
            self._update_state(input=input_name(new_addr))

        elif opcode in (cec.ACTIVE_SOURCE, cec.ROUTING_INFORMATION) and len(msg.operands) >= 2:
            with self._lock:
                self._active_source = (opcode == cec.ROUTING_INFORMATION
                                       and msg.phys_addr() == own_addr)
            self._update_state(input=input_name(msg.phys_addr()))

        elif opcode == cec.SET_STREAM_PATH and len(msg.operands) >= 2:
            addr = msg.phys_addr()
            with self._lock:
                self._active_source = addr == own_addr
            if addr == own_addr:
                self._send(cec.LA_BROADCAST, cec.ACTIVE_SOURCE, addr >> 8, addr & 0xFF)
            self._update_state(input=input_name(addr))

        elif opcode == cec.REQUEST_ACTIVE_SOURCE:
            if self._active_source:
                self._send(cec.LA_BROADCAST, cec.ACTIVE_SOURCE, own_addr >> 8, own_addr & 0xFF)

        elif opcode == cec.STANDBY:
            if msg.initiator == cec.LA_TV:
                with self._lock:
                    self._active_source = False
                    self._expected_power = None
                self._update_state(power="OFF")

        elif opcode == cec.REPORT_POWER_STATUS and msg.initiator == cec.LA_TV and msg.operands:
            # This TV sends every power status reply twice; the duplicate
            # arrives here as an unsolicited message
            power = cec.POWER_STATUS_NAMES.get(msg.operands[0])
            if power:
                self._apply_reported_power(power)

        elif opcode == cec.GIVE_DEVICE_POWER_STATUS and not msg.is_broadcast:
            self._send(msg.initiator, cec.REPORT_POWER_STATUS, 0x00)

        elif opcode in (cec.USER_CONTROL_PRESSED, cec.USER_CONTROL_RELEASED):
            pass  # handled by the kernel's CEC input device when rc_passthrough is on

        elif (not msg.is_broadcast and opcode != cec.FEATURE_ABORT
              and msg.initiator != cec.LA_BROADCAST):
            # A follower must reject directed messages it doesn't support
            self._send(msg.initiator, cec.FEATURE_ABORT, opcode, cec.ABORT_UNRECOGNIZED_OPCODE)
