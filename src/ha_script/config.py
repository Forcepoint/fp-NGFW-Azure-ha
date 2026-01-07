import io
import sys
import logging
import ipaddress
from dataclasses import dataclass
from typing import Any, Union

import ha_script
from ha_script.exceptions import HAScriptConfigError


LOGGER = logging.getLogger(__name__)


@dataclass
class HAScriptConfig:
    """config options for the HA script"""

    # Azure route table name containing the route that sends traffic
    # from customer subnet(s) to NGFW. Can be comma-separated for
    # multiple route tables.
    route_table_id: str

    # Azure VM names of the primary and secondary NGFWs. Both
    # properties must be declared on both primary and secondary.
    primary_instance_id: str
    secondary_instance_id: str

    # False to disable tcp probing from secondary to primary
    probe_enabled: bool = True

    # the comma-separated list of private ip addresses of the primary
    # ngfw used for probing. Optional. If unspecified, first ip of the
    # primary NICs will be used.  If none of these addresses responds
    # to the probe, the secondary will take over by changing the cloud
    # route table to the local protected network. The assumption is
    # that if the primary could not respond to probe, it is
    # dead. (network failure between primary and secondary is not
    # considered)
    probe_ip: str = ""

    # the TCP port used by the secondary to probe the primary (TCP
    # connections to this port must be allowed in the policy of the
    # primary)
    probe_port: int = 22

    # False to disable tcp probing from primary to remote host
    remote_probe_enabled: bool = False

    # the comma-separated list of remote site private ip addresses
    # that the primary ngfw probes periodically to make sure the vpn
    # tunnel is still up.
    # If none of these addresses responds to the probe, the primary
    # will hand off to the secondary by putting itself offline
    # Mandatory if remote_probe_enabled is true
    remote_probe_ip: str = ""

    # remote port to probe (see explanations for remote_probe_ip)
    remote_probe_port: int = 80

    # timeout in seconds after an attempt by the secondary to connect
    # to the primary is declared failed
    probe_timeout_sec: int = 2
    # number of consecutive failed attempts by the secondary to
    # connect to the primary before starting the switchover procedure
    # (the time will be probe_max_fail*check_interval_sec)
    probe_max_fail: int = 10

    # the facility used by this script to send events to the SMC
    log_facility: int = -1  # default SMCEventFacility.USER_DEFINED.

    # periodic interval in seconds for both primary and secondary to
    # check status
    check_interval_sec: int = 1
    # internal nic index that receives the traffic from the route
    # table, defaults to 0
    internal_nic_idx: int = 0

    # WAN nic index that receives public traffic from internet defaults to 0
    wan_nic_idx: int = 1

    # Reserved public IP name used for movable IP. Requires wan_nic_idx.
    reserved_public_ip_id: str = ""

    # set to true to disable the script
    disabled: bool = False

    # set to true or as a module name to turn debug level logging
    debug: Union[bool, str] = False

    # status of the engine
    status: str = ""

    # set to true to run in dry-run mode, no changes to the system are made
    dry_run: bool = False


MANDATORY_PROPERTIES = [
    "route_table_id",
    "primary_instance_id",
    "secondary_instance_id",
    "internal_nic_idx",
]

# ignore se_script_path and legacy properties
IGNORED_PROPERTIES = [
    "se_script_path",
    "vpn_broker_url",
    "vpn_broker_password",
    "primary_engine_name",
    "secondary_engine_name",
    "request_timeout_sec",
    "change_metrics_enabled",
    "uninstall"  # Note: This property is only used by the installer script.
]


def _read_custom_properties_file() -> dict[str, Any]:
    """Read custom properties file.

    The config file name is derived from the script name, typically
    /data/run-at-boot_allow

    :return: config file dictionary
    """
    script_file = sys.argv[0]
    config_file = f"{script_file}_allow"
    result = {}
    with io.open(config_file, encoding="utf-8") as f:  # noqa: UP020
        for line in f:
            (key, value) = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if key in IGNORED_PROPERTIES:
                LOGGER.warning(f"Ignoring property '{key}'.")
                continue
            result[key] = value
    return result


def _validate_config(config_data: dict[str, Any]) -> None:
    """Check if mandatory config parameters are present.

    :raise HAScriptConfigError: if config validation fails
    """
    if not config_data:
        raise HAScriptConfigError("custom config is empty")

    for key in MANDATORY_PROPERTIES:
        if (
            key not in config_data
            or config_data[key] == ""
            or config_data[key] is None
        ):
            raise HAScriptConfigError(f"Mandatory property is missing: {key}")

    if not config_data["route_table_id"].startswith(
        "/subscriptions/"
    ):
        raise HAScriptConfigError(
            f"Value for 'route_table_id' should start with "
            f"'/subscriptions/': "
            f"{config_data['route_table_id']}"
        )

    reserved_public_ip_id = config_data.get(
        "reserved_public_ip_id"
    )
    if reserved_public_ip_id and not (
        reserved_public_ip_id.startswith("/subscriptions/")
    ):
        raise HAScriptConfigError(
            f"Value for 'reserved_public_ip_id' should start "
            f"with '/subscriptions/': "
            f"{reserved_public_ip_id}"
        )

    if config_data.get("probe_ip"):
        for addr in config_data["probe_ip"].split(","):
            addr = addr.strip()
            try:
                ipaddress.ip_address(addr)
            except ValueError:
                raise HAScriptConfigError(
                    f"Value for 'probe_ip' is not an IP address: {addr}"
                )

    if (
        config_data.get("remote_probe_enabled")
        and not config_data.get("remote_probe_ip")
    ):
        raise HAScriptConfigError(
            "Mandatory property is missing: remote_probe_ip"
        )

    if config_data.get("remote_probe_ip"):
        for addr in config_data["remote_probe_ip"].split(","):
            addr = addr.strip()
            try:
                ipaddress.ip_address(addr)
            except ValueError:
                raise HAScriptConfigError(
                    f"Value for 'remote_probe_ip' is not an IP address: "
                    f"{addr}"
                )


def load_config(tags: dict[str, Any]) -> HAScriptConfig:
    """Load config from cloud tags and/or SMC custom properties file.

    Customer properties and cloud tags are merged.  Azure VM tags take
    precedence.

    :param tags: cloud tags.
    :return HAScriptConfig: validated configuration object
    """
    config_data = _read_custom_properties_file()
    config_data.update(tags)

    # Sanitize configuration values:
    # - enforce unicode
    # - cast string containing integers to int
    # - cast string containing booleans to bool
    for key in config_data:
        value = config_data[key]
        if isinstance(value, bytes):
            value = value.decode("utf8")
            config_data[key] = value

        if not isinstance(value, str):
            continue

        if key in (
            "probe_port", "remote_probe_port", "probe_timeout_sec",
            "probe_max_fail", "log_facility", "check_interval_sec",
            "internal_nic_idx", "wan_nic_idx"
        ):
            config_data[key] = int(value)

        # Debug can either be turned globally on or for a specific module
        if key == "debug":
            # True values turn the debug on globally
            if value.lower() == "true":
                config_data[key] = True
            # Specific module debug
            elif value.startswith(f"{ha_script.SCRIPT_NAME}."):
                config_data[key] = value

        if key in ("probe_enabled", "remote_probe_enabled",
                   "disabled", "dry_run"):
            config_data[key] = value.lower() == "true"

    _validate_config(config_data)
    config = HAScriptConfig(**config_data)
    LOGGER.info("Config loaded, config: %s", config)
    return config
