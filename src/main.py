"""Main entry point for MQTT-CEC bridge service."""

import logging
import signal
import sys
import threading

from .config import load_config
from .cec_controller import CECController
from .mqtt_handler import MQTTHandler


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level))
    root_logger.addHandler(console_handler)

    # Reduce verbosity of paho.mqtt
    logging.getLogger("paho").setLevel(logging.WARNING)

    logging.info(f"Logging configured at {log_level} level")


def main():
    """Main application entry point."""
    # Load configuration
    config = load_config()
    setup_logging(config.log_level)

    logger = logging.getLogger(__name__)
    logger.info("Starting MQTT-CEC bridge service")

    # Shutdown flag
    shutdown_requested = threading.Event()

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown")
        shutdown_requested.set()

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Initialize components
    cec = CECController(
        adapter_path=config.cec_adapter,
        osd_name=config.cec_osd_name,
        rc_passthrough=config.cec_remote_keys,
        poll_interval=config.cec_poll_interval,
    )
    mqtt_handler = MQTTHandler(config, cec)
    cec.on_state_change = mqtt_handler.on_state_change
    cec.start()
    mqtt_handler.connect()

    logger.info("MQTT-CEC controller running. Press Ctrl+C to exit.")

    # Main loop
    try:
        while not shutdown_requested.is_set():
            # Health check - restart CEC if dead
            if not cec.is_alive():
                logger.error("CEC controller died, restarting...")
                cec.stop()
                cec.start()

            # Periodic state publishing
            if mqtt_handler.should_publish_state():
                mqtt_handler.publish_state()

            shutdown_requested.wait(1)

    finally:
        logger.info("Shutting down...")
        mqtt_handler.shutdown()
        cec.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
