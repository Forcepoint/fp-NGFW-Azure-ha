"""Azure ARM REST API clients and high-level HA functions.

Provides compute and network operations for managing NGFW HA
failover on Azure, including route table manipulation, VM tag
management, and public IP reassignment.
"""
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from collections.abc import Iterator

import requests
import requests.adapters


import ha_script.azure as az
import ha_script.azure.auth as auth
import ha_script.azure.metadata as metadata
from ha_script.config import HAScriptConfig
from ha_script.exceptions import HAScriptError
from ha_script.smc_events import send_error_to_smc


LOGGER = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
API_VERSION = "2024-07-01"

LRO_POLL_INTERVAL = 1   # seconds between async operation polls
LRO_TIMEOUT = 120       # maximum wait in seconds


# Type alias for Azure client tuple
AzureClients = tuple['ComputeClient', 'NetworkClient']


@dataclass
class LocalNetContext:
    # Internal NIC name. Resolved on startup.
    internal_nic_id: str

    # WAN NIC name. Resolved on startup.
    wan_nic_id: str

    # Internal network private IP address. Resolved on startup.
    internal_ip: str

    # WAN network private IP address. Resolved on startup.
    wan_ip: Optional[str] = None


@dataclass
class RouteInfo:
    # Route state (e.g. "ACTIVE", "blackhole")
    route_state: str

    # Route destination CIDR (e.g.  "0.0.0.0/0")
    route_dest: str

    # Not used in Azure (routes use IPs directly), kept for
    # compatibility
    target_ip_id: str

    # The next hop IP address
    target_ip: str

    # Route name within the route table
    vnic_id: str

    # Route table name
    route_table_id: str


class AzureClient:
    """Base Azure ARM REST API client."""

    def __init__(self, provider: str, api_version: str,
                 request_signer: auth.RequestSigner):
        self._session = az.session_with_retry()
        self._signer = request_signer
        self._sub = metadata.get_subscription_id()
        self._provider = provider
        self._api_version = api_version

    def _request(
        self,
        method: str,
        resource_group: str,
        path: str,
        body: Any = None,
    ) -> requests.Response:
        """Make an authenticated request to Azure ARM API."""
        url = (
            f"{ARM_BASE}/subscriptions/{self._sub}"
            f"/resourceGroups/{resource_group}/providers"
            f"/{self._provider}{path}"
        )
        response = self._session.request(
            method=method,
            url=url,
            auth=self._signer,
            json=body,
            params={"api-version": self._api_version},
            timeout=30,
        )
        if response.status_code == 401:
            LOGGER.warning(
                "Azure API returned 401, refreshing token and retrying: "
                "%s %s", method, url,
            )
            self._signer.invalidate()
            response = self._session.request(
                method=method,
                url=url,
                auth=self._signer,
                json=body,
                params={"api-version": self._api_version},
                timeout=30,
            )
        if not response.ok:
            LOGGER.error(
                "Azure API request failed: %s %s"
                " - Status: %d - Response: %s",
                method, url,
                response.status_code, response.text,
            )
        response.raise_for_status()

        if method in ("PUT", "PATCH", "POST", "DELETE"):
            self._poll_lro(response)

        return response

    def _poll_lro(self, response: requests.Response) -> None:
        """Poll an Azure long-running operation until it completes.

        Azure ARM mutating operations may be asynchronous. When the
        response includes an Azure-AsyncOperation or Location header,
        the caller must poll that URL until the operation reaches a
        terminal state.  Azure-AsyncOperation takes precedence over
        Location.  The Retry-After header, when present, dictates the
        polling interval.

        https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations
        """
        poll_url = (response.headers.get("Azure-AsyncOperation")
                    or response.headers.get("Location"))
        if not poll_url:
            return

        LOGGER.debug("Polling Azure async operation: %s", poll_url)
        deadline = time.monotonic() + LRO_TIMEOUT
        while time.monotonic() < deadline:
            poll_resp = self._session.get(
                poll_url,
                auth=self._signer,
                timeout=30,
            )
            if poll_resp.status_code == 401:
                LOGGER.warning(
                    "LRO poll returned 401, refreshing token and retrying:"
                    " %s", poll_url,
                )
                self._signer.invalidate()
                poll_resp = self._session.get(
                    poll_url,
                    auth=self._signer,
                    timeout=30,
                )
            if not poll_resp.ok:
                LOGGER.error(
                    "LRO poll failed: %s"
                    " - Status: %d - Response: %s",
                    poll_url, poll_resp.status_code, poll_resp.text,
                )
            poll_resp.raise_for_status()

            if "Azure-AsyncOperation" in response.headers:
                status = poll_resp.json().get("status", "")
                if status == "Succeeded":
                    LOGGER.debug("Azure async operation succeeded")
                    return
                if status in ("Failed", "Canceled"):
                    error = poll_resp.json().get("error", {})
                    raise requests.HTTPError(
                        f"Azure async operation {status.lower()}: "
                        f"{error.get('code')}: {error.get('message')}",
                        response=poll_resp,
                    )
            elif poll_resp.status_code != 202:
                # Location-based polling: 202 means still in progress,
                # any 2xx means complete.
                LOGGER.debug("Azure async operation (loc) succeeded")
                return

            try:
                retry_after = int(poll_resp.headers["Retry-After"])
            except (KeyError, ValueError, TypeError) as err:
                LOGGER.debug("Unable to read Retry-After header: %s", str(err))
                retry_after = LRO_POLL_INTERVAL

            LOGGER.debug("LRO status: %s, retrying in %ds...",
                         poll_resp.status_code, retry_after)
            time.sleep(retry_after)

        raise requests.HTTPError(
            f"Azure async operation timed out after {LRO_TIMEOUT}s",
            response=response,
        )

    def get(self, resource_group: str, path: str) -> Any:
        return self._request("GET", resource_group, path).json()

    def put(self, resource_group: str, path: str, body: Any) -> Any:
        return self._request("PUT", resource_group, path, body).json()

    def patch(self, resource_group: str, path: str, body: Any) -> Any:
        return self._request("PATCH", resource_group, path, body).json()


