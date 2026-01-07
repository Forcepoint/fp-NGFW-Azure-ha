import sys
import logging
from logging.handlers import RotatingFileHandler


SCRIPT_NAME = __name__

LOGGER = logging.getLogger(__name__)
LOGGER_MAX_BYTES = 1000000
LOGGER_MAX_FILES = 5


def configure_logging(log_file_name: str, console: bool = False,
                      debug: bool = False) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                                  "%Y-%m-%dT%H:%M:%S%z")
    level = logging.DEBUG if debug else logging.INFO
    LOGGER.setLevel(level)

    file_handler = RotatingFileHandler(
        log_file_name, maxBytes=LOGGER_MAX_BYTES, backupCount=LOGGER_MAX_FILES)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        LOGGER.addHandler(console_handler)
