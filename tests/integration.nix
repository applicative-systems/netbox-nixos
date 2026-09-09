# end to end: a real netbox, the server in front of it, and nix as the client
{ pkgs, self }:
pkgs.testers.runNixOSTest {
  name = "netbox-nixos";

  nodes = {
    netbox =
      { pkgs, ... }:
      {
        # gunicorn workers plus the django shell that seeds the database
        virtualisation.memorySize = 4096;
        services.netbox = {
          enable = true;
          bind = "0.0.0.0:8001";
          settings.ALLOWED_HOSTS = [ "*" ];
        };
        # seeded on the vm, so the interactive demo needs no driver commands;
        # the marker survives --keep-vm-state
        systemd.services.netbox-seed = {
          wantedBy = [ "multi-user.target" ];
          requires = [ "netbox.service" ];
          after = [ "netbox.service" ];
          unitConfig.ConditionPathExists = "!/var/lib/netbox/.seeded";
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
          };
          script = "/run/current-system/sw/bin/netbox-manage shell < ${./seed.py} && touch /var/lib/netbox/.seeded";
        };
        networking.firewall.allowedTCPPorts = [ 8001 ];
      };

    server =
      { pkgs, ... }:
      {
        imports = [ self.nixosModules.server ];
        services.netbox-nixos = {
          enable = true;
          netboxUrl = "http://netbox:8001";
          # the token tests/seed.py creates
          tokenFile = toString (
            pkgs.writeText "token" "nbt_abcdefghijkl.0123456789abcdefghijklmnopqrstuvwxyz0123"
          );
          listen = "0.0.0.0:8080";
          publicUrl = "http://server:8080";
          ttl = 1;
        };
        networking.firewall.allowedTCPPorts = [ 8080 ];
      };

    client =
      { pkgs, ... }:
      {
        virtualisation.memorySize = 2048;
        virtualisation.writableStore = true;
        nix.settings = {
          experimental-features = [
            "nix-command"
            "flakes"
          ];
          # nix would otherwise keep serving its cached download of the mutable url
          tarball-ttl = 0;
        };
        nix.nixPath = [ "nixpkgs=${pkgs.path}" ];
      };
  };

  # `nix run .#demo`: netbox and the server reachable from the host, and a
  # nixpkgs input on the served flake so that nixosConfigurations exist; the
  # host already has this nixpkgs, so `--refresh` fetches nothing
  interactive.nodes = {
    netbox = {
      virtualisation.forwardPorts = [
        {
          from = "host";
          host.port = 8001;
          guest.port = 80;
        }
      ];
      # gunicorn serves no static files; the module collects them for a proxy.
      # the proxied host header has no port, so django must be told the origin
      # the browser uses
      services.netbox.settings.CSRF_TRUSTED_ORIGINS = [ "http://localhost:8001" ];
      services.nginx = {
        enable = true;
        recommendedProxySettings = true;
        virtualHosts.netbox = {
          default = true;
          locations."/".proxyPass = "http://127.0.0.1:8001";
          locations."/static/".alias = "/var/lib/netbox/static/";
        };
      };
      users.users.nginx.extraGroups = [ "netbox" ];
      networking.firewall.allowedTCPPorts = [ 80 ];
    };
    server = {
      virtualisation.forwardPorts = [
        {
          from = "host";
          host.port = 8080;
          guest.port = 8080;
        }
      ];
      services.netbox-nixos = {
        publicUrl = pkgs.lib.mkForce "http://localhost:8080";
        nixpkgs = "path:${pkgs.path}";
      };
    };
  };

  testScript = ''
    import json
    import re

    start_all()
    netbox.wait_for_unit("netbox-seed.service")
    server.wait_for_unit("netbox-nixos.service")


    def fetch():
        headers = client.wait_until_succeeds("curl -sfD - -o flake.tar.gz http://server:8080/flake.tar.gz")
        m = re.search(
            r'link: <(http://server:8080/rev/([0-9a-f]{40})/flake\.tar\.gz)\?rev=\2&lastModified=(\d+)>; rel="immutable"',
            headers,
            re.IGNORECASE,
        )
        assert m, headers
        return m.group(1), m.group(2), int(m.group(3))


    def evaluate(host, expr):
        prelude = (
            'let config = (import (<nixpkgs> + "/nixos/lib/eval-config.nix") { system = null; modules = [ '
            f'(builtins.getFlake "http://server:8080/flake.tar.gz").nixosProfiles.{host} '
            '{ nixpkgs.hostPlatform = "x86_64-linux"; } ]; }).config; in '
        )
        return json.loads(client.succeed(f"nix eval --json --impure --expr '{prelude}{expr}'"))


    def network(host, name):
        return evaluate(
            host,
            f'let n = config.systemd.network.networks."{name}"; in '
            "{ inherit (n) matchConfig linkConfig address vlan; gateways = map (r: r.Gateway) n.routes; }",
        )


    with subtest("the link header names an immutable url serving the same bytes"):
        immutable, rev, last_modified = fetch()
        client.succeed("cp flake.tar.gz first.tar.gz && mkdir x && tar xzf flake.tar.gz -C x")
        nar_hash = client.succeed("nix hash path x/netbox-nixos-*").strip()
        client.succeed(f"curl -sf -o rev.tar.gz {immutable} && cmp flake.tar.gz rev.tar.gz")

    # the server announces no narhash; nix computes it from what it fetched
    with subtest("nix locks the mutable url to the immutable one"):
        locked = json.loads(client.succeed("nix flake metadata --json http://server:8080/flake.tar.gz"))["locked"]
        expected = {"__final": True, "lastModified": last_modified, "narHash": nar_hash, "rev": rev, "type": "tarball", "url": immutable}
        assert locked == expected, locked
        hosts = json.loads(client.succeed("nix eval --json 'http://server:8080/flake.tar.gz#lib.hosts'"))
        assert sorted(hosts) == ["bobr", "router", "zubr"], hosts

    with subtest("the module maps netbox objects onto nixos options"):
        eth0 = network("router", "10-eth0")
        assert eth0 == {
            "matchConfig": {"MACAddress": "52:54:00:12:34:56"},
            "linkConfig": {"MTUBytes": "1500"},
            "address": ["10.0.0.5/24"],
            "vlan": ["eth0.10"],
            # the route comes from the site's config context, not from netbox's prefixes
            "gateways": ["10.0.0.1"],
        }, eth0
        vlan = evaluate("router", 'let d = config.systemd.network.netdevs."10-eth0.10"; in { inherit (d) netdevConfig vlanConfig; }')
        assert vlan == {"netdevConfig": {"Name": "eth0.10", "Kind": "vlan"}, "vlanConfig": {"Id": 10}}, vlan
        assert evaluate("router", "builtins.attrNames config.systemd.network.networks") == ["10-eth0", "10-eth0.10"]
        assert evaluate("router", "config.networking.firewall.allowedTCPPorts") == [22]
        # platform context (utc, openssh) under the heavier site context (krakow); every host is in krakow
        assert evaluate("router", "config.time.timeZone") == "Europe/Warsaw"
        assert evaluate("router", "config.services.openssh.enable") is True
        assert evaluate("bobr", "config.time.timeZone") == "Europe/Warsaw"
        hosts = evaluate("router", "config.networking.hosts")
        assert hosts["10.0.0.5"] == ["router", "router.example.org"], hosts
        assert hosts["10.0.0.10"] == ["bobr", "bobr.example.org"], hosts
        assert hosts["10.0.0.11"] == ["zubr", "zubr.example.org"], hosts
        # openssh from the platform context opens 22 as well
        assert sorted(evaluate("bobr", "config.networking.firewall.allowedTCPPorts")) == [22, 80, 443]
        assert evaluate("zubr", "config.networking.firewall.allowedUDPPorts") == [53]
        assert network("bobr", "10-eth0")["matchConfig"] == {"MACAddress": "52:54:00:AA:BB:CC"}

    with subtest("a change in netbox becomes a new revision and the old one stays"):
        netbox.succeed(
            """netbox-manage shell -c "from ipam.models import IPAddress; ip = IPAddress.objects.get(address='10.0.0.5/24'); ip.address = '10.0.0.6/24'; ip.save()" """
        )
        retry(lambda _: fetch()[1] != rev)
        assert network("router", "10-eth0")["address"] == ["10.0.0.6/24"]
        client.succeed(f"curl -sf -o old.tar.gz {immutable} && cmp first.tar.gz old.tar.gz")
  '';
}
