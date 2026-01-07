"""
Tests for moveable IP functionality in Azure environment.
"""

import logging
import pytest
from unittest.mock import Mock, patch

from conftest import AzureConf
from ha_script.azure import api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.mainloop import (
    primary_main_loop_handler,
    secondary_main_loop_handler
)


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_primary_moves_ip_when_becoming_active(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_vm_name: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Test that primary moves the public IP to itself when becoming active
    with a moveable IP"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ip_id=azure_conf.reserved_public_ip_name
    )
    get_vm_name.return_value = azure_conf.primary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_names[0],
        internal_ip=azure_conf.primary_ips[0],
        wan_nic_id=azure_conf.primary_nic_names[1],
        wan_ip=azure_conf.primary_ips[1],
    )
    create_local_net_context.return_value = primary_net_ctx

    # Secondary has the traffic initially
    azure_conf.state.route_tables[0]['properties']['routes'] = [
        {
            'name': 'default',
            'properties': {
                'addressPrefix': '0.0.0.0/0',
                'nextHopType': 'VirtualAppliance',
                'nextHopIpAddress': azure_conf.primary_ips[0],
            },
        },
    ]

    # Move public IP to secondary WAN NIC
    sec_wan_nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.secondary_nic_names[1]
    )
    sec_wan_nic['properties']['ipConfigurations'][0]['properties']['publicIPAddress'] = {
        'id': azure_conf.state.public_ips[0]['id']
    }
    azure_conf.network_client.update_network_interface(
        azure_conf.resource_group, azure_conf.secondary_nic_names[1], sec_wan_nic
    )

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # Verify public IP was moved to primary WAN interface
    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    pip_ref = nic['properties']['ipConfigurations'][0]['properties'].get('publicIPAddress', {})
    assert pip_ref.get('id', '').endswith(azure_conf.reserved_public_ip_name)

    # Verify notification was sent
    assert any(
        "Public IP address" in str(call) and "moved" in str(call)
        for call in send_notification_to_smc.mock_calls
    )


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_secondary_moves_ip_on_takeover(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_vm_name: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Test that secondary moves the public IP when taking over with a moveable
    IP"""
    caplog.set_level(logging.INFO)

    primary_ip = azure_conf.primary_ips[0]
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ip_id=azure_conf.reserved_public_ip_name,
        probe_port=12345,
        probe_ip=primary_ip
    )
    get_vm_name.return_value = azure_conf.secondary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.secondary_nic_names[0],
        internal_ip=azure_conf.secondary_ips[0],
        wan_nic_id=azure_conf.secondary_nic_names[1],
        wan_ip=azure_conf.secondary_ips[1],
    )
    create_local_net_context.return_value = secondary_net_ctx

    # Primary has the traffic but is offline
    azure_conf.state.route_tables[0]['properties']['routes'] = [
        {
            'name': 'default',
            'properties': {
                'addressPrefix': '0.0.0.0/0',
                'nextHopType': 'VirtualAppliance',
                'nextHopIpAddress': azure_conf.primary_ips[0],
            },
        },
    ]

    # Public IP is assigned to primary (already set in fixture)

    get_local_status.return_value = "online"
    get_primary_status.return_value = "offline"  # Primary is offline
    tcp_probe.return_value = True

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, secondary_net_ctx)

    # Verify public IP was moved to secondary WAN interface
    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.secondary_nic_names[1]
    )
    pip_ref = nic['properties']['ipConfigurations'][0]['properties'].get('publicIPAddress', {})
    assert pip_ref.get('id', '').endswith(azure_conf.reserved_public_ip_name)

    # Verify notification was sent
    assert any(
        "Public IP address" in str(call) and "moved" in str(call)
        for call in send_notification_to_smc.mock_calls
    )


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_no_ip_move_when_already_assigned(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_vm_name: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Test that IP is not moved if it's already assigned to the correct
    instance"""
    caplog.set_level(logging.INFO)

    primary_ip = azure_conf.primary_ips[0]
    primary_nic_name = azure_conf.primary_nic_names[0]

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ip_id=azure_conf.reserved_public_ip_name
    )
    get_vm_name.return_value = azure_conf.secondary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_nic_name,
        internal_ip=primary_ip,
        wan_nic_id=azure_conf.primary_nic_names[1],
        wan_ip=azure_conf.primary_ips[1],
    )
    create_local_net_context.return_value = primary_net_ctx

    # Primary has the traffic and public IP is already assigned to primary
    azure_conf.state.route_tables[0]['properties']['routes'] = [
        {
            'name': 'default',
            'properties': {
                'addressPrefix': '0.0.0.0/0',
                'nextHopType': 'VirtualAppliance',
                'nextHopIpAddress': azure_conf.primary_ips[0],
            },
        },
    ]

    # Public IP is already assigned to primary (set in fixture by default)
    # Record which NIC has it for comparison
    nic_before = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    original_pip_ref = nic_before['properties']['ipConfigurations'][0]['properties'].get('publicIPAddress', {})

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=True,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # Verify public IP was NOT moved (still assigned to primary)
    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    pip_ref = nic['properties']['ipConfigurations'][0]['properties'].get('publicIPAddress', {})
    assert pip_ref.get('id', '') == original_pip_ref.get('id', '')

    # Verify no notification about IP move was sent
    ip_move_notifications = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_notifications) == 0


