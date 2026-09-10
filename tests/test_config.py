import logging
import pytest
from unittest.mock import patch

from conftest import (
    ROUTE_TABLE_ID,
    PRIMARY_VM_ID,
    SECONDARY_VM_ID,
    network_id,
)

from ha_script.config import load_config
from ha_script.exceptions import HAScriptConfigError


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_instance_tags(read_custom_properties_file, caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = {}

    config = load_config({
        "probe_enabled": "false",
        "probe_port": 1234,
        "probe_timeout_sec": "7",
        "remote_probe_enabled": "true",
        "remote_probe_ip": "1.2.3.4",
        "remote_probe_port": 2222,
        "route_table_id": ROUTE_TABLE_ID,
        "primary_instance_id": PRIMARY_VM_ID,
        "secondary_instance_id": SECONDARY_VM_ID,
        "internal_nic_idx": 1,
    })

    assert config.probe_max_fail == 10
    assert config.probe_port == 1234
    assert config.probe_timeout_sec == 7
    assert not config.probe_enabled
    assert config.remote_probe_enabled
    assert config.remote_probe_ip == "1.2.3.4"
    assert config.remote_probe_port == 2222
    assert config.route_table_id == ROUTE_TABLE_ID
    assert config.primary_instance_id == PRIMARY_VM_ID
    assert config.secondary_instance_id == SECONDARY_VM_ID
    assert config.internal_nic_idx == 1
    assert config.wan_nic_idx == 1
    assert not config.reserved_public_ips

    assert len(caplog.records) == 1


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_custom_properties(read_custom_properties_file, caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = {
        "probe_enabled": "false",
        "probe_port": 1234,
        "probe_timeout_sec": "7",
        "remote_probe_enabled": "true",
        "remote_probe_ip": "1.2.3.4",
        "remote_probe_port": 2222,
        "route_table_id": ROUTE_TABLE_ID,
        "primary_instance_id": PRIMARY_VM_ID,
        "secondary_instance_id": SECONDARY_VM_ID,
        "internal_nic_idx": 1,
    }

    config = load_config({})

    assert config.probe_max_fail == 10
    assert config.probe_port == 1234
    assert config.probe_timeout_sec == 7
    assert not config.probe_enabled
    assert config.remote_probe_enabled
    assert config.remote_probe_ip == "1.2.3.4"
    assert config.remote_probe_port == 2222
    assert config.route_table_id == ROUTE_TABLE_ID
    assert config.primary_instance_id == PRIMARY_VM_ID
    assert config.secondary_instance_id == SECONDARY_VM_ID
    assert config.internal_nic_idx == 1
    assert config.wan_nic_idx == 1
    assert not config.reserved_public_ips

    assert len(caplog.records) == 1


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_merged_sources(read_custom_properties_file, caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = {
        "probe_enabled": "false",
        "probe_port": 1234,
        "probe_timeout_sec": "7",
        "remote_probe_enabled": "true",
        "remote_probe_ip": "1.2.3.4",
        "remote_probe_port": 2222,
    }

    config = load_config({
        "route_table_id": ROUTE_TABLE_ID,
        "primary_instance_id": PRIMARY_VM_ID,
        "secondary_instance_id": SECONDARY_VM_ID,
        "internal_nic_idx": 1,
    })

    assert config.probe_max_fail == 10
    assert config.probe_port == 1234
    assert config.probe_timeout_sec == 7
    assert not config.probe_enabled
    assert config.remote_probe_enabled
    assert config.remote_probe_ip == "1.2.3.4"
    assert config.remote_probe_port == 2222
    assert config.route_table_id == ROUTE_TABLE_ID
    assert config.primary_instance_id == PRIMARY_VM_ID
    assert config.secondary_instance_id == SECONDARY_VM_ID
    assert config.internal_nic_idx == 1
    assert config.wan_nic_idx == 1
    assert not config.reserved_public_ips

    assert len(caplog.records) == 1


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_mandatory_property_missing(read_custom_properties_file,
                                                caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = dict(
        primary_instance_id=PRIMARY_VM_ID,
        secondary_instance_id=SECONDARY_VM_ID,
        internal_nic_idx=1,
        probe_enabled="true",
        probe_port=1234,
        probe_timeout_sec="7",
    )

    with pytest.raises(HAScriptConfigError) as e:
        load_config({})
    assert str(e.value) == "Mandatory property is missing: route_table_id"


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_mandatory_invalid_route_table_id(
    read_custom_properties_file,
    caplog
):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = dict(
        route_table_id="1234",
        primary_instance_id=PRIMARY_VM_ID,
        secondary_instance_id=SECONDARY_VM_ID,
        internal_nic_idx=1,
        probe_enabled="true",
        probe_port=1234,
        probe_timeout_sec="7",
    )

    with pytest.raises(HAScriptConfigError) as e:
        load_config({})
    assert str(e.value) == \
        "Value for 'route_table_id' should start with '/subscriptions/': 1234"


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_invalid_probe_ip(read_custom_properties_file, caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = dict(
        route_table_id=ROUTE_TABLE_ID,
        primary_instance_id=PRIMARY_VM_ID,
        secondary_instance_id=SECONDARY_VM_ID,
        internal_nic_idx=1,
        probe_enabled="true",
        probe_port=1234,
        probe_timeout_sec="7",
        probe_ip="not an ip address",
    )

    with pytest.raises(HAScriptConfigError) as e:
        load_config({})
    assert str(e.value) == \
        "Value for 'probe_ip' is not an IP address: not an ip address"


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_invalid_remote_probe_ip(read_custom_properties_file,
                                             caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = dict(
        route_table_id=ROUTE_TABLE_ID,
        primary_instance_id=PRIMARY_VM_ID,
        secondary_instance_id=SECONDARY_VM_ID,
        internal_nic_idx=1,
        probe_enabled="true",
        probe_port=1234,
        probe_timeout_sec="7",
        remote_probe_ip="not an ip address",
    )

    with pytest.raises(HAScriptConfigError) as e:
        load_config({})
    assert str(e.value) == \
        "Value for 'remote_probe_ip' is not an IP address: not an ip address"


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_missing_remote_probe_ip(read_custom_properties_file,
                                             caplog):
    caplog.set_level(logging.INFO)

    read_custom_properties_file.return_value = dict(
        route_table_id=ROUTE_TABLE_ID,
        primary_instance_id=PRIMARY_VM_ID,
        secondary_instance_id=SECONDARY_VM_ID,
        internal_nic_idx=1,
        probe_enabled="false",
        probe_port=1234,
        probe_timeout_sec="7",
        remote_probe_enabled="true",
    )

    with pytest.raises(HAScriptConfigError) as e:
        load_config({})
    assert str(e.value) == "Mandatory property is missing: remote_probe_ip"


MOCK_MANDATORY_TAGS = {
    "route_table_id": ROUTE_TABLE_ID,
    "primary_instance_id": PRIMARY_VM_ID,
    "secondary_instance_id": SECONDARY_VM_ID,
    "internal_nic_idx": 0,
}


@patch("ha_script.config._read_custom_properties_file")
def test_probe_ip_comma_separated_valid(read_custom_properties_file):
    read_custom_properties_file.return_value = {}
    config = load_config({
        **MOCK_MANDATORY_TAGS,
        "probe_ip": "10.0.1.1,10.0.1.2,10.0.1.3",
    })
    assert config.probe_ip == "10.0.1.1,10.0.1.2,10.0.1.3"


@patch("ha_script.config._read_custom_properties_file")
def test_probe_ip_comma_separated_invalid_entry(read_custom_properties_file):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "probe_ip": "10.0.1.1,not-an-ip,10.0.1.3",
        })
    assert "not-an-ip" in str(exc_info.value)
    # The whole raw string must NOT appear as-is in the error
    assert "10.0.1.1,not-an-ip,10.0.1.3" not in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_remote_probe_ip_comma_separated_valid(read_custom_properties_file):
    read_custom_properties_file.return_value = {}
    config = load_config({
        **MOCK_MANDATORY_TAGS,
        "remote_probe_ip": "192.168.1.1, 192.168.1.2",
    })
    assert config.remote_probe_ip == "192.168.1.1, 192.168.1.2"


@patch("ha_script.config._read_custom_properties_file")
def test_remote_probe_ip_comma_separated_invalid_entry(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "remote_probe_ip": "192.168.1.1,bad-addr",
        })
    assert "bad-addr" in str(exc_info.value)
    assert "192.168.1.1,bad-addr" not in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_remote_probe_nic_idx_and_grace(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    config = load_config({
        **MOCK_MANDATORY_TAGS,
        "remote_probe_nic_idx": "2",
        "remote_probe_grace_sec": "45",
    })
    assert config.remote_probe_nic_idx == 2
    assert config.remote_probe_grace_sec == 45


@patch("ha_script.config._read_custom_properties_file")
def test_load_config_invalid_remote_probe_nic_idx(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "remote_probe_nic_idx": "-2",
        })
    assert "remote_probe_nic_idx" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_remote_probe_grace_sec_defaults_to_180(read_custom_properties_file):
    read_custom_properties_file.return_value = {}
    config = load_config({**MOCK_MANDATORY_TAGS})
    assert config.remote_probe_grace_sec == 180


@patch("ha_script.config._read_custom_properties_file")
def test_invalid_remote_probe_grace_sec(read_custom_properties_file):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "remote_probe_grace_sec": "-1",
        })
    assert "remote_probe_grace_sec" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_legacy_format_valid(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    public_ip_id = network_id("publicIPAddresses", "my-pip")
    config = load_config({
        **MOCK_MANDATORY_TAGS,
        "reserved_public_ip_id": public_ip_id,
    })
    assert config.reserved_public_ips == {"id": public_ip_id}


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_legacy_format_invalid(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_id": "not-a-resource-id",
        })
    assert "not-a-resource-id" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_triplet_valid(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    config = load_config({
        **MOCK_MANDATORY_TAGS,
        "reserved_public_ip_vpn": "203.0.113.10,10.0.1.5,10.0.2.5",
    })
    assert config.reserved_public_ips == {
        "vpn": "203.0.113.10,10.0.1.5,10.0.2.5"
    }


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_triplet_whitespace_valid(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    config = load_config({
        **MOCK_MANDATORY_TAGS,
        "reserved_public_ip_vpn": "203.0.113.10, 10.0.12.10, 10.0.22.10",
    })
    assert config.reserved_public_ips == {
        "vpn": "203.0.113.10, 10.0.12.10, 10.0.22.10"
    }


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_two_parts_rejected(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_vpn": "203.0.113.10,10.0.1.5",
        })
    assert "3 comma-separated" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_invalid_ip_address(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_vpn": "not-an-ip,10.0.1.5,10.0.2.5",
        })
    assert "invalid IP address" in str(exc_info.value)
    assert "not-an-ip" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_empty_parts(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_vpn": "203.0.113.10,,10.0.2.5",
        })
    assert "invalid IP address" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_all_empty(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_vpn": ",,",
        })
    assert "invalid IP address" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_multiple_legacy_rejected(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_id": network_id(
                "publicIPAddresses", "my-pip"
            ),
            "reserved_public_ip_web": network_id(
                "publicIPAddresses", "my-pip-2"
            ),
        })
    assert "Only one resource ID-format" in str(exc_info.value)


@patch("ha_script.config._read_custom_properties_file")
def test_reserved_public_ip_mixed_formats_rejected(
    read_custom_properties_file,
):
    read_custom_properties_file.return_value = {}
    with pytest.raises(HAScriptConfigError) as exc_info:
        load_config({
            **MOCK_MANDATORY_TAGS,
            "reserved_public_ip_id": network_id(
                "publicIPAddresses", "my-pip"
            ),
            "reserved_public_ip_web": "203.0.113.11,10.0.1.6,10.0.2.6",
        })
    assert "Cannot mix resource ID and triplet formats" in str(exc_info.value)
