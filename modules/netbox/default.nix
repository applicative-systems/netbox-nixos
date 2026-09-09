# maps one host of a netbox export onto nixos options. the export is the
# graphql response, so field names and values are netbox's. the hosts
# and the config context key are function arguments rather than options
# because they decide which options get set, and the module system cannot
# derive that from `config` without recursing
{
  hosts,
  host,
  configContextKey ? "nixos",
}:
{ config, lib, ... }:
let
  cfg = config.netbox;
  record = hosts.${host};

  ip = cidr: lib.head (lib.splitString "/" cidr);
  labels = lib.splitString "." record.name;

  # choice fields come back as the stored values, since netbox leaves
  # strawberry-django's GENERATE_ENUMS_FROM_CHOICES off; vm interfaces have no
  # type at all
  isVlan = i: (i.type or null) == "virtual" && i.parent != null && i.untagged_vlan != null;
  isPhysical =
    i:
    !(builtins.elem (i.type or null) [
      "virtual"
      "lag"
      "bridge"
    ]);
  configured = lib.filter (
    i: i.enabled && !(i.mgmt_only or false) && (isPhysical i || isVlan i)
  ) record.interfaces;
  vlans = lib.filter isVlan configured;
  mac = i: if i.primary_mac_address == null then null else i.primary_mac_address.mac_address;

  network = i: {
    matchConfig = if isPhysical i && mac i != null then { MACAddress = mac i; } else { Name = i.name; };
    linkConfig = lib.optionalAttrs (i.mtu != null) { MTUBytes = toString i.mtu; };
    address = map (a: a.address) i.ip_addresses;
    vlan = map (v: v.name) (lib.filter (v: v.parent.name == i.name) vlans);
  };

  netdev = i: {
    netdevConfig = {
      Name = i.name;
      Kind = "vlan";
    };
    vlanConfig.Id = i.untagged_vlan.vid;
  };

  units = f: interfaces: lib.listToAttrs (map (i: lib.nameValuePair "10-${i.name}" (f i)) interfaces);

  hostsEntries = lib.zipAttrsWith (_: names: lib.unique (lib.concatLists names)) (
    lib.concatMap (
      h:
      map (p: { ${ip p.address} = [ h.name ]; }) (
        lib.filter (p: p != null) [
          h.primary_ip4
          h.primary_ip6
        ]
      )
      ++ lib.concatMap (
        i: map (a: { ${ip a.address} = [ a.dns_name ]; }) (lib.filter (a: a.dns_name != "") i.ip_addresses)
      ) h.interfaces
    ) (lib.attrValues hosts)
  );

  ports =
    protocol:
    lib.unique (lib.concatMap (s: s.ports) (lib.filter (s: s.protocol == protocol) record.services));

  on = default: lib.mkEnableOption "" // { inherit default; };
in
{
  options.netbox = {
    host = lib.mkOption {
      type = lib.types.attrs;
      readOnly = true;
      default = record;
      description = "this host as netbox returned it";
    };
    hosts = lib.mkOption {
      type = lib.types.attrs;
      readOnly = true;
      default = hosts;
      description = "all exported hosts by name";
    };
    hostsFile.enable = on true;
    networking.enable = on true;
    firewall.enable = on true;
    configContext.enable = on true;
  };

  config = lib.mkMerge [
    {
      # networking.hostName rejects dots, so an fqdn is split
      networking = {
        hostName = lib.mkDefault (lib.head labels);
        domain = lib.mkIf (lib.length labels > 1) (
          lib.mkDefault (lib.concatStringsSep "." (lib.tail labels))
        );
      };
    }
    (lib.mkIf cfg.hostsFile.enable { networking.hosts = hostsEntries; })
    (lib.mkIf cfg.networking.enable {
      networking.useDHCP = lib.mkDefault false;
      systemd.network = {
        enable = true;
        networks = units network configured;
        netdevs = units netdev vlans;
      };
    })
    (lib.mkIf cfg.firewall.enable {
      networking.firewall = {
        allowedTCPPorts = ports "tcp";
        allowedUDPPorts = ports "udp";
      };
    })
    (lib.mkIf cfg.configContext.enable (
      lib.optionalAttrs (
        record.config_context ? ${configContextKey}
      ) record.config_context.${configContextKey}
    ))
  ];
}
