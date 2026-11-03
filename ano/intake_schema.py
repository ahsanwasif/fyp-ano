"""Flow B intake schema: everything Flow A would discover, stated by the user. No defaults anywhere.

Covers: routed / access / trunk / SVI / LAG interfaces, VLANs, VRFs, OSPF (per-interface area, network type,
passive), BGP (per device, neighbours, networks), static routes, filters, services, tests, failure tests.
A field that does not apply is written as null (or []) explicitly, so a missing field is always a question.
"""
from __future__ import annotations
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Device(Strict):
    name: str; vendor: str; platform: str; os_version: str; hw_model: str; role: str
    mgmt_ip: IPv4Interface; mgmt_interface: str; ssh_port: int; cred_profile: int
    # null for devices without one (servers, firewalls); required when the device runs OSPF/BGP (checked in b1)
    loopback_interface: Optional[str]; loopback: Optional[IPv4Interface]; loopback_ospf_area: Optional[str]
    router_id: Optional[IPv4Address]
    existing_config: Optional[str]          # path; required for brownfield, null for greenfield


class Interface(Strict):
    device: str; name: str
    mode: Literal["routed", "access", "trunk", "svi", "lag-member"]
    description: str
    ip: Optional[IPv4Interface]             # routed / svi
    vrf: Optional[str]                      # routed / svi; null = default VRF
    access_vlan: Optional[int]              # access; svi: the VLAN it routes
    trunk_vlans: Optional[list[int]]        # trunk
    lag: Optional[str]                      # lag-member: name of the LAG interface it joins
    lag_mode: Optional[Literal["active", "passive", "on"]]
    ospf_area: Optional[str]; ospf_passive: bool
    ospf_network: Optional[Literal["point-to-point", "broadcast"]]
    mtu: Optional[int]

    @model_validator(mode="after")
    def by_mode(self):
        need = {"routed": ["ip"], "svi": ["ip", "access_vlan"], "access": ["access_vlan"], "trunk": ["trunk_vlans"],
                "lag-member": ["lag", "lag_mode"]}[self.mode]
        missing = [f for f in need if getattr(self, f) in (None, [])]
        if missing:
            raise ValueError(f"{self.device}:{self.name} mode {self.mode} needs {missing}")
        if self.mode not in ("routed", "svi") and (self.ospf_area or self.ip):
            raise ValueError(f"{self.device}:{self.name} mode {self.mode} cannot have an IP or OSPF")
        return self


class Vlan(Strict):
    device: str; id: int; name: str


class Vrf(Strict):
    device: str; name: str


class Link(Strict):
    a: str; b: str


class StaticRoute(Strict):
    device: str; vrf: Optional[str]; prefix: IPv4Network
    next_hop: Union[IPv4Address, Literal["blackhole"]]


class BgpNeighbor(Strict):
    ip: IPv4Address; remote_as: int; description: str; update_source: Optional[str]


class Bgp(Strict):
    device: str; asn: int; neighbors: list[BgpNeighbor]; networks: list[IPv4Network]
    ebgp_without_policy: bool   # FRR refuses to exchange eBGP routes without a policy unless this is stated


class Ospf(Strict):
    process_id: int


class Routing(Strict):
    ospf: Optional[Ospf]; bgp: list[Bgp]; static_routes: list[StaticRoute]


class Policy(Strict):
    name: str; action: Literal["deny", "permit"]; src: IPv4Network; dst: IPv4Network
    protocol: Literal["ip", "tcp", "udp", "icmp"]; dst_port: Optional[int] = None
    apply_on: str; direction: Literal["in", "out"]
    sequence: int                           # order inside the interface's filter (several policies per interface)


class Services(Strict):
    ntp: list[str]; dns: list[str]; syslog: list[str]


class DeviceLogin(Strict):
    """Written into new configs. Only a HASH is accepted (e.g. `openssl passwd -6`), so no password is on disk."""
    username: str; secret_sha512: str


class Test(Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str; src: IPv4Network; dst: IPv4Network; protocol: Literal["ip", "tcp", "udp", "icmp"]
    dst_port: Optional[int] = None; expect: Literal["permit", "deny"]
    from_: str = Field(alias="from")      # device:interface where the test traffic enters


class FailureTest(Strict):
    name: str; down: list[str]; then: str


class Constraints(Strict):
    devices_allowed: list[str]; commit_confirm_seconds: int


class Monitoring(Strict):
    allowed: list[Literal["gnmi", "snmp", "ssh"]]; gnmi_port: Optional[int]; alert_webhook: str


class Intake(Strict):
    intent: str
    starting_point: Literal["greenfield", "brownfield"]
    device_login: DeviceLogin
    devices: list[Device]
    vlans: list[Vlan]
    vrfs: list[Vrf]
    interfaces: list[Interface]
    links: list[Link]
    routing: Routing
    policies: list[Policy]
    services: Services
    acceptance_tests: list[Test]
    failure_tests: list[FailureTest]
    emulation_images: dict[str, str]
    change_constraints: Constraints
    monitoring: Monitoring
