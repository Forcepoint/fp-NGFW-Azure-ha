"""
Tests for case-insensitive matching of Azure identifiers.

Azure does not guarantee that the casing it returns for a resource name
matches the casing that was originally supplied, and two APIs may report
the same resource with different casing.  Every comparison of an Azure
identifier therefore has to be case-insensitive:

https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules
"""
import logging
from unittest.mock import Mock, patch

import pytest

from conftest import AzureConf, network_id
from ha_script.azure import api
from ha_script.azure.api import (
    detach_public_ip,
    get_config_tags,
    same_resource_id,
    set_config_tag,
)
from ha_script.config import HAScriptConfig, _is_resource_id
from ha_script.context import HAScriptContext
from ha_script.exceptions import HAScriptConfigError
from ha_script.mainloop import primary_main_loop_handler
from ha_script.ngfw_utils import is_primary, is_secondary


def _recase_resource_group(resource_id: str, resource_group: str) -> str:
    """Return the ID with the resource group segment upper-cased.

    Reproduces Azure returning the same resource ID with a different
    resource group casing than the API it was read from.
    """
    return resource_id.replace(f"/{resource_group}/", f"/{resource_group.upper()}/")


def test_same_resource_id_ignores_casing() -> None:
    assert same_resource_id("/subscriptions/s/rg/my-nic", "/SUBSCRIPTIONS/S/RG/MY-NIC")
    assert same_resource_id("my-nic", "My-Nic")
    assert not same_resource_id("my-nic", "my-nic2")


def test_is_resource_id_ignores_casing() -> None:
    assert _is_resource_id("/subscriptions/s/resourceGroups/rg/providers/x/y")
    assert _is_resource_id("/Subscriptions/s/resourceGroups/rg/providers/x/y")
    assert not _is_resource_id("my-route-table")


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_no_ip_move_when_assignee_casing_differs(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_vm_name: Mock,
    azure_conf: AzureConf,
    caplog,
) -> None:
    """The public IP is not reassigned to the ipConfiguration that holds it.

    The target ipConfiguration ID is resolved on startup and the
    ipConfiguration ID of the public IP is read on every pass.  When
    the two report the resource group with different casing, the engine
    used to detach the public IP from its own NIC and attach it again
    on every pass.
    """
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
        },
    )
    get_vm_name.return_value = azure_conf.primary_vm_name
    clients = (azure_conf.compute_client, azure_conf.network_client)

    # The resolved target reports the WAN NIC ipConfiguration with an
    # upper-cased resource group, the public IP keeps the original casing.
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_ids[0],
        internal_ip=azure_conf.primary_ips[0],
        public_ip_targets=[
            (network_id("publicIPAddresses",
                        azure_conf.reserved_public_ip_name),
             _recase_resource_group(
                 azure_conf.primary_ip_config_ids[1],
                 azure_conf.resource_group
             ),
             "203.0.113.10"),
        ],
    )
    create_local_net_context.return_value = primary_net_ctx

    # The primary is active and already holds the public IP.
    azure_conf.state.route_tables[0]["properties"]["routes"] = [
        {
            "name": "default",
            "properties": {
                "addressPrefix": "0.0.0.0/0",
                "nextHopType": "VirtualAppliance",
                "nextHopIpAddress": azure_conf.primary_ips[0],
            },
        },
    ]
    get_local_status.return_value = "online"

    nic_before = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    pip_before = nic_before["properties"]["ipConfigurations"][0][
        "properties"
    ].get("publicIPAddress", {})

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=True,
    )
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    nic_after = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, azure_conf.primary_nic_names[1]
    )
    pip_after = nic_after["properties"]["ipConfigurations"][0][
        "properties"
    ].get("publicIPAddress", {})

    assert pip_after.get("id", "") == pip_before.get("id", "")
    assert "Detaching public IP" not in caplog.text
    assert not [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]


