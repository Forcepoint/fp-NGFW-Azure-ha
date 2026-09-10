import time
import logging
from typing import List, Optional

import ha_script.azure.api as api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.daemon import is_running
from ha_script.ngfw_utils import (
    get_local_status,
    get_primary_status,
    is_primary,
    set_local_status,
)
from ha_script.smc_events import send_error_to_smc, send_notification_to_smc
from ha_script.tcp_probing import tcp_probe


LOGGER = logging.getLogger(__name__)


def _public_ip_assigned_to_target(
    assignee_id: Optional[str],
    target_ip_config_id: str
) -> bool:
    """Check if a public IP is assigned to its target IP configuration.

    :param assignee_id: ipConfiguration ID from the public IP
    :param target_ip_config_id: target NIC ipConfiguration ID
    :return: True if the public IP is assigned to the target
    """
    if not assignee_id:
        return False
    return api.same_resource_id(assignee_id, target_ip_config_id)


def get_primary_probe_ip_addresses(config: HAScriptConfig,
                                   clients: api.AzureClients) -> List[str]:
    """Get the IP addresses for probing the primary engine.

    The IP addresses are taken either from the user config or by taking the
    first ip address of each NIC attached to the primary engine.

    :param config: HAScriptConfig object
    :param clients: Azure clients
    :return: list of IP addresses for probing the primary engine
    """
    ip_addresses = (
        config.probe_ip.split(",")
        if config.probe_ip else api.get_instance_ip_addresses(
            clients,
            config.primary_instance_id
        )
    )
    return ip_addresses


def primary_check_remote_hosts(config: HAScriptConfig,
                               ctx: HAScriptContext,
                               local_net_ctx: api.LocalNetContext) -> bool:
    """Probe remote hosts to check the VPN tunnel is up.

    The probe only means something once the primary carries traffic. While it
    was not active on the previous iteration (startup, or a failover still
    moving routes and public IPs back), the probe is skipped and a grace
    window (remote_probe_grace_sec) is opened. Once active, failing probes are
    tolerated until the window's first success or expiry, then probe_max_fail
    handling resumes. In dry-run mode the skip and the grace window do not
    apply: the probe runs on every iteration.

    :param config: HAScriptConfig object
    :param ctx: HAScriptContext object
    :param local_net_ctx: LocalNetContext with the remote probe source address
    :return: True on reach, when probing is disabled, or on a tolerated failure
    """
    if not config.remote_probe_enabled:
        return True

    # Not active last iteration: routes and IPs are still moving back, so a
    # probe would just time out. Skip it and open the grace window. In
    # dry-run mode prev_local_active is never updated, so this gate would
    # skip the probe forever; probe unconditionally instead.
    if not config.dry_run and ctx.prev_local_active is not True:
        if (
            config.remote_probe_grace_sec > 0 and
            ctx.probe_grace_deadline is None
        ):
            ctx.probe_grace_deadline = (
                time.monotonic() + config.remote_probe_grace_sec
            )
            ctx.probe_fail_count = 0
            LOGGER.info("Remote probe grace period armed for %ds.",
                        config.remote_probe_grace_sec)
        return True

    probe_ok = tcp_probe(config, config.remote_probe_ip.split(","),
                         config.remote_probe_port, ctx,
                         source_ip=local_net_ctx.remote_probe_src_ip)

    deadline = ctx.probe_grace_deadline
    if deadline is None:
        return probe_ok

    if probe_ok and ctx.probe_fail_count == 0:
        LOGGER.info("Remote probe succeeded; ending grace period.")
        ctx.probe_grace_deadline = None
        return True
    if time.monotonic() < deadline:
        return True
    # Window expired without success: clear the counter so normal probing
    # resumes with a full probe_max_fail budget rather than the partial count
    # carried over from the window.
    LOGGER.warning("Remote probe grace period expired without a successful "
                   "probe; resuming probe_max_fail handling.")
    ctx.probe_grace_deadline = None
    ctx.probe_fail_count = 0
    return True


