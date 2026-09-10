"""
Tests for Azure API utilities module.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses
from conftest import AzureConf

from ha_script.config import HAScriptConfig
from ha_script.exceptions import HAScriptError
from ha_script.azure import api
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
        assert ctx.public_ip_targets == []
        # The remote probe source defaults to the internal NIC IP.
        assert ctx.remote_probe_src_ip == azure_conf.primary_ips[0]


def test_create_local_net_context_remote_probe_nic(
    azure_conf: AzureConf
) -> None:
    """remote_probe_nic_idx selects another NIC's private IP."""
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        internal_nic_idx=0,
        wan_nic_idx=1,
        remote_probe_nic_idx=1,
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

        assert ctx.remote_probe_src_ip == azure_conf.primary_ips[1]


def test_create_local_net_context_remote_probe_nic_out_of_bounds(
    azure_conf: AzureConf
) -> None:
    config = HAScriptConfig(
        route_table_id=azure_conf.protected_route_table_name,
        primary_instance_id=azure_conf.primary_vm_name,
        secondary_instance_id=azure_conf.secondary_vm_name,
        internal_nic_idx=0,
        wan_nic_idx=1,
        remote_probe_nic_idx=5,
    )

    clients = (
        azure_conf.compute_client, azure_conf.network_client
    )

    with patch(
        'ha_script.azure.metadata.get_vm_name'
    ) as mock_get_vm_name:
        mock_get_vm_name.return_value = \
            azure_conf.primary_vm_name

        with pytest.raises(HAScriptError,
                           match="Failed to find remote probe NIC at index 5"):
            create_local_net_context(config, clients)


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

    assert len(ip_list) == 3
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


# --- 401 retry tests ---

NIC_URL = (
    f"{api.ARM_BASE}/subscriptions/sub-id/resourceGroups/rg"
    f"/providers/Microsoft.Network/networkInterfaces/test-nic"
)


@responses.activate
def test_request_sends_api_version(network_client):
    """Every request carries the api-version the client was built with."""
    responses.get(NIC_URL, json={"id": "nic"}, status=200)

    network_client.get("rg", "/networkInterfaces/test-nic")

    assert f"api-version={api.API_VERSION}" in responses.calls[0].request.url


@responses.activate
def test_request_retries_on_401_then_succeeds(network_client, caplog):
    """A 401 on the initial request triggers token refresh and retry."""
    responses.get(NIC_URL, status=401)
    responses.get(NIC_URL, json={"id": "nic"}, status=200)

    with caplog.at_level(logging.WARNING, logger="ha_script.azure.api"):
        result = network_client.get("rg", "/networkInterfaces/test-nic")

    assert result == {"id": "nic"}
    assert len(responses.calls) == 2
    network_client._signer.invalidate.assert_called_once()
    assert "401" in caplog.text


@responses.activate
def test_request_raises_on_double_401(network_client):
    """Two consecutive 401s should raise HTTPError."""
    responses.get(NIC_URL, status=401)
    responses.get(NIC_URL, status=401)

    with pytest.raises(requests.HTTPError):
        network_client.get("rg", "/networkInterfaces/test-nic")

    assert len(responses.calls) == 2
    network_client._signer.invalidate.assert_called_once()


PUBLIC_IPS_URL = (
    f"{api.ARM_BASE}/subscriptions/sub-id/resourceGroups/rg"
    f"/providers/Microsoft.Network/publicIPAddresses"
)
# Azure returns nextLink as an absolute URL that already carries its
# api-version, plus an opaque continuation token.
PUBLIC_IPS_PAGE_1_URL = f"{PUBLIC_IPS_URL}?api-version={api.API_VERSION}"
PUBLIC_IPS_PAGE_2_URL = (
    f"{PUBLIC_IPS_URL}?api-version={api.API_VERSION}&%24skipToken=opaque"
)


def _public_ip(name: str, ip_address: str) -> dict:
    return {
        "name": name,
        "id": (
            f"/subscriptions/sub-id/resourceGroups/rg/providers"
            f"/Microsoft.Network/publicIPAddresses/{name}"
        ),
        "properties": {"ipAddress": ip_address},
    }


