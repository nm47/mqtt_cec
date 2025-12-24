"""Persistent HDMI-CEC client controller."""

import logging
import subprocess
import threading
from typing import Optional


# HDMI physical address mapping for input switching (cec-ctl format)
HDMI_PHYSICAL_ADDRESSES = {
    "HDMI1": "1.0.0.0",
    "HDMI2": "2.0.0.0",
    "HDMI3": "3.0.0.0",
    "HDMI4": "4.0.0.0",
}


class CECController:
    """Manages a persistent cec-client subprocess for HDMI-CEC control."""

    def __init__(self, device: int = 1):
        """
        Initialize CEC controller.

        Args:
            device: CEC device number (HDMI port, typically 1)
        """
        self.device = device
        self.process: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__)
        self._output_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Initialize persistent cec-client process."""
        self.logger.info(f"Starting cec-client on device {self.device}")

        self.process = subprocess.Popen(
            ["cec-client", "-d", str(self.device)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            text=True
        )

        # Start background thread to consume output
        self._output_thread = threading.Thread(
            target=self._read_output,
            daemon=True
        )
        self._output_thread.start()

        self.logger.info("CEC client started successfully")

    def _read_output(self) -> None:
        """Consume stdout to prevent buffer blocking (runs in background thread)."""
        while self.process and self.process.poll() is None:
            try:
                line = self.process.stdout.readline()
                if line:
                    self.logger.debug(f"CEC: {line.strip()}")
            except Exception as e:
                self.logger.error(f"Error reading CEC output: {e}")

    def send_command(self, command: str) -> bool:
        """
        Send command to running cec-client.

        Args:
            command: CEC command to send (e.g., "on 0", "standby 0")

        Returns:
            bool: True if command was sent successfully, False otherwise
        """
        with self.lock:
            if not self.is_alive():
                self.logger.error("CEC process is not running")
                return False

            try:
                self.logger.info(f"Sending CEC command: {command}")
                self.process.stdin.write(f"{command}\n")
                self.process.stdin.flush()
                return True
            except Exception as e:
                self.logger.error(f"Failed to send command: {e}")
                return False

    def power_on(self) -> bool:
        """Send power ON command to TV."""
        return self.send_command("on 0")

    def power_off(self) -> bool:
        """Send power OFF (standby) command to TV."""
        return self.send_command("standby 0")

    def switch_input(self, hdmi_input: str) -> bool:
        """
        Switch TV to specified HDMI input.

        Args:
            hdmi_input: HDMI input name (HDMI1, HDMI2, HDMI3, HDMI4)

        Returns:
            bool: True if command was sent successfully, False otherwise
        """
        if hdmi_input not in HDMI_PHYSICAL_ADDRESSES:
            self.logger.error(f"Invalid HDMI input: {hdmi_input}")
            return False

        physical_addr = HDMI_PHYSICAL_ADDRESSES[hdmi_input]

        # Use cec-ctl for input switching as it works more reliably
        # Send ACTIVE_SOURCE command to TV (logical address 0)
        try:
            self.logger.info(f"Switching to {hdmi_input} (phys-addr={physical_addr})")
            result = subprocess.run(
                ["cec-ctl", "--to", "0", "--active-source", f"phys-addr={physical_addr}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            self.logger.error(f"Failed to switch input: {e}")
            return False

    def is_alive(self) -> bool:
        """Check if CEC process is still running."""
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        """Gracefully terminate cec-client process."""
        if self.process:
            self.logger.info("Stopping CEC client")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.logger.warning("CEC client did not terminate, killing")
                self.process.kill()