def primary_main_loop_handler(config: HAScriptConfig, clients: api.AzureClients,
                              ctx: HAScriptContext,
                              local_net_ctx: api.LocalNetContext) -> None:
    """Mainloop for the primary engine.

    Logic:
    - Notifies (via cloud tags) the secondary engine when the primary goes
      offline or online.
    - Goes offline if all the remote IP addresses are unreachable.
    - Always tries to re-route traffic to itself, if it is online.

    :param config: HAScriptConfig object
    :param clients: Azure clients
    :param ctx: HAScriptContext object
     Track changes since the last iteration:
       - prev_local_status: Last known admin status ("online"/"offline").
       - prev_local_active: Boolean. True if this engine was active at the
                            previous iteration.
     Global access to config:
       - route_table_id
    :param local_net_ctx: LocalNetContext object
      Track several IDs and IPs of the local network stack. Used to interact
      with the cloud APIs.
    """
    local_status = get_local_status()
    if not local_status:
        send_error_to_smc(
            config,
            "Failed to get local status. HA script is not working properly."
        )
        return

    if local_status != "online":
        # An offline primary has no failback in progress; drop any armed
        # grace window so the next failback arms a fresh one.
        ctx.probe_grace_deadline = None

    if ctx.prev_local_status is None:
        ctx.prev_local_status = local_status

    if ctx.prev_local_status != local_status:
        LOGGER.info("Notify secondary engine about status change: %s -> %s",
                    ctx.prev_local_status, local_status)
        # We change the previous status only in case of success, so that if we
        # fail to set the tag, it has a chance to succeed on next iteration.
        if api.set_config_tag(config, clients, "status", local_status):
            ctx.prev_local_status = local_status
            ctx.display_info_needed = True

    public_ips_to_move: list[tuple[str, str, str]] = []
    for pub_id, target_id, ip_addr in local_net_ctx.public_ip_targets:
        try:
            assignee = api.resolve_public_ip(clients, pub_id)
        except Exception as e:
            LOGGER.warning("Failed to resolve public IP '%s': %s", ip_addr, e)
            continue
        if local_status == "online" and not _public_ip_assigned_to_target(
            assignee, target_id
        ):
            public_ips_to_move.append((pub_id, target_id, ip_addr))

    if (
        not public_ips_to_move and
        local_status == "online" and
        not primary_check_remote_hosts(config, ctx, local_net_ctx)
    ):
        # We failed to reach all the configured remote IP addressed several
        # times (see config.probe_max_fail). We set the primary offline so that
        # the secondary takes over. In case public IP(s) need to be moved,
        # delay remote check to next iteration as remote is unreachable now.
        local_status = "offline"
        set_local_status(config, local_status)
        send_notification_to_smc(
            config,
            f"Primary '{config.primary_instance_id}' changed to offline as "
            f"remote probe failed to reach hosts '{config.remote_probe_ip}'.",
            alert=True)
        ctx.display_info_needed = True
        return

    # Iterate only over routes that use NGFW.
    ngfw_instance_ids = [
        config.primary_instance_id,
        config.secondary_instance_id
    ]

    for route_info in api.get_route_table_info(clients, config.route_table_id,
                                               ngfw_instance_ids):
        local_is_active = route_info.target_ip == local_net_ctx.internal_ip

        if local_is_active != ctx.prev_local_active:
            if local_status == "online" and not local_is_active:
                # Change detected: primary was active (processing traffic) and
                # secondary took over. This happens for instance because the
                # TCP probe from the secondary failed too many times.
                #
                # We turn the primary engine offline so that:
                # - The VPN tunnel is closed and VPN traffic goes to secondary
                #   (for engine version >= 6.10).
                # - The primary engine does not attempt to get back the traffic
                #   on next tick (ping-pong effect).
                #
                # Setting the node online requires human intervention
                # (from the smc or using "sg-cluster" command).
                local_status = "offline"
                set_local_status(config, local_status)
                send_notification_to_smc(
                    config,
                    f"Primary '{config.primary_instance_id}' address "
                    f"'{local_net_ctx.internal_ip}' is no longer active, "
                    "state changed to offline.",
                    alert=True
                )
            if not config.dry_run:
                ctx.prev_local_active = local_is_active
            ctx.display_info_needed = True

        need_reroute = local_status == "online" and not local_is_active

        LOGGER.debug(
            "Primary mainloop, route_table_id: %s, route_dest: %s, "
            "local_status: %s, local_is_active: %s, route_state: %s",
            route_info.route_table_id, route_info.route_dest, local_status,
            local_is_active, route_info.route_state
        )

        if ctx.display_info_needed or need_reroute:
            LOGGER.info(
                "route_table_id: %s, route_dest: %s, route_state: %s, "
                "route_table_target: %s, local_ip: %s, primary_status: %s, "
                "primary: %s",
                route_info.route_table_id, route_info.route_dest,
                route_info.route_state, route_info.target_ip,
                local_net_ctx.internal_ip, local_status,
                "active" if local_is_active else "not active"
            )
            ctx.display_info_needed = False

        if not need_reroute:
            continue

        ctx.display_info_needed = True

        if api.update_route_table(config, clients, route_info.route_table_id,
                                  route_info.route_dest, local_net_ctx):
            send_notification_to_smc(
                config,
                f"Route table '{route_info.route_table_id}' changed route "
                f"to '{route_info.route_dest}' via primary "
                f"'{local_net_ctx.internal_ip}'.",
                alert=True
            )

    for pub_id, target_id, ip_addr in public_ips_to_move:
        if api.move_public_ip(config, clients, pub_id, target_id):
            send_notification_to_smc(
                config,
                f"Public IP address '{ip_addr}' moved to primary "
                f"'{config.primary_instance_id}'.",
                alert=True
            )