class ComputeClient(AzureClient):
    """Azure Compute ARM REST API client."""

    def __init__(self, request_signer: auth.RequestSigner):
        super().__init__("Microsoft.Compute", API_VERSION, request_signer)

    def get_vm(self, resource_group: str,
               vm_name: str) -> Any:
        """Get a VirtualMachine resource.

        GET .../Microsoft.Compute/virtualMachines/{vmName}

        Returns a VirtualMachine object containing tags (dict),
        properties.networkProfile.networkInterfaces (list of
        NetworkInterfaceReference with id fields).

        https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/get

        :param resource_group: Azure resource group name
        :param vm_name: VM name or full ARM resource ID
        :return: VirtualMachine dict
        """
        return self.get(
            resource_group,
            f"/virtualMachines/{_resource_name(vm_name)}",
        )

    def update_vm_tags(self, resource_group: str,
                       vm_name: str,
                       tags: dict[str, str]) -> Any:
        """Update tags on a VirtualMachine resource.

        PATCH .../Microsoft.Compute/virtualMachines/{vmName}

        Sends {"tags": {..}} to merge tags into the VM resource.
        Returns the updated VirtualMachine object.

        https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/update

        :param resource_group: Azure resource group name
        :param vm_name: VM name or full ARM resource ID
        :param tags: complete tag dict to set on the VM
        :return: updated VirtualMachine dict
        """
        return self.patch(
            resource_group,
            f"/virtualMachines/{_resource_name(vm_name)}",
            {"tags": tags},
        )


