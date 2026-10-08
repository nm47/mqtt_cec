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
    cec_adapter: str = "/dev/cec0"
    cec_osd_name: str = "Raspberry Pi"
    cec_poll_interval: int = 30
    cec_remote_keys: bool = True
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
    def mqtt_stat_topic_prefix(self) -> str:
        """Get the prefix for per-command status topics (stat/{device}/POWER, ...)."""
        return f"stat/{self.mqtt_device_name}"

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

    # CEC_DEVICE was passed to cec-client's -d flag, which is its log level
    if os.getenv("CEC_DEVICE"):
        print("WARNING: CEC_DEVICE is deprecated and ignored. Use CEC_ADAPTER (default /dev/cec0).",
              file=sys.stderr)

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
        cec_adapter=os.getenv("CEC_ADAPTER", "/dev/cec0"),
        cec_osd_name=os.getenv("CEC_OSD_NAME", "Raspberry Pi"),
        cec_poll_interval=int(os.getenv("CEC_POLL_INTERVAL", "30")),
        cec_remote_keys=os.getenv("CEC_REMOTE_KEYS", "true").lower() in ("1", "true", "yes", "on"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper()
    )
