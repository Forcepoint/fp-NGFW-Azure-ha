"""
Tests for moveable IP functionality in Azure environment.
"""

import logging
import pytest
from unittest.mock import Mock, patch

from conftest import AzureConf, network_id
from ha_script.azure import api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.mainloop import (
    primary_main_loop_handler,
    secondary_main_loop_handler
)


def _assign_public_ip(azure_conf: AzureConf, pip_name: str,
                      nic_name: str, ip_config_name: str) -> None:
    """Attach a reserved public IP to a NIC ipConfiguration.

    The public IP is detached from its current ipConfiguration first,
    as Azure requires before it can be associated elsewhere.
    """
    clients = (azure_conf.compute_client, azure_conf.network_client)
    pip_id = network_id("publicIPAddresses", pip_name)
    api.detach_public_ip(clients, pip_id)

    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, nic_name
    )
    for ip_config in nic["properties"]["ipConfigurations"]:
        if ip_config["name"] == ip_config_name:
            ip_config["properties"]["publicIPAddress"] = {"id": pip_id}
    azure_conf.network_client.update_network_interface(
        azure_conf.resource_group, nic_name, nic
    )


def _public_ip_assignee(azure_conf: AzureConf, pip_name: str) -> str:
    """Return the ipConfiguration ID a reserved public IP is assigned to."""
    public_ip = azure_conf.network_client.get_public_ip(
        azure_conf.resource_group, pip_name
    )
    return public_ip["properties"]["ipConfiguration"]["id"]


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
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        }
    )
    get_vm_name.return_value = azure_conf.primary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for primary (with both WAN private IPs)
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_ids[0],
        internal_ip=azure_conf.primary_ips[0],
        public_ip_targets=[
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name),
             azure_conf.primary_ip_config_ids[1], "203.0.113.10"),
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name_2),
             azure_conf.primary_ip_config_wan_2_id, "203.0.113.11"),
        ]
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

    # Both public IPs assigned to secondary (each on its own ipConfiguration)
    _assign_public_ip(azure_conf, azure_conf.reserved_public_ip_name,
                      azure_conf.secondary_nic_names[1], 'ipconfig1')
    _assign_public_ip(azure_conf, azure_conf.reserved_public_ip_name_2,
                      azure_conf.secondary_nic_names[1], 'ipconfig2')

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # First public IP moved to its explicit target
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.primary_ip_config_ids[1]

    # Second public IP moved to its explicit target
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name_2
    ) == azure_conf.primary_ip_config_wan_2_id

    # Verify notifications were sent for both IPs
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 2


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
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
        probe_port=12345,
        probe_ip=primary_ip
    )
    get_vm_name.return_value = azure_conf.secondary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.secondary_nic_ids[0],
        internal_ip=azure_conf.secondary_ips[0],
        public_ip_targets=[
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name),
             azure_conf.secondary_ip_config_ids[1], "203.0.113.10"),
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name_2),
             azure_conf.secondary_ip_config_wan_2_id, "203.0.113.11"),
        ]
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

    # Both public IPs are assigned to primary (already set in fixture)

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

    # First public IP moved to its explicit target
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.secondary_ip_config_ids[1]

    # Second public IP moved to its explicit target
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name_2
    ) == azure_conf.secondary_ip_config_wan_2_id

    # Verify notifications were sent for both IPs
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 2


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

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"}
    )
    get_vm_name.return_value = azure_conf.secondary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_ids[0],
        internal_ip=primary_ip,
        public_ip_targets=[
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name),
             azure_conf.primary_ip_config_ids[1], "203.0.113.10"),
        ]
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

    # Public IP is already assigned to primary's target (set in fixture)

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=True,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # Verify public IP was NOT moved (still assigned to primary)
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.primary_ip_config_ids[1]
    assert "Detaching public IP" not in caplog.text

    # Verify no notification about IP move was sent
    ip_move_notifications = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_notifications) == 0