@responses.activate
def test_get_public_ip_by_ip_address_first_page(network_client):
    """A public IP on the first page is matched without further calls."""
    responses.get(
        PUBLIC_IPS_PAGE_1_URL,
        json={"value": [_public_ip("pip-1", "203.0.113.10")]},
    )

    public_ip = network_client.get_public_ip_by_ip_address(
        "rg", "203.0.113.10"
    )

    assert public_ip["name"] == "pip-1"
    assert len(responses.calls) == 1


@responses.activate
def test_get_public_ip_by_ip_address_follows_next_link(network_client):
    """The list operation is paged, so nextLink pages are read too.

    A public IP that only appears on a later page must still be found;
    stopping at the first page would fail to resolve it.
    """
    responses.get(
        PUBLIC_IPS_PAGE_1_URL,
        json={
            "value": [_public_ip("pip-1", "203.0.113.10")],
            "nextLink": PUBLIC_IPS_PAGE_2_URL,
        },
    )
    responses.get(
        PUBLIC_IPS_PAGE_2_URL,
        json={"value": [_public_ip("pip-2", "203.0.113.11")]},
    )

    public_ip = network_client.get_public_ip_by_ip_address(
        "rg", "203.0.113.11"
    )

    assert public_ip["name"] == "pip-2"
    assert len(responses.calls) == 2
    # The second call goes to the nextLink URL exactly as Azure gave it
    assert responses.calls[1].request.url == PUBLIC_IPS_PAGE_2_URL


@responses.activate
def test_get_public_ip_by_ip_address_not_found(network_client):
    """None is returned when no public IP in the group holds the IP."""
    responses.get(
        PUBLIC_IPS_PAGE_1_URL,
        json={"value": [_public_ip("pip-1", "203.0.113.10")]},
    )

    assert network_client.get_public_ip_by_ip_address(
        "rg", "198.51.100.99"
    ) is None


@responses.activate
def test_get_paged_retries_next_link_on_401(network_client):
    """A 401 on a nextLink page refreshes the token and retries."""
    responses.get(
        PUBLIC_IPS_PAGE_1_URL,
        json={"value": [], "nextLink": PUBLIC_IPS_PAGE_2_URL},
    )
    responses.get(PUBLIC_IPS_PAGE_2_URL, status=401)
    responses.get(
        PUBLIC_IPS_PAGE_2_URL,
        json={"value": [_public_ip("pip-2", "203.0.113.11")]},
    )

    public_ip = network_client.get_public_ip_by_ip_address(
        "rg", "203.0.113.11"
    )

    assert public_ip["name"] == "pip-2"
    assert len(responses.calls) == 3
    network_client._signer.invalidate.assert_called_once()


def test_routes_not_via_ngfw_logged_compactly(
    azure_conf: AzureConf, caplog
) -> None:
    """Every route that is not acted on is named on one line.

    A route table normally holds routes that bypass the NGFW on
    purpose.  They are of no interest until something has to be
    diagnosed, so they are logged as one line rather than one per
    route.
    """
    caplog.set_level(logging.DEBUG)
    clients = (azure_conf.compute_client, azure_conf.network_client)

    list(get_route_table_info(
        clients,
        azure_conf.protected_route_table_name,
        [azure_conf.primary_vm_name, azure_conf.secondary_vm_name],
    ))

    reported = [
        r for r in caplog.records if "routes not via NGFW" in r.getMessage()
    ]
    assert len(reported) == 1
    message = reported[0].getMessage()
    # The local route bypasses the NGFW, and is named with its next hop
    assert "local 10.0.0.0/16 VnetLocal" in message
    # A virtual appliance that is not an NGFW is reported with its address
    assert (
        f"other 192.168.0.0/24 VirtualAppliance {azure_conf.other_ip}"
        in message
    )
    # The route via the NGFW is acted on, not reported as skipped
    assert "default" not in message