class NetworkClient(AzureClient):
    """Azure Network ARM REST API client."""

    def __init__(self, request_signer: auth.RequestSigner):
        super().__init__("Microsoft.Network", API_VERSION, request_signer)

    def get_network_interface(self, resource_group: str,
                              nic_name: str) -> Any:
        """Get a NetworkInterface resource.

        GET .../Microsoft.Network/networkInterfaces/{nicName}

        Returns a NetworkInterface object containing
        properties.ipConfigurations (list of
        NetworkInterfaceIPConfiguration, each with
        properties.privateIPAddress and optionally
        properties.publicIPAddress with an id reference).

        https://learn.microsoft.com/en-us/rest/api/virtualnetwork/network-interfaces/get

        :param resource_group: Azure resource group name
        :param nic_name: NIC name or full ARM resource ID
        :return: NetworkInterface dict
        """
        return self.get(
            resource_group,
            f"/networkInterfaces/{_resource_name(nic_name)}",
        )

    def update_network_interface(self, resource_group: str,
                                 nic_name: str,
                                 body: dict[str, Any]) -> Any:
        """Create or update a NetworkInterface resource.

        PUT .../Microsoft.Network/networkInterfaces/{nicName}

        Expects a full NetworkInterface object as body. Used to
        associate or dissociate a PublicIPAddress by setting or
        removing properties.publicIPAddress on an
        ipConfiguration entry.

        Returns the updated NetworkInterface object.

        https://learn.microsoft.com/en-us/rest/api/virtualnetwork/network-interfaces/create-or-update

        :param resource_group: Azure resource group name
        :param nic_name: NIC name or full ARM resource ID
        :param body: full NetworkInterface dict
        :return: updated NetworkInterface dict
        """
        return self.put(
            resource_group,
            f"/networkInterfaces/{_resource_name(nic_name)}",
            body,
        )

    def get_route_table(self, resource_group: str,
                        rt_name: str) -> Any:
        """Get a RouteTable resource.

        GET .../Microsoft.Network/routeTables/{routeTableName}

        Returns a RouteTable object containing properties.routes
        (list of Route, each with properties.addressPrefix,
        properties.nextHopType, and
        properties.nextHopIpAddress).

        https://learn.microsoft.com/en-us/rest/api/virtualnetwork/route-tables/get

        :param resource_group: Azure resource group name
        :param rt_name: route table name or full ARM resource ID
        :return: RouteTable dict
        """
        return self.get(
            resource_group,
            f"/routeTables/{_resource_name(rt_name)}",
        )

    def update_route(self, resource_group: str, rt_name: str,
                     route_name: str,
                     body: dict[str, Any]) -> Any:
        """Create or update a Route within a RouteTable.

        PUT .../Microsoft.Network/routeTables/{rtName}/routes/{routeName}

        Expects a Route object as body with properties containing
        addressPrefix, nextHopType, and nextHopIpAddress.

        Returns the updated Route object.

        https://learn.microsoft.com/en-us/rest/api/virtualnetwork/routes/create-or-update

        :param resource_group: Azure resource group name
        :param rt_name: route table name or full ARM resource ID
        :param route_name: route name or full ARM resource ID
        :param body: Route dict
        :return: updated Route dict
        """
        return self.put(
            resource_group,
            (
                f"/routeTables/{_resource_name(rt_name)}"
                f"/routes/{_resource_name(route_name)}"
            ),
            body,
        )

    def get_public_ip(self, resource_group: str,
                      public_ip_name: str) -> Any:
        """Get a PublicIPAddress resource.

        GET .../Microsoft.Network/publicIPAddresses/{public_ipName}

        Returns a PublicIPAddress object containing
        properties.ipAddress (the allocated IP string) and
        properties.ipConfiguration (SubResource with id pointing
        to the NetworkInterfaceIPConfiguration it is associated
        with, or absent if unassociated).

        https://learn.microsoft.com/en-us/rest/api/virtualnetwork/public-ip-addresses/get

        :param resource_group: Azure resource group name
        :param public_ip_name: public IP name or full ARM resource ID
        :return: PublicIPAddress dict
        """
        return self.get(
            resource_group,
            f"/publicIPAddresses/{_resource_name(public_ip_name)}",
        )


