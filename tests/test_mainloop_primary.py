"""
Tests for primary mainloop in Azure environment.

This module tests the primary engine's main loop logic including:
- Staying online when active
- Switching from offline to online
- Going offline when secondary takes over
- Notifying secondary of status changes
"""
import logging
import time

from unittest.mock import Mock, patch

from conftest import AzureConf

from ha_script.azure import api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.mainloop import (
    primary_check_remote_hosts,
    primary_main_loop_handler,
)


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.update_route_table")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.azure.api.set_config_tag")
def test_online(
    set_config_tag,
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
    caplog,
):
    """Basic case: primary was active/online and still is"""
    caplog.set_level(logging.INFO)

    clients = (azure_conf.compute_client, azure_conf.network_client)
    primary_ip = azure_conf.primary_ips[0]
    primary_vnic_id = azure_conf.primary_nic_ids[0]
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )
    ctx = HAScriptContext(
        prev_local_status="online", prev_local_active=True, display_info_needed=True
    )

    local_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        public_ip_targets=[]
    )
    create_local_net_context.return_value = local_net_ctx

    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            "",
            primary_ip,
            primary_vnic_id,
            azure_conf.protected_route_table_name
        )
    ]
    get_local_status.return_value = "online"

    primary_main_loop_handler(config, clients, ctx, local_net_ctx)

    logmsgs = [r.message for r in caplog.records]
    assert any(
        "route_table_id:" in msg and
        "route_dest: 0.0.0.0/0" in msg and
        "route_state: ACTIVE" in msg and
        primary_ip in msg and
        "primary_status: online" in msg and
        "primary: active" in msg
        for msg in logmsgs
    )

    # Make sure no rerouting takes place
    assert len(send_notification_to_smc.mock_calls) == 0
    assert len(update_route_table.mock_calls) == 0
    assert len(set_config_tag.mock_calls) == 0

    assert ctx.prev_local_status == "online"
    assert ctx.prev_local_active
    assert not ctx.display_info_needed


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.update_route_table")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.azure.api.set_config_tag")
def test_offline_to_online_success(
    set_config_tag,
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
    caplog,
):
    """Primary was offline, becomes online. Needs to change the routing table
    to get the traffic again"""
    caplog.set_level(logging.INFO)

    primary_ip = azure_conf.primary_ips[0]
    secondary_ip = azure_conf.secondary_ips[0]
    primary_vnic_id = azure_conf.primary_nic_ids[0]

    # Since we were offline, the secondary has the traffic
    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            "",
            secondary_ip,
            azure_conf.secondary_nic_names[0],
            azure_conf.protected_route_table_name
        )
    ]
    get_local_status.return_value = "online"

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        public_ip_targets=[]
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure the standby is notified via tag change
    set_config_tag.assert_called_once_with(config, clients, "status", "online")

    update_route_table.assert_called_once_with(
        config, clients, azure_conf.protected_route_table_name, "0.0.0.0/0",
        local_net_ctx
    )

    # Make sure the SMC is notified
    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{azure_conf.protected_route_table_name}' changed route to "
        f"'0.0.0.0/0' via primary '{primary_ip}'.",
        alert=True)

    assert ctx.prev_local_status == "online"

    # The prev_local_active is still False: the reason is the
    # route change has been requested, but we have not yet checked
    # that it succeeded
    assert not ctx.prev_local_active


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.get_route_table_info", wraps=api.get_route_table_info)
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.azure.api.set_config_tag")
def test_offline_to_online_success_with_azure_mock(
    set_config_tag: Mock,
    create_local_net_context,
    get_route_table_info: Mock,
    get_local_status: Mock,
    get_primary_status: Mock,
    tcp_probe: Mock,
    send_notification_to_smc: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Primary was offline, becomes online. Needs to change the routing table.
    Same test as previous but using actual Azure mock state"""
    caplog.set_level(logging.INFO)

    primary_ip = azure_conf.primary_ips[0]
    primary_vnic_id = azure_conf.primary_nic_ids[0]
    secondary_ip = azure_conf.secondary_ips[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        public_ip_targets=[]
    )
    create_local_net_context.return_value = primary_net_ctx

    # The secondary has traffic initially - update the mock route table
    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.secondary_nic_ids[0],
        internal_ip=secondary_ip,
        public_ip_targets=[]
    )
    api.update_route_table(config, clients, azure_conf.protected_route_table_name,
                           "0.0.0.0/0", secondary_net_ctx)

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # Make sure the standby is notified via tag change
    set_config_tag.assert_called_once_with(config, clients, "status", "online")

    # Make sure route table was updated
    route_table = azure_conf.network_client.get_route_table(azure_conf.resource_group, azure_conf.protected_route_table_name)
    default_route = next(r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '0.0.0.0/0')
    assert default_route['properties']['nextHopIpAddress'] == azure_conf.primary_ips[0]

    # Make sure 'other_route' still goes via other NIC
    other_route = next(r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '192.168.0.0/24')
    assert other_route['properties']['nextHopIpAddress'] == azure_conf.other_ip

    # Make sure the SMC is notified
    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{azure_conf.protected_route_table_name}' changed route to '0.0.0.0/0' "
        f"via primary '{primary_ip}'.",
        alert=True)

    assert ctx.prev_local_status == "online"

    # The prev_local_active is still False: the reason is the
    # route change has been requested, but we have not yet checked
    # that it succeeded
    assert not ctx.prev_local_active


@patch("subprocess.call")
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.update_route_table")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.azure.api.set_config_tag")
def test_secondary_takeover(
    set_config_tag,
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    subprocess_call,
    azure_conf: AzureConf,
    caplog,
):
    """Primary was online, detects that secondary has taken over and goes offline"""
    caplog.set_level(logging.INFO)

    subprocess_call.return_value = 0
    primary_ip = azure_conf.primary_ips[0]
    primary_vnic_id = azure_conf.primary_nic_ids[0]
    secondary_ip = azure_conf.secondary_ips[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    ctx = HAScriptContext(
        prev_local_status="online", prev_local_active=True, display_info_needed=True
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        public_ip_targets=[]
    )
    create_local_net_context.return_value = local_net_ctx

    # The secondary has the traffic
    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            "",
            secondary_ip,
            azure_conf.secondary_nic_names[0],
            azure_conf.protected_route_table_name
        )
    ]
    get_local_status.return_value = "online"

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, local_net_ctx)

    subprocess_call.assert_called_once_with(["/usr/sbin/sg-cluster", "offline"])
    assert not ctx.prev_local_active
    assert ctx.prev_local_status == "online"  # will be set offline on next iteration

    # Make sure no rerouting takes place
    assert len(update_route_table.mock_calls) == 0

    send_notification_to_smc.assert_called_once_with(
        config,
        f"Primary '{azure_conf.primary_vm_name}' address '{primary_ip}' is no longer active, "
        "state changed to offline.",
        alert=True)


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.update_route_table")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.azure.api.set_config_tag")
def test_online_to_offline_success(
    set_config_tag,
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
    caplog,
):
    """Primary was online, becomes offline. Only action is to notify standby who will takeover"""
    caplog.set_level(logging.INFO)

    primary_ip = azure_conf.primary_ips[0]
    primary_vnic_id = azure_conf.primary_nic_ids[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    ctx = HAScriptContext(
        prev_local_status="online", prev_local_active=True, display_info_needed=True
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        public_ip_targets=[]
    )
    create_local_net_context.return_value = local_net_ctx

    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            "",
            primary_ip,
            primary_vnic_id,
            azure_conf.protected_route_table_name
        )
    ]
    get_local_status.return_value = "offline"

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure the standby is notified via tag change
    set_config_tag.assert_called_once_with(config, clients, "status", "offline")

    # Make sure no rerouting takes place
    assert len(send_notification_to_smc.mock_calls) == 0
    assert len(update_route_table.mock_calls) == 0

    assert ctx.prev_local_status == "offline"
    assert ctx.prev_local_active
    assert not ctx.display_info_needed


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.update_route_table")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.azure.api.set_config_tag")
def test_fail_to_change_status(
    set_config_tag,
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
    caplog,
):
    """Primary was online, becomes offline. Changing the status tag fails.
    In this case, the prev_local_status is unchanged"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    primary_ip = azure_conf.primary_ips[0]
    primary_vnic_id = azure_conf.primary_nic_ids[0]

    ctx = HAScriptContext(
        prev_local_status="online", prev_local_active=True, display_info_needed=True
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        public_ip_targets=[]
    )
    create_local_net_context.return_value = local_net_ctx

    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            "",
            primary_ip,
            primary_vnic_id,
            azure_conf.protected_route_table_name
        )
    ]
    get_local_status.return_value = "offline"
    set_config_tag.return_value = False

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure the standby is notified via tag change
    set_config_tag.assert_called_once_with(config, clients, "status", "offline")

    # This is the important part: prev status remains "online" so that
    # the status change is retried on the next iteration
    assert ctx.prev_local_status == "online"


