"""MQTT client handler for receiving power commands."""

import json
import logging
import time
from datetime import datetime
import paho.mqtt.client as mqtt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .cec_controller import CECController


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
        self.current_power_state: str = "UNKNOWN"
        self.current_input: str = "UNKNOWN"
        self.last_state_publish_time: float = 0.0

    def connect(self) -> None:
        """Initialize and connect MQTT client."""
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

        # Connect with automatic reconnection
        self.logger.info(
            f"Connecting to MQTT broker at "
            f"{self.config.mqtt_broker_host}:{self.config.mqtt_broker_port}"
        )

        self.client.connect(
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

            # Publish initial state (will be implemented in publish methods)
            # Note: uptime will be 0 on initial connect, actual uptime added from main loop
            self.publish_state(0)
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
            payload: Command payload (ON/OFF)
        """
        success = False
        if payload == "ON":
            success = self.cec.power_on()
            if success:
                self.current_power_state = "ON"
        elif payload == "OFF":
            success = self.cec.power_off()
            if success:
                self.current_power_state = "OFF"
        else:
            self.logger.warning(f"Unknown power payload: {payload}")

        # Publish state immediately after successful command
        if success:
            self.publish_state(0)

    def _handle_input_command(self, payload: str) -> None:
        """
        Handle INPUT command.

        Args:
            payload: Command payload (HDMI1/HDMI2/HDMI3/HDMI4)
        """
        # Validate payload format
        valid_inputs = ["HDMI1", "HDMI2", "HDMI3", "HDMI4"]
        if payload not in valid_inputs:
            self.logger.warning(
                f"Invalid input payload: {payload}. "
                f"Valid values: {', '.join(valid_inputs)}"
            )
            return

        success = self.cec.switch_input(payload)
        if success:
            self.current_input = payload
            # Publish state immediately after successful command
            self.publish_state(0)

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

    def publish_state(self, uptime_seconds: int) -> None:
        """
        Publish current state as JSON telemetry.

        Args:
            uptime_seconds: Service uptime in seconds
        """
        if not self.client:
            return

        # Calculate uptime in DDThh:mm:ss format
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        seconds = uptime_seconds % 60
        uptime_str = f"{days}T{hours:02d}:{minutes:02d}:{seconds:02d}"

        # Build Tasmota-compatible state payload
        state_payload = {
            "Time": datetime.now().isoformat(),
            "Uptime": uptime_str,
            "POWER": self.current_power_state,
            "INPUT": self.current_input
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
            self.client.loop_stop()
            self.client.disconnect()