def secondary_main_loop_handler(config: HAScriptConfig,
                                clients: api.AzureClients,
                                ctx: HAScriptContext,
                                local_net_ctx: api.LocalNetContext) -> None:
    """Monitors the primary engine.

    Logic:
     - Checks the primary admin status (online/offline) shared via Azure VM
       tags.
     - Checks health via periodic attempts to connect to SSH of the primary.
     - Checks routing status reported by cloud.

    It will re-route traffic from protected network to itself, if the primary
    is unreachable or offline. This involves changing the cloud route table to
    itself.

    :param config: HAScriptConfig object
    :param clients: Azure clients
    :param ctx: HAScriptContext object
     Track changes since the last iteration:
       - prev_local_status: Last known admin status ("online"/"offline").
       - prev_local_active: Boolean. True if this engine was active at the
                            previous iteration.
     Global access to config:
       - route_table_id
    :param local_net_ctx: LocalNetContext object
      Track several IDs and IPs of the local network stack. Used to interact
      with the cloud APIs.
    """
    local_status = get_local_status()
    if not local_status:
        send_error_to_smc(
            config,
            "Failed to get local status. HA script is not working properly."
        )
        return

    if ctx.prev_local_status is None:
        ctx.prev_local_status = local_status

    if ctx.prev_local_status != local_status:
        ctx.prev_local_status = local_status
        ctx.display_info_needed = True

    primary_status = get_primary_status(config, clients)
    # Fails to get primary status? No action needed:
    # - "primary_status" value is "unknown" (logged if value has changed).
    # - We can continue to check at least for health of the primary.

    if ctx.prev_primary_status != primary_status:
        ctx.prev_primary_status = primary_status
        ctx.display_info_needed = True

    # "tcp_probe_fails" will be set to True if the SSH connection to
    # primary on port 22 fails 10 times in a row.

    # We evaluate this SSH probe to primary only for the first route
    # (assuming all the routes have the same primary).

    # The evaluation is done in the loop because at this point we do
    # not have the address of the primary.
    primary_ip_addresses = get_primary_probe_ip_addresses(config, clients)
    tcp_probe_fails = config.probe_enabled and not tcp_probe(
        config, primary_ip_addresses, config.probe_port, ctx)

    public_ips_to_move: list[tuple[str, str, str]] = []
    for pub_id, target_id, ip_addr in local_net_ctx.public_ip_targets:
        try:
            assignee = api.resolve_public_ip(clients, pub_id)
        except Exception as e:
            LOGGER.warning("Failed to resolve public IP '%s': %s", ip_addr, e)
            continue
        if (local_status == "online"
                and not _public_ip_assigned_to_target(assignee, target_id)
                and (tcp_probe_fails or primary_status == "offline")):
            public_ips_to_move.append((pub_id, target_id, ip_addr))

    ngfw_instance_ids = [
        config.primary_instance_id,
        config.secondary_instance_id
    ]

    # Iterate only over routes that use NGFW.
    for route_info in api.get_route_table_info(clients, config.route_table_id,
                                               ngfw_instance_ids):
        # The active engine is the one processing traffic (i.e. the engine that
        # the route table points to).
        local_is_active = route_info.target_ip == local_net_ctx.internal_ip

        if ctx.prev_local_active != local_is_active:
            ctx.display_info_needed = True
            ctx.prev_local_active = local_is_active

        need_reroute = (
            local_status == "online"
            and not local_is_active
            and (
                tcp_probe_fails
                or route_info.route_state == "blackhole"
                or primary_status == "offline"
            )
        )

        LOGGER.debug(
            "Secondary mainloop, route_table_id: %s, route_dest: %s, "
            "local_status: %s, local_is_active: %s, tcp_probe_fails: %s, "
            "route_state: %s, primary_status: %s",
            route_info.route_table_id, route_info.route_dest, local_status,
            local_is_active, tcp_probe_fails, route_info.route_state,
            primary_status
        )

        if ctx.display_info_needed or need_reroute or public_ips_to_move:
            LOGGER.info(
                "route_table_id: %s, route_dest: %s, route_state: %s, "
                "route_table_target: %s, local_ip: %s, primary_status: %s, "
                "secondary_status: %s, secondary: %s",
                route_info.route_table_id, route_info.route_dest,
                route_info.route_state, route_info.target_ip,
                local_net_ctx.internal_ip, primary_status, local_status,
                "active" if local_is_active else "not active"
            )
            ctx.display_info_needed = False

        if not need_reroute:
            continue

        ctx.display_info_needed = True

        if api.update_route_table(config, clients, route_info.route_table_id,
                                  route_info.route_dest, local_net_ctx):
            send_notification_to_smc(
                config,
                f"Route table '{route_info.route_table_id}' changed route "
                f"to '{route_info.route_dest}' via secondary "
                f"'{local_net_ctx.internal_ip}'.",
                alert=True
            )

    for pub_id, target_id, ip_addr in public_ips_to_move:
        if api.move_public_ip(config, clients, pub_id, target_id):
            send_notification_to_smc(
                config,
                f"Public IP address '{ip_addr}' moved to "
                f"secondary '{config.secondary_instance_id}'.",
                alert=True
            )


def mainloop(config: HAScriptConfig, clients: api.AzureClients) -> None:
    """Loop forever

    Expected exceptions (e.g. Azure API) are caught and do not exit the
    mainloop. Unexpected exceptions must be handled by the caller.
    """
    primary = is_primary(config)
    main_loop_handler = (
        primary_main_loop_handler if primary else secondary_main_loop_handler
    )

    LOGGER.info("Role is '%s'", "primary" if primary else "secondary")

    ctx = HAScriptContext()
    local_net_ctx = api.create_local_net_context(config, clients, primary)

    while is_running():
        try:
            main_loop_handler(config, clients, ctx, local_net_ctx)
        except Exception as exc:
            LOGGER.exception("Got unexpected exception.")
            send_error_to_smc(
                config,
                f"HA not working. Unexpected exception: {exc}"
            )
        finally:
            time.sleep(config.check_interval_sec)