def _resource_name(resource_id: str) -> str:
    """Extract the resource name from a full ARM ID or plain name.

    "/subscriptions/.../publicIPAddresses/my-public_ip" -> "my-public_ip"
    "my-public_ip" -> "my-public_ip"
    """
    return resource_id.rsplit("/", 1)[-1]


def get_azure_clients() -> AzureClients:
    """Initialize and return Azure compute and network clients.

    Uses managed identity authentication via IMDS.

    :return: Azure cloud clients
    """
    try:
        request_signer = auth.RequestSigner()
        compute_client = ComputeClient(request_signer)
        network_client = NetworkClient(request_signer)
        return compute_client, network_client
    except Exception as e:
        LOGGER.critical(
            "Failed to initialize Azure clients: %s", str(e)
        )
        raise e from None


def get_config_tags(
    clients: AzureClients,
    instance_id: Optional[str] = None
) -> dict[str, Any]:
    """Create a dictionary config from Azure VM tags.

    Configuration properties are taken from VM tags. Only tags
    starting with 'FP_HA_' are considered.

    :param clients: Azure clients
    :param instance_id: VM name (or derive from IMDS)
    :return: dictionary of config properties
    """
    compute_client = clients[0]
    resource_group = metadata.get_resource_group()

    if not instance_id:
        instance_id = metadata.get_vm_name()

    try:
        vm = compute_client.get_vm(resource_group, instance_id)
        filtered_tags = {}
        tags = vm.get("tags", {})
        if tags:
            for key, value in tags.items():
                if key.startswith("FP_HA_"):
                    tag_key = key.replace("FP_HA_", "")
                    filtered_tags[tag_key] = value
        return filtered_tags
    except Exception as e:
        LOGGER.error("Failed to get VM tags: %s", str(e))
        return {}


def get_config_tag_value(
    clients: AzureClients,
    tag: str,
    instance_id: Optional[str] = None
) -> Optional[Any]:
    """Get value of a config property from Azure VM tags.

    :param clients: Azure clients
    :param tag: config property name
    :param instance_id: VM name
    :return: config property value or None
    """
    tags = get_config_tags(clients, instance_id)
    if tag in tags:
        return tags[tag]
    LOGGER.debug(
        "Azure VM tag not found, vm: %s, tag: %s",
        instance_id, tag
    )
    return None


def set_config_tag(
    config: HAScriptConfig,
    clients: AzureClients,
    tag: str,
    value: str,
    instance_id: Optional[str] = None
) -> bool:
    """Add a tag to the Azure VM.

    :param config: configuration from the main program
    :param clients: Azure clients
    :param tag: tag name
    :param value: value to set
    :param instance_id: VM name
    :return: True if the tag was set, False otherwise
    """
    compute_client = clients[0]
    resource_group = metadata.get_resource_group()

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not modify VM tag, key: FP_HA_%s,"
            " value: %s",
            tag, value
        )
        return True

    try:
        if not instance_id:
            instance_id = metadata.get_vm_name()

        vm = compute_client.get_vm(resource_group, instance_id)
        tags = vm.get("tags", {}).copy()
        tags[f"FP_HA_{tag}"] = value
        compute_client.update_vm_tags(resource_group, instance_id, tags)
        return True
    except Exception as e:
        send_error_to_smc(config, f"Failed to set Azure VM tag: {e}")
        return False


def get_ip_for_nic(clients: AzureClients, nic_id: str) -> str:
    """Get the primary private IP address of a NetworkInterface.

    Fetches the NetworkInterface resource and returns the
    privateIPAddress from its first ipConfiguration entry.

    :param clients: Azure clients
    :param nic_id: NIC name or full ARM resource ID
    :return: primary private IP address
    :raises HAScriptError: if ipConfigurations missing or empty
    """
    _, network_client = clients
    resource_group = metadata.get_resource_group()
    nic = network_client.get_network_interface(resource_group, nic_id)

    try:
        ip_configs = nic["properties"]["ipConfigurations"]
    except KeyError:
        raise HAScriptError(f"Failed to get NIC {nic_id} IP configuration")

    try:
        return ip_configs[0]["properties"]["privateIPAddress"]
    except (IndexError, KeyError):
        raise HAScriptError(f"Failed to find NIC {nic_id} private IP")


