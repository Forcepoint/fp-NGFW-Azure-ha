"""
Tests for Azure long-running operation (LRO) polling.
"""
from unittest.mock import patch

import pytest
import requests
import responses

from ha_script.azure import api


POLL_ASYNC_URL = "https://management.azure.com/subscriptions/sub/op/async/1"
POLL_LOCATION_URL = "https://management.azure.com/subscriptions/sub/op/loc/1"

# Matches the URL pattern built by AzureClient._request for a PUT
NIC_URL = (
    f"{api.ARM_BASE}/subscriptions/sub-id/resourceGroups/rg"
    f"/providers/Microsoft.Network/networkInterfaces/test-nic"
)


# --- No LRO header ---


@responses.activate
def test_lro_no_header_returns_immediately(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200)

    network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 1


# --- Azure-AsyncOperation header tests ---


@responses.activate
def test_lro_async_succeeds_on_first_poll(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 2


@responses.activate
def test_lro_async_polls_until_succeeded(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, json={"status": "InProgress"})
    responses.get(POLL_ASYNC_URL, json={"status": "InProgress"})
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    with patch("ha_script.azure.api.time.sleep"):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 4


@responses.activate
def test_lro_async_failed_raises(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, json={
        "status": "Failed",
        "error": {"code": "SomeError", "message": "something went wrong"},
    })

    with pytest.raises(requests.HTTPError, match="failed"):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


@responses.activate
def test_lro_async_canceled_raises(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, json={"status": "Canceled"})

    with pytest.raises(requests.HTTPError, match="canceled"):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


@responses.activate
def test_lro_async_respects_retry_after(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL,
                  json={"status": "InProgress"},
                  headers={"Retry-After": "5"})
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    with patch("ha_script.azure.api.time.sleep") as mock_sleep:
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    mock_sleep.assert_called_once_with(5)


@responses.activate
def test_lro_async_uses_default_interval_without_retry_after(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, json={"status": "InProgress"})
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    with patch("ha_script.azure.api.time.sleep") as mock_sleep:
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    mock_sleep.assert_called_once_with(api.LRO_POLL_INTERVAL)


@responses.activate
def test_lro_async_invalid_retry_after_falls_back_to_default(network_client):
    """Non-integer Retry-After falls back to LRO_POLL_INTERVAL."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL,
                  json={"status": "InProgress"},
                  headers={"Retry-After": "Wed, 28 Apr 2026 07:28:00 GMT"})
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    with patch("ha_script.azure.api.time.sleep") as mock_sleep:
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    mock_sleep.assert_called_once_with(api.LRO_POLL_INTERVAL)


@responses.activate
def test_lro_async_timeout_raises(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, json={"status": "InProgress"})

    with patch("ha_script.azure.api.time.monotonic") as mock_time, \
         patch("ha_script.azure.api.time.sleep"):
        mock_time.side_effect = [0, 0, api.LRO_TIMEOUT + 1]
        with pytest.raises(requests.HTTPError, match="timed out"):
            network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


# --- Location header tests ---


@responses.activate
def test_lro_location_succeeds_on_first_poll(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=200)

    network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 2


@responses.activate
def test_lro_location_polls_202_until_success(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=202)
    responses.get(POLL_LOCATION_URL, status=202)
    responses.get(POLL_LOCATION_URL, status=200)

    with patch("ha_script.azure.api.time.sleep"):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 4


@responses.activate
def test_lro_async_takes_precedence_over_location(network_client):
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={
                      "Azure-AsyncOperation": POLL_ASYNC_URL,
                      "Location": POLL_LOCATION_URL,
                  })
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 2
    assert responses.calls[1].request.url == POLL_ASYNC_URL


# --- Location error / edge-case tests ---


@responses.activate
def test_lro_location_poll_error_raises(network_client):
    """A non-retryable error during location polling should raise."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=403)

    with pytest.raises(requests.HTTPError):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


@responses.activate
def test_lro_location_timeout_raises(network_client):
    """Location polling stuck on 202 until deadline should raise."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=202)

    with patch("ha_script.azure.api.time.monotonic") as mock_time, \
         patch("ha_script.azure.api.time.sleep"):
        mock_time.side_effect = [0, 0, api.LRO_TIMEOUT + 1]
        with pytest.raises(requests.HTTPError, match="timed out"):
            network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


@responses.activate
def test_lro_location_respects_retry_after(network_client):
    """Location polling should honour the Retry-After header."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=202,
                  headers={"Retry-After": "7"})
    responses.get(POLL_LOCATION_URL, status=200)

    with patch("ha_script.azure.api.time.sleep") as mock_sleep:
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    mock_sleep.assert_called_once_with(7)


# --- Async-operation poll error test ---


@responses.activate
def test_lro_async_poll_error_raises(network_client):
    """A non-retryable error during async-operation polling should raise."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, status=403)

    with pytest.raises(requests.HTTPError):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


# --- 401 retry tests ---


@responses.activate
def test_lro_poll_retries_on_401(network_client):
    """A 401 during LRO polling triggers token refresh and retry."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, status=401)
    responses.get(POLL_ASYNC_URL, json={"status": "Succeeded"})

    network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    # 1 PUT + 1 poll 401 + 1 poll retry = 3
    assert len(responses.calls) == 3


@responses.activate
def test_lro_poll_raises_on_double_401(network_client):
    """Two consecutive 401s during LRO polling should raise HTTPError."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Azure-AsyncOperation": POLL_ASYNC_URL})
    responses.get(POLL_ASYNC_URL, status=401)
    responses.get(POLL_ASYNC_URL, status=401)

    with pytest.raises(requests.HTTPError):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})


@responses.activate
def test_lro_location_retries_on_401(network_client):
    """A 401 during location polling triggers token refresh and retry."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=401)
    responses.get(POLL_LOCATION_URL, status=200)

    network_client.update_network_interface("rg", "test-nic", {"id": "nic"})

    assert len(responses.calls) == 3


@responses.activate
def test_lro_location_raises_on_double_401(network_client):
    """Two consecutive 401s during location polling should raise."""
    responses.put(NIC_URL, json={"id": "nic"}, status=200,
                  headers={"Location": POLL_LOCATION_URL})
    responses.get(POLL_LOCATION_URL, status=401)
    responses.get(POLL_LOCATION_URL, status=401)

    with pytest.raises(requests.HTTPError):
        network_client.update_network_interface("rg", "test-nic", {"id": "nic"})
