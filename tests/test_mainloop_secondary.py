"""
Tests for secondary mainloop in Azure environment.

This module tests the secondary engine's main loop logic including:
- Taking over when primary is offline
- Taking over when probe fails
- Taking over when route is in blackhole state
- Respecting moveable IP functionality
"""
import logging

import pytest
from unittest.mock import Mock, patch

from conftest import AzureConf

from ha_script.azure import api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.mainloop import secondary_main_loop_handler


@pytest.mark.parametrize(
    "takeover_reason", ["probe_fails", "prim_offline", "route_blackhole"]
)
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.update_route_table")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
def test_secondary_takeover(
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
    caplog,
    takeover_reason,
):
    """Test secondary takeover for different reasons: probe fails, primary
    offline, or route blackhole"""
    caplog.set_level(logging.INFO)

    secondary_ip = azure_conf.secondary_ips[0]
    secondary_vnic_id = azure_conf.secondary_nic_names[0]
    primary_ip = azure_conf.primary_ips[0]
    route_table_id = azure_conf.protected_route_table_name

    config = HAScriptConfig(
        route_table_id=route_table_id,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        probe_port=12345,
        probe_ip=primary_ip
    )

    # For now the primary has the traffic
    route_state = "blackhole" if takeover_reason == "route_blackhole" else "ACTIVE"
    get_route_table_info.return_value = [
        api.RouteInfo(
            route_state,
            "0.0.0.0/0",
            "",
            primary_ip,
            azure_conf.primary_nic_names[0],
            route_table_id
        )
    ]
    get_local_status.return_value = "online"
    get_primary_status.return_value = "offline" if takeover_reason == "prim_offline" else "online"
    tcp_probe.return_value = False if takeover_reason == "probe_fails" else True

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for secondary
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        wan_nic_id=azure_conf.secondary_nic_names[1],
        wan_ip=azure_conf.secondary_ips[1],
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    tcp_probe.assert_called_once_with(config, [primary_ip], config.probe_port,
                                      ctx)

    update_route_table.assert_called_once_with(
        config, clients, route_table_id, "0.0.0.0/0", local_net_ctx
    )

    # Make sure the SMC is notified
    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{route_table_id}' changed route to '0.0.0.0/0' "
        f"via secondary '{secondary_ip}'.",
        alert=True)


@pytest.mark.parametrize("takeover_reason", ["probe_fails", "prim_offline"])
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.get_route_table_info", wraps=api.get_route_table_info)
@patch("ha_script.azure.api.create_local_net_context")
def test_secondary_takeover_with_azure_mock(
    create_local_net_context,
    get_route_table_info: Mock,
    get_local_status: Mock,
    get_primary_status: Mock,
    tcp_probe: Mock,
    send_notification_to_smc: Mock,
    azure_conf: AzureConf,
    caplog,
    takeover_reason,
):
    """Same test using Azure mock. Verifies that only routes via NGFW are modified"""
    caplog.set_level(logging.INFO)

    primary_vnic_id = azure_conf.primary_nic_names[0]
    secondary_vnic_id = azure_conf.secondary_nic_names[0]
    secondary_ip = azure_conf.secondary_ips[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        probe_port=12345,
    )

    # Make sure default route goes initially via the primary
    route_table = azure_conf.network_client.get_route_table(azure_conf.resource_group, azure_conf.protected_route_table_name)
    default_route = next(r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '0.0.0.0/0')
    assert default_route['properties']['nextHopIpAddress'] == azure_conf.primary_ips[0]

    # Make sure 'other_route' goes via other NIC
    other_route = next(r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '192.168.0.0/24')
    assert other_route['properties']['nextHopIpAddress'] == azure_conf.other_ip

    get_local_status.return_value = "online"
    tcp_probe.return_value = True

    if takeover_reason == "prim_offline":
        get_primary_status.return_value = "offline"
    elif takeover_reason == "probe_fails":
        tcp_probe.return_value = False

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for secondary
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        wan_nic_id=azure_conf.secondary_nic_names[1],
        wan_ip=azure_conf.secondary_ips[1],
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure the default route now points to secondary
    route_table = azure_conf.network_client.get_route_table(azure_conf.resource_group, azure_conf.protected_route_table_name)
    default_route = next(r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '0.0.0.0/0')
    assert default_route['properties']['nextHopIpAddress'] == azure_conf.secondary_ips[0]

    # Make sure 'other_route' still goes via other NIC
    other_route = next(r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '192.168.0.0/24')
    assert other_route['properties']['nextHopIpAddress'] == azure_conf.other_ip

    # Make sure the SMC is notified
    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{azure_conf.protected_route_table_name}' changed route to '0.0.0.0/0' "
        f"via secondary '{secondary_ip}'.",
        alert=True)


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.get_route_table_info")
@patch("ha_script.azure.api.create_local_net_context")
def test_secondary_no_takeover_when_primary_online(
    create_local_net_context,
    get_route_table_info,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Test that secondary does not take over when primary is healthy"""
    caplog.set_level(logging.INFO)

    secondary_ip = azure_conf.secondary_ips[0]
    secondary_vnic_id = azure_conf.secondary_nic_names[0]
    primary_ip = azure_conf.primary_ips[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        probe_port=12345,
        probe_ip=primary_ip
    )

    # Primary has the traffic and is healthy
    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            "",
            primary_ip,
            azure_conf.primary_nic_names[0],
            azure_conf.protected_route_table_name
        )
    ]
    get_local_status.return_value = "online"
    get_primary_status.return_value = "online"
    tcp_probe.return_value = True  # Primary responds to probe

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for secondary
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        wan_nic_id=azure_conf.secondary_nic_names[1],
        wan_ip=azure_conf.secondary_ips[1],
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure no takeover happened
    assert len(send_notification_to_smc.mock_calls) == 0

    # Context should reflect that secondary is still not active
    assert not ctx.prev_local_active


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.azure.api.get_route_table_info", wraps=api.get_route_table_info)
@patch("ha_script.azure.api.create_local_net_context")
def test_secondary_takeover_on_blackhole_route_with_azure_mock(
    create_local_net_context,
    get_route_table_info,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    azure_conf: AzureConf,
    caplog,
):
    caplog.set_level(logging.INFO)

    secondary_ip = azure_conf.secondary_ips[0]
    secondary_vnic_id = azure_conf.secondary_nic_names[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
    )

    # Replace default route with a blackhole (empty nextHopIpAddress)
    azure_conf.state.route_tables[0]['properties']['routes'] = [
        {'name': 'default', 'properties': {'addressPrefix': '0.0.0.0/0', 'nextHopType': 'None'}},
    ]

    get_local_status.return_value = "online"
    get_primary_status.return_value = "online"
    tcp_probe.return_value = True

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        wan_nic_id=azure_conf.secondary_nic_names[1],
        wan_ip=azure_conf.secondary_ips[1],
    )
    create_local_net_context.return_value = local_net_ctx
    clients = (azure_conf.compute_client, azure_conf.network_client)

    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Route must now point to secondary
    route_table = azure_conf.network_client.get_route_table(
        azure_conf.resource_group,
        azure_conf.protected_route_table_name
    )
    default_route = next(
        r for r in route_table['properties']['routes'] if r['properties']['addressPrefix'] == '0.0.0.0/0'
    )
    assert default_route['properties']['nextHopIpAddress'] == \
        azure_conf.secondary_ips[0]

    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{azure_conf.protected_route_table_name}' changed route "
        f"to '0.0.0.0/0' via secondary '{secondary_ip}'.",
        alert=True,
    )
