"""script to provide ha for a pair of ngfw instances firewall on cloud"""
import sys
import logging
import argparse
import datetime

import ha_script
import ha_script.config
import ha_script.mainloop
import ha_script.exceptions
import ha_script.ngfw_utils as ngfw
import ha_script.azure.api as api
import ha_script.daemon as daemon
import ha_script.smc_events as smc


__VERSION__ = "1.0.1"
LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments of the script.

    :return: parsed arguments
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter, description="HA script"
    )
    parser.add_argument(
        "-v", "--version", action="store_true", help="show script version"
    )
    parser.add_argument(
        "-c",
        "--console",
        action="store_true",
        help="run as normal app instead of daemon",
    )
    parser.add_argument(
        "-d", "--debug", action="store_true", help="debug logs"
    )
    args = parser.parse_args()
    return args


def main() -> None:
    """Entry point of the script."""
    now = datetime.datetime.now()
    date_time = now.strftime("%Y%m%d")
    log_file = f"/data/diagnostics/cloud-ha-{date_time}.log"

    script_info = "{}, file: {}, version: {}".format(
        ha_script.SCRIPT_NAME,
        __file__,
        __VERSION__,
    )

    args = parse_args()
    if args.version:
        print(script_info)
        sys.exit(1)

    ha_script.configure_logging(log_file, args.console, args.debug)
    LOGGER.info(f"Script started: {script_info}")

    daemon.die_with_parent()
    daemon.write_pid()
    daemon.install_signal_handlers()
    ngfw.configure_ca_cert()

    clients, config, role, tags = None, None, None, {}

    try:
        clients = api.get_azure_clients()
        tags = api.get_config_tags(clients)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception(
            "HA not starting. Failed with exception.", exc_info=True
        )
        smc.send_error_to_smc(
            config, f"HA not starting. Failed to get cloud tags: {exc}"
        )
        daemon.disable_service_and_exit(1)

    try:
        config = ha_script.config.load_config(tags)
        role = "primary" if ngfw.is_primary(config) else "secondary"
    except OSError as io_error:
        smc.send_error_to_smc(
            config, f"HA not starting. Failed to read config: {io_error}"
        )
        daemon.disable_service_and_exit(1)
    except ha_script.exceptions.HAScriptConfigError as config_error:
        smc.send_error_to_smc(
            config, f"HA not starting. Invalid config: {config_error}"
        )
        daemon.disable_service_and_exit(1)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception(
            "HA not starting. Failed with exception.", exc_info=True
        )
        smc.send_error_to_smc(config, f"HA not starting. Script exited: {exc}")
        daemon.disable_service_and_exit(1)

    smc.send_notification_to_smc(
        config, f"Script started: {script_info}, role: {role}"
    )

    if config.dry_run:
        LOGGER.warning("DRY-RUN: No changes will be made to the system.")

    if config.disabled:
        LOGGER.info("Script 'run-at-boot' is disabled. Exiting.")
        daemon.disable_service_and_exit(0)

    if config.debug:
        if isinstance(config.debug, str):
            logging.getLogger(config.debug).setLevel(logging.DEBUG)
        else:
            ha_script.LOGGER.setLevel(logging.DEBUG)

    try:
        ha_script.mainloop.mainloop(config, clients)
    except KeyboardInterrupt:
        LOGGER.warning("Leaving script.")
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception(
            "HA not working. Failed with exception.", exc_info=True
        )
        smc.send_error_to_smc(config, f"HA not working. Script exited: {exc}")

    daemon.cleanup_pid()
    smc.send_notification_to_smc(
        config, f"Script terminated: {script_info}, role: {role}"
    )


if __name__ == "__main__":
    main()