def test_resolve_public_ip_assignee(azure_conf: AzureConf):
    """Test resolving private IP to public IP"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ip_id=azure_conf.reserved_public_ip_name
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Initially assigned to primary (via fixture - public IP's ipConfiguration
    # points to primary WAN NIC)
    public_ip, assignee = api.resolve_public_ip(config, clients)
    assert assignee is not None
    assert azure_conf.primary_nic_names[1] in assignee

    # Move to secondary by updating NICs
    # Remove from primary WAN NIC
    pri_wan_nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    del pri_wan_nic['properties']['ipConfigurations'][0]['properties']['publicIPAddress']
    azure_conf.network_client.update_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1], pri_wan_nic
    )

    # Add to secondary WAN NIC
    sec_wan_nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.secondary_nic_names[1]
    )
    sec_wan_nic['properties']['ipConfigurations'][0]['properties']['publicIPAddress'] = {
        'id': azure_conf.state.public_ips[0]['id']
    }
    azure_conf.network_client.update_network_interface(
        azure_conf.resource_group, azure_conf.secondary_nic_names[1], sec_wan_nic
    )

    # Update public IP state to point to secondary
    azure_conf.state.public_ips[0]['properties']['ipConfiguration'] = {
        'id': azure_conf.state.nics[3]['id'] + "/ipConfigurations/ipconfig1"
    }

    public_ip, assignee = api.resolve_public_ip(config, clients)
    assert assignee is not None
    assert azure_conf.secondary_nic_names[1] in assignee


def test_move_public_ip_basic(azure_conf: AzureConf):
    """Test basic public IP move functionality"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ip_id=azure_conf.reserved_public_ip_name
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Create network context for secondary
    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.secondary_nic_names[0],
        internal_ip=azure_conf.secondary_ips[0],
        wan_nic_id=azure_conf.secondary_nic_names[1],
        wan_ip=azure_conf.secondary_ips[1],
    )

    # Initially assigned to primary (set in fixture)
    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    pip_ref = nic['properties']['ipConfigurations'][0]['properties'].get('publicIPAddress', {})
    assert pip_ref.get('id', '').endswith(azure_conf.reserved_public_ip_name)

    # Move to secondary
    api.move_public_ip(config, clients, secondary_net_ctx)

    # Verify it was moved
    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.secondary_nic_names[1]
    )
    pip_ref = nic['properties']['ipConfigurations'][0]['properties'].get('publicIPAddress', {})
    assert pip_ref.get('id', '').endswith(azure_conf.reserved_public_ip_name)

