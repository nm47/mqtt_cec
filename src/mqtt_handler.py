"""MQTT client handler for receiving TV commands and publishing TV state."""

import json
import logging
import time
from datetime import datetime
import paho.mqtt.client as mqtt
from typing import TYPE_CHECKING

from .cec_controller import HDMI_PHYSICAL_ADDRESSES

if TYPE_CHECKING:
    from .config import Config
    from .cec_controller import CECController


# Tasmota-compatible POWER payloads
POWER_ON_PAYLOADS = {"ON", "1", "TRUE"}
POWER_OFF_PAYLOADS = {"OFF", "0", "FALSE"}
POWER_TOGGLE_PAYLOADS = {"TOGGLE", "2"}


class MQTTHandler:
    """Handles MQTT connection lifecycle and message routing."""

    def __init__(self, config: "Config", cec_controller: "CECController"):
        """
        Initialize MQTT handler.

        Args:
            config: Configuration object with MQTT settings
            cec_controller: CEC controller instance for sending commands
        """
        self.config = config
        self.cec = cec_controller
        self.client = None
        self.logger = logging.getLogger(__name__)
        self.start_time: float = time.time()
        self.last_state_publish_time: float = 0.0

    def connect(self) -> None:
        """Initialize MQTT client and start connecting in the background."""
        self.client = mqtt.Client(client_id=self.config.mqtt_client_id)

        # Authentication if credentials provided
        if self.config.mqtt_username:
            self.client.username_pw_set(
                self.config.mqtt_username,
                self.config.mqtt_password
            )

        # Set up Last Will and Testament (must be done before connect)
        self.client.will_set(
            topic=self.config.mqtt_lwt_topic,
            payload=self.config.mqtt_availability_offline,
            qos=self.config.mqtt_qos,
            retain=True
        )

        # Callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.logger.info(
            f"Connecting to MQTT broker at "
            f"{self.config.mqtt_broker_host}:{self.config.mqtt_broker_port}"
        )

        # Connect asynchronously so an unreachable broker at startup is
        # retried by the network loop instead of crashing the service
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.connect_async(
            self.config.mqtt_broker_host,
            self.config.mqtt_broker_port,
            keepalive=60
        )

        # Start network loop in background thread
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to broker."""
        if rc == 0:
            self.logger.info("Connected to MQTT broker")

            # Subscribe to command topics (important for reconnection)
            client.subscribe(self.config.mqtt_command_topic, qos=self.config.mqtt_qos)
            self.logger.info(f"Subscribed to topic: {self.config.mqtt_command_topic}")

            client.subscribe(self.config.mqtt_input_command_topic, qos=self.config.mqtt_qos)
            self.logger.info(f"Subscribed to topic: {self.config.mqtt_input_command_topic}")

            # Publish online status
            self.publish_availability_online()
            self.publish_state()
        else:
            self.logger.error(f"Connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Callback when disconnected from broker."""
        if rc == 0:
            self.logger.info("Disconnected from MQTT broker (clean)")
        else:
            self.logger.warning(
                f"Unexpected disconnect from MQTT broker (code {rc}), "
                "will auto-reconnect"
            )

    def _on_message(self, client, userdata, msg):
        """Callback when message received."""
        try:
            payload = msg.payload.decode().strip().upper()
            self.logger.info(
                f"Received message on {msg.topic}: {payload}"
            )

            # A retained command would re-run on every reconnect
            if msg.retain:
                self.logger.warning(f"Ignoring retained command on {msg.topic}")
                return

            # Route to appropriate handler based on topic
            if msg.topic == self.config.mqtt_command_topic:
                self._handle_power_command(payload)
            elif msg.topic == self.config.mqtt_input_command_topic:
                self._handle_input_command(payload)
            else:
                self.logger.warning(f"Unexpected topic: {msg.topic}")

        except Exception as e:
            self.logger.error(f"Error handling message: {e}")

    def _handle_power_command(self, payload: str) -> None:
        """
        Handle POWER command.

        Args:
            payload: ON/OFF/TOGGLE (or 1/0/2), empty to query
        """
        if payload in POWER_TOGGLE_PAYLOADS:
            self.cec.toggle_power()
        elif payload in POWER_ON_PAYLOADS:
            self.cec.power_on()
        elif payload in POWER_OFF_PAYLOADS:
            self.cec.power_off()
        elif payload:
            self.logger.warning(f"Unknown power payload: {payload}")
            return

        self._publish_result("POWER", self.cec.power)

    def _handle_input_command(self, payload: str) -> None:
        """
        Handle INPUT command.

        Args:
            payload: HDMI1/HDMI2/HDMI3/HDMI4, empty to query
        """
        if payload:
            if payload not in HDMI_PHYSICAL_ADDRESSES:
                self.logger.warning(
                    f"Invalid input payload: {payload}. "
                    f"Valid values: {', '.join(HDMI_PHYSICAL_ADDRESSES)}"
                )
                return
            self.cec.switch_input(payload)

        self._publish_result("INPUT", self.cec.input)

    def on_state_change(self, power: str, input: str) -> None:
        """Publish TV state whenever it changes, whatever caused the change."""
        self._publish(f"{self.config.mqtt_stat_topic_prefix}/POWER", power)
        self._publish(f"{self.config.mqtt_stat_topic_prefix}/INPUT", input)
        self.publish_state()

    def _publish_result(self, key: str, value: str) -> None:
        """Answer a command Tasmota-style on stat/{device}/RESULT."""
        self._publish(f"{self.config.mqtt_stat_topic_prefix}/RESULT", json.dumps({key: value}))
        self._publish(f"{self.config.mqtt_stat_topic_prefix}/{key}", value)

    def _publish(self, topic: str, payload: str) -> None:
        if self.client:
            self.client.publish(topic=topic, payload=payload, qos=self.config.mqtt_qos, retain=False)

    def publish_availability_online(self) -> None:
        """Publish Online status to LWT topic."""
        if self.client:
            self.client.publish(
                topic=self.config.mqtt_lwt_topic,
                payload=self.config.mqtt_availability_online,
                qos=self.config.mqtt_qos,
                retain=True
            )
            self.logger.info(f"Published availability: {self.config.mqtt_availability_online}")

    def publish_state(self) -> None:
        """Publish current state as JSON telemetry."""
        if not self.client:
            return

        # Calculate uptime in DDThh:mm:ss format
        uptime_seconds = int(time.time() - self.start_time)
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        seconds = uptime_seconds % 60
        uptime_str = f"{days}T{hours:02d}:{minutes:02d}:{seconds:02d}"

        # Build Tasmota-compatible state payload
        state_payload = {
            "Time": datetime.now().isoformat(timespec="seconds"),
            "Uptime": uptime_str,
            "POWER": self.cec.power,
            "INPUT": self.cec.input
        }

        try:
            payload_json = json.dumps(state_payload)
            self.client.publish(
                topic=self.config.mqtt_state_topic,
                payload=payload_json,
                qos=self.config.mqtt_qos,
                retain=False  # Telemetry is not retained
            )
            self.logger.debug(f"Published state: {payload_json}")
            self.last_state_publish_time = time.time()
        except Exception as e:
            self.logger.error(f"Error publishing state: {e}")

    def should_publish_state(self) -> bool:
        """
        Check if enough time has passed since last state publish.

        Returns:
            bool: True if state should be published now
        """
        elapsed = time.time() - self.last_state_publish_time
        return elapsed >= self.config.mqtt_state_interval

    def shutdown(self) -> None:
        """Gracefully disconnect from MQTT broker."""
        if self.client:
            self.logger.info("Disconnecting from MQTT broker")
            # Clean disconnects don't trigger the LWT, so mark offline ourselves
            try:
                self.client.publish(
                    topic=self.config.mqtt_lwt_topic,
                    payload=self.config.mqtt_availability_offline,
                    qos=self.config.mqtt_qos,
                    retain=True
                ).wait_for_publish(timeout=2)
            except (RuntimeError, ValueError) as e:
                self.logger.warning(f"Could not publish offline status: {e}")
            self.client.disconnect()
            self.client.loop_stop()