# ---------------------------------------------------------------------------
# primary_check_remote_hosts grace window


def _remote_probe_config() -> HAScriptConfig:
    return HAScriptConfig(
        route_table_id="/subscriptions/sub/rt",
        primary_instance_id="primary-vm",
        secondary_instance_id="secondary-vm",
        remote_probe_enabled=True,
        remote_probe_ip="198.51.100.1",
    )


def _remote_probe_net_ctx() -> api.LocalNetContext:
    return api.LocalNetContext(
        internal_nic_id="nic0",
        internal_ip="10.0.0.10",
        public_ip_targets=[],
        remote_probe_src_ip="10.0.0.10",
    )


def _fake_probe(reachable: bool, fail_count: int):
    """Stand in for tcp_probe: set the fail counter as the real one would."""
    def _probe(config, ip_addresses, port, ctx, source_ip=""):
        ctx.probe_fail_count = fail_count
        return reachable
    return _probe


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.set_config_tag")
def test_offline_clears_grace_deadline(
    set_config_tag,
    get_route_table_info,
    get_local_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
):
    """An observed offline status drops any armed grace window (e.g. left
    over from an aborted failback), so the next failback arms a fresh
    one instead of inheriting a stale, possibly expired deadline."""
    config = _remote_probe_config()
    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        probe_grace_deadline=time.monotonic() - 100,
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)
    get_local_status.return_value = "offline"
    get_route_table_info.return_value = []

    primary_main_loop_handler(config, clients, ctx, _remote_probe_net_ctx())

    assert ctx.probe_grace_deadline is None
    tcp_probe.assert_not_called()


