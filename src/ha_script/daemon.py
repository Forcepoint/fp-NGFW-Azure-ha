import os
import sys
import ctypes
import ctypes.util
import logging
import subprocess
from contextlib import suppress
from typing import Any, NoReturn
from signal import SIGINT, SIGTERM, signal

import ha_script

running = True

LOGGER = logging.getLogger(__name__)


def signal_handler(signal_received: int, _: Any) -> None:
    LOGGER.info("Signal received: %d", signal_received)
    global running
    running = False


def disable_service_and_exit(exit_status: int) -> NoReturn:
    LOGGER.info("Disabling script 'user_hook' from msvc")
    subprocess.call(["/bin/msvc", "-d", "user_hook"])  # noqa: S603
    sys.exit(exit_status)


def install_signal_handlers() -> None:
    signal(SIGINT, signal_handler)
    signal(SIGTERM, signal_handler)


def write_pid() -> None:
    pid_file = f"/var/run/{ha_script.SCRIPT_NAME}.pid"
    pid = os.getpid()
    with open(pid_file, "w") as fp:  # noqa: PTH123
        fp.write(str(pid))
    LOGGER.info("pid=%d in file %s", pid, pid_file)


def cleanup_pid() -> None:
    pid_file = f"/var/run/{ha_script.SCRIPT_NAME}.pid"
    with suppress(Exception):
        os.remove(pid_file)  # noqa: PTH107


def die_with_parent() -> None:
    """make the script receive a SIGTERM when the parent dies.

    This is needed because apparently 'user_hook' does not terminate the
    run-at-boot script when 'msvc -d' is called
    """
    libname = ctypes.util.find_library("c")

    if not libname:
        LOGGER.error("Cannot find libc. msvc -d/-r will not work correctly.")
        return

    libc = ctypes.CDLL(libname)
    pr_set_pdeathsig = 1
    libc.prctl(pr_set_pdeathsig, SIGTERM)


def is_running() -> bool:
    return running