def _public_ip_ids_on_nic(azure_conf: AzureConf, nic_name: str) -> set:
    """Return the public IP IDs referenced by a NIC's ipConfigurations."""
    nic = azure_conf.network_client.get_network_interface(
        azure_conf.resource_group, nic_name
    )
    return {
        ip_config["properties"]["publicIPAddress"]["id"]
        for ip_config in nic["properties"]["ipConfigurations"]
        if "publicIPAddress" in ip_config["properties"]
    }


def test_detach_public_ip_with_differently_cased_config_id(
    azure_conf: AzureConf,
) -> None:
    """A public IP is detached even if the configured ID casing differs."""
    pip_id = azure_conf.state.public_ips[0]["id"]
    clients = (azure_conf.compute_client, azure_conf.network_client)

    detach_public_ip(clients, _recase_resource_group(
        pip_id, azure_conf.resource_group
    ))

    assert pip_id not in _public_ip_ids_on_nic(
        azure_conf, azure_conf.primary_nic_names[1]
    )


def test_detach_public_ip_with_differently_cased_ip_configuration(
    azure_conf: AzureConf,
) -> None:
    """The NIC name is parsed from an ipConfiguration of any casing."""
    pip = azure_conf.state.public_ips[0]
    pip["properties"]["ipConfiguration"]["id"] = (
        pip["properties"]["ipConfiguration"]["id"]
        .replace("/networkInterfaces/", "/networkinterfaces/")
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    detach_public_ip(clients, pip["id"])

    assert pip["id"] not in _public_ip_ids_on_nic(
        azure_conf, azure_conf.primary_nic_names[1]
    )


def test_detach_public_ip_keeps_other_ip_configurations(
    azure_conf: AzureConf,
) -> None:
    """Only the ipConfiguration holding the public IP is changed."""
    pip_id = azure_conf.state.public_ips[0]["id"]
    other_pip_id = azure_conf.state.public_ips[1]["id"]
    clients = (azure_conf.compute_client, azure_conf.network_client)

    detach_public_ip(clients, pip_id)

    assert _public_ip_ids_on_nic(
        azure_conf, azure_conf.primary_nic_names[1]
    ) == {other_pip_id}


def test_detach_public_ip_without_nic_in_ip_configuration(
    azure_conf: AzureConf,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ipConfiguration with no NIC segment is warned about, not fatal."""
    pip = azure_conf.state.public_ips[0]
    pip["properties"]["ipConfiguration"]["id"] = "/subscriptions/s/pip"
    clients = (azure_conf.compute_client, azure_conf.network_client)

    with caplog.at_level(logging.WARNING):
        detach_public_ip(clients, pip["id"])

    assert "Cannot parse NIC name from ipConfiguration" in caplog.text


@patch("ha_script.azure.metadata.get_instance_id")
def test_instance_role_with_differently_cased_config_id(
    get_instance_id: Mock,
    azure_conf: AzureConf,
) -> None:
    """The engine recognises itself when the configured ID casing differs."""
    instance_id = azure_conf.state.vms[1]["id"]
    get_instance_id.return_value = instance_id

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=_recase_resource_group(
            azure_conf.state.vms[0]["id"], azure_conf.resource_group
        ),
        secondary_instance_id=_recase_resource_group(
            instance_id, azure_conf.resource_group
        ),
    )

    assert is_secondary(config)
    assert not is_primary(config)


@patch("ha_script.azure.metadata.get_instance_id")
def test_unknown_instance_still_rejected(
    get_instance_id: Mock,
    azure_conf: AzureConf,
) -> None:
    get_instance_id.return_value = "/subscriptions/s/virtualMachines/other-vm"

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.state.vms[0]["id"],
        secondary_instance_id=azure_conf.state.vms[1]["id"],
    )

    with pytest.raises(HAScriptConfigError):
        is_primary(config)


def test_config_tags_read_with_any_prefix_casing(
    azure_conf: AzureConf,
) -> None:
    """Tag names are case-insensitive for Azure, so they are here too."""
    azure_conf.state.vms[0]["tags"] = {
        "FP_HA_route_table_id": "rt-1",
        "fp_ha_probe_port": "22",
        "Fp_Ha_Debug": "true",
        "Environment": "production",
    }
    clients = (azure_conf.compute_client, azure_conf.network_client)

    assert get_config_tags(clients, azure_conf.primary_vm_name) == {
        "route_table_id": "rt-1",
        "probe_port": "22",
        "debug": "true",
    }


def test_set_config_tag_replaces_existing_case_variant(
    azure_conf: AzureConf,
) -> None:
    """Azure resolves tag names case-insensitively, so send only one."""
    azure_conf.state.vms[0]["tags"] = {"fp_ha_status": "online"}
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    assert set_config_tag(
        config, clients, "status", "offline", azure_conf.primary_vm_name
    )

    tags = azure_conf.state.vms[0]["tags"]
    status_keys = [key for key in tags if key.casefold() == "fp_ha_status"]
    assert len(status_keys) == 1
    assert tags[status_keys[0]] == "offline"


def _mangle(value: str, rg: str) -> str:
    """Return the ID as a different API might report it."""
    return (value
            .replace(f"/{rg}/", f"/{rg.upper()}/")
            .replace("/networkInterfaces/", "/networkinterfaces/")
            .replace("/ipConfigurations/", "/IPCONFIGURATIONS/"))


@patch("ha_script.azure.metadata.get_vm_name")
@patch("ha_script.azure.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_no_churn_when_public_ip_api_recases(
    send_notification_to_smc: Mock, tcp_probe: Mock, get_primary_status: Mock,
    get_local_status: Mock, create_local_net_context: Mock, get_vm_name: Mock,
    azure_conf: AzureConf, caplog,
):
    """Two polls, public IP API re-casing its ipConfiguration reference."""
    caplog.set_level(logging.INFO)

    # The publicIPAddresses API reports the assignee with mangled casing
    for pip in azure_conf.state.public_ips:
        pip["properties"]["ipConfiguration"]["id"] = _mangle(
            pip["properties"]["ipConfiguration"]["id"], azure_conf.resource_group
        )

    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
    )
    get_vm_name.return_value = azure_conf.primary_vm_name
    clients = (azure_conf.compute_client, azure_conf.network_client)

    ctx_net = api.LocalNetContext(
        internal_nic_id=azure_conf.primary_nic_ids[0],
        internal_ip=azure_conf.primary_ips[0],
        public_ip_targets=[
            (network_id("publicIPAddresses", azure_conf.reserved_public_ip_name),
             azure_conf.primary_ip_config_ids[1], "203.0.113.10"),
            (network_id("publicIPAddresses", azure_conf.reserved_public_ip_name_2),
             azure_conf.primary_ip_config_wan_2_id, "203.0.113.11"),
        ],
    )
    create_local_net_context.return_value = ctx_net
    azure_conf.state.route_tables[0]["properties"]["routes"] = [
        {"name": "default", "properties": {
            "addressPrefix": "0.0.0.0/0", "nextHopType": "VirtualAppliance",
            "nextHopIpAddress": azure_conf.primary_ips[0]}},
    ]
    get_local_status.return_value = "online"

    ctx = HAScriptContext(prev_local_status="online", prev_local_active=True)
    for _ in range(2):
        primary_main_loop_handler(config, clients, ctx, ctx_net)

    assert "Detaching public IP" not in caplog.text
    assert not [c for c in send_notification_to_smc.mock_calls
                if "Public IP address" in str(c) and "moved" in str(c)]


def test_move_finds_target_ip_config_despite_casing(azure_conf: AzureConf):
    """move_public_ip() matches the target ipConfiguration case-insensitively."""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
    )
    clients = (azure_conf.compute_client, azure_conf.network_client)

    assert api.move_public_ip(
        config, clients,
        network_id("publicIPAddresses", azure_conf.reserved_public_ip_name),
        _mangle(azure_conf.secondary_ip_config_ids[1],
                azure_conf.resource_group),
    )

    pip = azure_conf.network_client.get_public_ip(
        azure_conf.resource_group, azure_conf.reserved_public_ip_name)
    assert pip["properties"]["ipConfiguration"]["id"] == \
        azure_conf.secondary_ip_config_ids[1]
