"""Configuration management for MQTT-CEC bridge service."""

import os
import sys
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration loaded from environment variables."""

    mqtt_broker_host: str
    mqtt_broker_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_device_name: str = "tv"
    mqtt_state_interval: int = 300
    mqtt_availability_online: str = "Online"
    mqtt_availability_offline: str = "Offline"
    mqtt_client_id: str = "mqtt_cec_controller"
    mqtt_qos: int = 1
    cec_device: int = 1
    log_level: str = "INFO"

    @property
    def mqtt_command_topic(self) -> str:
        """Get the command topic for receiving power commands."""
        return f"cmnd/{self.mqtt_device_name}/POWER"

    @property
    def mqtt_input_command_topic(self) -> str:
        """Get the command topic for receiving input switch commands."""
        return f"cmnd/{self.mqtt_device_name}/INPUT"

    @property
    def mqtt_state_topic(self) -> str:
        """Get the telemetry topic for publishing state updates."""
        return f"tele/{self.mqtt_device_name}/STATE"

    @property
    def mqtt_lwt_topic(self) -> str:
        """Get the Last Will and Testament topic for availability."""
        return f"tele/{self.mqtt_device_name}/LWT"


def load_config() -> Config:
    """
    Load configuration from environment variables.

    Returns:
        Config: Configuration object with validated values

    Raises:
        SystemExit: If required configuration is missing
    """
    mqtt_host = os.getenv("MQTT_BROKER_HOST")

    if not mqtt_host:
        print("ERROR: MQTT_BROKER_HOST environment variable is required", file=sys.stderr)
        sys.exit(1)

    # Check for deprecated MQTT_TOPIC variable
    if os.getenv("MQTT_TOPIC"):
        print("WARNING: MQTT_TOPIC is deprecated. Please use MQTT_DEVICE_NAME instead.", file=sys.stderr)
        print("         Example: MQTT_DEVICE_NAME=tv (creates topics like cmnd/tv/POWER)", file=sys.stderr)

    return Config(
        mqtt_broker_host=mqtt_host,
        mqtt_broker_port=int(os.getenv("MQTT_BROKER_PORT", "1883")),
        mqtt_username=os.getenv("MQTT_USERNAME", ""),
        mqtt_password=os.getenv("MQTT_PASSWORD", ""),
        mqtt_device_name=os.getenv("MQTT_DEVICE_NAME", "tv"),
        mqtt_state_interval=int(os.getenv("MQTT_STATE_INTERVAL", "300")),
        mqtt_availability_online=os.getenv("MQTT_AVAILABILITY_ONLINE", "Online"),
        mqtt_availability_offline=os.getenv("MQTT_AVAILABILITY_OFFLINE", "Offline"),
        mqtt_client_id=os.getenv("MQTT_CLIENT_ID", "mqtt_cec_controller"),
        mqtt_qos=int(os.getenv("MQTT_QOS", "1")),
        cec_device=int(os.getenv("CEC_DEVICE", "1")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper()
    )
