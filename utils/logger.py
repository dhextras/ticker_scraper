import inspect
import logging
import os
from datetime import datetime

import pytz


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[94m",  # Blue
        "INFO": "\033[92m",  # Green
        "WARNING": "\033[93m",  # Yellow
        "ERROR": "\033[91m",  # Red
        "CRITICAL": "\033[95m",  # Magenta
        "RESET": "\033[0m",  # Reset color
    }

    def format(self, record):
        log_message = super().format(record)
        return f"{self.COLORS.get(record.levelname, self.COLORS['RESET'])}{log_message}{self.COLORS['RESET']}"


def setup_logger(log_file=None):
    main_script = inspect.stack()[-1].filename
    caller_script = inspect.stack()[2].filename
    script_name = os.path.splitext(os.path.basename(main_script))[0]
    logger_name = os.path.splitext(os.path.basename(caller_script))[0]

    date = datetime.now().strftime("%Y/%m")
    folder_name = os.path.join("log", date, script_name)
    os.makedirs(folder_name, exist_ok=True)

    log_file = log_file or os.path.join(
        folder_name, f"{datetime.now().strftime('%d')}.log"
    )

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)

    if logger.hasHandlers():
        logger.handlers.clear()

    log_directory = "log"
    if not os.path.exists(log_directory):
        os.makedirs(log_directory)

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = ColoredFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def log_message(message, level="INFO"):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]
    logger = setup_logger()
    getattr(logger, level.lower())(f"[{timestamp}] {message}")