def test_move_public_ip_basic(azure_conf: AzureConf):
    """Test basic public IP move functionality"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"}
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # Initially assigned to primary (set in fixture)
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.primary_ip_config_ids[1]

    # Move to secondary
    assert api.move_public_ip(
        config, clients,
        network_id("publicIPAddresses", azure_conf.reserved_public_ip_name),
        azure_conf.secondary_ip_config_ids[1]
    )

    # Verify it was moved
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.secondary_ip_config_ids[1]


def test_move_public_ip_selects_target_ip_configuration(
    azure_conf: AzureConf,
):
    """The public IP is attached to the target ipConfiguration only.

    A NIC can hold several ipConfigurations, each with its own public
    IP.  Moving one must leave the others as they are.
    """
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.11"}
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    # The secondary WAN NIC already holds a public IP on ipconfig1
    _assign_public_ip(azure_conf, azure_conf.reserved_public_ip_name_2,
                      azure_conf.secondary_nic_names[1], 'ipconfig1')

    assert api.move_public_ip(
        config, clients,
        network_id("publicIPAddresses", azure_conf.reserved_public_ip_name),
        azure_conf.secondary_ip_config_wan_2_id
    )

    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.secondary_ip_config_wan_2_id
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name_2
    ) == azure_conf.secondary_ip_config_ids[1]


def test_move_public_ip_logs_both_resource_ids(
    azure_conf: AzureConf,
    caplog,
):
    """move_public_ip() logs the full source and destination IDs.

    The assignee ID comes from the Network API and the destination from
    the Compute API.  A mismatch between the two is only explainable if
    both are logged as they were read.
    """
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"}
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)
    public_ip_id = network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name
    )

    # The public IP is on the primary WAN NIC (set in the fixture).
    prev_assignee = api.resolve_public_ip(clients, public_ip_id)
    assert prev_assignee

    api.move_public_ip(config, clients, public_ip_id,
                       azure_conf.secondary_ip_config_ids[1])

    assert prev_assignee in caplog.text
    assert azure_conf.secondary_ip_config_ids[1] in caplog.text
    assert public_ip_id in caplog.text


def test_move_public_ip_dry_run(azure_conf: AzureConf, caplog):
    """Test that dry-run mode does not move the public IP"""
    caplog.set_level(logging.WARNING)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"},
        dry_run=True
    )

    clients = (azure_conf.compute_client, azure_conf.network_client)

    assert api.move_public_ip(
        config, clients,
        network_id("publicIPAddresses", azure_conf.reserved_public_ip_name),
        azure_conf.secondary_ip_config_ids[1]
    )

    assert "DRY-RUN" in caplog.text
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.primary_ip_config_ids[1]


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_partial_ip_move(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_vm_name: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Test that only non-local IPs are moved when one is already local"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        }
    )
    get_vm_name.return_value = azure_conf.primary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_ids[0],
        internal_ip=azure_conf.primary_ips[0],
        public_ip_targets=[
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name),
             azure_conf.primary_ip_config_ids[1], "203.0.113.10"),
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name_2),
             azure_conf.primary_ip_config_wan_2_id, "203.0.113.11"),
        ]
    )
    create_local_net_context.return_value = primary_net_ctx

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

    # First IP already on its target (fixture), second on secondary
    _assign_public_ip(azure_conf, azure_conf.reserved_public_ip_name_2,
                      azure_conf.secondary_nic_names[1], 'ipconfig2')

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # First IP should still be on primary (no move needed)
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.primary_ip_config_ids[1]

    # Second IP should now be moved to its explicit target on primary
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name_2
    ) == azure_conf.primary_ip_config_wan_2_id

    # Only one notification (for the second IP that was moved)
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 1
    assert "203.0.113.11" in str(ip_move_calls[0])


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_resolves_legacy_public_ip(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that legacy key 'id' with a resource ID resolves correctly"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    public_ip_id = network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name
    )
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"id": public_ip_id},
        wan_nic_idx=1,
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    assert pub_id == public_ip_id
    assert target_id == azure_conf.primary_ip_config_ids[1]
    assert ip_addr == "203.0.113.10"


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_resolves_ip_triplet_primary(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that is_primary=True picks primary private IP from triplet"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"},
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    # resolved from 203.0.113.10
    assert pub_id == network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name
    )
    assert target_id == azure_conf.primary_ip_config_ids[1]  # 10.0.12.10
    assert ip_addr == "203.0.113.10"


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_resolves_ip_triplet_whitespace(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that whitespace around triplet parts is stripped before lookup"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10, 10.0.12.10, 10.0.22.10"},
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    # resolved from 203.0.113.10
    assert pub_id == network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name
    )
    assert target_id == azure_conf.primary_ip_config_ids[1]  # 10.0.12.10
    assert ip_addr == "203.0.113.10"


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_resolves_ip_triplet_secondary(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that is_primary=False picks secondary private IP from triplet"""
    get_vm_name.return_value = azure_conf.secondary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"},
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx = api.create_local_net_context(config, clients, is_primary=False)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    # resolved from 203.0.113.10
    assert pub_id == network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name
    )
    assert target_id == azure_conf.secondary_ip_config_ids[1]  # 10.0.22.10
    assert ip_addr == "203.0.113.10"


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_resolves_multiple_entries(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that multiple triplets are all resolved"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 2
    # Both should resolve to the correct public IP and primary private IPs
    pub_ids = {t[0] for t in ctx.public_ip_targets}
    assert network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name
    ) in pub_ids
    assert network_id(
        "publicIPAddresses", azure_conf.reserved_public_ip_name_2
    ) in pub_ids
    target_ids = {t[1] for t in ctx.public_ip_targets}
    assert azure_conf.primary_ip_config_ids[1] in target_ids
    assert azure_conf.primary_ip_config_wan_2_id in target_ids


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_rejects_unknown_private_ip(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that a private IP not on any NIC raises an error"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "203.0.113.10,10.99.99.99,10.0.22.10"},
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    with pytest.raises(api.HAScriptError, match="not found on any NIC"):
        api.create_local_net_context(config, clients, is_primary=True)


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_public_ip_not_found(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that a non-existent public IP address raises error during context
    creation"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={"vpn": "198.51.100.99,10.0.12.10,10.0.22.10"},
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    with pytest.raises(api.HAScriptError, match="198.51.100.99"):
        api.create_local_net_context(config, clients, is_primary=True)


@patch("ha_script.azure.metadata.get_vm_name")
def test_create_context_mixed_legacy_and_triplet(
    get_vm_name: Mock,
    azure_conf: AzureConf,
):
    """Test that both legacy resource ID and triplet entries resolve correctly
    together"""
    get_vm_name.return_value = azure_conf.primary_vm_name
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={
            "id": network_id(
                "publicIPAddresses", azure_conf.reserved_public_ip_name
            ),
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
        wan_nic_idx=1,
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 2
    # Legacy entry resolved by resource ID
    legacy = [t for t in ctx.public_ip_targets
              if t[0] == network_id(
                  "publicIPAddresses", azure_conf.reserved_public_ip_name
              )]
    assert len(legacy) == 1
    assert legacy[0][2] == "203.0.113.10"
    # Triplet entry resolved by IP address
    triplet = [t for t in ctx.public_ip_targets
               if t[0] == network_id(
                   "publicIPAddresses", azure_conf.reserved_public_ip_name_2
               )]
    assert len(triplet) == 1
    assert triplet[0][2] == "203.0.113.11"


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.azure.api.send_error_to_smc")
def test_primary_continues_after_one_ip_move_fails(
    send_error_to_smc: Mock,
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_vm_name: Mock,
    azure_conf: AzureConf,
    caplog,
):
    """Test that if the first IP move fails, the second is still attempted"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        }
    )
    get_vm_name.return_value = azure_conf.primary_vm_name

    clients = (azure_conf.compute_client, azure_conf.network_client)

    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_ids[0],
        internal_ip=azure_conf.primary_ips[0],
        public_ip_targets=[
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name),
             azure_conf.primary_ip_config_ids[1], "203.0.113.10"),
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name_2),
             azure_conf.primary_ip_config_wan_2_id, "203.0.113.11"),
        ]
    )
    create_local_net_context.return_value = primary_net_ctx

    # Route already points to primary
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

    # Both IPs assigned to secondary (both need move)
    _assign_public_ip(azure_conf, azure_conf.reserved_public_ip_name,
                      azure_conf.secondary_nic_names[1], 'ipconfig1')
    _assign_public_ip(azure_conf, azure_conf.reserved_public_ip_name_2,
                      azure_conf.secondary_nic_names[1], 'ipconfig2')

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=False,
    )

    # Make the network client's NIC update fail on the first public IP,
    # succeed on the second, so move_public_ip returns False then True.
    original_update = azure_conf.network_client.update_network_interface
    call_count = {"n": 0}

    def failing_update(resource_group, nic_name, body):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Azure API timeout")
        return original_update(resource_group, nic_name, body)

    azure_conf.network_client.update_network_interface = failing_update

    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # First IP should NOT have moved (failed)
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name
    ) == azure_conf.secondary_ip_config_ids[1]

    # Second IP SHOULD have moved (succeeded despite first failure)
    assert _public_ip_assignee(
        azure_conf, azure_conf.reserved_public_ip_name_2
    ) == azure_conf.primary_ip_config_wan_2_id

    # Error was reported for the first IP
    assert send_error_to_smc.call_count == 1
    assert "203.0.113.10" in str(send_error_to_smc.call_args)

    # Success notification only for the second IP
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 1
    assert "203.0.113.11" in str(ip_move_calls[0])
