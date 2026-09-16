# populates the netbox vm with the objects tests/integration.nix asserts on
from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    MACAddress,
    Manufacturer,
    Platform,
    Site,
)
from extras.models import ConfigContext
from ipam.models import VLAN, IPAddress, Service
from users.models import Token, User

admin = User.objects.create_superuser("admin", "admin@example.org", "admin")
# the value tests/integration.nix hands to the server
Token(
    user=admin, key="abcdefghijkl", token="0123456789abcdefghijklmnopqrstuvwxyz0123"
).save()

site = Site.objects.create(name="Kraków", slug="krakow")
manufacturer = Manufacturer.objects.create(name="generic", slug="generic")
device_type = DeviceType.objects.create(
    manufacturer=manufacturer, model="server", slug="server"
)
role = DeviceRole.objects.create(name="router", slug="router")
server = DeviceRole.objects.create(name="server", slug="server")
nixos = Platform.objects.create(name="NixOS", slug="nixos")
lan = VLAN.objects.create(vid=10, name="conference", site=site)

router = Device.objects.create(
    name="router",
    device_type=device_type,
    role=role,
    site=site,
    platform=nixos,
    status="active",
)
eth0 = Interface.objects.create(
    device=router, name="eth0", type="1000base-t", mode="tagged", mtu=1500
)
eth0.tagged_vlans.add(lan)
eth0.primary_mac_address = MACAddress.objects.create(
    mac_address="52:54:00:12:34:56", assigned_object=eth0
)
eth0.save()
eth0_10 = Interface.objects.create(
    device=router,
    name="eth0.10",
    type="virtual",
    parent=eth0,
    mode="access",
    untagged_vlan=lan,
)
Interface.objects.create(device=router, name="eth1", type="1000base-t", enabled=False)
router.primary_ip4 = IPAddress.objects.create(
    address="10.0.0.5/24", dns_name="router.example.org", assigned_object=eth0
)
router.save()
IPAddress.objects.create(address="10.0.10.1/24", assigned_object=eth0_10)
Service.objects.create(parent=router, name="ssh", protocol="tcp", ports=[22])

# the escape hatch: a platform-wide baseline and a heavier site context on top
ConfigContext.objects.create(
    name="nixos",
    data={
        "nixos": {
            "time": {"timeZone": "UTC"},
            "services": {"openssh": {"enable": True}},
        }
    },
).platforms.add(nixos)
ConfigContext.objects.create(
    name="krakow",
    weight=2000,
    data={
        "nixos": {
            "time": {"timeZone": "Europe/Warsaw"},
            "systemd": {
                "network": {
                    "networks": {"10-eth0": {"routes": [{"Gateway": "10.0.0.1"}]}}
                }
            },
        }
    },
).sites.add(site)


def host(name, mac, address, service, protocol, ports):
    device = Device.objects.create(
        name=name,
        device_type=device_type,
        role=server,
        site=site,
        platform=nixos,
        status="active",
    )
    eth0 = Interface.objects.create(device=device, name="eth0", type="1000base-t")
    eth0.primary_mac_address = MACAddress.objects.create(
        mac_address=mac, assigned_object=eth0
    )
    eth0.save()
    device.primary_ip4 = IPAddress.objects.create(
        address=address, dns_name=f"{name}.example.org", assigned_object=eth0
    )
    device.save()
    Service.objects.create(parent=device, name=service, protocol=protocol, ports=ports)


host("bobr", "52:54:00:aa:bb:cc", "10.0.0.10/24", "http", "tcp", [80, 443])
host("zubr", "52:54:00:dd:ee:ff", "10.0.0.11/24", "dns", "udp", [53])
