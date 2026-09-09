# evaluates the module on the recorded export and checks the mapping
{ pkgs, lib }:
let
  data = builtins.fromJSON (builtins.readFile ../packages/netbox-nixos/tests/fixtures/snapshot.json);
  flake = import ../modules/netbox/outputs.nix { inherit data; };

  eval =
    name:
    (import (pkgs.path + "/nixos/lib/eval-config.nix") {
      system = null;
      modules = [
        flake.nixosProfiles.${name}
        { nixpkgs.hostPlatform = "x86_64-linux"; }
      ];
    }).config;
  router = eval "router";
  bobr = eval "bobr";

  network =
    cfg: name:
    let
      n = cfg.systemd.network.networks.${name};
    in
    {
      inherit (n)
        matchConfig
        linkConfig
        address
        vlan
        ;
    };

  results = lib.runTests {
    # the unnamed device is skipped
    testOutputs = {
      expr = builtins.attrNames flake.nixosProfiles;
      expected = [
        "bobr"
        "router"
      ];
    };
    testHostName = {
      expr = router.networking.hostName;
      expected = "router";
    };
    # nixos itself adds the 127.0.0.2 entry
    testHostsFile = {
      expr = router.networking.hosts;
      expected = {
        "127.0.0.2" = [ "router" ];
        "10.0.0.5" = [
          "router"
          "router.example.org"
        ];
        "2001:db8::5" = [ "router" ];
        "10.0.0.10" = [
          "bobr"
          "bobr.example.org"
        ];
      };
    };
    testPhysical = {
      expr = network router "10-eth0";
      expected = {
        matchConfig.MACAddress = "52:54:00:12:34:56";
        linkConfig.MTUBytes = "1500";
        address = [
          "10.0.0.5/24"
          "2001:db8::5/64"
        ];
        vlan = [ "eth0.10" ];
      };
    };
    testVlanNetwork = {
      expr = network router "10-eth0.10";
      expected = {
        matchConfig.Name = "eth0.10";
        linkConfig = { };
        address = [ "10.0.10.1/24" ];
        vlan = [ ];
      };
    };
    testVlanNetdev = {
      expr = {
        inherit (router.systemd.network.netdevs."10-eth0.10") netdevConfig vlanConfig;
      };
      expected = {
        netdevConfig = {
          Name = "eth0.10";
          Kind = "vlan";
        };
        vlanConfig.Id = 10;
      };
    };
    # disabled, management-only and lag interfaces stay unconfigured
    testConfiguredInterfaces = {
      expr = builtins.attrNames router.systemd.network.networks;
      expected = [
        "10-eth0"
        "10-eth0.10"
      ];
    };
    testFirewall = {
      expr = {
        tcp = router.networking.firewall.allowedTCPPorts;
        udp = router.networking.firewall.allowedUDPPorts;
      };
      expected = {
        tcp = [
          22
          53
        ];
        udp = [ 53 ];
      };
    };
    testPassthrough = {
      expr = router.time.timeZone;
      expected = "UTC";
    };
    # vm interfaces carry no type
    testVm = {
      expr = network bobr "10-eth0";
      expected = {
        matchConfig.MACAddress = "52:54:00:AA:BB:CC";
        linkConfig = { };
        address = [ "10.0.0.10/24" ];
        vlan = [ ];
      };
    };
    testVmWithoutPassthrough = {
      expr = bobr.time.timeZone;
      expected = null;
    };
  };
in
pkgs.runCommand "netbox-nixos-eval" { } (
  if results == [ ] then "touch $out" else throw "eval tests failed: ${builtins.toJSON results}"
)
