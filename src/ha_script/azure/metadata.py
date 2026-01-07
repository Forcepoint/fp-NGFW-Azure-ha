"""Azure Instance Metadata Service (IMDS) interface.

Queries the Azure IMDS at http://169.254.169.254 to retrieve
instance information such as VM name, resource group,
subscription ID, and network interface details.
"""
import logging
from typing import Any, Optional

import ha_script.azure as azure


LOGGER = logging.getLogger(__name__)

METADATA_URL = "http://169.254.169.254/metadata"
METADATA_HEADERS = {"Metadata": "true"}
API_VERSION = "2021-02-01"
ARM_RESOURCE = "https://management.azure.com/"


def get_metadata(path: str, params: Optional[dict] = None) -> Any:
    """Generic metadata retrieval.

    :param path: metadata path (appended to base URL)
    :param extra_params: additional query parameters
    :return: parsed JSON response
    """
    session = azure.session_with_retry()
    url = f"{METADATA_URL}/{path}"

    if not params:
        params = {"api-version": API_VERSION, "format": "json"}

    response = session.get(
        url, headers=METADATA_HEADERS, params=params, timeout=10
    )
    response.raise_for_status()
    return response.json()


def get_instance_metadata() -> dict[str, Any]:
    """Get full instance metadata.

    :return: instance metadata dict
    """
    return get_metadata("instance")


def get_vm_name() -> str:
    """Get the VM name.

    :return: VM name
    """
    data = get_instance_metadata()
    return data["compute"]["name"]


def get_resource_group() -> str:
    """Get the resource group name.

    :return: resource group name
    """
    data = get_instance_metadata()
    return data["compute"]["resourceGroupName"]


def get_subscription_id() -> str:
    """Get the subscription ID.

    :return: subscription ID
    """
    data = get_instance_metadata()
    return data["compute"]["subscriptionId"]


def get_instance_id() -> str:
    """Get the VM resource ID.

    :return: full ARM resource ID of this VM
    """
    data = get_instance_metadata()
    sub = data["compute"]["subscriptionId"]
    resource_group = data["compute"]["resourceGroupName"]
    name = data["compute"]["name"]
    return (
        f"/subscriptions/{sub}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{name}"
    )


def get_location() -> str:
    """Get the Azure region.

    :return: Azure region (e.g. "eastus")
    """
    data = get_instance_metadata()
    return data["compute"]["location"]


def get_network_interfaces() -> list[dict[str, Any]]:
    """Get network interface information from IMDS.

    :return: list of NIC metadata dicts with privateIpAddress
             and macAddress
    """
    data = get_instance_metadata()
    return data["network"]["interface"]


def get_identity_token() -> dict:
    """Fetch a managed identity token from IMDS.

    :return: identity token dict
    """
    return get_metadata(
        "identity/oauth2/token",
        params={"api-version": API_VERSION, "resource": ARM_RESOURCE},
    )