@patch("ha_script.mainloop.tcp_probe")
def test_remote_skips_probe_while_not_active(tcp_probe):
    """Not active on the previous iteration (prev_local_active False): the
    probe is skipped (routes still moving back) and the window is armed."""
    ctx = HAScriptContext(prev_local_active=False)

    assert primary_check_remote_hosts(
        _remote_probe_config(), ctx, _remote_probe_net_ctx()
    )
    assert ctx.probe_grace_deadline is not None
    tcp_probe.assert_not_called()


@patch("ha_script.mainloop.tcp_probe")
def test_remote_skips_probe_on_startup(tcp_probe):
    """prev_local_active is None at startup: probe skipped, window armed."""
    ctx = HAScriptContext()

    assert primary_check_remote_hosts(
        _remote_probe_config(), ctx, _remote_probe_net_ctx()
    )
    assert ctx.probe_grace_deadline is not None
    tcp_probe.assert_not_called()


@patch("ha_script.mainloop.tcp_probe")
def test_remote_skips_probe_while_not_active_without_grace(tcp_probe):
    """The skip while not active applies even with the grace window disabled;
    no window is armed."""
    config = _remote_probe_config()
    config.remote_probe_grace_sec = 0
    ctx = HAScriptContext(prev_local_active=False)

    assert primary_check_remote_hosts(config, ctx, _remote_probe_net_ctx())
    assert ctx.probe_grace_deadline is None
    tcp_probe.assert_not_called()


