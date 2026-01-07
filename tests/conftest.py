"""
Conftest for Azure-based tests.

This module provides mock Azure infrastructure for testing without
requiring an actual Azure connection. It mocks the ARM REST API
client classes from azure/api.py.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

import pytest


SUB_ID = "00000000-0000-0000-0000-000000000000"
RG = "test-rg"


def arm_id(provider: str, resource_type: str,
           name: str) -> str:
    """Build an Azure ARM resource ID."""
    return (
        f"/subscriptions/{SUB_ID}/resourceGroups/{RG}"
        f"/providers/{provider}/{resource_type}/{name}"
    )


def compute_id(resource_type: str, name: str) -> str:
    return arm_id("Microsoft.Compute", resource_type, name)


def network_id(resource_type: str, name: str) -> str:
    return arm_id("Microsoft.Network", resource_type, name)


# Commonly used test resource IDs
ROUTE_TABLE_ID = network_id("routeTables", "test-rt")
PRIMARY_VM_ID = compute_id("virtualMachines", "primary-ngfw")
SECONDARY_VM_ID = compute_id(
    "virtualMachines", "secondary-ngfw"
)


class MockComputeClient:
    """Mock Azure Compute Client (ARM REST)"""

    def __init__(self, state: 'AzureState'):
        self.state = state

    def get_vm(self, resource_group: str, vm_name: str) -> Dict:
        """Get VM details"""
        for vm in self.state.vms:
            if vm['name'] == vm_name:
                return vm.copy()
        raise ValueError(f"VM {vm_name} not found")

    def update_vm_tags(self, resource_group: str, vm_name: str,
                       tags: Dict) -> Dict:
        """Update VM tags (PATCH)"""
        for vm in self.state.vms:
            if vm['name'] == vm_name:
                vm['tags'].update(tags)
                return vm.copy()
        raise ValueError(f"VM {vm_name} not found")


class MockNetworkClient:
    """Mock Azure Network Client (ARM REST)"""

    def __init__(self, state: 'AzureState'):
        self.state = state

    def get_network_interface(self, resource_group: str,
                              nic_name: str) -> Dict:
        """Get network interface details"""
        name = nic_name.rsplit("/", 1)[-1]
        for nic in self.state.nics:
            if nic['name'] == name:
                return _deep_copy_dict(nic)
        raise ValueError(f"NIC {nic_name} not found")

    def update_network_interface(self, resource_group: str,
                                 nic_name: str, body: Dict) -> Dict:
        """Update network interface (PUT)"""
        name = nic_name.rsplit("/", 1)[-1]
        for i, nic in enumerate(self.state.nics):
            if nic['name'] == name:
                self.state.nics[i] = body
                # Sync public IP ipConfiguration references
                self._sync_public_ip_refs(body)
                return _deep_copy_dict(body)
        raise ValueError(f"NIC {nic_name} not found")

    def _sync_public_ip_refs(self, nic: Dict) -> None:
        """Keep public IP ipConfiguration refs in sync."""
        pip_ids = _collect_pip_ids(nic)
        for pip in self.state.public_ips:
            if pip['id'] not in pip_ids:
                continue
            ip_config_name = pip_ids[pip['id']]
            pip['properties']['ipConfiguration'] = {
                'id': f"{nic['id']}/ipConfigurations"
                      f"/{ip_config_name}"
            }

    def get_route_table(self, resource_group: str,
                        rt_name: str) -> Dict:
        """Get route table details"""
        for rt in self.state.route_tables:
            if rt['name'] == rt_name:
                return _deep_copy_dict(rt)
        raise ValueError(f"Route table {rt_name} not found")

    def update_route(self, resource_group: str, rt_name: str,
                     route_name: str, body: Dict) -> Dict:
        """Update an individual route (PUT)"""
        for rt in self.state.route_tables:
            if rt['name'] != rt_name:
                continue
            for j, route in enumerate(rt['properties']['routes']):
                if route['name'] == route_name:
                    rt['properties']['routes'][j] = body
                    return _deep_copy_dict(body)
            raise ValueError(
                f"Route {route_name} not found in {rt_name}"
            )
        raise ValueError(f"Route table {rt_name} not found")

    def get_public_ip(self, resource_group: str,
                      pip_name: str) -> Dict:
        """Get public IP address details"""
        for pip in self.state.public_ips:
            if pip['name'] == pip_name:
                return _deep_copy_dict(pip)
        raise ValueError(f"Public IP {pip_name} not found")


def _collect_pip_ids(nic: Dict) -> Dict[str, str]:
    """Return {public_ip_id: ip_config_name} from a NIC."""
    result = {}
    for ip_cfg in nic.get(
        'properties', {}
    ).get('ipConfigurations', []):
        pip_ref = ip_cfg.get(
            'properties', {}
        ).get('publicIPAddress')
        if pip_ref and pip_ref.get('id'):
            result[pip_ref['id']] = ip_cfg['name']
    return result


def _deep_copy_dict(d):
    """Simple deep copy for nested dicts/lists."""
    if isinstance(d, dict):
        return {k: _deep_copy_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_deep_copy_dict(item) for item in d]
    return d


class AzureState:
    """Holds the mocked Azure state"""

    def __init__(self):
        self.vms: List[Dict] = []
        self.nics: List[Dict] = []
        self.route_tables: List[Dict] = []
        self.public_ips: List[Dict] = []
        self.resource_group: str = RG
        self.subscription_id: str = SUB_ID


@dataclass
class AzureConf:
    """Azure configuration for tests"""
    compute_client: MockComputeClient
    network_client: MockNetworkClient
    state: AzureState
    resource_group: str
    subscription_id: str
    primary_vm_name: str
    secondary_vm_name: str
    protected_route_table_name: str
    primary_nic_names: List[str]
    secondary_nic_names: List[str]
    primary_ips: List[str]
    secondary_ips: List[str]
    other_nic_name: str
    other_ip: str
    reserved_public_ip_name: str


@pytest.fixture
def azure_conf() -> AzureConf:
    """Create a mock Azure environment for testing"""
    state = AzureState()

    compute_client = MockComputeClient(state)
    network_client = MockNetworkClient(state)

    # VM names
    primary_vm_name = "primary-ngfw"
    secondary_vm_name = "secondary-ngfw"

    # NIC names
    primary_internal_nic = "primary-internal-nic"
    primary_wan_nic = "primary-wan-nic"
    secondary_internal_nic = "secondary-internal-nic"
    secondary_wan_nic = "secondary-wan-nic"
    other_nic = "other-nic"

    # IPs
    primary_internal_ip = "10.0.11.10"
    primary_wan_ip = "10.0.12.10"
    secondary_internal_ip = "10.0.21.10"
    secondary_wan_ip = "10.0.22.10"
    other_ip = "10.0.1.50"

    # Route table
    protected_rt_name = "protected-rt"

    # Public IP
    reserved_pip_name = "reserved-pip"

    # Create VMs
    state.vms = [
        {
            'name': primary_vm_name,
            'id': compute_id("virtualMachines", primary_vm_name),
            'tags': {},
            'properties': {
                'networkProfile': {
                    'networkInterfaces': [
                        {
                            'id': network_id(
                                "networkInterfaces",
                                primary_internal_nic
                            ),
                            'properties': {
                                'primary': True
                            }
                        },
                        {
                            'id': network_id(
                                "networkInterfaces",
                                primary_wan_nic
                            ),
                            'properties': {
                                'primary': False
                            }
                        },
                    ]
                }
            }
        },
        {
            'name': secondary_vm_name,
            'id': compute_id(
                "virtualMachines", secondary_vm_name
            ),
            'tags': {},
            'properties': {
                'networkProfile': {
                    'networkInterfaces': [
                        {
                            'id': network_id(
                                "networkInterfaces",
                                secondary_internal_nic
                            ),
                            'properties': {
                                'primary': True
                            }
                        },
                        {
                            'id': network_id(
                                "networkInterfaces",
                                secondary_wan_nic
                            ),
                            'properties': {
                                'primary': False
                            }
                        },
                    ]
                }
            }
        },
    ]

    # Create NICs
    state.nics = [
        {
            'name': primary_internal_nic,
            'id': network_id(
                "networkInterfaces", primary_internal_nic
            ),
            'properties': {
                'ipConfigurations': [
                    {
                        'name': 'ipconfig1',
                        'properties': {
                            'privateIPAddress': primary_internal_ip,
                            'primary': True,
                        }
                    }
                ]
            }
        },
        {
            'name': primary_wan_nic,
            'id': network_id(
                "networkInterfaces", primary_wan_nic
            ),
            'properties': {
                'ipConfigurations': [
                    {
                        'name': 'ipconfig1',
                        'properties': {
                            'privateIPAddress': primary_wan_ip,
                            'primary': True,
                            'publicIPAddress': {
                                'id': network_id(
                                    "publicIPAddresses",
                                    reserved_pip_name
                                )
                            }
                        }
                    }
                ]
            }
        },
        {
            'name': secondary_internal_nic,
            'id': network_id(
                "networkInterfaces", secondary_internal_nic
            ),
            'properties': {
                'ipConfigurations': [
                    {
                        'name': 'ipconfig1',
                        'properties': {
                            'privateIPAddress': secondary_internal_ip,
                            'primary': True,
                        }
                    }
                ]
            }
        },
        {
            'name': secondary_wan_nic,
            'id': network_id(
                "networkInterfaces", secondary_wan_nic
            ),
            'properties': {
                'ipConfigurations': [
                    {
                        'name': 'ipconfig1',
                        'properties': {
                            'privateIPAddress': secondary_wan_ip,
                            'primary': True,
                        }
                    }
                ]
            }
        },
        {
            'name': other_nic,
            'id': network_id("networkInterfaces", other_nic),
            'properties': {
                'ipConfigurations': [
                    {
                        'name': 'ipconfig1',
                        'properties': {
                            'privateIPAddress': other_ip,
                            'primary': True,
                        }
                    }
                ]
            }
        },
    ]

    # Create route table with routes
    state.route_tables = [
        {
            'name': protected_rt_name,
            'id': network_id("routeTables", protected_rt_name),
            'properties': {
                'routes': [
                    # Local route (VNet-managed)
                    {
                        'name': 'local',
                        'properties': {
                            'addressPrefix': '10.0.0.0/16',
                            'nextHopType': 'VnetLocal',
                        }
                    },
                    # Default route via primary NGFW
                    {
                        'name': 'default',
                        'properties': {
                            'addressPrefix': '0.0.0.0/0',
                            'nextHopType': 'VirtualAppliance',
                            'nextHopIpAddress':
                                primary_internal_ip,
                        }
                    },
                    # Another route via other NIC
                    {
                        'name': 'other',
                        'properties': {
                            'addressPrefix': '192.168.0.0/24',
                            'nextHopType': 'VirtualAppliance',
                            'nextHopIpAddress': other_ip,
                        }
                    },
                ]
            }
        }
    ]

    # Create reserved public IP
    state.public_ips = [
        {
            'name': reserved_pip_name,
            'id': network_id(
                "publicIPAddresses", reserved_pip_name
            ),
            'properties': {
                'ipAddress': '203.0.113.10',
                'publicIPAllocationMethod': 'Static',
                'ipConfiguration': {
                    'id': (
                        network_id(
                            "networkInterfaces", primary_wan_nic
                        )
                        + "/ipConfigurations/ipconfig1"
                    )
                }
            }
        }
    ]

    return AzureConf(
        compute_client=compute_client,
        network_client=network_client,
        state=state,
        resource_group=RG,
        subscription_id=SUB_ID,
        primary_vm_name=primary_vm_name,
        secondary_vm_name=secondary_vm_name,
        protected_route_table_name=protected_rt_name,
        primary_nic_names=[
            primary_internal_nic, primary_wan_nic
        ],
        secondary_nic_names=[
            secondary_internal_nic, secondary_wan_nic
        ],
        primary_ips=[primary_internal_ip, primary_wan_ip],
        secondary_ips=[
            secondary_internal_ip, secondary_wan_ip
        ],
        other_nic_name=other_nic,
        other_ip=other_ip,
        reserved_public_ip_name=reserved_pip_name,
    )


@pytest.fixture(autouse=True)
def mock_get_resource_group(
    azure_conf: AzureConf,
    monkeypatch: pytest.MonkeyPatch
):
    """Automatically mock get_resource_group for all tests."""
    monkeypatch.setattr(
       'ha_script.azure.metadata.get_resource_group',
       lambda: azure_conf.state.resource_group
    )


@pytest.fixture(autouse=True)
def mock_get_subscription_id(
    azure_conf: AzureConf,
    monkeypatch: pytest.MonkeyPatch
):
    """Automatically mock get_subscription_id for all tests."""
    monkeypatch.setattr(
       'ha_script.azure.metadata.get_subscription_id',
       lambda: azure_conf.state.subscription_id
    )


@pytest.fixture(autouse=True)
def mock_send_event_to_smc(monkeypatch: pytest.MonkeyPatch):
    """Automatically mock SMC event sending on all tests."""
    monkeypatch.setattr(
       'ha_script.smc_events.send_event_to_smc',
       lambda *args, **_: print(*args)
    )
