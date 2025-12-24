"""Main entry point for MQTT-CEC bridge service."""

import logging
import signal
import sys
import threading
import time

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

    # Initialize components
    cec = CECController(config.cec_device)
    cec.start()

    mqtt_handler = MQTTHandler(config, cec)
    mqtt_handler.connect()

    # Shutdown flag
    shutdown_requested = threading.Event()

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown")
        shutdown_requested.set()

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("MQTT-CEC controller running. Press Ctrl+C to exit.")

    # Track service start time for uptime calculation
    start_time = time.time()

    # Main loop
    try:
        while not shutdown_requested.is_set():
            # Health check - restart CEC if dead
            if not cec.is_alive():
                logger.error("CEC process died, restarting...")
                cec.start()

            # Periodic state publishing
            uptime_seconds = int(time.time() - start_time)
            if mqtt_handler.should_publish_state():
                mqtt_handler.publish_state(uptime_seconds)

            time.sleep(1)

    finally:
        logger.info("Shutting down...")
        mqtt_handler.shutdown()
        cec.stop()
        logger.info("Shutdown complete")
        sys.exit(0)


if __name__ == "__main__":
    main()