@patch("ha_script.mainloop.tcp_probe")
def test_remote_probes_in_dry_run_while_not_active(tcp_probe):
    """Dry-run never updates prev_local_active, so the not-active skip is
    bypassed: the probe runs even at startup and no window is armed."""
    tcp_probe.side_effect = _fake_probe(reachable=True, fail_count=0)
    config = _remote_probe_config()
    config.dry_run = True
    ctx = HAScriptContext()

    assert primary_check_remote_hosts(config, ctx, _remote_probe_net_ctx())
    assert ctx.probe_grace_deadline is None
    tcp_probe.assert_called_once()


@patch("ha_script.mainloop.tcp_probe")
def test_remote_dry_run_probe_verdict_stands(tcp_probe):
    """In dry-run a failing probe verdict is returned unchanged even while
    not active; no grace tolerance applies."""
    tcp_probe.side_effect = _fake_probe(reachable=False, fail_count=3)
    config = _remote_probe_config()
    config.dry_run = True
    ctx = HAScriptContext(prev_local_active=False)

    assert not primary_check_remote_hosts(
        config, ctx, _remote_probe_net_ctx()
    )
    assert ctx.probe_grace_deadline is None


@patch("ha_script.mainloop.tcp_probe")
def test_remote_grace_under_threshold_stays_armed(tcp_probe):
    """An under-threshold failure (True, counter > 0) is tolerated and does
    not end the window."""
    tcp_probe.side_effect = _fake_probe(reachable=True, fail_count=2)
    ctx = HAScriptContext(prev_local_active=True,
                          probe_grace_deadline=time.monotonic() + 60)

    assert primary_check_remote_hosts(
        _remote_probe_config(), ctx, _remote_probe_net_ctx()
    )
    assert ctx.probe_grace_deadline is not None


@patch("ha_script.mainloop.tcp_probe")
def test_remote_grace_ends_on_success(tcp_probe):
    """The first successful probe (True with the counter back at 0) closes
    the window."""
    tcp_probe.side_effect = _fake_probe(reachable=True, fail_count=0)
    ctx = HAScriptContext(prev_local_active=True,
                          probe_grace_deadline=time.monotonic() + 60)

    assert primary_check_remote_hosts(
        _remote_probe_config(), ctx, _remote_probe_net_ctx()
    )
    assert ctx.probe_grace_deadline is None


@patch("ha_script.mainloop.tcp_probe")
def test_remote_no_grace_when_active(tcp_probe):
    """Active last iteration and no open window: the tcp_probe verdict
    stands unchanged, and the probe binds the source address resolved
    from remote_probe_nic_idx."""
    tcp_probe.side_effect = _fake_probe(reachable=True, fail_count=0)
    config = _remote_probe_config()
    ctx = HAScriptContext(prev_local_active=True)

    assert primary_check_remote_hosts(config, ctx, _remote_probe_net_ctx())
    assert ctx.probe_grace_deadline is None
    tcp_probe.assert_called_once_with(
        config, ["198.51.100.1"], config.remote_probe_port, ctx,
        source_ip="10.0.0.10",
    )


@patch("ha_script.mainloop.tcp_probe")
def test_remote_grace_expiry_resumes_normal_probing(tcp_probe):
    """When the window expires, it is cleared and the fail counter reset so
    normal probing resumes with a full probe_max_fail budget; the expiry tick
    itself does not force offline."""
    tcp_probe.side_effect = _fake_probe(reachable=False, fail_count=3)
    ctx = HAScriptContext(prev_local_active=True,
                          probe_grace_deadline=time.monotonic() - 1)

    assert primary_check_remote_hosts(
        _remote_probe_config(), ctx, _remote_probe_net_ctx()
    )
    assert ctx.probe_grace_deadline is None
    assert ctx.probe_fail_count == 0


@patch("ha_script.mainloop.tcp_probe")
def test_remote_grace_disabled_when_zero(tcp_probe):
    """With grace disabled and the primary active, a down verdict is respected
    immediately."""
    tcp_probe.side_effect = _fake_probe(reachable=False, fail_count=0)
    config = _remote_probe_config()
    config.remote_probe_grace_sec = 0
    ctx = HAScriptContext(prev_local_active=True)

    assert primary_check_remote_hosts(
        config, ctx, _remote_probe_net_ctx()
    ) is False
    assert ctx.probe_grace_deadline is None