def create_local_net_context(
    config: HAScriptConfig,
    clients: AzureClients
) -> LocalNetContext:
    """Create a context from the instance networking.

    :param config: configuration from the main program
    :param clients: Azure clients
    :return: Instance of LocalNetContext
    :raises HAScriptError: if NICs not found
    """
    compute_client, network_client = clients
    resource_group = metadata.get_resource_group()

    # VM networkProfile lists NICs in attachment order.
    # We get IPs from the ARM NIC resources directly,
    # not IMDS (IMDS NIC order is unreliable).
    vm_name = metadata.get_vm_name()
    vm = compute_client.get_vm(resource_group, vm_name)

    try:
        vm_nic_refs = vm["properties"]["networkProfile"]["networkInterfaces"]
    except KeyError:
        raise HAScriptError(f"Failed to find NICs for VM {vm_name}")

    try:
        internal_nic_id = vm_nic_refs[config.internal_nic_idx]["id"]
    except (IndexError, KeyError):
        raise HAScriptError(
            f"Failed to find internal NIC at index {config.internal_nic_idx}"
        )
    internal_ip = get_ip_for_nic(clients, internal_nic_id)

    wan_nic_id, wan_ip = None, None
    if config.wan_nic_idx is not None:
        try:
            wan_nic_id = vm_nic_refs[config.wan_nic_idx]["id"]
        except (IndexError, KeyError):
            raise HAScriptError(
                f"Failed to find wan NIC at index {config.wan_nic_idx}"
            )
        wan_ip = get_ip_for_nic(clients, wan_nic_id)

    ctx = LocalNetContext(
        internal_nic_id=internal_nic_id,
        internal_ip=internal_ip,
        wan_nic_id=wan_nic_id,
        wan_ip=wan_ip,
    )
    LOGGER.info("created local network context: %s", ctx)
    return ctx


def get_route_table_info(
    clients: AzureClients,
    route_table_ids: str,
    ngfw_instance_ids: list[str]
) -> Iterator[RouteInfo]:
    """Iterate over all routes via NGFWs from the route tables.

    :param clients: Azure clients
    :param route_table_ids: comma-separated route table names
    :param ngfw_instance_ids: list of NGFW VM names
    :return: yields RouteInfo per route found
    """
    network_client = clients[1]
    resource_group = metadata.get_resource_group()

    # Collect all NGFW IPs for matching
    ngfw_ips = set()
    for vm_name in ngfw_instance_ids:
        for ip in get_instance_ip_addresses(clients, vm_name):
            ngfw_ips.add(ip)

    for rt_id in route_table_ids.split(","):
        rt_name = rt_id.strip()
        route_table = network_client.get_route_table(resource_group, rt_name)

        for route in route_table.get(
            "properties", {}
        ).get("routes", []):
            props = route.get("properties", {})
            next_hop_type = props.get("nextHopType", "")
            next_hop_ip = props.get("nextHopIpAddress", "")

            if next_hop_type == "None" or (
                next_hop_type == "VirtualAppliance"
                and not next_hop_ip
            ):
                LOGGER.warning(
                    "route with blackhole/no next hop: %s",
                    props.get("addressPrefix", "<unknown>"),
                )
                yield RouteInfo(
                    route_state="blackhole",
                    route_dest=props.get(
                        "addressPrefix", ""
                    ),
                    target_ip_id="",
                    target_ip="",
                    vnic_id=route.get("name", ""),
                    route_table_id=rt_name,
                )
                continue

            if next_hop_type != "VirtualAppliance":
                LOGGER.warning(
                    "route with non-VirtualAppliance type next hop: %s",
                    route["name"],
                )
                continue

            if next_hop_ip in ngfw_ips:
                yield RouteInfo(
                    route_state="ACTIVE",
                    route_dest=props["addressPrefix"],
                    target_ip_id="",
                    target_ip=next_hop_ip,
                    vnic_id=route["name"],
                    route_table_id=rt_name,
                )


