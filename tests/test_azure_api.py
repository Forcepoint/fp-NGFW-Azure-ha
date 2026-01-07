"""
Tests for Azure API utilities module.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
from conftest import AzureConf

from ha_script.config import HAScriptConfig
from ha_script.azure.api import (
    get_config_tag_value,
    get_config_tags,
    get_instance_ip_addresses,
    get_route_table_info,
    set_config_tag,
    update_route_table,
    create_local_net_context,
    get_azure_clients,
)


def test_set_config_tag_success(azure_conf: AzureConf) -> None:
    """Test setting a config tag on an Azure VM"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    assert set_config_tag(
        config, clients, "status", "online",
        azure_conf.primary_vm_name
    )

    # Verify tag was set
    tags = get_config_tags(clients, azure_conf.primary_vm_name)
    assert tags["status"] == "online"


def test_set_config_tag_fails(
    azure_conf: AzureConf, caplog
) -> None:
    """Test handling of failures when setting config tags"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    mock_compute_client = MagicMock()
    mock_compute_client.get_vm.side_effect = \
        Exception("API Error")
    clients = (mock_compute_client, azure_conf.network_client)

    assert not set_config_tag(
        config, clients, "status", "online",
        azure_conf.primary_vm_name
    )
    assert len(caplog.records) >= 1


def test_get_config_tags_success(
    azure_conf: AzureConf
) -> None:
    """Test retrieving config tags from an Azure VM"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name
    )

    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    set_config_tag(
        config, clients, "tag1", "value1",
        instance_id=azure_conf.primary_vm_name
    )
    set_config_tag(
        config, clients, "tag2", "value2",
        instance_id=azure_conf.primary_vm_name
    )

    tags = get_config_tags(
        clients, azure_conf.primary_vm_name
    )
    assert tags == {"tag1": "value1", "tag2": "value2"}
    assert get_config_tag_value(
        clients, "tag2", azure_conf.primary_vm_name
    ) == "value2"


def test_create_local_net_context_success(
    azure_conf: AzureConf
) -> None:
    """Test creating local network context from Azure metadata"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        internal_nic_idx=0,
        wan_nic_idx=1
    )

    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    with patch(
        'ha_script.azure.metadata.get_vm_name'
    ) as mock_get_vm_name:
        mock_get_vm_name.return_value = \
            azure_conf.primary_vm_name

        ctx = create_local_net_context(config, clients)

        assert ctx.internal_nic_id.endswith(
            azure_conf.primary_nic_names[0]
        )
        assert ctx.internal_ip == azure_conf.primary_ips[0]
        assert ctx.wan_nic_id.endswith(
            azure_conf.primary_nic_names[1]
        )
        assert ctx.wan_ip == azure_conf.primary_ips[1]


def test_get_route_table_info_success(
    azure_conf: AzureConf
) -> None:
    """Test retrieving route info."""
    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    route_table_info = list(
        get_route_table_info(
            clients,
            azure_conf.protected_route_table_name,
            [
                azure_conf.primary_vm_name,
                azure_conf.secondary_vm_name
            ],
        )
    )

    # Should return only the default route (0.0.0.0/0) via
    # primary NGFW. The 192.168.0.0/24 route via "other"
    # should not be included.
    assert len(route_table_info) == 1
    assert route_table_info[0].route_dest == "0.0.0.0/0"
    assert route_table_info[0].target_ip == \
        azure_conf.primary_ips[0]
    assert route_table_info[0].target_ip_id == ""
    assert route_table_info[0].route_table_id == \
        azure_conf.protected_route_table_name
    assert route_table_info[0].route_state == "ACTIVE"


def test_update_route_table_info_success(
    azure_conf: AzureConf
) -> None:
    """Test route table update for a given destination"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        internal_nic_idx=0,
        wan_nic_idx=1
    )

    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    with patch(
        'ha_script.azure.metadata.get_vm_name'
    ) as mock_get_vm_name:
        mock_get_vm_name.return_value = \
            azure_conf.secondary_vm_name

        secondary_ctx = create_local_net_context(
            config, clients
        )

    # Update route to point to secondary
    assert update_route_table(
        config, clients,
        azure_conf.protected_route_table_name,
        "0.0.0.0/0", secondary_ctx
    )

    # Verify the route was updated
    route_table_info = list(
        get_route_table_info(
            clients,
            azure_conf.protected_route_table_name,
            [
                azure_conf.primary_vm_name,
                azure_conf.secondary_vm_name
            ],
        )
    )

    assert len(route_table_info) == 1
    assert route_table_info[0].route_dest == "0.0.0.0/0"
    assert route_table_info[0].target_ip == \
        azure_conf.secondary_ips[0]

    # Verify the other route has not been changed
    rt = azure_conf.network_client.get_route_table(
        azure_conf.resource_group,
        azure_conf.protected_route_table_name
    )
    other_route = next(
        r for r in rt['properties']['routes']
        if r['properties']['addressPrefix'] == '192.168.0.0/24'
    )
    assert other_route['properties']['nextHopIpAddress'] == \
        azure_conf.other_ip


