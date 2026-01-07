"""
Tests for Azure IMDS metadata module.

Uses responses to mock HTTP, exercising the full
get_metadata -> HTTP path.
"""
import responses

from ha_script.azure.metadata import (
    METADATA_URL,
    get_instance_metadata,
    get_vm_name,
    get_resource_group,
    get_subscription_id,
    get_identity_token,
)


INSTANCE_URL = f"{METADATA_URL}/instance"
TOKEN_URL = f"{METADATA_URL}/identity/oauth2/token"

INSTANCE_RESPONSE = {
    "compute": {
        "name": "test-vm",
        "resourceGroupName": "test-rg",
        "subscriptionId": "00000000-0000-0000-0000-000000000000",
        "location": "eastus",
    },
    "network": {
        "interface": []
    }
}


@responses.activate
def test_get_instance_metadata():
    responses.add(responses.GET, INSTANCE_URL,
                  json=INSTANCE_RESPONSE, status=200)

    data = get_instance_metadata()
    assert data["compute"]["name"] == "test-vm"


@responses.activate
def test_get_vm_name():
    responses.add(responses.GET, INSTANCE_URL,
                  json=INSTANCE_RESPONSE, status=200)

    assert get_vm_name() == "test-vm"


@responses.activate
def test_get_resource_group():
    responses.add(responses.GET, INSTANCE_URL,
                  json=INSTANCE_RESPONSE, status=200)

    assert get_resource_group() == "test-rg"


@responses.activate
def test_get_subscription_id():
    responses.add(responses.GET, INSTANCE_URL,
                  json=INSTANCE_RESPONSE, status=200)

    assert get_subscription_id() == \
        "00000000-0000-0000-0000-000000000000"


@responses.activate
def test_get_identity_token():
    responses.add(responses.GET, TOKEN_URL,
                  json={
                      "access_token": "token-value",
                      "expires_on": "1700000000",
                  }, status=200)

    data = get_identity_token()
    assert data["access_token"] == "token-value"
    assert "resource=https" in responses.calls[0].request.url