def update_route_table(
    config: HAScriptConfig,
    clients: AzureClients,
    route_table_id: str,
    dest: str,
    local_net_ctx: LocalNetContext
) -> bool:
    """Update an Azure route table route.

    :param config: configuration from the main program
    :param clients: Azure clients
    :param route_table_id: route table name
    :param dest: destination CIDR
    :param local_net_ctx: Local network context
    :return: True if successful, False otherwise
    """
    network_client = clients[1]
    resource_group = metadata.get_resource_group()

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not modify route, dest: %s,"
            " next_hop: %s",
            dest, local_net_ctx.internal_ip,
        )
        return True

    try:
        route_table = network_client.get_route_table(
            resource_group,
            route_table_id
        )
    except Exception as e:
        send_error_to_smc(config, f"Unable to read routes from API: {e}")
        return False

    route_name = None
    for route in route_table.get("properties", {}).get("routes", []):
        props = route.get("properties", {})
        if props.get("addressPrefix") == dest:
            route_name = route["name"]
            break

    if not route_name:
        LOGGER.warning("Route rule not found for destination: %s", dest)
        return False

    LOGGER.info(
        "Modifying route, dest: %s, next_hop: %s",
        dest, local_net_ctx.internal_ip,
    )

    try:
        network_client.update_route(
            resource_group,
            route_table_id,
            route_name,
            {
                "name": route_name,
                "properties": {
                    "addressPrefix": dest,
                    "nextHopType": "VirtualAppliance",
                    "nextHopIpAddress":
                        local_net_ctx.internal_ip,
                }
            },
        )
    except Exception as e:
        send_error_to_smc(config, f"Failed to update routes: {e}")
        return False

    LOGGER.info("Modifying route done.")
    return True


def detach_public_ip(clients: AzureClients, public_ip_id: str) -> None:
    """Detach a PublicIPAddress from its currently associated NIC.

    Resolves the current assignee from the PublicIPAddress
    resource's properties.ipConfiguration, then removes the
    publicIPAddress reference from that NIC's ipConfiguration.

    Does nothing if the public IP is not currently associated.

    :param clients: Azure clients
    :param public_ip_id: public IP name or full ARM resource ID
    """
    network_client = clients[1]
    resource_group = metadata.get_resource_group()
    public_ip = network_client.get_public_ip(resource_group, public_ip_id)

    try:
        ip_config = public_ip["properties"]["ipConfiguration"]
    except KeyError:
        LOGGER.debug(
            f"Attempted to detach unassigned public ip {public_ip_id}"
        )
        return

    try:
        ip_config_id = ip_config["id"]
    except KeyError:
        LOGGER.debug(f"Public IP {public_ip_id} IP configuration has no ID")
        return

    # There might be a better way to do this.  Gets the nic name by parsing
    # assignee ID from PIP ipConfiguration.
    parts = ip_config_id.split("/")
    try:
        nic_name = parts[parts.index("networkInterfaces") + 1]
    except (ValueError, IndexError):
        LOGGER.warning(
            "Cannot parse NIC name from ipConfiguration: %s",
            ip_config_id,
        )
        return

    LOGGER.info("Detaching public IP from NIC '%s'.", nic_name)
    nic = network_client.get_network_interface(resource_group, nic_name)

    # Find the matching ipConfiguration
    for nic_ip_config in nic["properties"]["ipConfigurations"]:
        try:
            nic_public_ip = nic_ip_config["properties"]["publicIPAddress"]
        except KeyError:
            continue

        if nic_public_ip["id"] == public_ip_id:
            LOGGER.debug(
                f"Removing matching public IP {public_ip_id} from NIC"
            )
            del nic_ip_config["properties"]["publicIPAddress"]

    network_client.update_network_interface(resource_group, nic_name, nic)


