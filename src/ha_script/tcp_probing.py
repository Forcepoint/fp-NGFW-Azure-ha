import socket
import logging
from typing import List

from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext


LOGGER = logging.getLogger(__name__)


def tcp_probe(config: HAScriptConfig, ip_addresses: List[str], port: int,
              ctx: HAScriptContext, source_ip: str = "") -> bool:
    """Attempt to open a connection to the given IP addresses and port.

    The function uses config parameters:
    - probe_max_fail
    - config.probe_timeout_sec

    The function uses context parameter:
    - ctx.probe_fail_count to remember the number of consecutive past failures

    :param config: The configuration from the main program
    :param ip_addresses: the address to try to connect to
    :param port: the port to try to connect to
    :param ctx: dict with a probe_fail_count key
    :param source_ip: local address to bind as the connection source;
        empty string leaves source address selection to the OS

    :return: True
    - if at least one connection to any ip_addresses:port is successful or
    - if the number of max consecutive failure (10 by default)
      has not been reached.
    """
    for ip_address in ip_addresses:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(config.probe_timeout_sec)
        if source_ip:
            try:
                s.bind((source_ip, 0))
            except OSError:
                LOGGER.exception(
                    "TCP probe cannot bind source address, source_ip: %s",
                    source_ip
                )
                s.close()
                # Every remaining address would fail to bind the same
                # way, so there is nothing left to try.
                break
        try:
            s.connect((ip_address, port))
            ctx.probe_fail_count = 0
            LOGGER.debug(
                "TCP probe ok, ip_address: %s, port: %d, source_ip: %s",
                ip_address,
                port,
                source_ip
            )
        except OSError:
            # We typically receive a TimeoutError, which is a subclass of
            # OSError (see https://docs.python.org/3/library/socket.html).
            if ctx.probe_fail_count == 0:
                LOGGER.exception(
                    "TCP probing failed, ip_address: %s, port: %d, "
                    "source_ip: %s",
                    ip_address,
                    port,
                    source_ip,
                    exc_info=True
                )
        else:
            return True
        finally:
            s.close()

    probe_fail_count = ctx.probe_fail_count
    probe_max_fail = config.probe_max_fail
    LOGGER.debug(
        "probe_fail_count: %d, probe_max_fail: %d",
        probe_fail_count,
        probe_max_fail
    )
    if probe_fail_count < probe_max_fail:
        probe_fail_count += 1
        ctx.probe_fail_count = probe_fail_count
        LOGGER.debug(
            "TCP probe failed, ip_addresses: %s, port: %d, attempts: %d",
            ip_addresses,
            port,
            probe_fail_count
        )
        return True

    ctx.probe_fail_count = 0
    LOGGER.warning(
        "TCP probe failed, ip_addresses: %s, port: %d, attempts: %d",
        ip_addresses,
        port,
        probe_max_fail
    )
    return False