def test_get_instance_ip_addresses_success(
    azure_conf: AzureConf
) -> None:
    """Test retrieving all IP addresses from an Azure VM"""
    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    ip_list = get_instance_ip_addresses(
        clients, azure_conf.primary_vm_name
    )

    assert len(ip_list) == 2
    assert azure_conf.primary_ips[0] in ip_list
    assert azure_conf.primary_ips[1] in ip_list


def test_dry_run_mode(azure_conf: AzureConf) -> None:
    """Test that dry-run mode prevents actual changes"""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        internal_nic_idx=0,
        wan_nic_idx=1,
        dry_run=True
    )

    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    with patch(
        'ha_script.azure.metadata.get_vm_name'
    ) as mock_get_vm_name:
        mock_get_vm_name.return_value = \
            azure_conf.secondary_vm_name

        secondary_ctx = create_local_net_context(
            config, clients
        )

    # Get original route target
    rt = azure_conf.network_client.get_route_table(
        azure_conf.resource_group,
        azure_conf.protected_route_table_name
    )
    default_route = next(
        r for r in rt['properties']['routes']
        if r['properties']['addressPrefix'] == '0.0.0.0/0'
    )
    original_target = \
        default_route['properties']['nextHopIpAddress']

    # Try to update route in dry-run mode
    assert update_route_table(
        config, clients,
        azure_conf.protected_route_table_name,
        "0.0.0.0/0", secondary_ctx
    )

    # Verify the route was NOT actually updated
    rt = azure_conf.network_client.get_route_table(
        azure_conf.resource_group,
        azure_conf.protected_route_table_name
    )
    default_route = next(
        r for r in rt['properties']['routes']
        if r['properties']['addressPrefix'] == '0.0.0.0/0'
    )
    assert default_route['properties']['nextHopIpAddress'] \
        == original_target


def test_get_route_table_info_blackhole(
    azure_conf: AzureConf
) -> None:
    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    azure_conf.state.route_tables[0]['properties']['routes'] = [
        {
            'name': 'default',
            'properties': {
                'addressPrefix': '0.0.0.0/0',
                'nextHopType': 'None',
            }
        }
    ]

    routes = list(
        get_route_table_info(
            clients,
            azure_conf.protected_route_table_name,
            [
                azure_conf.primary_vm_name,
                azure_conf.secondary_vm_name
            ],
        )
    )

    assert len(routes) == 1
    assert routes[0].route_state == "blackhole"
    assert routes[0].route_dest == "0.0.0.0/0"
    assert routes[0].target_ip == ""
    assert routes[0].target_ip_id == ""
    assert routes[0].route_table_id == \
        azure_conf.protected_route_table_name


def test_get_azure_clients_propagates_exception(caplog):
    original_error = RuntimeError("IMDS unreachable")

    with patch(
        "ha_script.azure.auth.RequestSigner",
        side_effect=original_error
    ):
        with caplog.at_level(
            logging.CRITICAL, logger="ha_script.azure.api"
        ):
            with pytest.raises(RuntimeError) as exc_info:
                get_azure_clients()

    assert exc_info.value is original_error

    critical_records = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL
    ]
    assert len(critical_records) == 1
    assert "IMDS unreachable" in critical_records[0].message