def resolve_public_ip(
    config: HAScriptConfig,
    clients: AzureClients
) -> tuple[Optional[str], Optional[str]]:
    """Get a public IP and its associated NIC IP config.

    :param config: configuration from the main program
    :param clients: Azure clients
    :return: tuple of (public IP address, NIC ipConfig ID)
    """
    network_client = clients[1]
    resource_group = metadata.get_resource_group()

    public_ip = network_client.get_public_ip(resource_group,
                                             config.reserved_public_ip_id)
    if not public_ip:
        raise HAScriptError(
            f"Unable to resolve public IP {config.reserved_public_ip_id}"
        )

    ip_addr = public_ip["properties"]["ipAddress"]
    ip_config = public_ip["properties"].get("ipConfiguration")
    ip_config_id = ip_config["id"] if ip_config else None
    return ip_addr, ip_config_id


def move_public_ip(
    config: HAScriptConfig,
    clients: AzureClients,
    local_net_ctx: LocalNetContext
) -> bool:
    """Move reserved public IP to the local instance's WAN NIC.

    :param config: configuration from the main program
    :param clients: Azure clients
    :param local_net_ctx: Local network context
    :return: True if successful, False otherwise
    """
    network_client = clients[1]
    resource_group = metadata.get_resource_group()

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not move public ip '%s',"
            " wan_nic: %s",
            config.reserved_public_ip_id, local_net_ctx.wan_nic_id,
        )
        return True

    if not local_net_ctx.wan_ip:
        raise HAScriptError("move_public_ip() called with incomplete context")

    LOGGER.info(
        "Moving public IP '%s' to WAN NIC '%s'.",
        config.reserved_public_ip_id, local_net_ctx.wan_nic_id,
    )

    # Detach the reserved public IP from its current NIC
    detach_public_ip(clients, config.reserved_public_ip_id)

    # Associate with the new WAN NIC
    wan_nic_id = local_net_ctx.wan_nic_id
    nic = network_client.get_network_interface(resource_group, wan_nic_id)
    nic["properties"]["ipConfigurations"][0][
        "properties"
    ]["publicIPAddress"] = {"id": config.reserved_public_ip_id}
    network_client.update_network_interface(resource_group, wan_nic_id, nic)

    LOGGER.info(
        "Public IP '%s' has been moved to '%s'.",
        config.reserved_public_ip_id, wan_nic_id,
    )
    return True


def get_instance_ip_addresses(
    clients: AzureClients,
    instance_id: str
) -> list[str]:
    """Get all private IP addresses from the given Azure VM.

    :param clients: Azure clients
    :param instance_id: VM name
    :return: list of private IP addresses
    """
    compute_client, network_client = clients
    resource_group = metadata.get_resource_group()

    try:
        vm = compute_client.get_vm(resource_group, instance_id)
    except Exception as e:
        LOGGER.error("Failed to find VM %s: %s", instance_id, str(e))
        return []

    try:
        nic_refs = vm["properties"]["networkProfile"]["networkInterfaces"]
    except KeyError:
        LOGGER.error("Failed to get NICs for VM %s", instance_id)
        return []

    ip_list = []
    for nic_ref in nic_refs:
        nic_id = nic_ref["id"]

        try:
            nic = network_client.get_network_interface(resource_group, nic_id)
        except Exception as e:
            LOGGER.error("Failed to get NIC %s: %s", nic_id, str(e))
            return []

        for ip_config in nic["properties"]["ipConfigurations"]:
            ip_list.append(ip_config["properties"]["privateIPAddress"])

    LOGGER.debug("found instance IPs: %s", str(ip_list))
    return ip_list
